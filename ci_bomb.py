"""CI "bomb test": prove the compiler survives every historical crash reproducer.

Pushes every program in ``test/crashes/`` (2500 fuzz-discovered programs that at
some point crashed, hung, or produced a compiler traceback) through the FULL
compile pipeline — lexer, parser, semantic analysis and LLVM codegen — using the
``--emit-llvm`` mode (no system linker / C compiler needed). A file passes when
the compiler terminates normally: it may return 0 (compiled cleanly) or 1 (a
clean semantic/parse diagnostic), but it must never hang, raise a Python
traceback, or drop into the compiler's "got an error" wrapper.

Files compile in parallel so the job stays a few minutes even on 5-platform CI.
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CPYTE = ROOT / "source" / "cpyte" / "mainpie.py"
CRASH_DIR = ROOT / "test" / "crashes"

# Combined-output fragments that mean the compiler itself blew up rather than
# producing an ordinary diagnostic.
FAIL_MARKERS = (
    "Traceback",
    "Cpyte got an error",
    "Segmentation fault",
    "Address access",
    "illegal instruction",
)

PER_FILE_TIMEOUT_S = 60


def _check_one(python: str, src: Path) -> tuple[Path, str, float]:
    start = time.monotonic()
    try:
        r = subprocess.run(
            [python, str(CPYTE), "--emit-llvm", str(src)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PER_FILE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return src, "hang (>%ds)" % PER_FILE_TIMEOUT_S, time.monotonic() - start
    elapsed = time.monotonic() - start
    if r.returncode not in (0, 1):
        return src, f"exit {r.returncode}", elapsed
    combined = (r.stdout or "") + (r.stderr or "")
    for marker in FAIL_MARKERS:
        if marker in combined:
            return src, f"crash marker {marker!r}", elapsed
    return src, "", elapsed


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    crashes = sorted(CRASH_DIR.glob("*.cpy"))
    if not crashes:
        print(f"No .cpy files under {CRASH_DIR}")
        return 1
    print(f"Bomb test: {len(crashes)} historical crash reproducers")

    python = sys.executable
    workers = max(1, min(8, os.cpu_count() or 1))

    failures: list[tuple[Path, str]] = []
    passed = 0
    start = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check_one, python, src): src for src in crashes}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            src, reason, elapsed = fut.result()
            if reason:
                failures.append((src, reason))
            else:
                passed += 1
            if done % 500 == 0:
                print(f"  {done}/{len(crashes)} done, {passed} clean ...")

    total = time.monotonic() - start
    print(f"\n{'=' * 60}")
    print(
        f"BOMB TEST: {passed}/{len(crashes)} survived "
        f"({total:.1f}s with {workers} workers)"
    )
    print(f"{'=' * 60}")

    if failures:
        print(f"FAILURES: {len(failures)}")
        for src, reason in failures[:30]:
            print(f"  - {src.relative_to(ROOT)}: {reason}")
        if len(failures) > 30:
            print(f"  ...and {len(failures) - 30} more")
        return 1

    print("The compiler survived the full corpus.")
    return 0


if __name__ == "__main__":
    sys.exit(main())