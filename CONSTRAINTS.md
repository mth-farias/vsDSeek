# CONSTRAINTS.md

The quality bar for this repo. **Tracked in git on purpose** — it is part of the
repo's contract, not local session state. Keep it short enough to read.

Three skills read this file: `tg-plan` names which constraints a change puts at
risk; `tg-review-plan` / `tg-review-last` / `tg-review-session` check that a diff
did not *lower* the bar to reach green.

## Gates

The commands that must pass. Detect the repo's **real** ones — do not paste a
template list.

| Gate | Command |
|---|---|
| tests | `runtime\python\python.exe test.py` — 85 offline tests, no network |
| lint | `ruff check .` — optional; ruff is not installed by default (see `STYLE.md`) |

No type checker and no build step. The suite is standard library only, and the
launchers are `.bat` / `.vbs`.

## Non-negotiables

Things a change must not weaken. Delete the rows that don't apply — an empty
section is better than a padded one.

- **No new suppressions.** No `@ts-ignore`, `@ts-expect-error`, `eslint-disable`,
  `# type: ignore`, `# noqa`, `--no-verify` added to silence an existing failure.
  If a suppression is genuinely correct, say why in the diff, don't just add it.
- **No test removal or skipping** to reach green. `pytest.skip`, `.skip(`,
  `xit(`, `@unittest.skip`, deleting or commenting out a test body — all count.
- **No loosened assertions.** No widened tolerances, relaxed thresholds, or
  `assert True`-shaped edits.
- **No swallowed errors.** No bare `except: pass` / empty `catch {}`.
- **No secrets in the repo.** No keys, tokens, or `.env` content committed.

## Ownership

Who is allowed to change the bar, and how.

- Only the user may *lower* a gate. The agent may propose it in the plan; it may
  never do it silently mid-execution.
- A change that needs a lower bar is a **scope change**, not an implementation
  detail. Stop and ask.

## Change log

Append when a gate or non-negotiable changes, and why. A bar that moves silently
is worse than no bar — this is the record that makes weakening visible.

| Date | Change | Why |
|---|---|---|
| 2026-09-29 | Created | Scaffolded with `tg-setup`; gates captured from `README.md`. |
