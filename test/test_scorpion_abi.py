#!/usr/bin/env python3
"""Regression test for the `#scorpion` ABI directive.

`#scorpion` on the first non-blank line injects the whole Scorpion ABI into
the language: the `SYS_*` / `SCORPION_*` constants, the `ScorpionLibInfo`
struct and all 22 `scorpion_*` syscall wrappers. This exercises

  1. directive detection / source stripping (and that it is *off* by default),
  2. semantic availability of every constant, the struct and every function,
     including call-arity checking that comes for free with the injected
     symbols,
  3. the exact LLVM surface: 22 extern declarations with the right signatures
     and `struct.ScorpionLibInfo` laid out as 7 x i32 + 2 x i16 = 32 bytes,
     matching the kernel-written struct in `abi/scorpion.h`,
  4. that every ABI symbol has a non-static definition in
     `source/cpyte/runtime_scorpion.c`, so the RV32 build actually links,
  5. that the LSP sees the same ABI (hover/completion come off analyzer globals).

Run standalone:  python test/test_scorpion_abi.py
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "source"))

from cpyte import scorpion_abi  # noqa: E402

FAILURES = []


def check(cond, label, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f"  -- {detail}" if detail else ""))
        FAILURES.append(label)


# ── 1. directive detection ────────────────────────────────────────────────


def test_detect():
    print("directive detection")
    src, on = scorpion_abi.detect("#scorpion\n\ndef main() -> int:\n    return 0\n")
    check(on is True, "#scorpion on the first line enables the ABI")
    check("#scorpion" not in src, "the directive is stripped from the source")
    check(
        src.split("\n")[0] == "", "it is replaced by a blank line (line numbers hold)"
    )

    src, on = scorpion_abi.detect(
        "\n\n   \n#scorpion\ndef main() -> int:\n    return 0\n"
    )
    check(on is True, "leading blank lines are skipped")

    src, on = scorpion_abi.detect("def main() -> int:\n    return 0\n#scorpion\n")
    check(on is False, "a directive that is not on the first non-blank line is inert")

    src, on = scorpion_abi.detect("#scorpionx\ndef main() -> int:\n    return 0\n")
    check(on is False, "a near-miss directive is not honoured")

    # The CLI scans directives in order, so #nogc then #scorpion both land.
    from cpyte.mainpie import _detect_nogc, _detect_scorpion

    src, nogc = _detect_nogc("#nogc\n#scorpion\ndef main() -> int:\n    return 0\n")
    src, on = _detect_scorpion(src)
    check(nogc and on, "#nogc followed by #scorpion enables both")
    check("#nogc" not in src and "#scorpion" not in src, "both directives are stripped")


# ── 2. semantic availability ──────────────────────────────────────────────

# One line per ABI function with the right arity, so any mismatch in the
# shared table surfaces as a semantic error.
FLAGGED_PROGRAM = """#scorpion

def use() -> int:
    ScorpionLibInfo info
    info.base = SYS_MAX + SCORPION_OK
    info.refcount = SCORPION_UNLOAD_GLOBAL
    info.flags = SCORPION_VER_ANY
    int rc = scorpion_lib_info(0, 0, &info)
    scorpion_yield()
    scorpion_exit()
    scorpion_block()
    scorpion_wake(0)
    scorpion_sleep(0)
    int a = scorpion_send(0, 0, 0, 0)
    int b = scorpion_recv(0, 0, 0, 0)
    scorpion_putc(0, 0)
    int c = scorpion_open(0, 0)
    int d = scorpion_read(0, 0, 0)
    int e = scorpion_write(0, 0, 0)
    int f = scorpion_close(0)
    int g = scorpion_spawn(0, 0, 0)
    int h = scorpion_terminate(0)
    int i = scorpion_loadlib(0, 0, 0)
    int j = scorpion_loadlib_global(0, 0)
    int k = scorpion_loadlib_local(0, 0)
    int l = scorpion_unloadlib(0, 0)
    void* m = scorpion_sym(0, 0)
    int n = scorpion_ver_hash(0, 0)
    void* o = scorpion_sym_ver(0, 0, 0, 0)
    return rc + a + b + c + d + e + f + g + h + i + j + k + l + n + o + m

def main() -> int:
    print(SYS_PUTC)
    print(SCORPION_ENOEXEC)
    print(SYS_MAX)
    print(use() == use())
    return 0
"""


def _compile_ok(source, filename):
    """Return (ok, diagnostics) for a source program."""
    from cpyte.lexar import Lexer
    from cpyte.astparse import parse_file
    from cpyte.semantic_analasis import SemanticAnalyzer

    src, _ = scorpion_abi.detect(source)
    parsed, _ = parse_file(Lexer(src).get_tokens())
    analyzer = SemanticAnalyzer(src, filepath=filename, scorpion=True)
    result = analyzer.analyze(parsed)
    diags = [d.message for d in analyzer.reporter.diagnostics]
    errs = [
        d for d in analyzer.reporter.diagnostics if d.level in ("error", "strict-error")
    ]
    return (not errs), diags


def test_semantic():
    print("semantic availability")
    ok, diags = _compile_ok(FLAGGED_PROGRAM, "abi_ok.cpy")
    check(
        ok,
        "a #scorpion program using the whole ABI analyses clean",
        "; ".join(diags[:4]),
    )

    # Without the directive the same program must NOT resolve.
    from cpyte.lexar import Lexer
    from cpyte.astparse import parse_file
    from cpyte.semantic_analasis import SemanticAnalyzer

    plain = FLAGGED_PROGRAM.replace("#scorpion\n", "", 1)
    parsed, _ = parse_file(Lexer(plain).get_tokens())
    analyzer = SemanticAnalyzer(plain, filepath="abi_off.cpy", scorpion=False)
    analyzer.analyze(parsed)
    errs = [
        d.message
        for d in analyzer.reporter.diagnostics
        if d.level in ("error", "strict-error")
    ]
    check(
        any("SYS_MAX" in m for m in errs),
        "without #scorpion the constants are undeclared",
        "; ".join(errs[:2]),
    )
    check(
        any("scorpion_lib_info" in m for m in errs),
        "without #scorpion the wrappers are undeclared",
    )

    # The diagnostic should point at the directive, not generic scope advice.
    from cpyte.lexar import Lexer as _Lexer
    from cpyte.astparse import parse_file as _parse
    from cpyte.semantic_analasis import SemanticAnalyzer as _SA

    src2 = "def main() -> int:\n    scorpion_yield()\n    return 0\n"
    parsed2, _ = _parse(_Lexer(src2).get_tokens())
    sa = _SA(src2)
    sa.analyze(parsed2)
    notes = [d.note for d in sa.reporter.diagnostics if d.note]
    check(
        any("#scorpion" in (n or "") for n in notes),
        "the error note names the missing #scorpion directive",
        "; ".join(map(str, notes)),
    )

    # Arity checking rides on the injected symbols.
    bad = FLAGGED_PROGRAM.replace("scorpion_yield()", "scorpion_yield(1)")
    ok, diags = _compile_ok(bad, "abi_arity.cpy")
    check(
        not ok
        and any("wrong number of arguments to `scorpion_yield`" in d for d in diags),
        "scorpion_yield(1) is a clean arity error",
        "; ".join(diags[:2]),
    )


# ── 3. the emitted LLVM surface ───────────────────────────────────────────


def _llvm_ir(source, filename, scorpion=True):
    """Compile to LLVM IR text, gating the ABI on `scorpion`."""
    from cpyte.mainpie import _compile
    from cpyte.bytecoding import LLVM

    src, _ = scorpion_abi.detect(source)
    parsed, generic = _compile(src, filepath=filename, scorpion=scorpion)
    c = LLVM(scorpion=scorpion)
    c.generic_instantiations = generic or {}
    c.emit_program(parsed)
    return str(c.module)


_UNFLAGGED_IR_SNIPPET = """
import sys
sys.path.insert(0, %r)
from cpyte.mainpie import _compile
from cpyte.bytecoding import LLVM
src = "def main() -> int:\\n    int x = 1\\n    print(x)\\n    return 0\\n"
parsed, generic = _compile(src, filepath="abi_ir_off.cpy", scorpion=False)
c = LLVM(scorpion=False)
c.generic_instantiations = generic or {}
c.emit_program(parsed)
print(str(c.module))
""" % os.path.join(ROOT, "source")


def _llvm_ir_subprocess():
    """LLVM IR for an unflagged program, in a fresh interpreter."""
    r = subprocess.run(
        [sys.executable, "-c", _UNFLAGGED_IR_SNIPPET], capture_output=True, text=True
    )
    return r.stdout if r.returncode == 0 else ""


def test_codegen():
    print("codegen")
    ir = _llvm_ir(FLAGGED_PROGRAM, "abi_ir.cpy")

    layout = re.search(r'%"struct\.ScorpionLibInfo" = type \{([^}]*)\}', ir)
    check(layout is not None, "struct.ScorpionLibInfo is emitted")
    if layout:
        types = [t.strip() for t in layout.group(1).split(",")]
        check(
            types == ["i32"] * 7 + ["i16"] * 2,
            "layout is 7 x i32 + 2 x i16 (the exact 32-byte kernel struct)",
            "got " + ", ".join(types),
        )

    decls = dict(re.findall(r'^declare [^@]*@"(\w+)"\((.*?)\)', ir, re.M))
    missing = sorted(set(scorpion_abi.FUNCTIONS) - set(decls))
    check(
        not missing,
        "all %d ABI functions are declared extern" % len(scorpion_abi.FUNCTIONS),
        "missing: " + ", ".join(missing),
    )

    # Spot-check a few signatures that carry pointers, plus the arities.
    if "scorpion_lib_info" in decls:
        check(
            decls["scorpion_lib_info"].count("i32") == 2
            and '%"struct.ScorpionLibInfo"*' in decls["scorpion_lib_info"],
            "scorpion_lib_info(i32, i32, struct.ScorpionLibInfo*)",
            decls["scorpion_lib_info"],
        )
    if "scorpion_sym_ver" in decls:
        check(
            decls["scorpion_sym_ver"].count("i8*") == 2
            and decls["scorpion_sym_ver"].count("i32") == 2,
            "scorpion_sym_ver(i8*, i32, i8*, i32)",
            decls["scorpion_sym_ver"],
        )
    if "scorpion_yield" in decls:
        check(
            decls["scorpion_yield"].strip() == "",
            "scorpion_yield()",
            decls["scorpion_yield"],
        )

    # Strictly opt-in: an unflagged program gets no ABI surface at all (not
    # even unused declarations that would bloat every SEF). Run in a
    # subprocess: llvmlite's context is process-global, so once the flagged
    # build above has registered the type it would show up here regardless.
    clean_ir = _llvm_ir_subprocess()
    check("scorpion_" not in clean_ir, "without #scorpion no ABI externs are emitted")
    check(
        "ScorpionLibInfo" not in clean_ir,
        "without #scorpion no ScorpionLibInfo struct is emitted",
    )


# ── 4. the RV32 runtime actually defines them ─────────────────────────────

RUNTIME = os.path.join(ROOT, "source", "cpyte", "runtime_scorpion.c")


def test_runtime():
    print("runtime_scorpion.c")
    c = open(RUNTIME).read()
    defined = set(
        re.findall(r"^(?!static\b)[A-Za-z_][\w \*]*?\b(scorpion_\w+)\s*\(", c, re.M)
    )
    missing = sorted(set(scorpion_abi.FUNCTIONS) - defined)
    check(
        not missing,
        "every ABI symbol has a non-static definition",
        "missing: " + ", ".join(missing),
    )
    check(
        "typedef struct {" in c and "unsigned short refcount;" in c,
        "the runtime declares the same 32-byte ScorpionLibInfo layout",
    )


# ── 5. the LSP sees the same ABI ───────────────────────────────────────────


def test_lsp():
    print("lsp_server")
    from cpyte import lsp_server

    tokens, parsed, analyzer, error = lsp_server._analyze(
        FLAGGED_PROGRAM, "abi_lsp.cpy"
    )
    check(error is None, "a #scorpion program lints clean in the LSP", str(error))
    if analyzer is None:
        return
    check(
        analyzer.globals.lookup("scorpion_lib_info") is not None,
        "LSP completions see scorpion_lib_info",
    )
    check(analyzer.globals.lookup("SYS_MAX") is not None, "LSP completions see SYS_MAX")
    check(
        analyzer.globals.lookup("ScorpionLibInfo") is not None,
        "LSP knows the ScorpionLibInfo type",
    )

    plain = FLAGGED_PROGRAM.replace("#scorpion\n", "", 1)
    _, _, analyzer2, _ = lsp_server._analyze(plain, "abi_lsp_off.cpy")
    check(
        analyzer2 is not None and analyzer2.globals.lookup("SYS_MAX") is None,
        "without #scorpion the LSP does not offer the ABI",
    )


def main() -> int:
    test_detect()
    test_semantic()
    test_codegen()
    test_runtime()
    test_lsp()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("all scorpion ABI checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
