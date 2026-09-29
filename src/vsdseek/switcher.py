"""Write/undo the DeepSeek routing in Claude Code and VS Code settings.

Two files carry the switch:

- ``%USERPROFILE%\\.claude\\settings.json`` — the CLI (and the panel's
  spawned CLI) read the ``env`` block; this is the authoritative place.
- ``%APPDATA%\\Code\\User\\settings.json`` — ``claudeCode.environment``
  variables for the extension host, kept in parity.

Both are JSONC and hand-edited, so writes go through ``jsonc`` (comments
and unrelated keys survive; a timestamped backup lands in ``state/``).
The API key is written, never printed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import config, jsonc
from .config import Layout

MODEL_CAPABILITIES = "effort,max_effort,thinking,interleaved_thinking"
THINKING_KILL_FLAGS = (
    "CLAUDE_CODE_DISABLE_THINKING",
    "DISABLE_INTERLEAVED_THINKING",
)
ARTIFACT_DENY = (
    "Artifact",
    "ArtifactComments",
    "ArtifactData",
    "ArtifactCheck",
)
UNSET_ON_RESTORE = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)
MANAGED_ENV = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_DESCRIPTION",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_SUPPORTED_CAPABILITIES",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "DISABLE_TELEMETRY",
    "DISABLE_ERROR_REPORTING",
)
DROPPED_ON_DEEPSEEK = (
    "Remote Control, voice, cloud/mobile/Slack sessions",
    "Routines /schedule, Ultrareview, Chrome extension, computer use",
    "Artifacts (denied locally; upstream regex bug)",
    "Fast mode, Advisor, Channels, analytics, server-managed settings",
    "Anthropic cache_control (DeepSeek disk cache instead)",
    "Vision on Pro (Flash has vision)",
)


def home() -> Path:
    """Return the user profile directory."""
    return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


def claude_home() -> Path:
    """Return the Claude Code home (``~/.claude``)."""
    override = (os.environ.get("VSDSEEK_CLAUDE_HOME") or "").strip()
    if override:
        return Path(override)
    return home() / ".claude"


def claude_settings_path() -> Path:
    """Return Claude Code's user ``settings.json``."""
    return claude_home() / "settings.json"


def vscode_user_dir() -> Path:
    """Return VS Code's user data directory (``%APPDATA%\\Code\\User``)."""
    override = (os.environ.get("VSDSEEK_VSCODE_USER") or "").strip()
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("APPDATA is not set; cannot find VS Code settings.")
    return Path(appdata) / "Code" / "User"


def vscode_settings_path() -> Path:
    """Return VS Code's user ``settings.json``."""
    return vscode_user_dir() / "settings.json"


def _pick_family(pins: dict[str, Any], family: str, tier: str) -> str:
    picker = pins["models"]["picker"]
    for model_id, target in picker.items():
        if target == tier and family in model_id:
            return model_id
    return next(iter(picker))


def deepseek_env(pins: dict[str, Any], key: str) -> dict[str, str]:
    """Return the full managed env block for the DeepSeek provider."""
    port = int(pins["app"]["proxy_port"])
    display = pins["models"].get("display", {})
    opus = _pick_family(pins, "opus", "pro")
    sonnet = _pick_family(pins, "sonnet", "flash")
    haiku = _pick_family(pins, "haiku", "flash")
    fable = _pick_family(pins, "fable", "pro")
    env = {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": key,
        # Alias, not the concrete id: the model picker keeps a row for the
        # active model, and an alias lands on the curated "Flash" row.
        "ANTHROPIC_MODEL": "sonnet",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": opus,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": sonnet,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": fable,
        "CLAUDE_CODE_SUBAGENT_MODEL": pins["models"]["subagent"],
        # Owner's default for every new session: Flash at low effort.
        "CLAUDE_CODE_EFFORT_LEVEL": "low",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    }
    names = {"opus": opus, "sonnet": sonnet, "haiku": haiku, "fable": fable}
    tiers = {"opus": "Pro", "fable": "Pro", "sonnet": "Flash",
             "haiku": "Flash"}
    for family, model_id in names.items():
        upper = family.upper()
        env[f"ANTHROPIC_DEFAULT_{upper}_MODEL_NAME"] = display.get(
            model_id, model_id
        )
        env[f"ANTHROPIC_DEFAULT_{upper}_MODEL_DESCRIPTION"] = (
            f"Claude {family.title()} family, rewritten to "
            f"DeepSeek V4 {tiers[family]} on the wire."
        )
        env[f"ANTHROPIC_DEFAULT_{upper}_MODEL_SUPPORTED_CAPABILITIES"] = (
            MODEL_CAPABILITIES
        )
    return env


def model_picker(pins: dict[str, Any]) -> dict[str, Any]:
    """Curate the /model picker: two rows, named for the wire models.

    Claude Code's built-in lineup is a four-family ladder (Opus, Fable,
    Sonnet, Haiku); ``modelPicker`` replaces it outright with rows we
    label. The rows use family aliases so the existing
    ``ANTHROPIC_DEFAULT_*_MODEL`` mapping (and the proxy) stay the single
    source of ids.
    """
    return {
        "replaceBuiltInOptions": True,
        "options": [
            {
                "model": "opus",
                "label": "Pro",
                "description": (
                    f"{pins['models']['wire']['pro']} - strongest, "
                    "text only (no images)"
                ),
            },
            {
                "model": "sonnet",
                "label": "Flash",
                "description": (
                    f"{pins['models']['wire']['flash']} - faster, "
                    "handles images"
                ),
            },
        ],
    }


def merge_claude_env(
    settings: dict[str, Any],
    incoming: dict[str, str] | None,
    *,
    restore: bool = False,
) -> dict[str, Any]:
    """Replace managed env keys; on restore, blank the provider trio."""
    env = settings.get("env")
    env = dict(env) if isinstance(env, dict) else {}
    for name in MANAGED_ENV:
        env.pop(name, None)
    if incoming:
        for name in THINKING_KILL_FLAGS:
            env.pop(name, None)
        env.update(incoming)
    elif restore:
        for name in UNSET_ON_RESTORE:
            env[name] = ""
    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)
    return settings


def merge_artifact_deny(
    settings: dict[str, Any], deny_artifacts: bool
) -> dict[str, Any]:
    """Drop Artifact tools on DeepSeek (upstream regex bug in CC 2.1.x)."""
    perms = settings.get("permissions")
    perms = dict(perms) if isinstance(perms, dict) else {}
    deny = perms.get("deny")
    deny = list(deny) if isinstance(deny, list) else []
    deny = [item for item in deny if item not in ARTIFACT_DENY]
    if deny_artifacts:
        deny.extend(ARTIFACT_DENY)
    if deny:
        perms["deny"] = deny
    else:
        perms.pop("deny", None)
    if perms:
        settings["permissions"] = perms
    else:
        settings.pop("permissions", None)
    return settings


def merge_vscode(
    settings: dict[str, Any],
    incoming: dict[str, str] | None,
    *,
    disable_login: bool,
) -> dict[str, Any]:
    """Replace managed ``claudeCode.environmentVariables`` entries."""
    raw = settings.get("claudeCode.environmentVariables")
    kept: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name in MANAGED_ENV:
                continue
            if incoming and name in THINKING_KILL_FLAGS:
                continue
            if "name" in item and "value" in item:
                kept.append(
                    {"name": str(item["name"]), "value": str(item["value"])}
                )
    if incoming:
        for name, value in incoming.items():
            kept.append({"name": name, "value": value})
    settings["claudeCode.environmentVariables"] = kept
    settings["claudeCode.disableLoginPrompt"] = disable_login
    return settings


BACKUP_KEEP = 20


def _prune_backups(layout: Layout, label: str) -> None:
    """Keep only the newest ``BACKUP_KEEP`` backups for *label*."""
    pattern = f"{label}.*.bak"
    rows = sorted(layout.backups.glob(pattern))
    for stale in rows[:-BACKUP_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass


def write_settings(
    path: Path, data: dict[str, Any], layout: Layout, *, label: str,
    dry_run: bool,
) -> bool:
    """Write one JSONC settings file, keeping comments; back up first.

    A file whose content would not change is left untouched (no write, no
    backup, no mtime bump): editors and extensions watch these files, and
    a pointless rewrite is what makes Claude Code spawn a fresh session.

    Args:
        path: Settings file to write.
        data: Full desired top-level object.
        layout: App tree (backups land in ``state/backups``).
        label: Backup prefix — both settings files are named
            ``settings.json``, so the label disambiguates them.
        dry_run: Print the intent instead of writing.

    Returns:
        True when the file was (or, in dry-run, would be) rewritten.
    """
    if dry_run:
        print(f"[dry-run] would write {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if path.exists():
        # newline="" everywhere: without it, Windows text mode turns \n
        # into \r\n and every write would look "changed" on the next run.
        raw = path.read_text(encoding="utf-8-sig", newline="")
        if raw.strip():
            try:
                synced = jsonc.sync_jsonc_top_level(raw, data)
                if not synced.endswith("\n"):
                    synced += "\n"
                json.loads(jsonc.strip_jsonc(synced))
                text = synced
            except (ValueError, json.JSONDecodeError):
                print(f"Note: comments in {path} could not be kept.")
        if text.encode("utf-8") == path.read_bytes():
            print(f"Unchanged {path}")
            return False
        layout.backups.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = layout.backups / f"{label}.{stamp}.bak"
        backup.write_bytes(path.read_bytes())
        _prune_backups(layout, label)
    # Atomic replace: a crash mid-write must never leave a truncated
    # settings file (VS Code and Claude Code both parse it at startup).
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)
    print(f"Wrote {path}")
    return True


def provider_of(settings_path: Path) -> str:
    """Classify the configured provider: deepseek / anthropic / other."""
    try:
        data = jsonc.load_jsonc(settings_path)
    except SystemExit:
        return "unreadable"
    env = data.get("env")
    base = ""
    if isinstance(env, dict):
        base = str(env.get("ANTHROPIC_BASE_URL") or "")
    if not base:
        return "anthropic"
    if "127.0.0.1" in base or "localhost" in base:
        return "deepseek"
    return "other"


def apply_deepseek(
    layout: Layout,
    *,
    dry_run: bool = False,
    claude_path: Path | None = None,
    vscode_path: Path | None = None,
) -> bool:
    """Point Claude Code (CLI + panel) at the local DeepSeek passthrough."""
    pins = config.load_versions(layout.versions_file)
    key = config.read_env_key(layout)
    incoming = deepseek_env(pins, key)
    claude_file = claude_path or claude_settings_path()
    vscode_file = vscode_path or vscode_settings_path()

    claude_settings = jsonc.load_jsonc(claude_file)
    merge_claude_env(claude_settings, incoming)
    merge_artifact_deny(claude_settings, True)
    claude_settings["modelPicker"] = model_picker(pins)
    # No client-side cap: "max" is DeepSeek's real top tier and stays
    # selectable; the proxy maps the ladder per the docs' table.
    claude_settings["maxEffortLevel"] = "max"
    changed = write_settings(claude_file, claude_settings, layout,
                             label="claude-settings", dry_run=dry_run)

    vscode_settings = jsonc.load_jsonc(vscode_file)
    merge_vscode(vscode_settings, incoming, disable_login=True)
    changed = write_settings(vscode_file, vscode_settings, layout,
                             label="vscode-settings", dry_run=dry_run) or changed

    port = pins["app"]["proxy_port"]
    print(f"Provider: DeepSeek via http://127.0.0.1:{port}")
    print(
        f"  Opus/Fable picker -> {pins['models']['wire']['pro']} | "
        f"Sonnet/Haiku -> {pins['models']['wire']['flash']}"
    )
    print("Not available while DeepSeek is selected:")
    for line in DROPPED_ON_DEEPSEEK:
        print(f"  - {line}")
    return changed


def apply_anthropic(
    layout: Layout,
    *,
    dry_run: bool = False,
    claude_path: Path | None = None,
    vscode_path: Path | None = None,
) -> bool:
    """Remove the DeepSeek routing; Claude Code prompts to log in again."""
    claude_file = claude_path or claude_settings_path()
    vscode_file = vscode_path or vscode_settings_path()

    claude_settings = jsonc.load_jsonc(claude_file)
    merge_claude_env(claude_settings, None, restore=True)
    merge_artifact_deny(claude_settings, False)
    claude_settings.pop("modelPicker", None)
    claude_settings.pop("maxEffortLevel", None)
    changed = write_settings(claude_file, claude_settings, layout,
                             label="claude-settings", dry_run=dry_run)

    vscode_settings = jsonc.load_jsonc(vscode_file)
    merge_vscode(vscode_settings, None, disable_login=False)
    changed = write_settings(vscode_file, vscode_settings, layout,
                             label="vscode-settings", dry_run=dry_run) or changed

    print("Provider: Anthropic (claude.ai login / subscription).")
    print("Claude Code will ask to log in again on next start.")
    return changed
