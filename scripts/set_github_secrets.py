#!/usr/bin/env python3
"""Set GitHub Actions secrets over the REST API (libsodium sealed boxes).

    python scripts/set_github_secrets.py \
        --repo owner/repo \
        --secret FIREBASE_CREDENTIALS_JSON=state/firebase-key.json \
        --secret FIREBASE_DB_URL=https://example.firebaseio.com

Values are read from a file when the right-hand side is an existing path; the
value itself is never printed.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

import requests

API = "https://api.github.com"
REPO_ROOT = Path(__file__).resolve().parents[1]


def secret_payload(source: str) -> bytes:
    """Read a file-backed secret, otherwise treat the source as the value.

    A long access token is not a filename. ``Path.is_file()`` raises ENAMETOOLONG
    on Linux for values above the filesystem name limit, so the path probe must
    never decide whether a token is a literal value.
    """
    if not source:
        return b""
    try:
        candidate = Path(source)
        if candidate.is_file():
            return candidate.read_bytes()
    except (OSError, ValueError):
        pass
    return source.encode("utf-8")


def read_env(key: str) -> str:
    value = (os.environ.get(key) or "").strip()
    if value:
        return value
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload encrypted Actions secrets")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--secret", action="append", required=True, metavar="NAME=VALUE_OR_PATH")
    args = parser.parse_args(argv)

    try:
        from nacl import encoding, public
    except ImportError:
        raise SystemExit("PyNaCl missing: python -m pip install pynacl")

    token = read_env("SECRET_PAT")
    if not token:
        raise SystemExit("SECRET_PAT missing")

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kronos-secret-uploader",
        }
    )

    key_response = session.get(f"{API}/repos/{args.repo}/actions/secrets/public-key", timeout=30)
    if key_response.status_code != 200:
        raise SystemExit(f"cannot read repo public key: HTTP {key_response.status_code}")
    key_data = key_response.json()
    sealed = public.SealedBox(public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder()))

    failures = 0
    for item in args.secret:
        name, _, source = item.partition("=")
        payload = secret_payload(source)
        encrypted = base64.b64encode(sealed.encrypt(payload)).decode("ascii")
        response = session.put(
            f"{API}/repos/{args.repo}/actions/secrets/{name}",
            json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
            timeout=30,
        )
        ok = response.status_code in (201, 204)
        failures += 0 if ok else 1
        print(f"  {name:<28} {'created/updated' if ok else 'FAILED ' + str(response.status_code)}")

    listing = session.get(f"{API}/repos/{args.repo}/actions/secrets", timeout=30)
    if listing.status_code == 200:
        names = sorted(item["name"] for item in listing.json().get("secrets", []))
        print("secrets now present:", ", ".join(names) or "(none)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
