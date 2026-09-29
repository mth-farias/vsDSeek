# Style

This project follows **Google** language guides. Agents and participants
match them when adding code or comments.

## Python

- Guide: https://google.github.io/styleguide/pyguide.html
- Indent 4 spaces; line length 80; UTF-8
- Docstrings: Google style (module, public functions, public classes)
- Names: `snake_case` functions and variables, `CapWords` classes,
  `UPPER_SNAKE` module constants
- Comments explain *why*, not a restatement of the next line

Ruff is configured in `pyproject.toml` (`pydocstyle` convention `google`).
Ruff is an optional dev tool; it is not installed by default, and the test
suite uses only the standard library.

## JavaScript

No hand-written JavaScript. The launchers are `.bat` / `.vbs`; the vendored
Claude Code CLI and VS Code extension are third-party (see `NOTICE`).

## Docs

- Markdown: short sentences, paths in backticks, no secret values
- Third-party tools and datasets: name them and point at `NOTICE`
