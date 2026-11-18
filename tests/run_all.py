#!/usr/bin/env python3
"""Run every check in this folder. Usage: .venv/bin/python tests/run_all.py

Each test file is its own program that prints PASS/FAIL lines and exits
non-zero on a failure, so this just runs them in turn and adds up the result.
No framework, nothing to install.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    files = sorted(p for p in HERE.glob("test_*.py"))
    if not files:
        print("no test files found")
        return 1

    failed = []
    for path in files:
        print(f"\n--- {path.name}")
        result = subprocess.run([sys.executable, str(path)])
        if result.returncode != 0:
            failed.append(path.name)

    print("\n" + "=" * 46)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"All {len(files)} test files passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
