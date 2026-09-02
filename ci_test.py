"""Cross-platform CI test harness for Cpyte.

Builds every program in the curated corpus as a native executable (AOT), then
runs it, to exercise the full compile pipeline — lexer, parser, semantic
analysis, LLVM codegen, the C runtime, the GC runtime and the system linker —
on every CI OS (macOS / Linux / Windows).

The corpus is explicit rather than a wildcard directory glob so that programs
which cannot be exercised generically in CI are never picked up:
- programs that block on stdin (e.g. ``input_big``) would hang the job;
- ``examples/test.cpy`` installs a global macOS event tap and drops key
  events, which can affect the host / CI runner;
- programs with intentional semantic errors (e.g. ``test_uninitialized``) are
  negative tests and belong in their own harness, not here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CPYTE = ROOT / "source" / "cpyte" / "__main__.py"

# Curated programs that compile (AOT) and run cleanly on all CI platforms.
# Each entry is a repo-relative path from ROOT.
CORPUS: tuple[str, ...] = (
    "examples/c_import_example.cpy",
    "examples/test2am9august.cpy",
    "test/test_big.cpy",
    "test/test_big2.cpy",
    "test/test_big_hex.cpy",
    "test/test_big_mixed.cpy",
    "test/test_decorator.cpy",
    "test/test_hex_e.cpy",
    "test/most_complex.cpy",
)


def _snapshot_artifacts() -> set[Path]:
    """Return the compiler's intermediate ".o" artifacts currently in the repo.

    Covers ``foo.o``, ``foo.gc.o`` and ``foo.runtime.o`` (all end in ".o").
    """
    artifacts: set[Path] = set()
    for d in (ROOT / "examples", ROOT / "test"):
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix == ".o":
                artifacts.add(p)
    return artifacts


def main() -> int:
    # Windows CI consoles default to cp1252, which cannot encode the ✓ / ✗
    # markers used by this harness. Force UTF-8 (with replacement) on our own
    # streams so the report can never crash the job with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    python = sys.executable

    failures: list[str] = []
    built = 0

    with tempfile.TemporaryDirectory(prefix="cpyte_ci_") as tmp:
        tmpdir = Path(tmp)

        for rel in CORPUS:
            src = ROOT / rel

            if not src.is_file():
                failures.append(f"{rel}: source file not found")
                continue

            built += 1
            exe = tmpdir / ("prog.exe" if os.name == "nt" else "prog")

            print(f"\n{'=' * 60}")
            print(f"Build + run: {rel}")
            print(f"{'=' * 60}")

            # Snapshot existing compiler artifacts so we can remove any the
            # build creates, leaving the repository untouched.
            pre_existing = _snapshot_artifacts()

            # Build a native executable (AOT). This exercises the compiler on
            # the current OS, including the C/GC runtimes and system linker.
            # Source-relative C imports (e.g. `import "examples/foo.c"`)
            # resolve from the repo, so we build in place from the original
            # file.
            #
            # The package lives under source/ and is not guaranteed to be
            # installed on the runner, so put source/ on PYTHONPATH for the
            # compiler subprocess. This also ensures we exercise THIS checkout
            # rather than whatever may be installed in the environment.
            env = dict(os.environ)
            src_on_path = str(ROOT / "source")
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                src_on_path + os.pathsep + existing if existing else src_on_path
            )

            build = subprocess.run(
                [python, str(CPYTE), "build", "-o", str(exe), str(src)],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                text=True,
            )

            # The `build` command writes intermediate objects next to the
            # source and any C files it imports; remove the ones this run
            # created so CI leaves the repository untouched.
            def _cleanup() -> None:
                for p in _snapshot_artifacts() - pre_existing:
                    p.unlink(missing_ok=True)

            if build.returncode != 0:
                print(build.stdout)
                print(f"✗ BUILD FAILED: {rel}")
                failures.append(f"{rel}: build failed")
                _cleanup()
                continue

            if not exe.exists():
                print(f"✗ BUILD FAILED (no output): {rel}")
                failures.append(f"{rel}: build produced no executable")
                _cleanup()
                continue

            run = subprocess.run(
                [str(exe)],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                text=True,
            )
            _cleanup()
            if run.returncode != 0:
                print(run.stdout)
                print(f"✗ RUN FAILED: {rel}")
                failures.append(f"{rel}: run failed (exit {run.returncode})")
                continue

            print(
                f"✓ PASSED: {rel} -> {run.stdout.splitlines()[0] if run.stdout.strip() else '<no output>'}"
            )

    print(f"\n{'=' * 60}")
    print(f"Built {built}/{len(CORPUS)} programs. Failures: {len(failures)}")
    print(f"{'=' * 60}")

    if failures:
        for f in failures:
            print(f"  - {f}")
        return 1

    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
