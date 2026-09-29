# CLAUDE.md

Project guidance for Claude Code in this repo. Fill in the sections below as the
project takes shape — this file starts intentionally generic. Keep it under 200
lines, and keep **procedures in skills, not here**.

Global defaults — the reply contract, the asking policy, the standing rules, the
skill list, and the vocabulary — live in `~/.claude/CLAUDE.md` and apply here
automatically. Do not copy them into this file; that is how the two drift apart.

## What this project is

<!-- one or two sentences: what it does, who it's for -->

## Working conventions

- Plan non-trivial work before executing — `tg-confirm-align`, then `tg-plan`,
  then `tg-plan-improve`. After execute, `tg-review-plan`; before closing,
  `tg-review-session`.
- The quality bar is `CONSTRAINTS.md` at the repo root. Name what a change
  endangers in the plan, and check in review that the bar was not weakened to
  reach green.
- Code and prose follow Google's guides. Where a formatter disagrees on layout,
  the formatter wins. The details load by file type from `~/.claude/rules/`.
- Anything odd or project-specific — a required command, a forbidden path, a
  convention that is not obvious — belongs in this file.

## Workspace (`.claude/workspace/`)

Session files for this repo. The layout is documented once, in
`~/.claude/CLAUDE.md`, and applies to every repo — do not copy the table here;
that is how the two drift apart. What matters locally: `.claude/` is gitignored,
while `CLAUDE.md` and `CONSTRAINTS.md` are tracked.

Start a cold session with `tg-onboard`; close with `tg-handoff`.
