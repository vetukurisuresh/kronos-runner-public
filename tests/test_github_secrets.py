from __future__ import annotations

from pathlib import Path

from scripts.set_github_secrets import secret_payload


def test_long_secret_value_is_not_treated_as_a_filename() -> None:
    token = "a" * 400
    assert secret_payload(token) == token.encode("utf-8")


def test_file_backed_secret_still_reads_the_file(tmp_path: Path) -> None:
    source = tmp_path / "firebase.json"
    source.write_text('{"project_id":"kronos"}', encoding="utf-8")
    assert secret_payload(str(source)) == source.read_bytes()


def test_empty_secret_is_empty_bytes() -> None:
    assert secret_payload("") == b""
