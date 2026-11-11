"""Launch the Streamlit cockpit.

Run from anywhere; the script resolves the repo root and hands Streamlit the
absolute path so the working directory does not matter.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "dashboard" / "live_dashboard.py"

sys.path.insert(0, str(REPO_ROOT))

from config.settings import get_settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the KRONOS dashboard")
    parser.add_argument("--port", type=int, default=get_settings().dashboard_port)
    parser.add_argument("--headless", action="store_true", help="do not open a browser")
    arguments = parser.parse_args(argv)

    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("streamlit is not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        return 2

    if not DASHBOARD.is_file():
        print(f"dashboard not found: {DASHBOARD}", file=sys.stderr)
        return 2

    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(DASHBOARD),
        "--server.port",
        str(arguments.port),
    ]
    if arguments.headless:
        command += ["--server.headless", "true"]

    print(f"starting dashboard on http://localhost:{arguments.port}")
    return subprocess.call(command, cwd=str(REPO_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
