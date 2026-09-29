"""Command-line entry point for vsDSeek (local VS Code route).

Verbs:
    install   ensure local VS Code + Claude Code extension + CLI
    update    re-resolve the latest pinned versions, then install
    apply     route Claude Code to DeepSeek (default) or back to Anthropic
    restore   shortcut for ``apply anthropic``
    start     start only the passthrough
    stop      stop only the passthrough
    status    one screen: provider, proxy, versions, paths
    doctor    full health report
    proxy     run the passthrough in the foreground (used internally)
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from . import (
    __version__,
    autostart,
    config,
    doctor,
    installer,
    proxyctl,
    switcher,
)
from .config import Layout

VERBS = (
    "install", "update", "apply", "restore", "start", "stop", "status",
    "doctor", "autostart", "proxy",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vsdseek",
        description="Local VS Code + Claude Code wired to DeepSeek.",
    )
    parser.add_argument(
        "--version", action="version", version=f"vsdseek {__version__}"
    )
    sub = parser.add_subparsers(dest="verb", metavar="VERB")
    for verb in VERBS:
        child = sub.add_parser(verb)
        child.add_argument(
            "--root", type=Path, default=None,
            help="app tree (default: the real app root)",
        )
        child.add_argument("--quiet", action="store_true")
        child.add_argument(
            "--dry-run", action="store_true",
            help="show what would change without writing",
        )
        if verb == "autostart":
            child.add_argument(
                "action", nargs="?", choices=("enable", "disable", "status"),
                default="status",
            )
        if verb == "apply":
            child.add_argument(
                "provider", nargs="?", choices=("deepseek", "anthropic"),
                default="deepseek",
            )
            child.add_argument(
                "--no-install", action="store_true",
                help="skip the VS Code/extension check",
            )
    return parser


def _vscode_running(exe: Path) -> bool:
    """True when a Code.exe from the installed tree is running."""
    if os.name != "nt":
        return False
    from .winutil import powershell_hidden_argv, run_hidden

    root = str(exe.parent).replace("'", "''")
    script = (
        "@(Get-Process Code -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Path -like '{root}\\*' }}).Count"
    )
    completed = run_hidden(powershell_hidden_argv(script))
    try:
        return int((completed.stdout or "0").strip() or "0") > 0
    except ValueError:
        return False


def _after_switch(layout: Layout, changed: bool) -> None:
    """Tell the user what to do — and nothing when nothing changed.

    Reloading VS Code restarts the Claude Code extension host, which
    closes the panel's open conversations; so a no-op apply must stay a
    no-op and must not ask for a reload.
    """
    if not changed:
        print("\nAlready active - settings unchanged, so nothing needs "
              "reloading. Your open conversations stay as they are.")
        return
    exe = installer.vscode_exe()
    if exe is None:
        return
    if _vscode_running(exe):
        print("\nVS Code is running: Command Palette -> Developer: "
              "Reload Window to pick the new settings up. Past "
              "conversations stay on disk (Claude Code: Reopen Closed "
              "Session).")
        return
    print("\nOpening VS Code ...")
    from .winutil import clean_gui_env

    subprocess.Popen(
        [str(exe)], env=clean_gui_env(), close_fds=True
    )


def _apply(
    layout: Layout, provider: str, *, dry_run: bool, install: bool,
    quiet: bool,
) -> int:
    if provider == "deepseek":
        if install and not dry_run:
            installer.ensure_all(layout, quiet=quiet)
        if not dry_run:
            # Start the proxy before touching settings: if the port is
            # taken or it never becomes healthy, Claude Code is left on
            # its previous (working) configuration.
            proxyctl.start(layout, quiet=quiet)
        changed = switcher.apply_deepseek(layout, dry_run=dry_run)
        if not dry_run:
            _after_switch(layout, changed)
        return 0
    changed = switcher.apply_anthropic(layout, dry_run=dry_run)
    if not dry_run:
        proxyctl.stop(layout, quiet=quiet)
        autostart.disable(quiet=quiet)
        exe = installer.vscode_exe()
        if changed and exe and _vscode_running(exe):
            print("Reload VS Code to drop the DeepSeek routing.")
    return 0


def _status(layout: Layout) -> int:
    exe = installer.vscode_exe()
    claude_file = switcher.claude_settings_path()
    state = proxyctl.status(layout)
    print(f"vsDSeek {__version__} ({layout.root})")
    print(f"Provider : {switcher.provider_of(claude_file)}")
    print(f"Settings : {claude_file}")
    print(f"VS Code  : {exe} ({installer.vscode_version(exe) if exe else '-'})")
    print(f"Port     : {state['port']} "
          f"({'ours' if state['ours'] else 'not ours'})")
    print(f"Pin file : {layout.versions_file}")
    for line in config.pin_lines(layout.versions_file):
        print(f"  {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = _build_parser()
    args, rest = parser.parse_known_args(argv)
    if not args.verb:
        parser.print_help()
        return 0
    layout = config.Layout(args.root or config.APP_ROOT)
    layout.ensure_dirs()
    if args.verb == "proxy":
        from .passthrough import main as proxy_main

        return proxy_main(rest)
    if args.verb == "install":
        if args.dry_run:
            for line in config.pin_lines(layout.versions_file):
                print(f"would ensure: {line}")
            return 0
        installer.ensure_all(layout, quiet=args.quiet)
        return 0
    if args.verb == "update":
        installer.update_pins(
            layout, quiet=args.quiet, dry_run=args.dry_run
        )
        if args.dry_run:
            return 0
        installer.ensure_all(layout, quiet=args.quiet)
        return 0
    if args.verb == "apply":
        return _apply(
            layout, args.provider, dry_run=args.dry_run,
            install=not args.no_install, quiet=args.quiet,
        )
    if args.verb == "restore":
        return _apply(
            layout, "anthropic", dry_run=args.dry_run, install=False,
            quiet=args.quiet,
        )
    if args.verb == "start":
        proxyctl.start(layout, quiet=args.quiet)
        return 0
    if args.verb == "stop":
        proxyctl.stop(layout, quiet=args.quiet)
        return 0
    if args.verb == "status":
        return _status(layout)
    if args.verb == "autostart":
        if args.action == "enable":
            autostart.enable(layout)
        elif args.action == "disable":
            autostart.disable()
        else:
            state = autostart.status(layout)
            print(f"Autostart: {'enabled' if state['enabled'] else 'disabled'}"
                  f" ({state['path']})")
        return 0
    if args.verb == "doctor":
        return doctor.run(layout)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
