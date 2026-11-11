"""Weight-download planning and manifest tests. No network is touched."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from config.settings import KRONOS_MODELS  # noqa: E402
from model.download_all_kronos import (  # noqa: E402
    plan_downloads,
    verify_manifest,
    write_manifest,
)


def test_plan_covers_the_whole_trio(tmp_path: Path) -> None:
    plan = plan_downloads(tmp_path)
    assert [entry["name"] for entry in plan] == ["base", "small", "mini"]
    assert all(entry["present"] is False for entry in plan)


def test_plan_marks_cached_weights(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "config.json").write_text("{}", encoding="utf-8")
    plan = {entry["name"]: entry for entry in plan_downloads(tmp_path)}
    assert plan["base"]["present"] is True
    assert plan["mini"]["present"] is False


def test_model_registry_matches_documented_size() -> None:
    total = sum(spec["params_m"] for spec in KRONOS_MODELS.values())
    assert total == pytest.approx(131.1, abs=0.2)
    assert KRONOS_MODELS["base"]["params_m"] == pytest.approx(102.3)
    assert sum(spec["weight"] for spec in KRONOS_MODELS.values()) == pytest.approx(1.0)


def test_manifest_roundtrip(tmp_path: Path) -> None:
    for name in KRONOS_MODELS:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "config.json").write_text("{}", encoding="utf-8")

    manifest_path = write_manifest(tmp_path)
    assert manifest_path.is_file()

    ok, problems = verify_manifest(tmp_path)
    assert ok, problems


def test_verify_manifest_reports_missing_manifest(tmp_path: Path) -> None:
    ok, problems = verify_manifest(tmp_path)
    assert ok is False
    assert any("missing manifest" in problem for problem in problems)


def test_verify_manifest_reports_missing_weights(tmp_path: Path) -> None:
    write_manifest(tmp_path)
    ok, problems = verify_manifest(tmp_path)
    assert ok is False
    assert len(problems) == len(KRONOS_MODELS)
