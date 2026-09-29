"""Top-level JSONC object sync: update keys without dropping comments.

VS Code's ``settings.json`` and Claude Code's ``settings.json`` are JSONC
files that people edit by hand. Rewriting them with ``json.dumps`` would
drop comments and layout, so only the touched top-level keys are replaced
in place; everything else is copied byte for byte.

The scanner is string aware: braces, commas, and ``//`` inside string
values are never treated as syntax (a prototype regex version corrupted
values like ``{"a": "x,}"}`` — this module is the fix).
"""

from __future__ import annotations

import json
from typing import Any


def skip_ws_comments(text: str, i: int) -> int:
    """Return the first index at/after *i* that is neither space nor comment."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\n\r":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(n, i + 2)
            continue
        break
    return i


def _parse_string(text: str, i: int) -> int:
    """Return the index just past the string starting at *i*."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated string")


def _parse_number(text: str, i: int) -> int:
    n = len(text)
    if text[i] == "-":
        i += 1
    while i < n and text[i].isdigit():
        i += 1
    if i < n and text[i] == ".":
        i += 1
        while i < n and text[i].isdigit():
            i += 1
    if i < n and text[i] in "eE":
        i += 1
        if i < n and text[i] in "+-":
            i += 1
        while i < n and text[i].isdigit():
            i += 1
    return i


def _parse_container(text: str, i: int, open_ch: str, close_ch: str) -> int:
    n = len(text)
    depth = 0
    while i < n:
        i = skip_ws_comments(text, i)
        if i >= n:
            break
        c = text[i]
        if c == '"':
            i = _parse_string(text, i)
            continue
        if c == open_ch:
            depth += 1
            i += 1
            continue
        if c == close_ch:
            depth -= 1
            i += 1
            if depth == 0:
                return i
            continue
        if c in "{[":
            inner_close = "}" if c == "{" else "]"
            i = _parse_container(text, i, c, inner_close)
            continue
        i += 1
    raise ValueError("unterminated container")


def parse_value_end(text: str, i: int) -> int:
    """Return the index just past the JSON value starting at *i*."""
    i = skip_ws_comments(text, i)
    if i >= len(text):
        raise ValueError("expected value")
    c = text[i]
    if c == '"':
        return _parse_string(text, i)
    if c == "{":
        return _parse_container(text, i, "{", "}")
    if c == "[":
        return _parse_container(text, i, "[", "]")
    if c == "-" or c.isdigit():
        return _parse_number(text, i)
    for word, size in (("true", 4), ("false", 5), ("null", 4)):
        if text.startswith(word, i):
            return i + size
    raise ValueError(f"expected value at {i}")


def top_level_properties(text: str) -> tuple[list[tuple[str, int, int]], int]:
    """Return ``[(key, key_start, value_end)], close_brace_index``."""
    i = skip_ws_comments(text, 0)
    if i >= len(text) or text[i] != "{":
        raise ValueError("expected top-level object")
    i += 1
    props: list[tuple[str, int, int]] = []
    n = len(text)
    while True:
        i = skip_ws_comments(text, i)
        if i >= n:
            raise ValueError("unterminated object")
        if text[i] == "}":
            return props, i
        if text[i] == ",":
            i += 1
            continue
        if text[i] != '"':
            raise ValueError("expected object key")
        key_start = i
        key_end = _parse_string(text, i)
        key = json.loads(text[key_start:key_end])
        i = skip_ws_comments(text, key_end)
        if i >= n or text[i] != ":":
            raise ValueError("expected colon")
        val_end = parse_value_end(text, i + 1)
        props.append((key, key_start, val_end))
        i = val_end


def strip_jsonc(text: str) -> str:
    """Return *text* with comments and trailing commas removed."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = False
    escape = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\n\r":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(n, i + 2)
            continue
        out.append(c)
        i += 1
    return drop_trailing_commas("".join(out))


def drop_trailing_commas(text: str) -> str:
    """Remove ``,]`` / ``},`` artifacts that confuse strict JSON parsers."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = False
    escape = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\n\r":
                out.append(text[i])
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            out.append(c)
            i += 1
            while i < n:
                out.append(text[i])
                if i > 0 and text[i - 1] == "*" and text[i] == "/":
                    i += 1
                    break
                i += 1
            continue
        if c == ",":
            j = skip_ws_comments(text, i + 1)
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_jsonc(path: Any) -> dict[str, Any]:
    """Parse a JSONC object file (missing/empty file -> empty dict)."""
    from pathlib import Path

    target = Path(path)
    if not target.exists():
        return {}
    raw = target.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return {}
    try:
        data = json.loads(strip_jsonc(raw))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {target}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SystemExit(f"Expected a JSON object in {target}")
    return data


def _format_value(value: Any, key_indent: int) -> str:
    dumped = json.dumps(value, indent=2, ensure_ascii=False)
    if "\n" not in dumped:
        return dumped
    pad = " " * key_indent
    lines = dumped.splitlines()
    return lines[0] + "\n" + "\n".join(pad + line for line in lines[1:])


def _line_indent(text: str, index: int) -> int:
    line_start = text.rfind("\n", 0, index) + 1
    indent = 0
    while line_start + indent < index and text[line_start + indent] in " \t":
        indent += 1
    return indent


def _removal_span(text: str, key_start: int, val_end: int) -> tuple[int, int]:
    j = skip_ws_comments(text, val_end)
    if j < len(text) and text[j] == ",":
        return key_start, j + 1
    k = key_start - 1
    while k >= 0 and text[k] in " \t\r\n":
        k -= 1
    if k >= 0 and text[k] == ",":
        return k, val_end
    return key_start, val_end


def skip_ws_comments_back(text: str, i: int) -> int:
    """Return the last index at/before *i* that is neither space nor comment."""
    while i >= 0:
        if text[i] in " \t\r\n":
            i -= 1
            continue
        if text[i] == "/" and i >= 1 and text[i - 1] == "*":
            i -= 2
            while i >= 1 and not (text[i - 1] == "/" and text[i] == "*"):
                i -= 1
            i -= 2
            continue
        line_start = text.rfind("\n", 0, i + 1) + 1
        j = line_start
        in_str = False
        escape = False
        comment_at: int | None = None
        while j <= i:
            c = text[j]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
                j += 1
                continue
            if c == '"':
                in_str = True
                j += 1
                continue
            if c == "/" and j + 1 <= i and text[j + 1] == "/":
                comment_at = j
                break
            j += 1
        if comment_at is not None:
            i = comment_at - 1
            continue
        break
    return i


def _needs_comma_before(text: str, close_idx: int) -> bool:
    i = skip_ws_comments_back(text, close_idx - 1)
    if i < 0:
        return False
    return text[i] not in "{,"


def sync_jsonc_top_level(raw: str, new_data: dict[str, Any]) -> str:
    """Rewrite top-level keys in a JSONC object; keep comments and layout.

    Replacements and removals run first; the insert of new keys then
    re-scans the updated text, so comma placement accounts for removed
    siblings (doing both in one pass duplicated the separator).
    """
    props, _ = top_level_properties(raw)
    edits: list[tuple[int, int, str]] = []
    for key, key_start, val_end in props:
        if key not in new_data:
            start, end = _removal_span(raw, key_start, val_end)
            edits.append((start, end, ""))
            continue
        key_end = _parse_string(raw, key_start)
        colon = skip_ws_comments(raw, key_end)
        val_start = skip_ws_comments(raw, colon + 1)
        key_indent = _line_indent(raw, key_start)
        edits.append(
            (val_start, val_end, _format_value(new_data[key], key_indent))
        )
    for start, end, repl in sorted(edits, key=lambda item: item[0],
                                   reverse=True):
        raw = raw[:start] + repl + raw[end:]

    props_after, close_idx = top_level_properties(raw)
    present = {key for key, _, _ in props_after}
    missing = [key for key in new_data if key not in present]
    if missing:
        brace_indent = _line_indent(raw, close_idx)
        item_indent = brace_indent + 2
        comma = ",\n" if _needs_comma_before(raw, close_idx) else "\n"
        chunks: list[str] = []
        pad = " " * item_indent
        for idx, key in enumerate(missing):
            prefix = comma if idx == 0 else ",\n"
            chunks.append(
                f"{prefix}{pad}{json.dumps(key, ensure_ascii=False)}: "
                f"{_format_value(new_data[key], item_indent)}"
            )
        chunks.append("\n" + (" " * brace_indent))
        raw = raw[:close_idx] + "".join(chunks) + raw[close_idx:]
    return drop_trailing_commas(raw)
