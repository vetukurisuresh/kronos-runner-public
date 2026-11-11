"""Structural self-check for the repository tree.

Thin wrapper over :mod:`verify_cloud` so the same rules can be run from the
relay workflow, a pre-commit hook, or by hand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRONOS structural self-check")
    parser.add_argument("--fast", action="store_true", help="skip import checks")
    arguments = parser.parse_args(argv)

    try:
        from verify_cloud import run_checks, render_report
    except ImportError as exc:
        print(f"cannot import verify_cloud: {exc}", file=sys.stderr)
        return 2

    report = run_checks(include_imports=not arguments.fast)
    print(render_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
