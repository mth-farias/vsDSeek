"""App identity, paths, and the pinned artifact set (single source)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[2]

VERSIONS_FILE = APP_ROOT / "versions.json"
ENV_FILE = APP_ROOT / ".env"


class Layout:
    """Paths for one vsDSeek tree (the real app root or a scratch root)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.versions_file = self.root / "versions.json"
        self.env_file = self.root / ".env"
        self.runtime = self.root / "runtime"
        self.python = self.runtime / "python"
        self.python_exe = self.python / "python.exe"
        self.pythonw_exe = self.python / "pythonw.exe"
        self.src = self.root / "src"
        self.state = self.root / "state"
        self.pid_file = self.state / "proxy.pid"
        self.log_file = self.state / "proxy.log"
        self.backups = self.state / "backups"
        self.cache = self.root / ".cache"

    def ensure_dirs(self) -> None:
        """Create the generated directories this app owns."""
        for path in (self.state, self.backups, self.cache):
            path.mkdir(parents=True, exist_ok=True)


def load_versions(path: Path | None = None) -> dict[str, Any]:
    """Return the parsed ``versions.json`` pin set.

    Args:
        path: Pin file to read; defaults to the app's own file.

    Returns:
        The full pin document (app identity, models, artifacts, resolvers).

    Raises:
        SystemExit: If the file is missing or is not a JSON object.
    """
    target = path or VERSIONS_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Missing pin file: {target}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Expected a JSON object in {target}")
    return data


def save_versions(data: dict[str, Any], path: Path | None = None) -> None:
    """Write the pin document back (used by the update resolver)."""
    target = path or VERSIONS_FILE
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    target.write_text(text, encoding="utf-8")


def read_env_key(layout: Layout) -> str:
    """Read ``DEEPSEEK_API_KEY`` from the app's ``.env`` (never logged).

    Raises:
        SystemExit: If the file or the key is missing.
    """
    try:
        lines = layout.env_file.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        raise SystemExit(f"Missing {layout.env_file} (fill in the key).")
    for raw in lines:
        line = raw.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            key = line.split("=", 1)[1].strip().strip("'\"")
            if key:
                return key
    raise SystemExit(
        f"No DEEPSEEK_API_KEY in {layout.env_file}; fill it in and retry."
    )


def pin_lines(path: Path | None = None) -> list[str]:
    """Return one summary line per pinned artifact (used by doctor)."""
    data = load_versions(path)
    artifacts = data.get("artifacts") or {}
    lines: list[str] = []
    for name in sorted(artifacts):
        row = artifacts[name]
        version = row.get("version", "?")
        digest = row.get("sha256") or row.get("id") or "n/a"
        hint = digest[:16] if isinstance(digest, str) else str(digest)
        lines.append(f"{name}: {version} ({hint})")
    app = data.get("app") or {}
    lines.append(f"proxy port: {app.get('proxy_port')}")
    return lines
