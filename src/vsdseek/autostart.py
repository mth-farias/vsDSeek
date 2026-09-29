"""Log-in autostart for the passthrough (so nobody has to click a bat).

Implemented as a per-user **Scheduled Task** with an *At logon* trigger:
Windows may delay or skip Startup-folder shortcuts (observed live: a boot
at 11:33 left the proxy unstarted until 11:35), while a logon task runs
immediately. The task also carries restart-on-failure and no execution
time limit, so a crashed or long-lived proxy is handled by Windows.

An earlier build used a Startup-folder shortcut; ``enable`` and
``disable`` remove that legacy link when they see it.
"""

from __future__ import annotations

from pathlib import Path

from .config import Layout
from .winutil import (
    powershell_hidden_argv,
    run_hidden,
    startup_dir,
)

TASK_NAME = "vsDSeek proxy"
LEGACY_SHORTCUT = "vsDSeek proxy.lnk"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def legacy_shortcut() -> Path | None:
    """Return the old Startup-folder shortcut path, when it exists."""
    try:
        path = startup_dir() / LEGACY_SHORTCUT
    except SystemExit:
        return None
    return path if path.exists() else None


def _remove_legacy() -> None:
    path = legacy_shortcut()
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def _query() -> dict[str, str]:
    """Return the task's LIST fields; empty dict when it does not exist."""
    completed = run_hidden(
        ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST", "/v"]
    )
    if completed.returncode != 0:
        return {}
    fields: dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()
    return fields


def status(layout: Layout) -> dict[str, object]:
    """Return ``{enabled, path, target_ok, running}`` for the task."""
    fields = _query()
    enabled = bool(fields)
    target = fields.get("task to run", "").lower()
    target_ok = bool(
        enabled
        and str(layout.pythonw_exe).lower() in target
        and str(layout.root / "app.py").lower() in target
    )
    return {
        "enabled": enabled,
        "path": TASK_NAME,
        "target_ok": target_ok,
        "running": fields.get("status", "").lower() == "running",
    }


def enable(layout: Layout, *, quiet: bool = False) -> bool:
    """Register/refresh the logon task; True when it is in place."""
    if not layout.pythonw_exe.exists():
        if not quiet:
            print("Note: app runtime missing; skipping autostart setup.")
        return False
    arguments = (
        f'"{layout.root / "app.py"}" proxy --root "{layout.root}" --quiet'
    )
    script = (
        "$action = New-ScheduledTaskAction "
        f"-Execute {_ps_quote(str(layout.pythonw_exe))} "
        f"-Argument {_ps_quote(arguments)} "
        f"-WorkingDirectory {_ps_quote(str(layout.root))}; "
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
        "$settings = New-ScheduledTaskSettingsSet "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} "
        "-Action $action -Trigger $trigger -Settings $settings -Force "
        "| Out-Null"
    )
    completed = run_hidden(powershell_hidden_argv(script))
    _remove_legacy()
    state = status(layout)
    ok = bool(state["enabled"] and state["target_ok"])
    if not quiet:
        print(
            f"Log-in autostart {'enabled' if ok else 'FAILED'} "
            f"(scheduled task '{TASK_NAME}')"
        )
    if not ok and completed.returncode != 0 and not quiet:
        detail = (completed.stderr or completed.stdout or "").strip()
        print(f"  {detail[:300]}")
    return ok


def disable(*, quiet: bool = False) -> bool:
    """Remove the logon task (and any legacy shortcut)."""
    run_hidden(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    _remove_legacy()
    gone = not _query()
    if not quiet and gone:
        print("Log-in autostart disabled.")
    return gone
