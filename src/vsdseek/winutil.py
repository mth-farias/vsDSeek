"""Windows helpers: hidden subprocess flags and process queries.

Everything the app spawns must stay invisible: the launcher is clicked
from the desktop, so no console windows may flash while the proxy starts
or npm runs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


_GUI_DROP_EXACT = {
    "ELECTRON_RUN_AS_NODE",
    "VSCODE_CLI_DATA_DIR",
    "VSCODE_PORTABLE",
    "CLAUDE_CONFIG_DIR",
}
_GUI_DROP_PREFIXES = (
    "ANTHROPIC_",
    "DEEPSEEK_",
    "VSDSEEK_",
    "DEEPCC_",
    "CLAUDE_CODE_",
)


def clean_gui_env() -> dict[str, str]:
    """Child env for a VS Code GUI launch.

    Strips session/CLI leakage — in particular ``ELECTRON_RUN_AS_NODE``,
    which (inherited from an editor's extension host) makes Code.exe run
    as plain Node and exit without a window.
    """
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _GUI_DROP_EXACT:
            continue
        if key.startswith(_GUI_DROP_PREFIXES):
            continue
        out[key] = value
    return out


def nt_no_window_kwargs(
    kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return *kwargs* plus CREATE_NO_WINDOW / SW_HIDE on Windows."""
    out: dict[str, Any] = dict(kwargs or {})
    if os.name != "nt":
        return out
    out["creationflags"] = int(
        out.get("creationflags", 0) or 0
    ) | subprocess.CREATE_NO_WINDOW
    if out.get("startupinfo") is None:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        out["startupinfo"] = info
    return out


def powershell_hidden_argv(command: str) -> list[str]:
    """Return a powershell.exe argv that runs *command* without a window.

    Output is forced to UTF-8: the console codepage would mangle non-ASCII
    install paths, and those strings feed the proxy-ownership check.
    """
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-Command",
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command,
    ]


def run_hidden(
    argv: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run a console helper without ever flashing a window."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return subprocess.run(argv, **nt_no_window_kwargs(kwargs))


def run_batch(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a ``.cmd``/``.bat`` tool through ``cmd /c`` (batch is not exec-able)."""
    return run_hidden(["cmd", "/c", *argv], **kwargs)


def startup_dir() -> Path:
    """Return the per-user Startup folder (login autostart)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("APPDATA is not set; cannot find Startup folder.")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def process_command_line(pid: int) -> str:
    """Return a process command line via CIM (empty string when gone)."""
    completed = run_hidden(
        powershell_hidden_argv(
            "(Get-CimInstance Win32_Process -Filter "
            f"'ProcessId={int(pid)}').CommandLine"
        )
    )
    return (completed.stdout or "").strip()
