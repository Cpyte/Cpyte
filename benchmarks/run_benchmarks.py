#!/usr/bin/env python3
import os
import subprocess
import time
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
RUNTIME_C = os.path.join(HERE, "runtime.c")

C_CC = "clang"
C_CFLAGS = ["-O3", "-lm"]
CPY_COMPILE = [sys.executable, os.path.join(PROJECT, "mainpie.py"), "--aot"]
CPY_LINK = [C_CC, "program.o", RUNTIME_C, "-o", "{name}", "-lm"]

ALL_BENCHMARKS = [
    {"name": "fib_recursive",  "has_cpy": True},
    {"name": "fib_iterative",  "has_cpy": True},
    {"name": "prime_count",    "has_cpy": True},
    {"name": "matrix_mult",    "has_cpy": True},
    {"name": "call_overhead",  "has_cpy": True},
    {"name": "quicksort",      "has_cpy": True},
    {"name": "string_concat",  "has_cpy": True},
    {"name": "mem_alloc",      "has_cpy": True},
]


def log(msg):
    print(f"  {msg}", flush=True)


def compile_c(name):
    src = os.path.join(HERE, f"{name}.c")
    out = os.path.join(HERE, name)
    subprocess.run(
        [C_CC, *C_CFLAGS, "-o", out, src],
        capture_output=True, text=True,
    )


def compile_cpy(name):
    cpy_src = os.path.join(HERE, f"{name}.cpy")
    r = subprocess.run(
        [*CPY_COMPILE, cpy_src],
        capture_output=True, text=True, cwd=HERE,
    )
    if r.returncode != 0:
        print(f"    cpy compile failed: {r.stderr.strip()}")
        return False
    exe = os.path.join(HERE, name)
    r2 = subprocess.run(
        [C_CC, "program.o", RUNTIME_C, "-o", exe, "-lm"],
        capture_output=True, text=True, cwd=HERE,
    )
    if r2.returncode != 0:
        print(f"    cpy link failed: {r2.stderr.strip()}")
        return False
    return True


def run_and_time_ns(cmd, cwd=None):
    start = time.perf_counter_ns()
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or HERE)
    elapsed_ns = time.perf_counter_ns() - start
    output = (r.stdout or "").strip()
    if r.returncode != 0:
        print(f"      exit code {r.returncode}: {(r.stderr or '').strip()}")
        return None, None
    return elapsed_ns, output


def fmt_ns(ns):
    if ns < 1000:
        return f"{ns}ns"
    if ns < 1_000_000:
        return f"{ns/1_000:.3f}us"
    if ns < 1_000_000_000:
        return f"{ns/1_000_000:.3f}ms"
    return f"{ns/1_000_000_000:.6f}s"


def main():
    os.chdir(HERE)

    src_runtime = os.path.join(PROJECT, "runtime.c")
    if not os.path.exists(RUNTIME_C) and os.path.exists(src_runtime):
        shutil.copy2(src_runtime, RUNTIME_C)

    print(f"\n{'='*70}")
    print(f"  Compiling benchmarks...")
    print(f"{'='*70}\n")

    for bm in ALL_BENCHMARKS:
        name = bm["name"]
        print(f"  [{name}]")

        compile_c(name)
        log("C OK")

        if bm["has_cpy"]:
            ok = compile_cpy(name)
            log("cpy OK" if ok else "cpy SKIPPED")
        else:
            log("cpy N/A")

        log("Python OK")
        print()

    print(f"{'='*70}")
    print(f"  Running benchmarks (3 trials, no averaging)...")
    print(f"{'='*70}\n")

    results = {}

    for bm in ALL_BENCHMARKS:
        name = bm["name"]
        print(f"  [{name}]")

        for lang, cmd, key in [
            ("C",     [os.path.join(HERE, name)], "C"),
            ("Python", [sys.executable, os.path.join(HERE, f"{name}.py")], "Python"),
        ]:
            trials = []
            out = None
            for trial in range(3):
                t, o = run_and_time_ns(cmd)
                if t is None:
                    trials.append(None)
                else:
                    trials.append(t)
                    out = o
            results[(name, key)] = (trials, out)
            if trials[0] is None:
                log(f"{lang:8s} FAILED")
            else:
                parts = ", ".join(fmt_ns(t) for t in trials)
                log(f"{lang:8s} {parts}  [{out}]")

        if bm["has_cpy"]:
            lang = "cpy"
            cmd = [os.path.join(HERE, name)]
            trials = []
            out = None
            for trial in range(3):
                t, o = run_and_time_ns(cmd)
                if t is None:
                    trials.append(None)
                else:
                    trials.append(t)
                    out = o
            results[(name, "cpy")] = (trials, out)
            if trials[0] is None:
                log(f"{lang:8s} FAILED")
            else:
                parts = ", ".join(fmt_ns(t) for t in trials)
                log(f"{lang:8s} {parts}  [{out}]")

        print()

    print(f"{'='*70}")
    print(f"  Results (raw ns per trial, no averaging)")
    print(f"{'='*70}\n")

    rows = []
    for bm in ALL_BENCHMARKS:
        name = bm["name"]

        def best(trials):
            if not trials:
                return None
            valid = [t for t in trials if t is not None]
            return min(valid) if valid else None

        c_trials, _ = results.get((name, "C"), (None, ""))
        py_trials, _ = results.get((name, "Python"), (None, ""))
        cpy_trials, _ = results.get((name, "cpy"), (None, ""))

        def fmt(trials):
            if not trials or trials[0] is None:
                return "FAIL"
            return ", ".join(fmt_ns(t) for t in trials)

        c_s = fmt(c_trials)
        py_s = fmt(py_trials)
        cpy_s = fmt(cpy_trials) if bm["has_cpy"] and cpy_trials and cpy_trials[0] is not None else "N/A"

        r = ""
        c_best = best(c_trials)
        py_best = best(py_trials)
        cpy_best = best(cpy_trials)
        if c_best and py_best:
            r += f"py/c={py_best/c_best:.1f}x"
            if cpy_best:
                r += f" cpy/c={cpy_best/c_best:.1f}x"
        rows.append((name, c_s, py_s, cpy_s, r))

    w = max(len(r[0]) for r in rows) + 2
    print(f"{'Benchmark':<{w}} {'C':<42} {'Python':<42} {'cpy':<42}")
    print("-" * (w + 126))
    for name, c_s, py_s, cpy_s, ratio in rows:
        print(f"{name:<{w}} {c_s:<42} {py_s:<42} {cpy_s:<42}")
        if ratio:
            print(f"{'':>{w}} {ratio}")
    print()

    # Cleanup
    for bm in ALL_BENCHMARKS:
        exe = os.path.join(HERE, bm["name"])
        if os.path.exists(exe):
            os.remove(exe)
    obj = os.path.join(HERE, "program.o")
    if os.path.exists(obj):
        os.remove(obj)


if __name__ == "__main__":
    main()
