import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CPYTE = ROOT / "source" / "cpyte" / "__main__.py"

for directory in ("examples", "crash"):
    path = ROOT / directory

    if not path.is_dir():
        continue

    for file in sorted(path.rglob("*.cpy")):
        relative = file.relative_to(ROOT)

        print(f"\n{'=' * 60}")
        print(f"Testing: {relative}")
        print(f"{'=' * 60}")

        result = subprocess.run(
            [sys.executable, str(CPYTE), str(file)],
            cwd=ROOT,
            check=False
        )

        if result.returncode != 0:
            print(f"✗ FAILED: {relative}")
            sys.exit(result.returncode)

        print(f"✓ PASSED: {relative}")

print("\nAll tests passed.")
