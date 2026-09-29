"""Offline tests for the two-model wire passthrough."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from vsdseek import __version__, config, passthrough
from vsdseek.config import Layout
from vsdseek.passthrough import (
    APP_ID,
    ProxyContext,
    ProxyLog,
    PolicyError,
    capture_record,
    catalog_payload,
    copy_auth_headers,
    estimate_input_tokens,
    make_handler,
    model_row,
    rewrite_model,
    sanitize_payload,
    sse_error_frame,
    strip_one_m,
)

PINS = config.load_versions()
WIRE = PINS["models"]["wire"]


class RewriteTests(unittest.TestCase):
    def test_families(self) -> None:
        self.assertEqual(rewrite_model("claude-opus-4-8", WIRE), "deepseek-v4-pro")
        self.assertEqual(rewrite_model("claude-fable-5-1[1m]", WIRE), "deepseek-v4-pro")
        self.assertEqual(rewrite_model("sonnet", WIRE), "deepseek-flash")
        self.assertEqual(rewrite_model("claude-haiku-4-5[1m]", WIRE), "deepseek-flash")

    def test_one_m_stripped(self) -> None:
        self.assertEqual(strip_one_m("claude-sonnet-4-6[1m]"), "claude-sonnet-4-6")
        self.assertNotIn("[1m]", rewrite_model("claude-sonnet-4-6[1m]", WIRE))

    def test_exact_wire_ids_pass(self) -> None:
        self.assertEqual(rewrite_model("deepseek-flash", WIRE), "deepseek-flash")
        self.assertEqual(rewrite_model("deepseek-v4-pro", WIRE), "deepseek-v4-pro")

    def test_unknown_is_loud(self) -> None:
        for bad in ("gpt-4o", "claude-mystery-9", "deepseek-v5-ultra"):
            with self.assertRaises(PolicyError):
                rewrite_model(bad, WIRE)

    def test_empty_model_is_rejected(self) -> None:
        for bad in ("", "   ", None):
            with self.assertRaises(PolicyError):
                rewrite_model(bad, WIRE)

    def test_missing_model_field_is_rejected(self) -> None:
        with self.assertRaises(PolicyError):
            sanitize_payload({"messages": []}, WIRE)


class SanitizeTests(unittest.TestCase):
    def _body(self, **extra: object) -> dict:
        base = {"model": "claude-sonnet-4-6", "messages": []}
        base.update(extra)
        return base

    def test_adaptive_to_enabled_and_budget_dropped(self) -> None:
        out, wire_id, _ = sanitize_payload(
            self._body(thinking={"type": "adaptive", "budget_tokens": 4096}),
            WIRE,
        )
        self.assertEqual(out["thinking"], {"type": "enabled"})
        self.assertEqual(wire_id, "deepseek-flash")

    def test_disabled_pops_effort_shapes(self) -> None:
        out, _, _ = sanitize_payload(
            self._body(
                thinking={"type": "disabled"},
                reasoning_effort="high",
                output_config={"effort": "high"},
            ),
            WIRE,
        )
        self.assertNotIn("reasoning_effort", out)
        self.assertNotIn("output_config", out)

    def test_reasoning_effort_moved_and_popped(self) -> None:
        out, _, _ = sanitize_payload(
            self._body(thinking={"type": "enabled"}, reasoning_effort="low"),
            WIRE,
        )
        self.assertNotIn("reasoning_effort", out)
        self.assertEqual(out["output_config"], {"effort": "low"})

    def test_output_config_normalized(self) -> None:
        out, _, notes = sanitize_payload(
            self._body(output_config={"effort": "medium", "verbosity": "low"}),
            WIRE,
        )
        self.assertEqual(out["output_config"], {"effort": "high"})
        self.assertEqual(notes, [])

    def test_effort_map_owner_policy(self) -> None:
        for raw, want in (
            ("minimal", "low"),
            ("low", "low"),
            ("medium", "high"),
            ("high", "max"),
            ("xhigh", "max"),
            ("max", "max"),
            ("ultra", "max"),
        ):
            out, _, _ = sanitize_payload(
                self._body(output_config={"effort": raw}), WIRE
            )
            self.assertEqual(out["output_config"], {"effort": want}, raw)

    def test_unknown_effort_dropped_with_note(self) -> None:
        out, _, notes = sanitize_payload(
            self._body(output_config={"effort": "extreme"}), WIRE
        )
        self.assertNotIn("output_config", out)
        self.assertTrue(any("dropped-effort" in n for n in notes))

    def test_cache_control_stripped(self) -> None:
        out, _, _ = sanitize_payload(
            self._body(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "hi",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ]
            ),
            WIRE,
        )
        self.assertNotIn("cache_control", json.dumps(out))

    def test_redacted_thinking_stripped(self) -> None:
        out, _, notes = sanitize_payload(
            self._body(
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "redacted_thinking", "data": "x"},
                            {"type": "text", "text": "ok"},
                        ],
                    }
                ]
            ),
            WIRE,
        )
        kinds = [b["type"] for b in out["messages"][0]["content"]]
        self.assertEqual(kinds, ["text"])
        self.assertIn("stripped:redacted_thinking", notes)

    def test_document_blocked(self) -> None:
        with self.assertRaises(PolicyError):
            sanitize_payload(
                self._body(
                    messages=[
                        {"role": "user", "content": [{"type": "document"}]}
                    ]
                ),
                WIRE,
            )

    def test_image_on_pro_blocked(self) -> None:
        body = self._body(
            model="claude-opus-4-8",
            messages=[
                {"role": "user", "content": [{"type": "image"}]}
            ],
        )
        with self.assertRaises(PolicyError):
            sanitize_payload(body, WIRE)

    def test_image_inside_tool_result_blocked_on_pro(self) -> None:
        body = self._body(
            model="claude-fable-5-1",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [{"type": "image"}],
                        }
                    ],
                }
            ],
        )
        with self.assertRaises(PolicyError):
            sanitize_payload(body, WIRE)

    def test_image_on_flash_passes(self) -> None:
        body = self._body(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": [{"type": "image"}]}],
        )
        out, wire_id, _ = sanitize_payload(body, WIRE)
        self.assertEqual(wire_id, "deepseek-flash")
        self.assertEqual(out["messages"][0]["content"][0]["type"], "image")


class HelperTests(unittest.TestCase):
    def test_estimate_positive(self) -> None:
        self.assertGreater(estimate_input_tokens({"messages": [{"a": 1}]}), 0)

    def test_auth_headers(self) -> None:
        headers = copy_auth_headers({"Authorization": "Bearer sk-test"})
        self.assertEqual(headers["x-api-key"], "sk-test")
        self.assertEqual(headers["Authorization"], "Bearer sk-test")

    def test_sse_error_frame(self) -> None:
        frame = sse_error_frame("boom")
        self.assertTrue(frame.startswith(b"event: error\ndata: "))
        self.assertIn(b"api_error", frame)

    def test_catalog_trimmed(self) -> None:
        payload = catalog_payload(PINS)
        ids = [row["id"] for row in payload["data"]]
        self.assertEqual(len(ids), 4)
        for row in payload["data"]:
            self.assertNotIn("[1m]", row["id"])
            self.assertTrue(row["display_name"])
        self.assertIsNotNone(model_row(PINS, "claude-opus-4-8"))
        self.assertIsNotNone(model_row(PINS, "claude-opus-4-8[1m]"))
        self.assertIsNone(model_row(PINS, "haiku"))

    def test_capture_record_has_no_content(self) -> None:
        secret = "PROMPT-SECRET-123"
        body = {
            "model": "claude-sonnet-4-6",
            "thinking": {"type": "enabled", "budget_tokens": 1},
            "tool_choice": {"type": "auto"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": secret},
                        {"type": "image"},
                    ],
                }
            ],
        }
        record = capture_record(body, "deepseek-flash")
        blob = json.dumps(record)
        self.assertNotIn(secret, blob)
        self.assertTrue(record["has_image"])
        self.assertEqual(record["tool_choice"], {"type": "auto"})


class ProxyLogTests(unittest.TestCase):
    def test_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.txt"
            log = ProxyLog(path)
            with patch("vsdseek.passthrough.LOG_ROTATE_BYTES", 100):
                log.line("x" * 80)
                log.line("y" * 80)
            self.assertTrue(path.exists())
            self.assertTrue(path.with_suffix(".txt.1").exists())


class FakeResponse:
    """Minimal stand-in for an ``http.client`` response."""

    def __init__(self, status: int = 200, body: bytes = b"{}",
                 headers: list | None = None) -> None:
        self.status = status
        self._body = body
        self._headers = headers or [("Content-Type", "application/json")]

    def getheaders(self) -> list:
        return self._headers

    def read(self, _n: int | None = None) -> bytes:
        return self._body

    def read1(self, _n: int | None = None) -> bytes:
        return self._body


class FakeConn:
    """Minimal stand-in for ``http.client.HTTPSConnection``."""

    def __init__(self, resp: FakeResponse | None = None,
                 fail_on_request: bool = False) -> None:
        self._resp = resp
        self._fail = fail_on_request
        self.closed = False

    def request(self, *_args: object, **_kwargs: object) -> None:
        if self._fail:
            raise OSError("connection refused")

    def getresponse(self) -> FakeResponse:
        assert self._resp is not None
        return self._resp

    def close(self) -> None:
        self.closed = True


class HttpHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="vsdseek-http-")
        cls.layout = Layout(Path(cls._tmp.name))
        cls.layout.state.mkdir(parents=True, exist_ok=True)
        ctx = ProxyContext(
            layout=cls.layout,
            pins=PINS,
            log=ProxyLog(cls.layout.state / "t.log"),
            capture_path=cls.layout.state / "capture.jsonl",
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ctx))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def _get(self, path: str) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, json.loads(raw) if raw else {}

    def test_health_carries_app_id_and_root(self) -> None:
        status, data = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["app"], APP_ID)
        self.assertEqual(data["root"], str(self.layout.root))
        self.assertEqual(data["version"], __version__)

    def test_models_trimmed(self) -> None:
        status, data = self._get("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["data"]), 4)
        status, row = self._get("/v1/models/claude-sonnet-4-6")
        self.assertEqual(status, 200)
        self.assertEqual(row["id"], "claude-sonnet-4-6")
        status, _ = self._get("/v1/models/nope")
        self.assertEqual(status, 404)

    def test_unknown_model_400_without_upstream(self) -> None:
        status, data = self._post(
            "/v1/messages",
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["type"], "not_found_error")

    def test_query_string_tolerated(self) -> None:
        status, _ = self._post(
            "/v1/messages?beta=true",
            {"model": "gpt-4o", "messages": []},
        )
        self.assertEqual(status, 400)

    def test_vision_policy_local(self) -> None:
        status, data = self._post(
            "/v1/messages",
            {
                "model": "claude-opus-4-8",
                "messages": [
                    {"role": "user", "content": [{"type": "image"}]}
                ],
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("not supported", data["error"]["message"])

    def test_capture_written(self) -> None:
        ctx = ProxyContext(
            layout=self.layout,
            pins=PINS,
            log=ProxyLog(self.layout.state / "t2.log"),
            capture_path=self.layout.state / "capture2.jsonl",
        )
        ctx.capture(
            capture_record({"model": "claude-sonnet-4-6"}, "deepseek-flash")
        )
        row = json.loads(
            (self.layout.state / "capture2.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        self.assertEqual(row["model_out"], "deepseek-flash")

    def test_rejected_requests_are_not_captured(self) -> None:
        # Rejected requests never reach the wire, so they are logged as
        # "local-policy" instead of polluting the capture file.
        self._post(
            "/v1/messages?beta=true",
            {"model": "gpt-4o", "messages": []},
        )
        capture = self.layout.state / "capture.jsonl"
        self.assertFalse(capture.exists())

    def _serve(self, capture: Path) -> int:
        """Start an extra server with its own capture file; return port."""
        ctx = ProxyContext(
            layout=self.layout,
            pins=PINS,
            log=ProxyLog(self.layout.state / "t3.log"),
            capture_path=capture,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ctx))
        threading.Thread(
            target=server.serve_forever, daemon=True
        ).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def _post_port(self, port: int, path: str, body: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{port}{path}"
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return exc.code, {}

    def test_accepted_request_is_forwarded_and_captured(self) -> None:
        capture = self.layout.state / "capture-ok.jsonl"
        port = self._serve(capture)
        body = {
            "model": "claude-opus-4-8[1m]",
            "messages": [{"role": "user", "content": "hi"}],
        }
        with patch(
            "vsdseek.passthrough._open_upstream",
            return_value=FakeConn(FakeResponse(200, b'{"ok": true}')),
        ):
            status, _ = self._post_port(port, "/v1/messages", body)
        self.assertEqual(status, 200)
        row = json.loads(capture.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["model_out"], "deepseek-v4-pro")

    def test_unknown_path_is_rejected_before_capture(self) -> None:
        capture = self.layout.state / "capture-path.jsonl"
        port = self._serve(capture)
        status, _ = self._post_port(
            port, "/v1/other", {"model": "claude-sonnet-4-6", "messages": []}
        )
        self.assertEqual(status, 404)
        self.assertFalse(capture.exists())

    def test_count_tokens_stub_when_upstream_404(self) -> None:
        capture = self.layout.state / "capture-ct.jsonl"
        port = self._serve(capture)
        body = {
            "model": "claude-sonnet-4-6[1m]",
            "messages": [{"role": "user", "content": "hi"}],
        }
        with patch(
            "vsdseek.passthrough._open_upstream",
            return_value=FakeConn(FakeResponse(404, b"{}")),
        ):
            status, data = self._post_port(
                port, "/v1/messages/count_tokens", body
            )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data.get("input_tokens", 0), 1)

    def test_retry_then_success(self) -> None:
        capture = self.layout.state / "capture-retry.jsonl"
        port = self._serve(capture)
        conns = [
            FakeConn(fail_on_request=True),
            FakeConn(FakeResponse(200, b'{"ok": true}')),
        ]
        body = {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
        }
        with patch(
            "vsdseek.passthrough._open_upstream",
            side_effect=lambda: conns.pop(0),
        ):
            with patch.object(passthrough.time, "sleep"):
                status, _ = self._post_port(port, "/v1/messages", body)
        self.assertEqual(status, 200)
        self.assertFalse(conns)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
