"""Ensure the local VS Code, the Claude Code extension, and the CLI.

Everything installed here is user-scoped (no admin): VS Code's official
user setup lands in ``%LOCALAPPDATA%\\Programs\\Microsoft VS Code``, the
extension goes into the normal ``~/.vscode/extensions``, and the CLI into
the npm global prefix under ``%APPDATA%\\npm``.

Versions come from ``versions.json``; ``update`` re-resolves the latest
ones and rewrites the pins.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from . import config
from .config import Layout
from .winutil import (
    powershell_hidden_argv,
    run_batch,
    run_hidden,
)

INSTALL_WAIT_S = 300
DOWNLOAD_RETRIES = 3
NPM_TIMEOUT_S = 300
EXT_TIMEOUT_S = 240
EXT_INSTALL_ATTEMPTS = 3
EXT_INSTALL_BACKOFF_S = 3.0
USER_AGENT = "vsDSeek-installer"


class InstallError(SystemExit):
    """A setup step failed with a user-facing message."""


def vscode_candidates() -> list[Path]:
    """Return the standard per-user VS Code install locations."""
    out: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(
            Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe"
        )
    program_files = os.environ.get("PROGRAMFILES")
    if program_files:
        out.append(Path(program_files) / "Microsoft VS Code" / "Code.exe")
    return out


def registry_install_dir() -> Path | None:
    """Return the InstallLocation VS Code's uninstall entry reports."""
    script = (
        "$roots = @("
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'"
        "); "
        "Get-ItemProperty $roots -ErrorAction SilentlyContinue | "
        "Where-Object { $_.DisplayName -like '*Visual Studio Code*' } | "
        "Select-Object -First 1 -ExpandProperty InstallLocation"
    )
    completed = run_hidden(powershell_hidden_argv(script))
    value = (completed.stdout or "").strip()
    return Path(value) if value else None


def vscode_exe() -> Path | None:
    """Return the installed ``Code.exe`` path, or None when absent."""
    for candidate in vscode_candidates():
        if candidate.exists():
            return candidate
    location = registry_install_dir()
    if location:
        candidate = location / "Code.exe"
        if candidate.exists():
            return candidate
    return None


def code_cmd(exe: Path) -> Path:
    """Return the ``bin\\code.cmd`` next to an installed ``Code.exe``."""
    return exe.parent / "bin" / "code.cmd"


def vscode_version(exe: Path) -> str | None:
    """Return the installed VS Code version string."""
    cmd = code_cmd(exe)
    if not cmd.exists():
        return None
    completed = run_batch([str(cmd), "--version"])
    first = (completed.stdout or "").strip().splitlines()
    return first[0].strip() if first else None


def resolve_vscode(pins: dict[str, Any], version: str = "latest"
                   ) -> dict[str, str]:
    """Resolve a VS Code installer download (url + sha256) from the API."""
    template = pins["resolvers"]["vscode_update"]
    url = template.format(version=version)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {
        "version": str(data.get("productVersion") or data.get("name")),
        "commit": str(data.get("version")),
        "url": str(data["url"]),
        "sha256": str(data["sha256hash"]),
    }


def resolve_extension_version(pins: dict[str, Any]) -> str | None:
    """Return the newest marketplace version of the pinned extension."""
    url = pins["resolvers"].get("vsix_gallery")
    if not url:
        return None
    ext_id = pins["artifacts"]["claude_code_extension"]["id"]
    payload = json.dumps(
        {
            "filters": [{"criteria": [{"filterType": 7, "value": ext_id}]}],
            "flags": 2151,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json;api-version=7.2-preview.1",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = data["results"][0]["extensions"][0]["versions"]
        return str(rows[0]["version"])
    except Exception:
        return None


def download(url: str, dest: Path, sha256: str, *, quiet: bool = False
             ) -> Path:
    """Download *url* to *dest*, retrying, and verify its SHA-256."""
    if dest.exists():
        if _sha256(dest) == sha256.lower():
            if not quiet:
                print(f"Using cached {dest.name}")
            return dest
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            if not quiet:
                print(f"Downloading {url}")
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(request, timeout=120) as resp:
                with dest.open("wb") as handle:
                    shutil.copyfileobj(resp, handle, 1024 * 1024)
            if _sha256(dest) != sha256.lower():
                raise InstallError(
                    f"SHA-256 mismatch for {dest.name}; download again."
                )
            return dest
        except InstallError:
            raise
        except Exception as exc:  # network hiccup: retry with backoff
            last_error = exc
            dest.unlink(missing_ok=True)
            time.sleep(2.0 * (attempt + 1))
    raise InstallError(f"Download failed: {url} ({last_error})")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_vscode(layout: Layout, *, quiet: bool = False) -> Path:
    """Return the installed VS Code exe, installing it when missing."""
    exe = vscode_exe()
    if exe:
        if not quiet:
            print(f"VS Code found: {exe} ({vscode_version(exe)})")
        return exe
    pins = config.load_versions(layout.versions_file)
    artifact = pins["artifacts"]["vscode"]
    url = artifact.get("download") or resolve_vscode(
        pins, artifact.get("version", "latest")
    )["url"]
    sha = artifact.get("sha256") or resolve_vscode(
        pins, artifact.get("version", "latest")
    )["sha256"]
    installer = download(
        url, layout.cache / Path(url).name, sha, quiet=quiet
    )
    print(f"Installing VS Code {artifact.get('version')} (silent, "
          "user-scoped) ...")
    run_hidden(
        [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/MERGETASKS=!runcode,addcontextmenufiles,"
            "addcontextmenufolders,associatewithfiles,addtopath",
        ],
        timeout=INSTALL_WAIT_S,
    )
    deadline = time.monotonic() + INSTALL_WAIT_S
    while time.monotonic() < deadline:
        exe = vscode_exe()
        if exe:
            print(f"VS Code installed: {exe} ({vscode_version(exe)})")
            return exe
        time.sleep(2)
    raise InstallError(
        "VS Code installer ran but Code.exe did not appear. Check the "
        "installer log in %TEMP% and retry."
    )


def extension_versions(code: Path, ext_id: str) -> str | None:
    """Return the installed version of *ext_id*, or None."""
    completed = run_batch(
        [str(code), "--list-extensions", "--show-versions"],
        timeout=EXT_TIMEOUT_S,
    )
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if line.lower().startswith(ext_id.lower() + "@"):
            return line.split("@", 1)[1]
    return None


def _install_extension(code: Path, spec: str) -> str:
    """Run one ``--install-extension`` call; return its combined output."""
    completed = run_batch(
        [str(code), "--install-extension", spec, "--force"],
        timeout=EXT_TIMEOUT_S,
    )
    return f"{completed.stdout or ''}\n{completed.stderr or ''}".strip()


def ensure_extension(layout: Layout, exe: Path, *, quiet: bool = False
                     ) -> str | None:
    """Install the pinned Claude Code extension; retry, then verify.

    The marketplace returns transient 503s; retrying the same pinned
    version avoids falling back to an older one by mistake.
    """
    pins = config.load_versions(layout.versions_file)
    artifact = pins["artifacts"]["claude_code_extension"]
    ext_id = artifact["id"]
    want = artifact["version"]
    code = code_cmd(exe)
    have = extension_versions(code, ext_id)
    if have == want:
        if not quiet:
            print(f"Extension {ext_id} {have} already installed.")
        return have
    print(f"Installing extension {ext_id}@{want} ...")
    output = ""
    for attempt in range(EXT_INSTALL_ATTEMPTS):
        output = _install_extension(code, f"{ext_id}@{want}")
        have = extension_versions(code, ext_id)
        if have == want:
            print(f"Extension {ext_id} {have} installed.")
            return have
        if attempt < EXT_INSTALL_ATTEMPTS - 1:
            time.sleep(EXT_INSTALL_BACKOFF_S * (attempt + 1))
    attempted_latest = False
    if have is None:
        # Nothing landed at all: try the marketplace latest as a fallback.
        attempted_latest = True
        output = _install_extension(code, ext_id)
        have = extension_versions(code, ext_id)
    if have is None:
        raise InstallError(f"Could not install {ext_id}. {output[:400]}")
    if have != want:
        if attempted_latest:
            print(
                f"Note: pinned {want} unavailable; {ext_id} {have} "
                "installed (marketplace latest). Run update to refresh "
                "the pins."
            )
        else:
            print(
                f"Note: pinned {want} could not be installed; {ext_id} "
                f"{have} is present. Run update to refresh the pins or "
                "retry install."
            )
    return have


def npm_cmd() -> Path | None:
    """Return a system npm.cmd, ignoring portable trees on PATH."""
    candidates: list[Path] = []
    for root in (os.environ.get("PROGRAMFILES"),
                 os.environ.get("PROGRAMFILES(X86)")):
        if root:
            candidates.append(Path(root) / "nodejs" / "npm.cmd")
    found = shutil.which("npm")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        text = str(candidate).replace("\\", "/").lower()
        if "DeepCC".lower() in text and "portable" in text:
            continue
        if candidate.exists():
            return candidate
    return None


def claude_cmd() -> Path | None:
    """Return the npm-global ``claude.cmd``, or None."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "npm" / "claude.cmd"
        if candidate.exists():
            return candidate
    return None


def cli_version() -> str | None:
    """Return the npm-global Claude Code CLI version, or None."""
    cmd = claude_cmd()
    if not cmd:
        return None
    completed = run_batch([str(cmd), "--version"])
    text = (completed.stdout or "").strip()
    return text.split()[0] if text else None


def ensure_cli(layout: Layout, *, quiet: bool = False) -> str | None:
    """Install/update the npm-global Claude Code CLI to the pin."""
    pins = config.load_versions(layout.versions_file)
    artifact = pins["artifacts"]["claude_code_cli"]
    package = artifact["id"]
    want = artifact["version"]
    have = cli_version()
    if have == want:
        if not quiet:
            print(f"Claude Code CLI {have} already installed.")
        return have
    npm = npm_cmd()
    if npm is None:
        print("Note: npm not found; skipping the terminal CLI (the VS Code "
              "extension works on its own).")
        return have
    print(f"Installing Claude Code CLI {package}@{want} (npm -g) ...")
    completed = run_batch(
        [str(npm), "install", "-g", f"{package}@{want}"],
        timeout=NPM_TIMEOUT_S,
    )
    have = cli_version()
    if have != want:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise InstallError(
            f"npm install of {package}@{want} did not take "
            f"(now: {have}). {detail[:400]}"
        )
    print(f"Claude Code CLI {have} installed.")
    return have


def ensure_runtime(layout: Layout, *, quiet: bool = False) -> Path:
    """Ensure the vendored CPython exists; fetch it when missing.

    The launchers cannot run without it, so a deleted ``runtime/`` is a
    fatal, unexplained failure for a non-developer — restore it instead.
    """
    if layout.python_exe.exists():
        return layout.python_exe
    pins = config.load_versions(layout.versions_file)
    artifact = pins["artifacts"]["python"]
    url = str(artifact.get("download") or "")
    sha = str(artifact.get("sha256") or "")
    if not url or not sha:
        raise InstallError(
            f"App runtime missing at {layout.python_exe} and versions.json "
            "has no python download pin to restore it from."
        )
    archive = download(url, layout.cache / Path(url).name, sha, quiet=quiet)
    print(f"Restoring the app runtime ({artifact.get('version')}) ...")
    layout.python.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(layout.python)
    _enable_site(layout.python)
    if not layout.python_exe.exists():
        raise InstallError(
            "Runtime archive extracted but python.exe is missing; "
            "delete runtime/ and retry."
        )
    print(f"App runtime restored: {layout.python_exe}")
    return layout.python_exe


def _enable_site(python_dir: Path) -> None:
    """Uncomment ``import site`` in the embeddable ``._pth`` (idempotent)."""
    for pth in sorted(python_dir.glob("python*._pth")):
        try:
            text = pth.read_text(encoding="utf-8")
        except OSError:
            continue
        fixed = text.replace("#import site", "import site")
        if fixed != text:
            pth.write_text(fixed, encoding="utf-8")


def ensure_all(layout: Layout, *, quiet: bool = False) -> dict[str, Any]:
    """Ensure the runtime, VS Code, extension, and CLI at the pins."""
    from . import autostart

    ensure_runtime(layout, quiet=quiet)
    autostart.enable(layout, quiet=quiet)
    exe = ensure_vscode(layout, quiet=quiet)
    extension = ensure_extension(layout, exe, quiet=quiet)
    cli = ensure_cli(layout, quiet=quiet)
    return {
        "vscode": str(exe),
        "vscode_version": vscode_version(exe),
        "extension": extension,
        "cli": cli,
    }


def update_pins(layout: Layout, *, quiet: bool = False,
                dry_run: bool = False) -> dict[str, Any]:
    """Re-resolve the latest versions and rewrite ``versions.json``."""
    pins = config.load_versions(layout.versions_file)
    before = json.dumps(pins, sort_keys=True)

    resolved = resolve_vscode(pins, "latest")
    pins["artifacts"]["vscode"].update(
        {
            "version": resolved["version"],
            "commit": resolved["commit"],
            "download": resolved["url"],
            "sha256": resolved["sha256"],
        }
    )

    registry = pins["resolvers"]["npm_registry"]
    request = urllib.request.Request(
        registry, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=60) as resp:
        npm_latest = str(json.loads(resp.read().decode("utf-8"))["version"])
    pins["artifacts"]["claude_code_cli"]["version"] = npm_latest

    # The extension ships on the marketplace on its own cadence; pin it
    # from the gallery, not from the CLI's npm version.
    extension_latest = resolve_extension_version(pins)
    if extension_latest:
        pins["artifacts"]["claude_code_extension"]["version"] = (
            extension_latest
        )
    elif not quiet:
        print("Note: marketplace extension version not resolved; "
              "keeping the current pin.")

    if json.dumps(pins, sort_keys=True) == before:
        print("Pins already current.")
        return pins
    if dry_run:
        print("[dry-run] would write pins:")
        for name in ("vscode", "claude_code_cli", "claude_code_extension"):
            print(f"  {name}: {pins['artifacts'][name]['version']}")
        return pins
    config.save_versions(pins, layout.versions_file)
    print("Pins updated:")
    for line in config.pin_lines(layout.versions_file):
        print(f"  {line}")
    return pins


def main(argv: list[str] | None = None) -> int:
    """Debug entry point: ``python -m vsdseek.installer install|update``."""
    import argparse

    parser = argparse.ArgumentParser(prog="vsdseek.installer")
    parser.add_argument("verb", choices=("install", "update"))
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    layout = Layout(args.root or config.APP_ROOT)
    if args.verb == "install":
        ensure_all(layout)
    else:
        update_pins(layout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
