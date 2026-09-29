"""Start/stop/status for this app's passthrough, with strict ownership.

The prototype killed whatever held the port; this module only ever
signals a process whose command line names this app tree, and only after
``/health`` confirmed the owner is vsDSeek.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import config
from .config import Layout
from .winutil import nt_no_window_kwargs, process_command_line, run_hidden

APP_ID = "vsdseek"
HEALTH_WAIT_S = 20
HEALTH_POLL_S = 0.2


def proxy_port(layout: Layout) -> int:
    """Return the pinned loopback port."""
    pins = config.load_versions(layout.versions_file)
    return int(pins["app"]["proxy_port"])


def health(port: int, *, timeout: float = 1.0) -> dict[str, Any] | None:
    """Return the parsed ``/health`` body, or None when nothing answers."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def is_ours(body: dict[str, Any] | None, layout: Layout) -> bool:
    """True when *body* is this app running from this exact tree."""
    if not body or body.get("app") != APP_ID:
        return False
    return _norm(body.get("root")) == _norm(layout.root)


def _norm(value: Any) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").lower()


def parse_listening_pid(netstat_text: str, port: int) -> int | None:
    """Return the PID listening on ``127.0.0.1:<port>`` (Windows netstat)."""
    for line in netstat_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        if "LISTENING" not in line.upper():
            continue
        try:
            return int(parts[-1])
        except ValueError:
            return None
    return None


def listening_pid(port: int) -> int | None:
    """Return the PID listening on our loopback port, if any."""
    if os.name != "nt":
        return None
    completed = run_hidden(["netstat", "-ano", "-p", "tcp"])
    return parse_listening_pid(completed.stdout or "", port)


def read_pid_file(layout: Layout) -> int | None:
    """Return the recorded proxy PID, if the file parses."""
    try:
        return int(layout.pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_our_process(pid: int, layout: Layout) -> bool:
    """True when *pid*'s command line names this app tree and the proxy.

    Every accepted form keeps a path boundary, so a sibling tree like
    ``C:/x/app2`` can never match ``C:/x/app``: the absolute launcher
    (``<root>/app.py``), the module path (``<root>/src``), or a
    ``--root <root>`` argument (any launcher style).
    """
    if not pid:
        return False
    # Quotes are stripped so a quoted ``--root "C:/..."`` still matches.
    cmd = (
        process_command_line(pid)
        .replace("\\", "/")
        .replace('"', "")
        .lower()
    )
    if not cmd:
        return False
    root = _norm(layout.root)
    # The --root token must end at a space or the end of the line, so
    # "--root C:/x/app" never matches a sibling "--root C:/x/app2".
    root_flag = f"--root {root}"
    flagged = root_flag + " " in cmd or cmd.endswith(root_flag)
    return "proxy" in cmd and (
        f"{root}/app.py" in cmd
        or f"{root}/src" in cmd
        or flagged
    )


def _write_pid(layout: Layout, pid: int) -> None:
    layout.state.mkdir(parents=True, exist_ok=True)
    layout.pid_file.write_text(str(pid), encoding="utf-8")


def _unlink_pid(layout: Layout) -> None:
    try:
        layout.pid_file.unlink()
    except OSError:
        pass


def _tail_log(layout: Layout, lines: int = 8) -> str:
    try:
        text = layout.log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    rows = text.strip().splitlines()[-lines:]
    return "\n".join(rows)


def start(layout: Layout, *, quiet: bool = False) -> int:
    """Ensure the passthrough runs; return its PID.

    Raises:
        SystemExit: When another app owns the port or startup fails.
    """
    port = proxy_port(layout)
    body = health(port)
    if body:
        if is_ours(body, layout):
            pid = int(body.get("pid") or 0)
            if pid:
                _write_pid(layout, pid)
            if not quiet:
                print(f"Passthrough already running (pid {pid}).")
            return pid
        owner = body.get("app", "unknown app")
        raise SystemExit(
            f"Port {port} is already used by '{owner}' "
            f"(root {body.get('root')}). Stop that app or change "
            "app.proxy_port in versions.json."
        )
    if not layout.pythonw_exe.exists():
        raise SystemExit(
            f"App runtime missing at {layout.pythonw_exe}; run "
            "'app.py install' to restore it."
        )
    layout.state.mkdir(parents=True, exist_ok=True)
    stderr_file = (layout.state / "proxy.stderr.log").open("ab")
    kwargs: dict[str, Any] = {
        "cwd": str(layout.root),
        "stdout": subprocess.DEVNULL,
        "stderr": stderr_file,
        "stdin": subprocess.DEVNULL,
    }
    kwargs = nt_no_window_kwargs(kwargs)
    proc = subprocess.Popen(
        [
            str(layout.pythonw_exe),
            str(layout.root / "app.py"),
            "proxy",
            "--root",
            str(layout.root),
        ],
        **kwargs,
    )
    deadline = time.monotonic() + HEALTH_WAIT_S
    while time.monotonic() < deadline:
        body = health(port)
        if body and is_ours(body, layout):
            pid = int(body.get("pid") or proc.pid)
            _write_pid(layout, pid)
            if not quiet:
                print(f"Passthrough started (pid {pid}).")
            return pid
        if proc.poll() is not None:
            break
        time.sleep(HEALTH_POLL_S)
    tail = _tail_log(layout)
    raise SystemExit(
        f"Passthrough did not become healthy on port {port} within "
        f"{HEALTH_WAIT_S}s."
        + (f"\nLast log lines:\n{tail}" if tail else "")
    )


def stop(layout: Layout, *, quiet: bool = False) -> int:
    """Stop the passthrough owned by this tree; never touches others."""
    port = proxy_port(layout)
    candidates: set[int] = set()
    file_pid = read_pid_file(layout)
    if file_pid and is_our_process(file_pid, layout):
        candidates.add(file_pid)
    listener = listening_pid(port)
    if listener and is_our_process(listener, layout):
        candidates.add(listener)
    killed = True
    for pid in candidates:
        completed = run_hidden(["taskkill", "/PID", str(pid), "/F"])
        if completed.returncode != 0:
            killed = False
    time.sleep(0.3)
    body = health(port)
    if body and is_ours(body, layout):
        if not killed:
            raise SystemExit(
                f"Could not stop pid(s) {sorted(candidates)} on port {port} "
                f"(taskkill failed). Close it from Task Manager, then "
                "retry."
            )
        _unlink_pid(layout)
        raise SystemExit(
            f"Passthrough still answers on {port} after stop; retry or "
            "reboot."
        )
    _unlink_pid(layout)
    if body:
        if not quiet:
            print(
                f"Note: port {port} still answers but belongs to "
                f"'{body.get('app')}' — not stopped."
            )
        return 0
    if not quiet:
        print("Passthrough stopped.")
    return 0


def status(layout: Layout) -> dict[str, Any]:
    """Return a small state dict for ``status``/``doctor`` output."""
    port = proxy_port(layout)
    body = health(port)
    listener = listening_pid(port)
    return {
        "port": port,
        "health": body,
        "ours": is_ours(body, layout),
        "listener_pid": listener,
        "pid_file": read_pid_file(layout),
        "pid_file_verified": bool(
            read_pid_file(layout)
            and is_our_process(read_pid_file(layout) or 0, layout)
        ),
    }


def main(argv: list[str] | None = None) -> int:
    """Debug entry point: ``python -m vsdseek.proxyctl status|start|stop``."""
    import argparse

    parser = argparse.ArgumentParser(prog="vsdseek.proxyctl")
    parser.add_argument("verb", choices=("start", "stop", "status"))
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    layout = Layout(args.root or config.APP_ROOT)
    if args.verb == "start":
        start(layout)
    elif args.verb == "stop":
        stop(layout)
    else:
        print(json.dumps(status(layout), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
