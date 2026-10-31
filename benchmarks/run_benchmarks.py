#!/usr/bin/env python3
"""Build and compare matching C, Cpy, and Python benchmark programs."""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
COMPILER = os.path.join(PROJECT, "source", "cpyte", "mainpie.py")


def discover_benchmarks():
    names = sorted(
        name[:-4]
        for name in os.listdir(HERE)
        if name.endswith(".cpy")
        and all(os.path.isfile(os.path.join(HERE, name[:-4] + ext))
                for ext in (".c", ".py"))
    )
    return names


def run_process(command, cwd, timeout):
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout:g}s"
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        return None, f"process exited {result.returncode}: {details}"
    return result.stdout.strip(), None


def compile_benchmark(name, build_dir):
    paths = {}
    c_path = os.path.join(build_dir, name + "_c")
    c_out, error = run_process(
        ["clang", "-O3", "-o", c_path, os.path.join(HERE, name + ".c"), "-lm"],
        build_dir,
        60,
    )
    if error:
        return None, f"C compile failed: {error}"
    paths["C"] = [c_path]
    paths["Python"] = [sys.executable, os.path.join(HERE, name + ".py")]

    cpy_source = os.path.join(build_dir, name + ".cpy")
    shutil.copy2(os.path.join(HERE, name + ".cpy"), cpy_source)
    _, error = run_process([sys.executable, COMPILER, "--aot", cpy_source],
                           build_dir, 180)
    if error:
        return None, f"Cpy compile failed: {error}"
    cpy_path = os.path.join(build_dir, name)
    if not os.path.isfile(cpy_path):
        return None, f"Cpy compiler did not produce {cpy_path}"
    paths["Cpy"] = [cpy_path]
    return paths, None


def execute_once(command, cwd, timeout):
    start = time.perf_counter_ns()
    output, error = run_process(command, cwd, timeout)
    elapsed = time.perf_counter_ns() - start
    return elapsed, output, error


def benchmark_group(commands, cwd, warmups, trials, timeout):
    results = {
        language: {"samples_ns": [], "output": None}
        for language in ("C", "Cpy", "Python")
    }
    for language in results:
        for _ in range(warmups):
            _, output, error = execute_once(commands[language], cwd, timeout)
            if error:
                results[language] = {"error": error}
                break
            previous = results[language]["output"]
            if previous is not None and previous != output:
                results[language]["error"] = (
                    f"output changed between runs: {output!r}"
                )
                break
            results[language]["output"] = output

    for trial in range(trials):
        active = [lang for lang in results if "error" not in results[lang]]
        if not active:
            break
        order = active[trial % len(active):] + active[:trial % len(active)]
        for language in order:
            elapsed, output, error = execute_once(
                commands[language], cwd, timeout
            )
            result = results[language]
            if error:
                result["error"] = error
                continue
            if result["output"] is None:
                result["output"] = output
            elif result["output"] != output:
                result["error"] = f"output changed between runs: {output!r}"
                continue
            result["samples_ns"].append(elapsed)

    for result in results.values():
        if "error" not in result:
            result["median_ns"] = int(statistics.median(result["samples_ns"]))
    return results


def format_time(ns):
    if ns < 1_000:
        return f"{ns}ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.2f}us"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.2f}ms"
    return f"{ns / 1_000_000_000:.3f}s"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=7,
                        help="measured runs per language (default: 7)")
    parser.add_argument("--warmups", type=int, default=1,
                        help="unmeasured runs per language (default: 1)")
    parser.add_argument("--filter", help="run names containing this substring")
    parser.add_argument("--timeout", type=float, default=60,
                        help="per-run timeout in seconds (default: 60)")
    parser.add_argument("--json", dest="json_path",
                        help="write raw results to this JSON file")
    args = parser.parse_args()
    if args.trials < 1 or args.warmups < 0 or args.timeout <= 0:
        parser.error("--trials must be positive; warmups nonnegative; timeout positive")

    names = discover_benchmarks()
    if args.filter:
        names = [name for name in names if args.filter in name]
    if not names:
        parser.error("no complete C/Cpy/Python benchmark triplets found")

    results = {}
    with tempfile.TemporaryDirectory(prefix="cpy_bench_") as build_dir:
        for name in names:
            print(f"\n[{name}] compiling...", flush=True)
            commands, error = compile_benchmark(name, build_dir)
            if error:
                results[name] = {"error": error}
                print(f"  ERROR: {error}")
                continue

            timings = benchmark_group(
                commands, HERE, args.warmups, args.trials, args.timeout
            )
            for language, result in timings.items():
                if "error" in result:
                    print(f"  {language:6s} ERROR: {result['error']}")
                else:
                    print(
                        f"  {language:6s} median {format_time(result['median_ns'])}"
                        f"  output={result['output']!r}"
                    )

            valid = [timings[lang] for lang in ("C", "Cpy", "Python")
                     if "error" not in timings[lang]]
            if len(valid) == 3 and len({run["output"] for run in valid}) != 1:
                message = "language outputs differ; timings are not comparable"
                for language in timings:
                    timings[language]["comparison_error"] = message
                print(f"  ERROR: {message}")
                results[name] = {"error": message, **timings}
                continue
            results[name] = timings

    print("\nMedian wall time per process run (includes process startup)")
    print(f"{'Benchmark':<22} {'C':>12} {'Cpy':>12} {'Python':>12}"
          f" {'Cpy/C':>10} {'Py/C':>10}")
    print("-" * 82)
    for name in names:
        row = results[name]
        if "error" in row:
            print(f"{name:<22} ERROR: {row['error']}")
            continue
        values = [row[lang].get("median_ns") for lang in ("C", "Cpy", "Python")]
        show = lambda value: format_time(value) if value is not None else "FAIL"
        cpy_ratio = values[1] / values[0] if values[0] and values[1] else None
        py_ratio = values[2] / values[0] if values[0] and values[2] else None
        print(
            f"{name:<22} {show(values[0]):>12} {show(values[1]):>12}"
            f" {show(values[2]):>12}"
            f" {f'{cpy_ratio:.2f}x' if cpy_ratio else 'n/a':>10}"
            f" {f'{py_ratio:.2f}x' if py_ratio else 'n/a':>10}"
        )

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as result_file:
            json.dump(results, result_file, indent=2)
            result_file.write("\n")

    if any(
        "error" in result
        or any("error" in lang_result for lang_result in result.values())
        for result in results.values()
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
