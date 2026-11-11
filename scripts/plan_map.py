"""Validate docs/plan_map.json against the filesystem.

The map is the contract for what may be added or removed. Drift in either
direction is a failure: a mapped file that vanished, or a production module that
nobody declared.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_MAP = REPO_ROOT / "docs" / "plan_map.json"

SKIP_PARTS = {"__init__.py"}
SKIP_DIRS = {"tests", "docs", "kronos-runner-public", ".git", ".github", "state", "_vendor"}


def load_map(path: Path = PLAN_MAP) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"plan map missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def mapped_paths(plan: dict) -> list[str]:
    return [str(entry["path"]) for entry in plan.get("modules", [])]


def discover_modules() -> set[str]:
    found: set[str] = set()
    for folder in ("agent", "config", "model", "market", "strategy", "execution", "dashboard", "mobile_app", "scripts"):
        base = REPO_ROOT / folder
        if not base.is_dir():
            continue
        for file in base.rglob("*.py"):
            relative = file.relative_to(REPO_ROOT)
            if file.name in SKIP_PARTS or any(part in SKIP_DIRS for part in relative.parts):
                continue
            found.add(relative.as_posix())
    return found


def validate(plan: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    paths = mapped_paths(plan)
    if not paths:
        return ["plan map declares no modules"], warnings

    for path in paths:
        if not (REPO_ROOT / path).exists():
            errors.append(f"mapped file is missing on disk: {path}")

    duplicates = {path for path in paths if paths.count(path) > 1}
    for duplicate in duplicates:
        errors.append(f"duplicate map entry: {duplicate}")

    declared = set(paths)
    for module in sorted(discover_modules() - declared):
        warnings.append(f"production module not declared in the map: {module}")

    phases = {entry.get("phase") for entry in plan.get("modules", [])}
    if None in phases:
        errors.append("every module needs a phase")
    return errors, warnings


def render(plan: dict) -> str:
    rows = [f"{'phase':>5}  {'layer':<12} {'status':<10} path"]
    for entry in plan.get("modules", []):
        rows.append(
            f"{str(entry.get('phase', '?')):>5}  {entry.get('layer', '?'):<12} "
            f"{entry.get('status', '?'):<10} {entry.get('path', '?')}"
        )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the add/remove plan map")
    parser.add_argument("--json", action="store_true", help="print the raw plan")
    arguments = parser.parse_args(argv)

    plan = load_map()
    if arguments.json:
        print(json.dumps(plan, indent=2))
        return 0

    errors, warnings = validate(plan)
    print(render(plan))
    print(f"\n{len(plan.get('modules', []))} modules mapped")

    for warning in warnings:
        print(f"WARN  {warning}")
    for error in errors:
        print(f"FAIL  {error}")

    if errors:
        print("\nplan map is out of sync with the tree")
        return 1
    print("plan map matches the tree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
