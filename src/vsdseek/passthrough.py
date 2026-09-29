"""Two-model wire passthrough on 127.0.0.1 (port from ``versions.json``).

Sanitizes Claude Code requests for exactly two DeepSeek models
(``deepseek-flash`` and ``deepseek-v4-pro``), enforces local policies
(unknown model ids, vision on pro, unsupported blocks), forwards SSE
byte-for-byte, and logs one redacted line per request.

Every transformed field is backed by the DeepSeek docs:
- ``cache_control`` ignored; ``budget_tokens`` ignored  -> strip/drop.
- effort: DeepSeek's real tiers are low/high/max. This app maps
  minimal/low to ``low``, medium to ``high``, and everything at or above
  high (high/xhigh/ultra/max) to ``max`` - the owner's policy, so the
  High and Max picks both run at full effort. The endpoint 422s
  anything outside its seven-value enum.
- unknown model ids silently fall back to flash upstream -> allowlist.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, config
from .config import Layout

PROXY_HOST = "127.0.0.1"
UPSTREAM_HOST = "api.deepseek.com"
UPSTREAM_PREFIX = "/anthropic"
UPSTREAM_MESSAGES_PATH = f"{UPSTREAM_PREFIX}/v1/messages"
UPSTREAM_COUNT_TOKENS_PATH = f"{UPSTREAM_PREFIX}/v1/messages/count_tokens"
APP_ID = "vsdseek"

READ_TIMEOUT_S = 600
CHUNK = 8192
RETRY_STATUS = (429, 500, 502, 503, 529)
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 1.0
LOG_ROTATE_BYTES = 1024 * 1024
ONE_M = re.compile(r"\[1m\]$", re.IGNORECASE)
EFFORT_CLAMP = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "max",
    "xhigh": "max",
    "ultra": "max",
    "max": "max",
}
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "expect",
    "host",
    "content-length",
}
PRO_FAMILIES = ("opus", "fable")
FLASH_FAMILIES = ("sonnet", "haiku")
STRIPPED_BLOCKS = ("redacted_thinking",)
BLOCKED_BLOCKS = ("document",)


class PolicyError(Exception):
    """A request the app refuses locally, with a user-facing message."""

    def __init__(self, message: str, code: str = "invalid_request_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def strip_one_m(model: str) -> str:
    """Remove a trailing ``[1m]`` client suffix (never sent upstream)."""
    return ONE_M.sub("", (model or "").strip())


def _has_family(low: str, name: str) -> bool:
    return (
        low == name
        or low.startswith(name)
        or low.startswith(f"claude-{name}")
        or f"-{name}-" in low
        or low.endswith(f"-{name}")
    )


def rewrite_model(model: str, wire: dict[str, str]) -> str:
    """Resolve a client model id to exactly one of the two wire ids.

    Raises:
        PolicyError: For any id this app cannot confidently map.
    """
    raw = strip_one_m(model)
    if not raw:
        raise PolicyError(
            "Request carries no model id. Pick a model in the Claude Code "
            "model picker and resend.",
            code="invalid_request_error",
        )
    low = raw.lower()
    if low in (wire["pro"], wire["flash"]):
        return low
    if any(_has_family(low, name) for name in PRO_FAMILIES):
        return wire["pro"]
    if any(_has_family(low, name) for name in FLASH_FAMILIES):
        return wire["flash"]
    raise PolicyError(
        f"Unknown model id '{model}'. This app allows exactly "
        f"'{wire['flash']}' and '{wire['pro']}'. Pick a model in the "
        "Claude Code model picker.",
        code="not_found_error",
    )


def _drop_cache_control(obj: Any) -> None:
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for value in obj.values():
            _drop_cache_control(value)
    elif isinstance(obj, list):
        for item in obj:
            _drop_cache_control(item)


def _block_types(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        str(block.get("type", ""))
        for block in content
        if isinstance(block, dict)
    ]


def _check_blocks(content: Any, is_pro: bool, notes: list[str]) -> None:
    """Enforce block policy; may raise PolicyError. Mutates redacted strips."""
    if not isinstance(content, list):
        return
    for block in list(content):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "image" and is_pro:
            raise PolicyError(
                "Images are not supported on Opus/Fable (DeepSeek V4 Pro). "
                "Switch to Sonnet or Haiku (V4.1 Flash) and resend the "
                "image."
            )
        if kind in BLOCKED_BLOCKS:
            raise PolicyError(
                f"'{kind}' content is not supported by DeepSeek. Remove the "
                "attachment and resend."
            )
        if kind in STRIPPED_BLOCKS:
            content.remove(block)
            notes.append(f"stripped:{kind}")
            continue
        if kind == "tool_result":
            _check_blocks(block.get("content"), is_pro, notes)


def sanitize_payload(
    body: dict[str, Any], wire: dict[str, str]
) -> tuple[dict[str, Any], str, list[str]]:
    """Return (sanitized body, wire model id, notes) for one request."""
    out = copy.deepcopy(body)
    notes: list[str] = []
    wire_id = rewrite_model(str(out.get("model") or ""), wire)
    out["model"] = wire_id
    _drop_cache_control(out)

    thinking = out.get("thinking")
    ttype: str | None = None
    if isinstance(thinking, dict):
        ttype = str(thinking.get("type") or "")
        if ttype == "adaptive":
            ttype = "enabled"
        if ttype:
            out["thinking"] = {"type": ttype}

    effort_raw = out.pop("reasoning_effort", None)
    output_config = out.get("output_config")
    if ttype == "disabled":
        out.pop("output_config", None)
        effort_raw = None
    elif effort_raw is None and isinstance(output_config, dict):
        effort_raw = output_config.get("effort")
    if ttype != "disabled" and effort_raw is not None:
        effort = EFFORT_CLAMP.get(str(effort_raw).strip().lower())
        if effort:
            out["output_config"] = {"effort": effort}
        else:
            out.pop("output_config", None)
            notes.append(f"dropped-effort:{effort_raw}")

    is_pro = wire_id == wire["pro"]
    for message in out.get("messages") or []:
        if isinstance(message, dict):
            _check_blocks(message.get("content"), is_pro, notes)

    return out, wire_id, notes


def capture_record(body: dict[str, Any], wire_id: str) -> dict[str, Any]:
    """Redacted request shape for live-capture checklists.

    Never contains prompt text, headers, or secrets — only field shapes so
    the effort field, thinking values, tool_choice values, and image
    blocks can be confirmed.
    """
    messages = body.get("messages") or []
    shapes = [
        _block_types(m.get("content")) if isinstance(m, dict) else []
        for m in messages
    ]
    thinking = body.get("thinking")
    return {
        "model_in": body.get("model"),
        "model_out": wire_id,
        "thinking": thinking if isinstance(thinking, dict) else None,
        "output_config": body.get("output_config"),
        "reasoning_effort": body.get("reasoning_effort"),
        "tool_choice": body.get("tool_choice"),
        "block_shapes": shapes,
        "has_image": any("image" in row for row in shapes),
        "stream": bool(body.get("stream")),
        "message_count": len(messages),
    }


def estimate_input_tokens(body: dict[str, Any]) -> int:
    """Crude character-based estimate for the count_tokens stub."""
    blob = json.dumps(body.get("messages") or [], ensure_ascii=False)
    return max(1, len(blob) // 4)


def copy_auth_headers(incoming: Any) -> dict[str, str]:
    """Map the client's Authorization / x-api-key to DeepSeek's form."""
    headers: dict[str, str] = {}
    xkey = incoming.get("x-api-key") if incoming else None
    auth = None
    if incoming:
        auth = incoming.get("Authorization") or incoming.get("authorization")
    token = ""
    if xkey:
        token = str(xkey).strip()
    elif auth:
        raw = str(auth).strip()
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if token:
        headers["x-api-key"] = token
        headers["Authorization"] = f"Bearer {token}"
    return headers


def extra_upstream_headers(incoming: Any) -> dict[str, str]:
    """Forward Anthropic version/beta headers; never Host or hop-by-hop."""
    out: dict[str, str] = {"anthropic-version": "2023-06-01"}
    if not incoming:
        return out
    version = incoming.get("anthropic-version")
    if version:
        out["anthropic-version"] = str(version)
    beta = incoming.get("anthropic-beta")
    if beta:
        out["anthropic-beta"] = str(beta)
    return out


class ProxyLog:
    """One redacted line per request; rotated at ``LOG_ROTATE_BYTES``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def line(self, text: str) -> None:
        """Append one line, rotating the file when it grows too large."""
        with self._lock:
            try:
                if (
                    self.path.exists()
                    and self.path.stat().st_size > LOG_ROTATE_BYTES
                ):
                    backup = self.path.with_suffix(
                        self.path.suffix + ".1"
                    )
                    backup.unlink(missing_ok=True)
                    self.path.rename(backup)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{stamp} {text}\n")
            except OSError:
                pass


@dataclass
class ProxyContext:
    """Everything a request handler needs; built once per serve run."""

    layout: Layout
    pins: dict[str, Any]
    log: ProxyLog
    capture_path: Path | None = None
    _capture_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def wire(self) -> dict[str, str]:
        return self.pins["models"]["wire"]

    @property
    def port(self) -> int:
        return int(self.pins["app"]["proxy_port"])

    def capture(self, record: dict[str, Any]) -> None:
        """Append a redacted request-shape record (never content)."""
        if self.capture_path is None:
            return
        with self._capture_lock:
            self.capture_path.parent.mkdir(parents=True, exist_ok=True)
            with self.capture_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")


def catalog_payload(pins: dict[str, Any]) -> dict[str, Any]:
    """The trimmed ``/v1/models`` payload: exactly the picker ids."""
    picker = pins["models"]["picker"]
    display = pins["models"].get("display", {})
    rows = []
    for picker_id in picker:
        model_id = strip_one_m(picker_id)
        rows.append(
            {
                "type": "model",
                "id": model_id,
                "display_name": display.get(picker_id, model_id),
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
    return {
        "data": rows,
        "has_more": False,
        "first_id": rows[0]["id"] if rows else "",
        "last_id": rows[-1]["id"] if rows else "",
    }


def model_row(pins: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    """Return one catalog row by id (``[1m]`` and URL-encoding tolerated)."""
    want = strip_one_m(urllib.parse.unquote(model_id))
    for row in catalog_payload(pins)["data"]:
        if row["id"] == want:
            return row
    return None


def sse_error_frame(message: str) -> bytes:
    """An Anthropic-shaped SSE ``error`` event for mid-stream failures."""
    payload = {
        "type": "error",
        "error": {"type": "api_error", "message": message},
    }
    return b"event: error\ndata: " + json.dumps(payload).encode() + b"\n\n"


def _read_chunk(resp: Any) -> bytes:
    read1 = getattr(resp, "read1", None)
    if callable(read1):
        return read1(CHUNK)
    return resp.read(CHUNK)


def _open_upstream() -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(
        UPSTREAM_HOST, timeout=READ_TIMEOUT_S,
        context=ssl.create_default_context(),
    )


def make_handler(ctx: ProxyContext) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class bound to *ctx*."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"vsdseek/{__version__}"

        def log_message(self, fmt: str, *args: object) -> None:
            return  # handled by our own one-line log

        def _close_after(self) -> None:
            self.send_header("Connection", "close")
            self.close_connection = True

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self._close_after()
            self.end_headers()
            self.wfile.write(raw)

        def _send_policy_error(self, error: PolicyError) -> None:
            self._send_json(
                {
                    "type": "error",
                    "error": {"type": error.code, "message": error.message},
                },
                status=400,
            )

        def _log_request(self, path: str, status: int, started: float,
                         detail: str = "") -> None:
            ms = int((time.monotonic() - started) * 1000)
            suffix = f" {detail}" if detail else ""
            ctx.log.line(f"{self.command} {path} {status} {ms}ms{suffix}")

        def _route(self) -> str:
            return urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"

        def do_GET(self) -> None:  # noqa: N802
            started = time.monotonic()
            path = self._route()
            if path == "/health":
                self._send_json(
                    {
                        "status": "ok",
                        "app": APP_ID,
                        "root": str(ctx.layout.root),
                        "version": __version__,
                        "pid": os.getpid(),
                        "upstream": f"https://{UPSTREAM_HOST}{UPSTREAM_PREFIX}",
                    }
                )
                self._log_request(path, 200, started)
                return
            if path == "/v1/models":
                self._send_json(catalog_payload(ctx.pins))
                self._log_request(path, 200, started)
                return
            if path.startswith("/v1/models/"):
                row = model_row(ctx.pins, path[len("/v1/models/"):])
                if row:
                    self._send_json(row)
                    self._log_request(path, 200, started)
                else:
                    self.send_error(404)
                    self._log_request(path, 404, started)
                return
            self.send_error(404)
            self._log_request(path, 404, started)

        def do_POST(self) -> None:  # noqa: N802
            started = time.monotonic()
            path = self._route()
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_policy_error(
                    PolicyError("Request body is not valid JSON.")
                )
                self._log_request(path, 400, started)
                return
            if not isinstance(body, dict):
                self._send_policy_error(
                    PolicyError("Request body must be a JSON object.")
                )
                self._log_request(path, 400, started)
                return
            if path not in ("/v1/messages", "/v1/messages/count_tokens"):
                # Unknown paths are rejected before any sanitize/capture so
                # they cannot pollute the redacted capture stream.
                self.send_error(404)
                self._log_request(path, 404, started)
                return
            try:
                clean, wire_id, notes = sanitize_payload(body, ctx.wire)
            except PolicyError as error:
                self._send_policy_error(error)
                self._log_request(path, 400, started, "local-policy")
                return
            detail = f"{body.get('model')}->{wire_id}"
            if notes:
                detail += " " + ",".join(sorted(set(notes)))
            ctx.capture(capture_record(body, wire_id))
            data = json.dumps(clean, ensure_ascii=False).encode("utf-8")
            auth = copy_auth_headers(self.headers)
            if path == "/v1/messages/count_tokens":
                self._count_tokens(data, auth, started, detail)
                return
            if path != "/v1/messages":
                self.send_error(404)
                self._log_request(path, 404, started)
                return
            stream = bool(clean.get("stream"))
            self._messages(data, auth, stream, started, detail)

        def _upstream_headers(self, data: bytes, auth: dict[str, str],
                              accept: str) -> dict[str, str]:
            headers = {
                "Host": UPSTREAM_HOST,
                "Content-Type": "application/json",
                "Content-Length": str(len(data)),
                "Accept": accept,
                "Connection": "close",
            }
            headers.update(auth)
            headers.update(extra_upstream_headers(self.headers))
            return headers

        def _request_upstream(self, data: bytes, auth: dict[str, str],
                              accept: str) -> tuple[Any, Any]:
            """POST upstream with bounded retries on overload/limits."""
            headers = self._upstream_headers(data, auth, accept)
            last: Exception | None = None
            for attempt in range(RETRY_ATTEMPTS):
                conn = _open_upstream()
                try:
                    conn.request(
                        "POST", UPSTREAM_MESSAGES_PATH,
                        body=data, headers=headers,
                    )
                    resp = conn.getresponse()
                except Exception as exc:  # connection-level failure
                    last = exc
                    conn.close()
                    if attempt == RETRY_ATTEMPTS - 1:
                        raise
                    time.sleep(RETRY_BACKOFF_S * (attempt + 1))
                    continue
                if resp.status in RETRY_STATUS and (
                    attempt < RETRY_ATTEMPTS - 1
                ):
                    resp.read()
                    conn.close()
                    time.sleep(RETRY_BACKOFF_S * (attempt + 1))
                    continue
                return conn, resp
            raise last or OSError("upstream unreachable")

        def _messages(self, data: bytes, auth: dict[str, str], stream: bool,
                      started: float, detail: str) -> None:
            accept = "text/event-stream" if stream else "application/json"
            try:
                conn, resp = self._request_upstream(data, auth, accept)
            except Exception as exc:
                self._send_json(
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": f"Upstream unreachable "
                                       f"({exc.__class__.__name__}).",
                        },
                    },
                    status=502,
                )
                self._log_request("/v1/messages", 502, started, detail)
                return
            headers_sent = False
            try:
                self.send_response(resp.status)
                self._close_after()
                for key, value in resp.getheaders():
                    if key.lower() in HOP_BY_HOP:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                headers_sent = True
                if stream:
                    while True:
                        chunk = _read_chunk(resp)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                else:
                    payload = resp.read()
                    usage = self._usage(payload)
                    self.wfile.write(payload)
                    self._log_request(
                        "/v1/messages", resp.status, started,
                        f"{detail} {usage}",
                    )
                    detail = ""
            except Exception:
                if headers_sent and stream:
                    with contextlib.suppress(OSError):
                        self.wfile.write(
                            sse_error_frame("Upstream stream failed.")
                        )
                        self.wfile.flush()
                elif not headers_sent:
                    self._send_json(
                        {
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": "Upstream error before headers.",
                            },
                        },
                        status=502,
                    )
                    self._log_request(
                        "/v1/messages", 502, started, detail
                    )
                    detail = ""
                else:
                    self._log_request(
                        "/v1/messages", resp.status, started,
                        f"{detail} read-error",
                    )
                    detail = ""
            finally:
                conn.close()
                if stream:
                    self._log_request(
                        "/v1/messages", resp.status, started, detail
                    )

        @staticmethod
        def _usage(payload: bytes) -> str:
            try:
                parsed = json.loads(payload.decode("utf-8"))
                usage = parsed.get("usage") or {}
                return (
                    f"in={usage.get('input_tokens', '?')} "
                    f"out={usage.get('output_tokens', '?')}"
                )
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                return "in=? out=?"

        def _count_tokens(self, data: bytes, auth: dict[str, str],
                          started: float, detail: str) -> None:
            try:
                conn, resp = self._request_count(data, auth)
            except Exception:
                self._send_json(
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": "Upstream unreachable for "
                                       "count_tokens.",
                        },
                    },
                    status=502,
                )
                self._log_request(
                    "/v1/messages/count_tokens", 502, started, detail
                )
                return
            try:
                payload = resp.read()
                if resp.status in (404, 501):
                    self._count_stub(data, started, detail)
                    return
                self.send_response(resp.status)
                self._close_after()
                for key, value in resp.getheaders():
                    if key.lower() in HOP_BY_HOP:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self._log_request(
                    "/v1/messages/count_tokens", resp.status, started, detail
                )
            except Exception:
                self._send_json(
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": "Upstream read failed for "
                                       "count_tokens.",
                        },
                    },
                    status=502,
                )
                self._log_request(
                    "/v1/messages/count_tokens", 502, started, detail
                )
            finally:
                conn.close()

        def _request_count(self, data: bytes, auth: dict[str, str]
                           ) -> tuple[Any, Any]:
            headers = self._upstream_headers(data, auth, "application/json")
            conn = _open_upstream()
            try:
                conn.request(
                    "POST", UPSTREAM_COUNT_TOKENS_PATH,
                    body=data, headers=headers,
                )
                return conn, conn.getresponse()
            except Exception:
                conn.close()
                raise

        def _count_stub(self, data: bytes, started: float, detail: str) -> None:
            try:
                body = json.loads(data.decode("utf-8"))
                stub = json.dumps(
                    {"input_tokens": estimate_input_tokens(body)}
                ).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                stub = b'{"input_tokens": 1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(stub)))
            self._close_after()
            self.end_headers()
            self.wfile.write(stub)
            self._log_request(
                "/v1/messages/count_tokens", 200, started, f"{detail} stub"
            )

    return Handler


def _probe_owner(ctx: ProxyContext) -> str | None:
    """Describe who answers /health on our port, if anyone."""
    import urllib.request

    url = f"http://{PROXY_HOST}:{ctx.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if data.get("app") == APP_ID:
        if str(data.get("root")) == str(ctx.layout.root):
            return "this app is already running"
        return (
            f"another {APP_ID} instance is running from {data.get('root')}"
        )
    return "a different app owns this port"


def serve(
    layout: Layout,
    *,
    capture_path: Path | None = None,
    quiet: bool = False,
) -> int:
    """Run the passthrough until interrupted. Returns an exit code."""
    pins = config.load_versions(layout.versions_file)
    log = ProxyLog(layout.log_file)
    ctx = ProxyContext(
        layout=layout, pins=pins, log=log, capture_path=capture_path
    )
    handler = make_handler(ctx)
    try:
        server = ThreadingHTTPServer((PROXY_HOST, ctx.port), handler)
    except OSError as exc:
        owner = _probe_owner(ctx)
        hint = owner or "another process holds the port"
        message = (
            f"Could not bind {PROXY_HOST}:{ctx.port} ({exc}). {hint}."
        )
        log.line(message)
        print(message, file=sys.stderr)
        return 1
    layout.state.mkdir(parents=True, exist_ok=True)
    layout.pid_file.write_text(str(os.getpid()), encoding="utf-8")
    log.line(f"listening on {PROXY_HOST}:{ctx.port} (pid {os.getpid()})")
    if not quiet:
        print(f"vsDSeek passthrough on http://{PROXY_HOST}:{ctx.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        layout.pid_file.unlink(missing_ok=True)
        log.line("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Module entry point: ``python -m vsdseek.passthrough``."""
    parser = argparse.ArgumentParser(
        prog="vsdseek.passthrough",
        description="Two-model DeepSeek passthrough for Claude Code.",
    )
    parser.add_argument(
        "--root", type=Path, default=None,
        help="app tree (default: the real app root)",
    )
    parser.add_argument(
        "--capture", type=Path, default=None,
        help="append redacted request-shape records to this file",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    layout = Layout(args.root or config.APP_ROOT)
    return serve(layout, capture_path=args.capture, quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
