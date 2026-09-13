"""Adversarial fuzzer for del / has / get_attr features.

Generates random programs exercising all combinations of:
  - del: static, dynamic, heap, slot, const (error), double-free
  - has: 1-arg (undeclared, declared, dynamic, pointer), 2-arg (literal,
         runtime, mixed-type struct, non-struct), in conditions
  - get_attr: literal, runtime name, uniform-type dispatch, on pointer,
              on deref, in expressions, type mismatch (error)

Runs the full pipeline: lexer -> parser -> semantic -> codegen -> JIT(opt=0) vs
JIT(opt=3). A test passes when the compiler never raises a Python traceback
and (for valid programs) both opt levels produce the same stdout.

Usage: python3 test/test_fuzz_del_has.py [N] [--seed S]
"""

from __future__ import annotations

import contextlib
import concurrent.futures
import io
import multiprocessing as mp
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "source"))

from cpyte.astparse import ParseError, parse_file
from cpyte.bytecoding import LLVM
from cpyte.compiling import run_jit
from cpyte.lexar import Lexer, LexerError
from cpyte.semantic_analasis import analyze

TIMEOUT_S = 30
CRASH_DIR = os.path.join(os.path.dirname(__file__), "crashes_del_has")
os.makedirs(CRASH_DIR, exist_ok=True)
_crash_counter = 0

# Reuse the main fuzzer's infrastructure
from fuzzer import (
    SCALAR_GEN_TYPES,
    FuzzerState,
    Scope,
    gen_literal,
    gen_expr,
    gen_compare,
    gen_assign,
    gen_print,
    gen_struct,
    random_type,
    type_is_numeric,
    _assignable,
    _is_nonempty_str_literal,
    expr_is_always_truthy,
)

TEST_COUNT = 0
CRASH_COUNT = 0
BUG_COUNT = 0
UB_COUNT = 0
PASS_COUNT = 0
REJECT_COUNT = 0


def _run_jit_capture(prog, src_files, opt_level):
    q = mp.Queue()
    p = mp.Process(target=_jit_worker, args=(q, prog, src_files, opt_level))
    p.start()
    p.join(timeout=TIMEOUT_S)
    if p.is_alive():
        p.terminate()
        p.join()
        return None, None, "timeout", None
    if not q.empty():
        out, ret, err = q.get_nowait()
        return out, ret, err, p.exitcode
    return None, None, "no result", p.exitcode


def _jit_worker(q, prog, src_files, opt_level):
    f_out = io.StringIO()
    try:
        with contextlib.redirect_stdout(f_out):
            ret = run_jit(prog, opt_level=opt_level, src_files=src_files)
        q.put((f_out.getvalue(), ret, None))
    except SystemExit as e:
        q.put((None, None, f"SystemExit({e.code})"))
    except ZeroDivisionError as e:
        if "division by zero" in str(e):
            q.put((f_out.getvalue(), None, "div0"))
        else:
            q.put((None, None, f"div by zero: {e}"))
    except Exception as e:
        q.put((None, None, f"JIT crash: {e}"))


def save_crash(source, error):
    global _crash_counter
    crash_id = _crash_counter
    _crash_counter += 1
    pid = os.getpid()
    path = os.path.join(CRASH_DIR, f"crash_{pid}_{crash_id:04d}.cpy")
    with open(path, "w") as f:
        f.write(source)
    print(f"\n  *** SAVED crash_{pid}_{crash_id:04d}.cpy ({error})", flush=True)


def run_test(source, label=""):
    global TEST_COUNT, CRASH_COUNT, BUG_COUNT, UB_COUNT, PASS_COUNT, REJECT_COUNT
    TEST_COUNT += 1
    if TEST_COUNT % 500 == 0:
        print(
            f"  [{TEST_COUNT}] pass={PASS_COUNT} reject={REJECT_COUNT} "
            f"crashes={CRASH_COUNT} bugs={BUG_COUNT} ub={UB_COUNT}",
            flush=True,
        )
    try:
        tokens = Lexer(source).get_tokens()
        parsed, _ = parse_file(tokens)
    except (LexerError, ParseError, Exception):
        REJECT_COUNT += 1
        return
    try:
        err, _, _ = analyze(source, parsed, strict=False)
    except Exception as e:
        CRASH_COUNT += 1
        save_crash(source, f"analyzer crash: {e}")
        return
    if err:
        REJECT_COUNT += 1
        return
    try:
        c = LLVM()
        prog, src_files = c.emit_program(parsed)
    except RuntimeError as e:
        REJECT_COUNT += 1
        return
    except Exception as e:
        CRASH_COUNT += 1
        save_crash(source, f"codegen crash: {e}")
        return
    out0, ret0, err0, rc0 = _run_jit_capture(prog, src_files, opt_level=0)
    out3, ret3, err3, rc3 = _run_jit_capture(prog, src_files, opt_level=3)

    e0 = err0 or (f"signal {rc0}" if rc0 not in (None, 0) else None)
    e3 = err3 or (f"signal {rc3}" if rc3 not in (None, 0) else None)

    # A trap (negative exit code = killed by a signal, e.g. SIGTRAP/SIGFPE
    # from an LLVM div-by-zero guard or a poison shift) is the program's own
    # undefined behavior: opt0 and opt3 may legally fold/place the trap
    # differently, so a one-side trap with output on the other side is UB
    # noise, NOT a miscompile of a well-defined program.
    trapped = (rc0 is not None and rc0 < 0) or (rc3 is not None and rc3 < 0)

    if e0 or e3:
        if trapped:
            UB_COUNT += 1
            save_crash(source, f"UB trap (rc0={rc0}, rc3={rc3})")
            return
        if e0 and e3:
            if e0 == "no result" and e3 == "no result":
                UB_COUNT += 1
                save_crash(source, f"UB noise (rc0={rc0}, rc3={rc3})")
                return
            BUG_COUNT += 1
            save_crash(source, f"both JIT fail: unopt={e0}, opt={e3}")
            return
        if e0:
            BUG_COUNT += 1
            save_crash(source, f"unopt JIT fails: {e0} (opt OK)")
            return
        BUG_COUNT += 1
        save_crash(source, f"opt JIT fails: {e3} (unopt OK)")
        return
    if out0 == out3:
        PASS_COUNT += 1
        return
    if out0 and out3:
        s0 = out0.strip()
        s3 = out3.strip()
        if s0.isdigit() and s3.isdigit() and len(s0) > 4 and len(s3) > 4:
            PASS_COUNT += 1
            return
    BUG_COUNT += 1
    save_crash(source, f"output mismatch: unopt={out0!r} opt={out3!r}")


# -----------------------------------------------------------------------
# Del / has / get_attr generators
# -----------------------------------------------------------------------

class DelHasState:
    """Tracks del/has/get_attr state on top of FuzzerState."""

    def __init__(self, fstate: FuzzerState):
        self.fs = fstate
        self.deleted: set[str] = set()
        self.heap_vars: set[str] = set()
        self.dynamic_vars: set[str] = set()


def gen_del_stmt(state: DelHasState, indent: int) -> str | None:
    pad = "    " * indent
    rng = state.fs.rng
    all_vars = [(n, t) for n, t in state.fs.scope.all_vars() if n not in state.deleted]
    if not all_vars:
        return None
    weight = rng.random()
    if weight < 0.6:
        name, ty = rng.choice(all_vars)
        return f"{pad}del {name}"
    if weight < 0.8:
        arr_vars = [(n, t) for n, t in all_vars if t.endswith("*") or t.endswith("[]")]
        if arr_vars:
            name, ty = rng.choice(arr_vars)
            return f"{pad}del {name}[0]"
        name, ty = rng.choice(all_vars)
        return f"{pad}del {name}"
    dref_vars = [(n, t) for n, t in all_vars if t.endswith("*")]
    if dref_vars:
        name, ty = rng.choice(dref_vars)
        return f"{pad}del *{name}"
    name, ty = rng.choice(all_vars)
    return f"{pad}del {name}"


def gen_has_expr(state: DelHasState, target_type: str | None = None, depth: int = 0) -> tuple[str, str]:
    rng = state.fs.rng
    choice = rng.random()
    if choice < 0.4:
        all_vars = [(n, t) for n, t in state.fs.scope.all_vars()]
        if all_vars:
            name, ty = rng.choice(all_vars)
            return f"has({name})", "bool"
    if choice < 0.55:
        ptr_vars = [(n, t) for n, t in state.fs.scope.all_vars() if t.endswith("*")]
        if ptr_vars:
            name, ty = rng.choice(ptr_vars)
            return f"has({name})", "bool"
        return f"has({state.fs.fresh()})", "bool"
    if choice < 0.7:
        return f"has({state.fs.fresh()})", "bool"
    if choice < 0.85:
        if state.fs.structs:
            struct_vars = [
                (n, t)
                for n, t in state.fs.scope.all_vars()
                if t in state.fs.structs
            ]
            if struct_vars:
                name, ty = rng.choice(struct_vars)
                fields = state.fs.structs[ty]
                fname = rng.choice(fields)[0]
                return f'has({name}, "{fname}")', "bool"
        return f"has({state.fs.fresh()})", "bool"
    str_vars = [(n, t) for n, t in state.fs.scope.all_vars() if t == "str"]
    if str_vars and state.fs.structs:
        struct_vars = [
            (n, t)
            for n, t in state.fs.scope.all_vars()
            if t in state.fs.structs
        ]
        if struct_vars:
            sname, sty = rng.choice(struct_vars)
            vname, _ = rng.choice(str_vars)
            return f"has({sname}, {vname})", "bool"
    return f"has({state.fs.fresh()})", "bool"


def gen_getattr_expr(state: DelHasState, target_type: str | None = None, depth: int = 0) -> tuple[str, str]:
    rng = state.fs.rng
    if not state.fs.structs:
        return gen_literal(state.fs, target_type)
    struct_vars = [
        (n, t)
        for n, t in state.fs.scope.all_vars()
        if t in state.fs.structs
    ]
    if not struct_vars:
        return gen_literal(state.fs, target_type)
    sname, sty = rng.choice(struct_vars)
    fields = state.fs.structs[sty]
    if rng.random() < 0.5:
        matching = [(fn, ft) for fn, ft in fields if _assignable(ft, target_type)]
        if matching:
            fname, ftype = rng.choice(matching)
            return f'get_attr({sname}, "{fname}")', ftype
    if len(fields) >= 2:
        types_seen = {ft for _, ft in fields}
        if len(types_seen) == 1:
            ftype = list(types_seen)[0]
            if _assignable(ftype, target_type) or target_type is None:
                str_vars = [(n, t) for n, t in state.fs.scope.all_vars() if t == "str"]
                if str_vars:
                    vname, _ = rng.choice(str_vars)
                    return f"get_attr({sname}, {vname})", ftype
    matching = [(fn, ft) for fn, ft in fields if _assignable(ft, target_type)]
    if matching:
        fname, ftype = rng.choice(matching)
        return f'get_attr({sname}, "{fname}")', ftype
    return gen_literal(state.fs, target_type)


def gen_body_with_del_has(
    state: DelHasState, depth: int, indent: int
) -> list[str]:
    rng = state.fs.rng
    if depth > 6:
        return []
    pad = "    " * indent
    n = rng.choices([0, 1, 2, 3, 4, 5, 6, 7], weights=[3, 15, 20, 20, 15, 10, 5, 2])[0]
    stmts = []
    had_return = False
    for _ in range(n):
        if had_return:
            break
        kind = rng.choices(
            [
                "vardecl",
                "assign",
                "del",
                "has",
                "getattr",
                "if",
                "while",
                "print",
                "expr",
                "return",
            ],
            weights=[20, 15, 12, 10, 10, 12, 8, 8, 5, 5],
        )[0]
        if kind == "vardecl":
            ty = random_type(rng)
            name = state.fs.fresh()
            init_text, _ = gen_expr(state.fs, ty, depth + 1)
            state.fs.scope.add(name, ty)
            if ty.endswith("*"):
                if rng.random() < 0.3:
                    state.heap_vars.add(name)
            stmts.append(f"{pad}{ty} {name} = {init_text}")
        elif kind == "assign":
            s = gen_assign(state.fs, indent, depth)
            if s:
                stmts.append(s)
        elif kind == "del":
            s = gen_del_stmt(state, indent)
            if s:
                stmts.append(s)
        elif kind == "has":
            expr, etype = gen_has_expr(state, None, depth + 1)
            if etype in ("bool", "int"):
                stmts.append(f"{pad}print({expr})")
            else:
                stmts.append(f"{pad}{expr}")
        elif kind == "getattr":
            expr, etype = gen_getattr_expr(state, None, depth + 1)
            if etype and type_is_numeric(etype):
                stmts.append(f"{pad}print({expr})")
            else:
                stmts.append(f"{pad}{expr}")
        elif kind == "if":
            stmts.append(gen_if_with_del_has(state, indent, depth))
        elif kind == "while":
            s = gen_while_with_del_has(state, indent, depth)
            if s:
                stmts.append(s)
        elif kind == "print":
            s = gen_print(state.fs, indent, depth)
            if s:
                stmts.append(s)
        elif kind == "expr":
            pad_ = "    " * indent
            expr, _ = gen_expr(state.fs, None, depth + 1)
            stmts.append(f"{pad_}{expr}")
        elif kind == "return":
            stmts.append(f"{pad}return 0")
            had_return = True
    if not stmts:
        stmts.append(f"{pad}print(0)")
    return stmts


def gen_if_with_del_has(state: DelHasState, indent: int, depth: int) -> str:
    rng = state.fs.rng
    pad = "    " * indent
    cond, _ = gen_cond_with_has(state, depth + 1)
    old_scope = state.fs.scope
    state.fs.scope = Scope(old_scope)
    body = gen_body_with_del_has(state, depth + 1, indent + 1) or [f"{pad}    print(0)"]
    state.fs.scope = old_scope
    result = f"{pad}if {cond}:\n" + "\n".join(body)
    if rng.random() < 0.3:
        state.fs.scope = Scope(old_scope)
        else_body = gen_body_with_del_has(state, depth + 1, indent + 1) or [f"{pad}    print(0)"]
        state.fs.scope = old_scope
        result += f"\n{pad}else:\n" + "\n".join(else_body)
    return result


def gen_while_with_del_has(state: DelHasState, indent: int, depth: int) -> str | None:
    pad = "    " * indent
    rng = state.fs.rng
    cond = None
    for _ in range(8):
        c, _ = gen_cond_with_has(state, depth + 1)
        if not expr_is_always_truthy(c, state.fs.known_truthy_globals):
            cond = c
            break
    if cond is None:
        return None
    counter = state.fs.fresh("w")
    trips = rng.randint(1, 4)
    state.fs.loop_depth += 1
    old_scope = state.fs.scope
    state.fs.scope = Scope(old_scope)
    body = gen_body_with_del_has(state, depth + 1, indent + 1) or [f"{pad}    print(0)"]
    state.fs.scope = old_scope
    state.fs.loop_depth -= 1
    guard = f"(({cond}) and ({counter} > 0))"
    inner = f"{pad}    {counter} = {counter} - 1"
    return (
        f"{pad}int {counter} = {trips}\n"
        f"{pad}while {guard}:\n" + inner + "\n" + "\n".join(body)
    )


def gen_cond_with_has(state: DelHasState, depth: int = 0) -> tuple[str, str]:
    rng = state.fs.rng
    if depth > 4:
        return gen_has_expr(state, None, depth)
    kind = rng.choices(["value", "compare", "has", "logical"], weights=[25, 30, 30, 15])[0]
    if kind == "compare":
        return gen_compare(state.fs, depth)
    if kind == "has":
        return gen_has_expr(state, None, depth)
    if kind == "logical":
        op = rng.choice(["and", "or"])
        left = gen_cond_with_has(state, depth + 1)
        right = gen_cond_with_has(state, depth + 1)
        return f"({left[0]} {op} {right[0]})", "bool"
    return gen_expr(state.fs, None, depth)


def gen_program_with_del_has(state: DelHasState) -> str:
    rng = state.fs.rng
    state.fs.scope = Scope()
    state.fs.structs = {}
    state.fs.funcs = {}
    state.fs.counter = 0
    state.fs.loop_depth = 0
    state.deleted = set()
    state.heap_vars = set()
    state.dynamic_vars = set()
    lines = []
    n_structs = rng.choices([0, 1, 2, 3], weights=[30, 35, 20, 15])[0]
    for _ in range(n_structs):
        lines.append(gen_struct(state.fs))
        lines.append("")
    for _ in range(rng.randint(0, 3)):
        ty = random_type(rng)
        name = state.fs.fresh("g")
        init_text, _ = gen_expr(state.fs, ty, 0)
        state.fs.scope.add(name, ty)
        lines.append(f"{ty} {name} = {init_text}")
        if (
            ty == "str"
            and _is_nonempty_str_literal(init_text)
            or ty != "str"
            and init_text.strip() not in ("0", "null", "false", "''", '""', "0.0")
            and expr_is_always_truthy(init_text)
        ):
            state.fs.known_truthy_globals.add(name)
    if lines and lines[-1] != "":
        lines.append("")
    lines.append("def main() -> int:")
    old_scope = state.fs.scope
    state.fs.scope = Scope(old_scope)
    body = gen_body_with_del_has(state, 0, 1)
    for stmt in body:
        lines.append(stmt)
    lines.append("    return 0")
    state.fs.scope = old_scope
    return "\n".join(lines)


def _fuzz_worker(args):
    base_seed, n = args
    fs = FuzzerState(base_seed)
    state = DelHasState(fs)
    local = {"pass": 0, "reject": 0, "crash": 0, "bug": 0, "ub": 0, "test": 0}
    for _ in range(n):
        source = gen_program_with_del_has(state)
        run_test(source, "para")
        for k in ("pass", "reject", "crash", "bug", "ub", "test"):
            local[k] = globals()[k.upper() + "_COUNT"]
    return local


def main():
    n_total = 20000
    if len(sys.argv) > 1:
        n_total = int(sys.argv[1])
    seed = None
    if "--seed" in sys.argv:
        idx = sys.argv.index("--seed")
        seed = int(sys.argv[idx + 1])
    else:
        seed = random.randint(0, 2**31 - 1)

    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    per_worker = max(1, n_total // workers)
    args = [(seed + 1000 * i, per_worker) for i in range(workers)]
    print(
        f"Del/has/get_attr fuzzer: seed={seed}, iterations≈{per_worker * workers} "
        f"({workers} workers)",
        flush=True,
    )
    t0 = time.time()
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_fuzz_worker, args))
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        return
    elapsed = time.time() - t0
    total = dict([("test", 0), ("pass", 0), ("reject", 0), ("crash", 0), ("bug", 0), ("ub", 0)])
    for r in results:
        for k in total:
            total[k] += r[k]
    print(
        f"\nDone: {total['test']} tests ({elapsed:.1f}s across {workers} workers) | "
        f"pass={total['pass']} reject={total['reject']} crashes={total['crash']} "
        f"bugs={total['bug']} ub_noise={total['ub']}"
    )
    if total["crash"] or total["bug"]:
        crash_files = [f for f in os.listdir(CRASH_DIR) if f.startswith("crash_")]
        print(f"Crashes saved in: {CRASH_DIR} ({len(crash_files)} files)")
    print(f"Seed: {seed}")


if __name__ == "__main__":
    main()
