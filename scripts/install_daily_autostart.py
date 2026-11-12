#!/usr/bin/env python3
"""Register the idempotent Windows desk startup task.

The task starts ``dashboard/server.py`` at login and every weekday at 08:25
IST.  The dashboard owns the in-process AutoIgnitionSupervisor, so starting the
dashboard is enough to mint today's token and spawn/restart the live session.
The task is deliberately per-user and does not require a paid runner.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "KRONOS_Autonomous_Desk"
REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "dashboard" / "server.py"
STARTUP_VBS = "KRONOS_Autonomous_Desk.vbs"


def _powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def install() -> int:
    if sys.platform != "win32":
        print("This installer supports Windows Task Scheduler only.")
        return 2
    if not SERVER.is_file():
        print(f"missing dashboard server: {SERVER}")
        return 2
    python = Path(sys.executable)
    # Prefer pythonw so the login trigger does not flash a console window.
    pythonw = python.with_name("pythonw.exe")
    exe = pythonw if pythonw.is_file() else python
    argument = f'-u "{SERVER}" --host 127.0.0.1 --port 8080'
    repo = str(REPO_ROOT)
    script = rf"""
$ErrorActionPreference = 'Stop'
$name = '{TASK_NAME}'
$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing) {{
  Unregister-ScheduledTask -TaskName $name -Confirm:$false
}}
$action = New-ScheduledTaskAction -Execute '{exe}' -Argument '{argument}' -WorkingDirectory '{repo}'
$logon = New-ScheduledTaskTrigger -AtLogOn
$morning = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:25
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 10) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger @($logon,$morning) -Settings $settings -Principal $principal -Description 'KRONOS zero-touch desk: dashboard + token + live session auto-ignition' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $name
[pscustomobject]@{{TaskName=$task.TaskName;State=[string]$task.State;Execute=$task.Actions.Execute;Arguments=$task.Actions.Arguments;Triggers=($task.Triggers | ForEach-Object {{ $_.CimClass.CimClassName }}) -join ','}} | ConvertTo-Json -Compress
"""
    completed = _powershell(script)
    if completed.returncode != 0:
        print("Task registration failed.")
        print((completed.stdout or "").strip())
        print((completed.stderr or "").strip())
        return completed.returncode or 1
    print((completed.stdout or "").strip())
    print(json.dumps({
        "task_name": TASK_NAME,
        "repo": repo,
        "server": str(SERVER),
        "executable": str(exe),
        "triggers": ["AtLogOn", "Weekly Mon-Fri 08:25"],
    }, indent=1))
    return 0


def _startup_dir() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        raise RuntimeError("APPDATA is not set")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_startup_guardian() -> int:
    """No-admin login fallback when Task Scheduler registration is denied.

    A per-user Startup-folder VBS is enough to wake the dashboard at login; the
    dashboard's in-process supervisor then owns the token/feed/session loop.
    The elevated SCHEDULED TASK remains available for the 08:25 clock trigger.
    """
    if sys.platform != "win32":
        print("Login startup is supported on Windows only.")
        return 2
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    exe = pythonw if pythonw.is_file() else python
    target = _startup_dir() / STARTUP_VBS
    target.parent.mkdir(parents=True, exist_ok=True)
    command = f'"{exe}" -u "{SERVER}" --host 127.0.0.1 --port 8080'
    vbs = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{REPO_ROOT}"\r\n'
        f'sh.Run "{command.replace(chr(34), chr(34) * 2)}", 0, False\r\n'
    )
    target.write_text(vbs, encoding="utf-8")
    print(json.dumps({
        "startup_guardian": str(target),
        "command": command,
        "triggers": ["Windows login (no admin required)"],
        "scheduled_task": "requires elevation; run without --startup-only to register",
    }, indent=1))
    return 0


def uninstall() -> int:
    if sys.platform != "win32":
        return 2
    try:
        (_startup_dir() / STARTUP_VBS).unlink(missing_ok=True)
        print(f"removed login guardian {_startup_dir() / STARTUP_VBS}")
    except OSError as exc:
        print(f"startup guardian removal: {exc}")
    completed = _powershell(
        f"Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue | "
        "Unregister-ScheduledTask -Confirm:$false"
    )
    if completed.returncode != 0:
        print((completed.stderr or completed.stdout or "").strip())
    else:
        print(f"removed {TASK_NAME}")
    return completed.returncode or 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the KRONOS weekday auto-start task")
    parser.add_argument("--uninstall", action="store_true", help="remove startup entries")
    parser.add_argument("--startup-only", action="store_true", help="install the no-admin login guardian")
    args = parser.parse_args(argv)
    if args.uninstall:
        return uninstall()
    if args.startup_only:
        return install_startup_guardian()
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
