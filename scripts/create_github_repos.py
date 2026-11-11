#!/usr/bin/env python3
"""Bootstrap the two KRONOS GitHub repositories and link the local git remotes.

Creates, idempotently, through the GitHub REST API:

  * kronos-trading-brain  (private) - core trading brain & quantitative models
  * kronos-runner-public  (public)  - GitHub Actions runner workspace & APK builder

Then initialises local git, makes the initial commit, links ``origin`` and pushes.

Credentials come from the environment, a local ``.env`` file, or a hidden prompt:

  GITHUB_USERNAME  GitHub account that will own the repositories
  SECRET_PAT       Personal Access Token with ``repo`` and ``workflow`` scopes

Usage:
    python scripts/create_github_repos.py [--dry-run]

Safety notes:
  * The token is never written to disk, never committed, and never stored in
    ``.git/config``: pushes authenticate through a per-command HTTP header.
  * Repositories that already exist are detected (HTTP 422) and reused instead
    of aborting the run.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import subprocess
import sys
from pathlib import Path

import requests

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = REPO_ROOT / "kronos-runner-public"

PRIVATE_SPEC = {
    "name": "kronos-trading-brain",
    "private": True,
    "description": "Private core trading brain & quantitative models for KRONOS Nifty50 bot",
}
PUBLIC_SPEC = {
    "name": "kronos-runner-public",
    "private": False,
    "description": "Public runner workspace for 16GB GitHub Actions workflows & mobile APK builder",
}

INITIAL_COMMIT_MESSAGE = "Initial production commit: KRONOS 102.3M Quant Engine"
PUBLIC_COMMIT_MESSAGE = "Initial public commit: KRONOS runner workspace & APK builder workflow"


class BootstrapError(RuntimeError):
    """A recoverable, user-facing bootstrap failure."""


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def load_dotenv(path: Path) -> None:
    """Populate os.environ from a ``KEY=VALUE`` file without overriding real env vars."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def read_credentials() -> tuple[str, str]:
    load_dotenv(REPO_ROOT / ".env")

    username = (os.environ.get("GITHUB_USERNAME") or "").strip()
    token = (os.environ.get("SECRET_PAT") or "").strip()
    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

    if not username:
        if not interactive:
            raise BootstrapError(
                "GITHUB_USERNAME is not set. Export it or add it to .env before running."
            )
        username = input("GitHub username: ").strip()

    if not token:
        if not interactive:
            raise BootstrapError(
                "SECRET_PAT is not set. Export it or add it to .env before running."
            )
        token = getpass.getpass("GitHub Personal Access Token (input hidden): ").strip()

    if not username or not token:
        raise BootstrapError("Both GITHUB_USERNAME and SECRET_PAT are required.")
    return username, token


# --------------------------------------------------------------------------- #
# GitHub REST API
# --------------------------------------------------------------------------- #


def api_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "kronos-repo-bootstrap",
        }
    )
    return session


def _api_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or "no response body"
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])[:300]
    return str(payload)[:300]


def manual_url(owner: str, spec: dict) -> str:
    private = "true" if spec["private"] else "false"
    return f"https://github.com/new?name={spec['name']}&private={private}&owner={owner}"


def ensure_repo(
    session: requests.Session, owner: str, spec: dict, dry_run: bool = False
) -> tuple[dict, str]:
    """Create the repository or reuse it if it already exists.

    Returns ``(repo_json, status)`` where status is ``created``, ``exists`` or ``planned``.
    """
    name = spec["name"]
    if dry_run:
        print(f"  [dry-run] would create {owner}/{name} (private={spec['private']})")
        return (
            {
                "name": name,
                "private": spec["private"],
                "html_url": f"https://github.com/{owner}/{name}",
            },
            "planned",
        )

    payload = {
        "name": name,
        "description": spec["description"],
        "private": spec["private"],
        "has_issues": True,
        "has_wiki": False,
        "auto_init": False,
    }

    try:
        response = session.post(f"{API_ROOT}/user/repos", json=payload, timeout=30)
    except requests.RequestException as exc:
        raise BootstrapError(f"Could not reach api.github.com: {exc}") from exc

    if response.status_code == 201:
        print(f"  ✔ created {owner}/{name} (private={spec['private']})")
        return response.json(), "created"

    if response.status_code == 422:
        # Most common cause: the repository already exists. Reuse it.
        try:
            existing = session.get(f"{API_ROOT}/repos/{owner}/{name}", timeout=30)
        except requests.RequestException as exc:
            raise BootstrapError(f"Could not reach api.github.com: {exc}") from exc
        if existing.status_code == 200:
            print(f"  ℹ {owner}/{name} already exists — reusing it")
            return existing.json(), "exists"
        raise BootstrapError(
            f"GitHub rejected {owner}/{name} (422: {_api_message(response)}). "
            f"The name may be taken by another account or blocked by policy."
        )

    if response.status_code == 401:
        raise BootstrapError(
            "GitHub rejected SECRET_PAT (401). The token is invalid, expired or revoked.\n"
            "  Create a new classic token with the `repo` and `workflow` scopes at\n"
            "  https://github.com/settings/tokens\n"
            f"  Manual fallback: {manual_url(owner, spec)}"
        )

    if response.status_code == 403:
        raise BootstrapError(
            f"GitHub refused the request (403: {_api_message(response)}).\n"
            "  The token most likely lacks the `repo` or `workflow` scope, or is\n"
            "  restricted by SSO / organisation policy. Fix the scopes at\n"
            "  https://github.com/settings/tokens\n"
            f"  Manual fallback: {manual_url(owner, spec)}"
        )

    raise BootstrapError(
        f"Unexpected GitHub API status {response.status_code} while creating "
        f"{owner}/{name}: {_api_message(response)}"
    )


def remote_commit_sha(
    session: requests.Session, owner: str, name: str, branch: str = "main"
) -> str | None:
    """Return the SHA of ``branch`` on GitHub, or None when it does not exist yet."""
    response = session.get(
        f"{API_ROOT}/repos/{owner}/{name}/commits/{branch}", timeout=30
    )
    if response.status_code == 200:
        return response.json().get("sha")
    return None


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #


def run_git(
    args: list[str],
    cwd: Path,
    extra_header: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run git in ``cwd``, redacting any credential header from error output."""
    command = ["git", "-C", str(cwd)]
    display = ["git", "-C", str(cwd)]
    if extra_header:
        command += ["-c", f"http.extraheader=Authorization: Basic {extra_header}"]
        display += ["-c", "http.extraheader=Authorization: Basic ***"]
    command += args
    display += args

    environment = dict(os.environ)
    environment.setdefault("GIT_TERMINAL_PROMPT", "0")
    environment.setdefault("GCM_INTERACTIVE", "Never")

    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BootstrapError(f"`{' '.join(display)}` failed:\n{detail}")
    return result


def init_repo(path: Path) -> bool:
    """Initialise a git repository on branch ``main``. Returns True if newly created."""
    path.mkdir(parents=True, exist_ok=True)
    if (path / ".git").exists():
        return False
    result = run_git(["init", "-b", "main"], path, check=False)
    if result.returncode != 0:  # git < 2.28 has no -b flag
        run_git(["init"], path)
        run_git(["symbolic-ref", "HEAD", "refs/heads/main"], path)
    return True


def ensure_identity(path: Path, username: str) -> None:
    """Fall back to a repo-local identity when no global git identity is configured."""
    if not run_git(["config", "user.name"], path, check=False).stdout.strip():
        run_git(["config", "user.name", username], path)
        print(f"  • set repo-local user.name to {username}")
    if not run_git(["config", "user.email"], path, check=False).stdout.strip():
        run_git(["config", "user.email", f"{username}@users.noreply.github.com"], path)
        print(f"  • set repo-local user.email to {username}@users.noreply.github.com")


def commit_all(path: Path, message: str) -> bool:
    """Stage everything and commit. Returns False when there was nothing to commit."""
    run_git(["add", "-A"], path)
    staged = run_git(["diff", "--cached", "--name-only"], path).stdout.strip()
    if not staged:
        print(f"  • nothing new to commit in {path.name or '.'}")
        return False
    run_git(["commit", "-m", message], path)
    print(f"  ✔ committed: {message}")
    return True


def ensure_remote(path: Path, url: str) -> None:
    remotes = run_git(["remote"], path).stdout.split()
    if "origin" in remotes:
        current = run_git(["remote", "get-url", "origin"], path).stdout.strip()
        if current != url:
            run_git(["remote", "set-url", "origin", url], path)
            print(f"  • origin re-pointed to {url}")
    else:
        run_git(["remote", "add", "origin", url], path)
        print(f"  • origin added: {url}")


def push_branch(path: Path, branch: str, token: str) -> None:
    """Push with a short-lived Authorization header so the token never hits disk."""
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    result = run_git(
        ["push", "-u", "origin", branch], path, extra_header=header, check=False
    )
    if result.returncode == 0:
        print(f"  ✔ pushed {branch} → origin")
        return

    detail = (result.stderr or result.stdout or "").strip()
    lowered = detail.lower()
    if "workflow" in lowered:
        raise BootstrapError(
            "Push rejected: SECRET_PAT cannot write .github/workflows files.\n"
            "  Add the `workflow` scope to the token (or grant it in a fine-grained\n"
            "  token's Workflows permission) and re-run.\n"
            f"  git said:\n{detail}"
        )
    if "rejected" in lowered or "non-fast-forward" in lowered or "fetch first" in lowered:
        raise BootstrapError(
            f"Push to {path.name or '.'} was rejected because the remote already has\n"
            f"  commits. Nothing was overwritten. Reconcile manually:\n"
            f"    git -C {path} pull --rebase origin {branch}\n"
            f"  git said:\n{detail}"
        )
    raise BootstrapError(f"Push from {path} failed:\n{detail}")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def bootstrap_private(
    repo_dir: Path,
    username: str,
    token: str,
    session: requests.Session,
    dry_run: bool,
) -> str | None:
    print("\n[1/2] Local git – private core repository (kronos-trading-brain)")
    if init_repo(repo_dir):
        print("  • initialised git repository on branch main")
    ensure_identity(repo_dir, username)

    if dry_run:
        print("  [dry-run] would stage all files and commit:")
        print(f"            {INITIAL_COMMIT_MESSAGE!r}")
        print(f"  [dry-run] would push to https://github.com/{username}/{PRIVATE_SPEC['name']}.git")
        return None

    commit_all(repo_dir, INITIAL_COMMIT_MESSAGE)
    url = f"https://github.com/{username}/{PRIVATE_SPEC['name']}.git"
    ensure_remote(repo_dir, url)
    push_branch(repo_dir, "main", token)
    return remote_commit_sha(session, username, PRIVATE_SPEC["name"])


def bootstrap_public(
    public_dir: Path,
    username: str,
    token: str,
    session: requests.Session,
    dry_run: bool,
) -> str | None:
    print("\n[2/2] Local git – public runner workspace (kronos-runner-public)")
    if not public_dir.is_dir():
        raise BootstrapError(f"Expected the public runner workspace at {public_dir}")

    if init_repo(public_dir):
        print("  • initialised nested git repository on branch main")
    ensure_identity(public_dir, username)

    if dry_run:
        print("  [dry-run] would stage the public files, commit and push to")
        print(f"            https://github.com/{username}/{PUBLIC_SPEC['name']}.git")
        return None

    commit_all(public_dir, PUBLIC_COMMIT_MESSAGE)
    url = f"https://github.com/{username}/{PUBLIC_SPEC['name']}.git"
    ensure_remote(public_dir, url)
    push_branch(public_dir, "main", token)
    return remote_commit_sha(session, username, PUBLIC_SPEC["name"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the KRONOS GitHub repositories and link local remotes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be created and pushed without changing anything",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    username, token = read_credentials()
    print("KRONOS repository bootstrap")
    print(f"  account : {username}")
    print(f"  token   : {'*' * 8} (from {'environment' if os.environ.get('SECRET_PAT') else 'prompt'})")
    if args.dry_run:
        print("  mode    : DRY RUN — nothing will be created or pushed")

    session = api_session(token)

    print("\n[API] Ensuring repositories exist via POST /user/repos")
    private_repo, private_status = ensure_repo(session, username, PRIVATE_SPEC, args.dry_run)
    public_repo, public_status = ensure_repo(session, username, PUBLIC_SPEC, args.dry_run)

    private_sha = bootstrap_private(REPO_ROOT, username, token, session, args.dry_run)
    public_sha = bootstrap_public(PUBLIC_DIR, username, token, session, args.dry_run)

    print("\n" + "=" * 74)
    if args.dry_run:
        print("DRY RUN complete — re-run without --dry-run to apply.")
        return 0

    print("వివరాలు / Details")
    rows = [
        (PRIVATE_SPEC["name"], "private", private_repo.get("private"), private_status,
         private_repo.get("html_url", ""), private_sha),
        (PUBLIC_SPEC["name"], "public", public_repo.get("private"), public_status,
         public_repo.get("html_url", ""), public_sha),
    ]
    for name, expected_visibility, is_private, status, url, sha in rows:
        if is_private is not None:
            actual = "private" if is_private else "public"
            visibility = f"{expected_visibility} ✔" if actual == expected_visibility else f"{actual} ✖"
        else:
            visibility = expected_visibility
        head = f"main @ {sha[:7]}" if sha else "main: NOT verified ✖"
        print(f"  • {name:<22} {visibility:<12} {status:<10} {head}")
        print(f"    {url}")

    ok = bool(private_sha and public_sha)
    if ok:
        print("\n✅ రెండు రిపోజిటరీలు విజయవంతంగా సృష్టించబడ్డాయి మరియు లింక్ చేయబడ్డాయి.")
        print("✅ Both repositories are created, linked and pushed on GitHub.")
        print("\nNext: from now on just use `git push` — a credential helper or")
        print("      `gh auth login` is recommended for interactive pushes.")
    else:
        print("\n⚠️  Repositories exist, but the pushed branch could not be verified.")
        return 1

    print("\nSecurity: this token was typed into a chat. Revoke it at")
    print("          https://github.com/settings/tokens and issue a fresh one.")
    return 0


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        raise SystemExit(main())
    except BootstrapError as error:
        print(f"\n❌ బూట్స్ట్రాప్ విఫలమైంది / Bootstrap failed:\n{error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        raise SystemExit(130)
