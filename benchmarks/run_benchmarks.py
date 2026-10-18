#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

C_CC = "clang"
C_CFLAGS = ["-O3", "-lm"]
CPY_COMPILE = [
    sys.executable,
    os.path.join(PROJECT, "source", "cpyte", "mainpie.py"),
    "--aot",
]


ALL_BENCHMARKS = [
    {"name": "fib_recursive", "has_cpy": True},
    {"name": "fib_iterative", "has_cpy": True},
    {"name": "prime_count", "has_cpy": True},
    {"name": "matrix_mult", "has_cpy": True},
    {"name": "call_overhead", "has_cpy": True},
    {"name": "quicksort", "has_cpy": True},
    {"name": "string_concat", "has_cpy": True},
    {"name": "mem_alloc", "has_cpy": True},
]


def log(msg):
    print(f"  {msg}", flush=True)


def compile_c(name):
    src = os.path.join(HERE, f"{name}.c")
    out = os.path.join(HERE, f"{name}_c")
    subprocess.run(
        [C_CC, *C_CFLAGS, "-o", out, src],
        capture_output=True,
        text=True,
    )


def compile_cpy(name):
    """Build `<name>.cpy` to a native executable in a throwaway temp dir.

    `cpyte --aot <src>` writes `<src>.o` AND links `<src without extension>`
    itself (compiling.run_aot: out_name = output.rsplit(".", 1)[0], then
    linker.link with aot_link_libraries()), so there is no manual link step
    here -- the old `clang program.o runtime.c` link broke when --aot stopped
    hardcoding its object name to `program.o`. The temp dir keeps the tracked
    benchmarks/*.o artifacts from being clobbered.

    Returns (exe_path, build_dir) on success, (None, None) on failure.
    """
    cpy_src = os.path.join(HERE, f"{name}.cpy")
    build_dir = tempfile.mkdtemp(prefix=f"cpy_bench_{name}_")
    src = os.path.join(build_dir, os.path.basename(cpy_src))
    shutil.copy2(cpy_src, src)
    r = subprocess.run(
        [*CPY_COMPILE, src],
        capture_output=True,
        text=True,
        cwd=build_dir,
    )
    if r.returncode != 0:
        print(f"    cpy compile failed: {(r.stderr or r.stdout).strip()}")
        shutil.rmtree(build_dir, ignore_errors=True)
        return None, None
    exe = os.path.join(build_dir, name)
    if not os.path.isfile(exe):
        print(f"    cpy link failed: {exe} was not produced")
        if (r.stderr or "").strip():
            print(f"    {r.stderr.strip()}")
        shutil.rmtree(build_dir, ignore_errors=True)
        return None, None
    return exe, build_dir


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
        return f"{ns / 1_000:.3f}us"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.3f}ms"
    return f"{ns / 1_000_000_000:.6f}s"


def cleanup(build_dirs):
    for bm in ALL_BENCHMARKS:
        for suffix in ("_c", "_cpy"):
            exe = os.path.join(HERE, bm["name"] + suffix)
            if os.path.exists(exe):
                os.remove(exe)
    for build_dir in build_dirs:
        shutil.rmtree(build_dir, ignore_errors=True)


def main():
    os.chdir(HERE)

    print(f"\n{'=' * 70}")
    print("  Compiling benchmarks...")
    print(f"{'=' * 70}\n")

    build_dirs = []
    for bm in ALL_BENCHMARKS:
        name = bm["name"]
        print(f"  [{name}]")

        compile_c(name)
        log("C OK")

        bm["cpy_ok"] = False
        bm["cpy_exe"] = None
        if bm["has_cpy"]:
            exe, build_dir = compile_cpy(name)
            if build_dir:
                build_dirs.append(build_dir)
            bm["cpy_exe"] = exe
            bm["cpy_ok"] = exe is not None
            log("cpy OK" if bm["cpy_ok"] else "cpy FAILED")
        else:
            log("cpy N/A")

        log("Python OK")
        print()

    failed = [
        bm["name"] for bm in ALL_BENCHMARKS if bm["has_cpy"] and not bm["cpy_ok"]
    ]
    if failed:
        cleanup(build_dirs)
        print(f"{'=' * 70}")
        print(f"  cpy benchmark build FAILED: {', '.join(failed)}")
        print(f"{'=' * 70}")
        sys.exit(1)

    print(f"{'=' * 70}")
    print("  Running benchmarks (3 trials, no averaging)...")
    print(f"{'=' * 70}\n")

    results = {}

    for bm in ALL_BENCHMARKS:
        name = bm["name"]
        print(f"  [{name}]")

        for lang, cmd, key in [
            ("C", [os.path.join(HERE, f"{name}_c")], "C"),
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

        if bm.get("cpy_ok"):
            lang = "cpy"
            cmd = [bm["cpy_exe"]]
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

    print(f"{'=' * 70}")
    print("  Results (raw ns per trial, no averaging)")
    print(f"{'=' * 70}\n")

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
        cpy_s = (
            fmt(cpy_trials)
            if bm.get("cpy_ok") and cpy_trials and cpy_trials[0] is not None
            else "N/A"
        )

        r = ""
        c_best = best(c_trials)
        py_best = best(py_trials)
        cpy_best = best(cpy_trials)
        if c_best and py_best:
            r += f"py/c={py_best / c_best:.1f}x"
            if cpy_best:
                r += f" cpy/c={cpy_best / c_best:.1f}x"
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
    cleanup(build_dirs)


if __name__ == "__main__":
    main()
