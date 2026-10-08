# vsDSeek — Claude Code on DeepSeek (local VS Code)

Click `VsDSeek.bat`. From then on, VS Code's Claude Code panel and the
`claude` command talk to **DeepSeek** instead of Anthropic, using only two
wire models:

| Picker row | Wire model |
|---|---|
| **Pro** | `deepseek-v4-pro` (text only, no images) |
| **Flash** | `deepseek-flash` (handles images) |

Subagents ride Flash. The picker is curated via `modelPicker` (with
`replaceBuiltInOptions`) in `~/.claude/settings.json`. Every new session
starts on **Flash at Low effort**; raising the slider (or picking Pro)
lasts for that session only.

Effort: DeepSeek's real tiers are low/high/max. This app maps
`minimal/low→low`, `medium→high`, and `high/xhigh/ultra/max→max` (the
owner's policy: High and Max both run at full effort). No client-side
cap — `maxEffortLevel: max` keeps the top tier selectable.

`VsDSeek-restore.bat` puts everything back on Anthropic (claude.ai login).

## What it installs (user-scoped, no admin)

- **Visual Studio Code 1.138.0** — official user setup, silent, into
  `%LOCALAPPDATA%\Programs\Microsoft VS Code` (added to PATH).
- **Claude Code extension** — the pinned/available version, into your
  normal `~/.vscode/extensions`.
- **Claude Code CLI** — the pinned version, npm global (`%APPDATA%\npm`).
- **The passthrough** — a small local proxy on `127.0.0.1:18766` that
  rewrites Claude-shaped requests into DeepSeek's two models.

The DeepSeek API key lives in `.env` (this folder), is copied into
`~/.claude/settings.json` + VS Code user settings by the switcher, and —
because those files are backed up before every write — also appears in
the app's `state/backups/` copies (gitignored, newest 20 kept). It is
never logged and never leaves the machine except to `api.deepseek.com`.

## Daily use

| Action | Do this |
|---|---|
| Switch to DeepSeek | `VsDSeek.bat` |
| Back to Claude (claude.ai) | `VsDSeek-restore.bat` |
| Check everything | `VsDSeek-doctor.bat` |
| Update pins to latest | `VsDSeek-update.bat` |

After switching, reload VS Code (`Ctrl+Shift+P` → *Developer: Reload
Window*) if it was already open; then click the Claude icon in the
sidebar. **After a reboot there is nothing to do** — the passthrough
starts itself at log-in through a Scheduled Task (`vsDSeek proxy`,
created by `install`; Windows restarts it automatically up to 3 times if
it ever dies). Turn it off with `app.py autostart disable`. Clicking
`VsDSeek.bat` when DeepSeek is already active is a no-op: settings are
left byte-identical, so nothing reloads.

### About your conversations (history)

Claude Code keeps every conversation as a transcript under
`%USERPROFILE%\.claude\projects\<your folder>\` — nothing here deletes
them. Reloading the VS Code window (or restarting VS Code) *closes* the
panel's open conversations; it does not destroy them. To bring one back:
Command Palette → **Claude Code: Reopen Closed Session** (or run
`claude --resume` in a terminal). Because a no-op `VsDSeek.bat` click no
longer rewrites settings, your open conversations stay put through normal
daily use.

## Where things live

- Settings written by the switcher:
  `%USERPROFILE%\.claude\settings.json` and
  `%APPDATA%\Code\User\settings.json` (backups in `state/backups/`,
  labeled `claude-settings.*` / `vscode-settings.*`).
- Proxy log (one redacted line per request): `state/proxy.log`.
- Pins (versions + hashes): `versions.json`. App runtime: `runtime/python`
  (restored automatically by `VsDSeek.bat` if it is ever deleted).

## First run

1. Double-click `VsDSeek.bat`. The first run downloads and installs VS
   Code (~1–2 min), the extension, the CLI, writes the settings, starts
   the passthrough, and opens VS Code.
2. In VS Code, click the Claude icon (sidebar) and start a session.

## Uninstall

1. `VsDSeek-restore.bat` — removes the DeepSeek routing from both
   settings files (`state/backups/` keeps restorable copies), stops the
   passthrough, and removes the log-in task (or run
   `app.py autostart disable` on its own).
2. Optional removals: `code --uninstall-extension anthropic.claude-code`
   and `npm uninstall -g @anthropic-ai/claude-code` (extension and CLI),
   then uninstall VS Code from Windows *Apps → Installed apps*.
3. Delete this folder — nothing else writes outside it except the settings
   above and the app's `.cache`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Panel says "not logged in" / talks to Anthropic | Run `VsDSeek.bat` again, then reload the VS Code window. |
| `VsDSeek-doctor.bat` shows FAIL Passthrough | Click `VsDSeek.bat` (it starts the proxy); port 18766 must be free. |
| Doctor warns "running vX, app is vY" | You updated the code; run `app.py stop` then `app.py start` (or click `VsDSeek.bat`) to reload the passthrough. |
| New model id causes an error mid-session | The proxy refuses unknown ids on purpose (DeepSeek silently downgrades). Pick a model from the picker. |
| Images fail with a policy message | Send images with Flash; V4 Pro has no vision. |
| DeepSeek org limits the key | Fix quotas at platform.deepseek.com, then retry. |

## Not available while DeepSeek is selected

Remote Control / voice / cloud sessions, Artifacts, Fast mode, Advisor,
Anthropic server-side caching (`cache_control` is stripped; DeepSeek has
its own disk cache), claude.ai connectors, and vision on V4 Pro.

## Tests

```
runtime\python\python.exe test.py
```

81 offline tests (no network, no live installs). See `STYLE.md` and
`NOTICE`.
