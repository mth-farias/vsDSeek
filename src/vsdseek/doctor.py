"""Read-only health checks for the whole local wiring."""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import (
    __version__,
    autostart,
    config,
    installer,
    jsonc,
    proxyctl,
    switcher,
)
from .config import Layout


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Report:
    """Collects PASS / WARN / FAIL lines and renders a summary."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, label: str, detail: str = "") -> None:
        """Record one check result."""
        self.rows.append((status, label, detail))

    def render(self) -> int:
        """Print the report; return the exit code (1 when a FAIL exists)."""
        for status, label, detail in self.rows:
            suffix = f" - {detail}" if detail else ""
            print(f"{status} {label}{suffix}")
        fails = sum(1 for status, _, _ in self.rows if status == "FAIL")
        warns = sum(1 for status, _, _ in self.rows if status == "WARN")
        if fails:
            print(f"\n{fails} check(s) failed, {warns} warning(s).")
            return 1
        if warns:
            print(f"\nALL GREEN with {warns} warning(s).")
            return 0
        print("\nALL GREEN")
        return 0


def run(layout: Layout) -> int:
    """Run every check and print the report."""
    pins = config.load_versions(layout.versions_file)
    report = Report()

    if layout.python_exe.exists():
        report.add("PASS", "App runtime", f"{layout.python_exe}")
    else:
        report.add(
            "FAIL", "App runtime",
            f"missing {layout.python_exe}; re-copy the app",
        )

    exe = installer.vscode_exe()
    pin_vscode = pins["artifacts"]["vscode"]["version"]
    if exe:
        have = installer.vscode_version(exe)
        status = "PASS" if have == pin_vscode else "WARN"
        report.add(status, "VS Code", f"{have} at {exe} (pin {pin_vscode})")
    else:
        report.add("FAIL", "VS Code", "not installed; run install")

    pin_ext = pins["artifacts"]["claude_code_extension"]
    if exe:
        have = installer.extension_versions(
            installer.code_cmd(exe), pin_ext["id"]
        )
        if have is None:
            report.add(
                "FAIL", "Claude Code extension", "not installed; run install"
            )
        else:
            status = "PASS" if have == pin_ext["version"] else "WARN"
            report.add(
                status, "Claude Code extension",
                f"{have} (pin {pin_ext['version']})",
            )
    else:
        report.add("FAIL", "Claude Code extension", "VS Code missing")

    pin_cli = pins["artifacts"]["claude_code_cli"]["version"]
    have_cli = installer.cli_version()
    if have_cli is None:
        report.add(
            "WARN", "Claude Code CLI",
            "not installed (extension still works)",
        )
    else:
        status = "PASS" if have_cli == pin_cli else "WARN"
        report.add(status, "Claude Code CLI", f"{have_cli} (pin {pin_cli})")

    try:
        key = config.read_env_key(layout)
        report.add("PASS", "DeepSeek key", "present in .env")
    except SystemExit as exc:
        key = ""
        report.add("FAIL", "DeepSeek key", str(exc))

    claude_file = switcher.claude_settings_path()
    provider = switcher.provider_of(claude_file)
    if provider == "deepseek":
        report.add("PASS", "Provider", f"DeepSeek ({claude_file})")
    elif provider == "anthropic":
        report.add(
            "WARN", "Provider",
            f"Anthropic/unset — click VsDSeek.bat to switch ({claude_file})",
        )
    else:
        report.add("WARN", "Provider", f"{provider} ({claude_file})")

    if key and provider == "deepseek":
        try:
            data = jsonc.load_jsonc(claude_file)
            stored = str((data.get("env") or {}).get(
                "ANTHROPIC_AUTH_TOKEN") or "")
            if stored and _digest(stored) == _digest(key):
                report.add("PASS", "Settings key", "matches .env")
            else:
                report.add(
                    "FAIL", "Settings key",
                    "stale; click VsDSeek.bat to rewrite",
                )
        except SystemExit:
            pass

    state = proxyctl.status(layout)
    port = state["port"]
    if state["ours"]:
        info = state["health"] or {}
        running = str(info.get("version") or "")
        if running and running != __version__:
            report.add(
                "WARN", "Passthrough",
                f"running v{running}, app is v{__version__}; "
                "code changed since it started — run 'stop' then 'start'",
            )
        else:
            report.add(
                "PASS", "Passthrough",
                f"http://127.0.0.1:{port} pid {info.get('pid')} "
                f"(v{info.get('version')})",
            )
    elif state["health"]:
        report.add(
            "FAIL", "Passthrough",
            f"port {port} owned by '{state['health'].get('app')}'",
        )
    elif provider == "deepseek":
        report.add(
            "FAIL", "Passthrough",
            f"not running on {port}; click VsDSeek.bat",
        )
    else:
        report.add("PASS", "Passthrough", f"stopped (port {port} free)")

    if state["pid_file"] and not state["pid_file_verified"]:
        report.add(
            "WARN", "PID file",
            f"{state['pid_file']} does not match a live process",
        )

    try:
        auto = autostart.status(layout)
    except SystemExit as exc:
        auto = {"enabled": False, "path": str(exc), "target_ok": False}
    if auto["enabled"] and auto["target_ok"]:
        report.add("PASS", "Autostart", f"log-in task ({auto['path']})")
    elif auto["enabled"]:
        report.add(
            "WARN", "Autostart",
            "task exists but points elsewhere; click VsDSeek.bat",
        )
    else:
        report.add(
            "WARN", "Autostart",
            "off - you must click VsDSeek.bat after each reboot",
        )

    return report.render()


def main(argv: list[str] | None = None) -> int:
    """Debug entry point: ``python -m vsdseek.doctor``."""
    import argparse

    parser = argparse.ArgumentParser(prog="vsdseek.doctor")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    return run(Layout(args.root or config.APP_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
