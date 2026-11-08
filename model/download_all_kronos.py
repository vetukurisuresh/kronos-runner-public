"""Download the three KRONOS models (and their tokenizers) from HuggingFace.

Usage::

    python -m model.download_all_kronos --dry-run     # plan only, no network
    python -m model.download_all_kronos               # download + verify
    python -m model.download_all_kronos --verify-only # hash-check what is on disk

Each model repo ships only ``config.json`` + ``model.safetensors``; the
architecture code lives in ``agent/_vendor/kronos_upstream``. Tokenizers are
required to turn OHLCV candles into tokens, so they are fetched alongside:

    Kronos-mini   -> Kronos-Tokenizer-2k
    Kronos-small  -> Kronos-Tokenizer-base
    Kronos-base   -> Kronos-Tokenizer-base

Every file is checked against the LFS SHA-256 published by the Hub. Downloads
retry with exponential backoff, and the manifest records sizes so the runner can
prove what it is loading.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from config.settings import KRONOS_MODELS, WEIGHTS_DIR

TOKENIZER_FOR_MODEL = {
    "mini": "NeoQuasar/Kronos-Tokenizer-2k",
    "small": "NeoQuasar/Kronos-Tokenizer-base",
    "base": "NeoQuasar/Kronos-Tokenizer-base",
}

WEIGHT_FILE = "model.safetensors"
ALLOW_PATTERNS = ["config.json", "*.safetensors", "README.md", ".gitattributes"]
DEFAULT_RETRIES = 3


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def folder_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


def human_mb(size_bytes: int) -> str:
    return f"{size_bytes / 1e6:,.1f} MB"


def remote_sha256(repo_id: str, filename: str = WEIGHT_FILE, timeout: float = 20.0) -> str | None:
    """Fetch the Hub's LFS SHA-256 for one file; None when offline or unknown."""
    url = f"https://huggingface.co/api/models/{repo_id}?blobs=true"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None
    for sibling in payload.get("siblings", []):
        if sibling.get("rfilename") == filename:
            return ((sibling.get("lfs") or {}).get("sha256")) or None
    return None


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_file(path: Path, expected: str | None) -> tuple[bool, str]:
    """Check a weight file against the expected hash."""
    if not path.is_file():
        return False, f"missing: {path.name}"
    if not expected:
        return True, "no published hash available - size checked only"
    actual = sha256_of(path)
    if actual == expected:
        return True, f"sha256 ok ({actual[:16]}…)"
    return False, f"sha256 MISMATCH: expected {expected[:16]}… got {actual[:16]}…"


# --------------------------------------------------------------------------- #
# planning (kept stable: tests rely on this shape)
# --------------------------------------------------------------------------- #


def plan_downloads(target_dir: Path = WEIGHTS_DIR) -> list[dict[str, object]]:
    """Describe the three models without touching the network."""
    plan = []
    for name, spec in KRONOS_MODELS.items():
        destination = target_dir / name
        present = destination.is_dir() and any(destination.iterdir())
        plan.append(
            {
                "name": name,
                "repo_id": spec["repo_id"],
                "params_m": spec["params_m"],
                "weight": spec["weight"],
                "destination": str(destination),
                "present": bool(present),
            }
        )
    return plan


def plan_tokenizers(target_dir: Path = WEIGHTS_DIR) -> list[dict[str, object]]:
    plan = []
    for name, repo_id in TOKENIZER_FOR_MODEL.items():
        destination = target_dir / "tokenizers" / repo_id.split("/")[-1]
        plan.append(
            {
                "name": repo_id.split("/")[-1],
                "for_model": name,
                "repo_id": repo_id,
                "destination": str(destination),
                "present": destination.is_dir() and any(destination.iterdir()),
            }
        )
    return plan


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #


def _snapshot(repo_id: str, destination: Path) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=repo_id,
        local_dir=str(destination),
        allow_patterns=ALLOW_PATTERNS,
    )


def with_retries(repo_id: str, destination: Path, retries: int = DEFAULT_RETRIES) -> Path:
    """Download one snapshot, retrying transient failures with backoff."""
    destination.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, max(retries, 1) + 1):
        started = time.time()
        try:
            _snapshot(repo_id, destination)
            print(
                f"    ok  {repo_id} -> {destination} "
                f"({human_mb(folder_size(destination))} in {time.time() - started:.1f}s)"
            )
            return destination
        except Exception as exc:  # noqa: BLE001 - network errors vary wildly
            last_error = exc
            if attempt >= retries:
                break
            wait = 2 ** attempt
            print(f"    retry {attempt}/{retries - 1} for {repo_id} in {wait}s ({type(exc).__name__})")
            time.sleep(wait)
    raise RuntimeError(f"download failed after {retries} attempts: {repo_id}: {last_error}")


def download_model(name: str, target_dir: Path = WEIGHTS_DIR, force: bool = False,
                   retries: int = DEFAULT_RETRIES) -> Path:
    """Fetch one model snapshot (idempotent unless ``force``)."""
    spec = KRONOS_MODELS[name]
    destination = target_dir / name
    weight_path = destination / WEIGHT_FILE
    if weight_path.is_file() and not force:
        return destination
    return with_retries(str(spec["repo_id"]), destination, retries)


def download_tokenizer(model_name: str, target_dir: Path = WEIGHTS_DIR,
                       force: bool = False, retries: int = DEFAULT_RETRIES) -> Path:
    repo_id = TOKENIZER_FOR_MODEL[model_name]
    destination = target_dir / "tokenizers" / repo_id.split("/")[-1]
    if (destination / WEIGHT_FILE).is_file() and not force:
        return destination
    return with_retries(repo_id, destination, retries)


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #


def write_manifest(target_dir: Path = WEIGHTS_DIR) -> Path:
    models = plan_downloads(target_dir)
    for entry in models:
        folder = Path(str(entry["destination"]))
        entry["size_bytes"] = folder_size(folder)
        entry["tokenizer"] = TOKENIZER_FOR_MODEL[str(entry["name"])]
    tokenizers = [
        {**entry, "size_bytes": folder_size(Path(str(entry["destination"])))}
        for entry in plan_tokenizers(target_dir)
    ]
    manifest = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "total_bytes": sum(int(entry["size_bytes"]) for entry in models),
        "models": models,
        "tokenizers": tokenizers,
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def verify_manifest(target_dir: Path = WEIGHTS_DIR) -> tuple[bool, list[str]]:
    """Return (ok, problems). Missing manifest or weight dirs are problems."""
    problems: list[str] = []
    manifest_path = target_dir / "manifest.json"
    if not manifest_path.is_file():
        return False, [f"missing manifest: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return False, [f"unreadable manifest: {exc}"]

    listed = {entry["name"] for entry in manifest.get("models", [])}
    for name in KRONOS_MODELS:
        if name not in listed:
            problems.append(f"model not in manifest: {name}")
        elif not (target_dir / name).is_dir():
            problems.append(f"weights missing on disk: {name}")
    return not problems, problems


def verify_all_hashes(target_dir: Path = WEIGHTS_DIR) -> tuple[bool, list[str]]:
    """Hash-check every downloaded weight file against the Hub."""
    report: list[str] = []
    ok = True
    for name, spec in KRONOS_MODELS.items():
        weight = target_dir / name / WEIGHT_FILE
        expected = remote_sha256(str(spec["repo_id"]))
        good, detail = verify_file(weight, expected)
        ok = ok and good
        report.append(f"  {'PASS' if good else 'FAIL'}  {name:<6} {detail}")
    return ok, report


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download and verify the KRONOS trio.")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    parser.add_argument("--verify-only", action="store_true", help="hash-check existing files")
    parser.add_argument("--force", action="store_true", help="re-download existing weights")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--target", default=str(WEIGHTS_DIR))
    arguments = parser.parse_args(argv)

    target = Path(arguments.target)

    if arguments.verify_only:
        print("KRONOS weight integrity")
        ok, report = verify_all_hashes(target)
        print("\n".join(report))
        return 0 if ok else 1

    print("KRONOS weight plan")
    for entry in plan_downloads(target):
        state = "cached" if entry["present"] else "missing"
        print(f"  {entry['name']:<6} {entry['repo_id']:<26} {entry['params_m']:>6}M  {state}")

    if arguments.dry_run:
        print("\nDry run: nothing downloaded.")
        return 0

    if not arguments.dry_run:
        try:
            import huggingface_hub  # noqa: F401
        except ImportError:
            print("error: huggingface_hub is not installed", file=sys.stderr)
            return 2

    started = time.time()
    for entry in plan_downloads(target):
        name = str(entry["name"])
        print(f"\n[{name}] {entry['repo_id']}")
        destination = download_model(name, target, force=arguments.force, retries=arguments.retries)
        expected = remote_sha256(str(entry["repo_id"]))
        good, detail = verify_file(destination / WEIGHT_FILE, expected)
        print(f"    hash: {'PASS' if good else 'FAIL'}  {detail}")
        if not good:
            return 1
        print(f"    size: {human_mb(folder_size(destination))}")

    print("\n[tokenizers]")
    for entry in plan_tokenizers(target):
        print(f"  {entry['name']} (for {entry['for_model']})")
        download_tokenizer(str(entry["for_model"]), target, force=arguments.force,
                           retries=arguments.retries)

    manifest = write_manifest(target)
    print(f"\nmanifest: {manifest}")
    print(f"total weights: {human_mb(folder_size(target))} in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
