"""Contract tests: jsonc, switcher, proxyctl, installer, cli, doctor.

All offline: subprocesses and HTTP calls are mocked; the only real reads
are the repo's own contract files.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from vsdseek import (
    autostart,
    cli,
    config,
    doctor,
    installer,
    jsonc,
    proxyctl,
    switcher,
)
from vsdseek.config import Layout

from . import APP_ROOT

PINS = config.load_versions(APP_ROOT / "versions.json")
PORT = PINS["app"]["proxy_port"]


def completed(rc: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["cmd"], rc, stdout, "")


class _urlopen_stub:
    """Context-manager stand-in for ``urllib.request.urlopen``."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_urlopen_stub":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _layout(tmp: str) -> Layout:
    root = Path(tmp) / "app"
    root.mkdir()
    shutil.copy2(APP_ROOT / "versions.json", root / "versions.json")
    return Layout(root)


class JsoncTests(unittest.TestCase):
    def test_string_values_not_corrupted(self) -> None:
        raw = '{"a": "x,}", "b": "// not a comment /**/"}'
        self.assertEqual(
            json.loads(jsonc.strip_jsonc(raw)),
            {"a": "x,}", "b": "// not a comment /**/"},
        )

    def test_comments_and_siblings_survive_sync(self) -> None:
        raw = (
            "{\n"
            "  // keep me\n"
            '  "keep": 1,\n'
            '  "change": "old", // trailing\n'
            '  "drop": true\n'
            "}\n"
        )
        out = jsonc.sync_jsonc_top_level(
            raw, {"keep": 1, "change": "new", "added": [1, 2]}
        )
        self.assertIn("// keep me", out)
        self.assertIn("// trailing", out)
        self.assertNotIn('"drop"', out)
        data = json.loads(jsonc.strip_jsonc(out))
        self.assertEqual(data, {"keep": 1, "change": "new", "added": [1, 2]})

    def test_load_missing_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            empty = Path(tmp) / "empty.json"
            empty.write_text("", encoding="utf-8")
            self.assertEqual(jsonc.load_jsonc(missing), {})
            self.assertEqual(jsonc.load_jsonc(empty), {})


class SwitcherTests(unittest.TestCase):
    def _env(self, key: str = "test-key") -> dict:
        return switcher.deepseek_env(PINS, key)

    def test_deepseek_env_shape(self) -> None:
        env = self._env()
        self.assertEqual(env["ANTHROPIC_BASE_URL"], f"http://127.0.0.1:{PORT}")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "test-key")
        self.assertEqual(
            env["CLAUDE_CODE_SUBAGENT_MODEL"], PINS["models"]["subagent"]
        )
        self.assertEqual(env["CLAUDE_CODE_EFFORT_LEVEL"], "low")
        self.assertEqual(env["ANTHROPIC_MODEL"], "sonnet")
        self.assertEqual(
            env["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"],
            "Opus (DeepSeek V4 Pro)",
        )
        self.assertIn("effort", env[
            "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES"
        ])

    def test_model_picker_shape(self) -> None:
        picker = switcher.model_picker(PINS)
        self.assertIs(picker["replaceBuiltInOptions"], True)
        self.assertEqual(
            [row["model"] for row in picker["options"]], ["opus", "sonnet"]
        )
        self.assertEqual(
            [row["label"] for row in picker["options"]], ["Pro", "Flash"]
        )
        self.assertIn("deepseek-v4-pro", picker["options"][0]["description"])

    def test_merge_claude_env_replaces_and_keeps(self) -> None:
        settings = {
            "env": {"KEEP": "1", "ANTHROPIC_BASE_URL": "http://old"},
            "model": "opus",
        }
        out = switcher.merge_claude_env(settings, self._env())
        self.assertEqual(out["env"]["KEEP"], "1")
        self.assertEqual(
            out["env"]["ANTHROPIC_BASE_URL"], f"http://127.0.0.1:{PORT}"
        )
        self.assertEqual(out["model"], "opus")

    def test_merge_claude_env_removes_kill_flags(self) -> None:
        settings = {
            "env": {"CLAUDE_CODE_DISABLE_THINKING": "1"},
        }
        out = switcher.merge_claude_env(settings, self._env())
        self.assertNotIn("CLAUDE_CODE_DISABLE_THINKING", out["env"])

    def test_restore_blanks_provider_trio(self) -> None:
        settings = {"env": self._env()}
        out = switcher.merge_claude_env(settings, None, restore=True)
        for name in switcher.UNSET_ON_RESTORE:
            self.assertEqual(out["env"][name], "")
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", out["env"])

    def test_artifact_deny_add_then_remove(self) -> None:
        settings: dict = {}
        switcher.merge_artifact_deny(settings, True)
        self.assertEqual(
            sorted(settings["permissions"]["deny"]),
            sorted(switcher.ARTIFACT_DENY),
        )
        switcher.merge_artifact_deny(settings, False)
        self.assertNotIn("permissions", settings)

    def test_merge_vscode_filters_managed(self) -> None:
        settings = {
            "claudeCode.environmentVariables": [
                {"name": "KEEP", "value": "1"},
                {"name": "ANTHROPIC_BASE_URL", "value": "http://old"},
            ]
        }
        out = switcher.merge_vscode(settings, self._env(), disable_login=True)
        names = [
            row["name"]
            for row in out["claudeCode.environmentVariables"]
        ]
        self.assertIn("KEEP", names)
        self.assertEqual(names.count("ANTHROPIC_BASE_URL"), 1)
        self.assertIs(out["claudeCode.disableLoginPrompt"], True)

    def test_apply_and_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            layout.env_file.write_text(
                "DEEPSEEK_API_KEY=test-key\n", encoding="utf-8"
            )
            claude = Path(tmp) / ".claude" / "settings.json"
            vscode = Path(tmp) / "User" / "settings.json"
            claude.parent.mkdir(parents=True)
            claude.write_text(
                "{\n  // note\n  \"model\": \"opus\"\n}\n", encoding="utf-8"
            )
            switcher.apply_deepseek(
                layout, claude_path=claude, vscode_path=vscode
            )
            self.assertEqual(switcher.provider_of(claude), "deepseek")
            text = claude.read_text(encoding="utf-8")
            self.assertIn("// note", text)
            data = jsonc.load_jsonc(claude)
            self.assertEqual(data["model"], "opus")
            self.assertIn("test-key", json.dumps(data))
            self.assertEqual(len(data["modelPicker"]["options"]), 2)
            self.assertEqual(data["maxEffortLevel"], "max")
            vscode_data = jsonc.load_jsonc(vscode)
            self.assertTrue(vscode_data["claudeCode.disableLoginPrompt"])

            switcher.apply_anthropic(
                layout, claude_path=claude, vscode_path=vscode
            )
            self.assertEqual(switcher.provider_of(claude), "anthropic")
            data = jsonc.load_jsonc(claude)
            self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "")
            self.assertNotIn("permissions", data)
            self.assertNotIn("modelPicker", data)
            self.assertNotIn("maxEffortLevel", data)

    def test_backups_are_labeled_and_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            target = Path(tmp) / "settings.json"
            target.write_text("{}\n", encoding="utf-8")
            counter = {"n": 0}

            def stamp(_fmt: str) -> str:
                counter["n"] += 1
                return f"20260919-{counter['n']:06d}"

            with patch.object(
                switcher.time, "strftime", side_effect=stamp
            ):
                for index in range(switcher.BACKUP_KEEP + 3):
                    with redirect_stdout(io.StringIO()):
                        switcher.write_settings(
                            target, {"k": index}, layout,
                            label="claude-settings", dry_run=False,
                        )
            names = sorted(p.name for p in layout.backups.iterdir())
            self.assertTrue(
                all(n.startswith("claude-settings.") for n in names), names
            )
            self.assertEqual(len(names), switcher.BACKUP_KEEP)
            self.assertNotIn(
                "claude-settings.20260919-000001.bak", names
            )

    def test_write_settings_skips_unchanged_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            target = Path(tmp) / "settings.json"
            target.write_text('{\n  "a": 0\n}\n', encoding="utf-8",
                              newline="")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                first = switcher.write_settings(
                    target, {"a": 1}, layout,
                    label="claude-settings", dry_run=False,
                )
                mtime = target.stat().st_mtime_ns
                second = switcher.write_settings(
                    target, {"a": 1}, layout,
                    label="claude-settings", dry_run=False,
                )
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(target.stat().st_mtime_ns, mtime)
            self.assertIn("Unchanged", buffer.getvalue())
            self.assertEqual(len(list(layout.backups.iterdir())), 1)

    def test_provider_of_classifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(switcher.provider_of(path), "anthropic")
            path.write_text(
                '{"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}}',
                encoding="utf-8",
            )
            self.assertEqual(switcher.provider_of(path), "other")
            path.write_text(
                '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18766"}}',
                encoding="utf-8",
            )
            self.assertEqual(switcher.provider_of(path), "deepseek")


class ProxyctlTests(unittest.TestCase):
    def test_parse_listening_pid(self) -> None:
        sample = (
            "  TCP    127.0.0.1:18766        0.0.0.0:0"
            "              LISTENING       4321\n"
            "  TCP    127.0.0.1:18765        0.0.0.0:0"
            "              LISTENING       1111\n"
        )
        self.assertEqual(proxyctl.parse_listening_pid(sample, 18766), 4321)
        self.assertIsNone(proxyctl.parse_listening_pid(sample, 9999))

    def test_is_ours_normalizes_slashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            body = {
                "app": "vsdseek",
                "root": str(layout.root).replace("\\", "/").upper(),
            }
            self.assertTrue(proxyctl.is_ours(body, layout))
            self.assertFalse(
                proxyctl.is_ours({"app": "deepcc", "root": "x"}, layout)
            )

    def test_is_our_process_requires_tree_and_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            good = f'"pythonw.exe" "{layout.root}/app.py" proxy'
            with patch.object(
                proxyctl, "process_command_line", return_value=good
            ):
                self.assertTrue(proxyctl.is_our_process(5, layout))
            with patch.object(
                proxyctl, "process_command_line",
                return_value='"pythonw.exe" "C:/other/app.py" proxy',
            ):
                self.assertFalse(proxyctl.is_our_process(5, layout))

    def test_is_our_process_accepts_relative_and_root_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            relative = f'"pythonw.exe" app.py proxy --root "{layout.root}"'
            with patch.object(
                proxyctl, "process_command_line", return_value=relative
            ):
                self.assertTrue(proxyctl.is_our_process(5, layout))

    def test_is_our_process_rejects_sibling_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            for sibling in (
                f'"{layout.root}2/app.py" proxy',
                f'"pythonw.exe" app.py proxy --root "{layout.root}2"',
            ):
                with patch.object(
                    proxyctl, "process_command_line", return_value=sibling
                ):
                    self.assertFalse(
                        proxyctl.is_our_process(5, layout), sibling
                    )

    def test_stop_surfaces_taskkill_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            layout.state.mkdir(parents=True, exist_ok=True)
            layout.pid_file.write_text("4242", encoding="utf-8")
            with patch.object(
                proxyctl, "health",
                return_value={"app": "vsdseek", "root": str(layout.root)},
            ):
                with patch.object(
                    proxyctl, "listening_pid", return_value=4242
                ):
                    with patch.object(
                        proxyctl, "is_our_process", return_value=True
                    ):
                        with patch.object(
                            proxyctl, "run_hidden",
                            return_value=completed(1),
                        ):
                            with self.assertRaises(SystemExit) as ctx:
                                proxyctl.stop(layout)
            self.assertIn("taskkill failed", str(ctx.exception))
            self.assertTrue(layout.pid_file.exists())

    def test_stop_refuses_foreign_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            buffer = io.StringIO()
            calls: list[list[str]] = []
            with patch.object(
                proxyctl, "health",
                return_value={"app": "deepcc", "root": "C:/deepcc"},
            ):
                with patch.object(
                    proxyctl, "listening_pid", return_value=1234
                ):
                    with patch.object(
                        proxyctl, "is_our_process", return_value=False
                    ):
                        with patch.object(
                            proxyctl, "run_hidden",
                            side_effect=lambda cmd, **k: calls.append(cmd),
                        ):
                            with redirect_stdout(buffer):
                                rc = proxyctl.stop(layout)
            self.assertEqual(rc, 0)
            self.assertIn("belongs to 'deepcc'", buffer.getvalue())
            self.assertFalse(
                any("taskkill" in cmd for cmd in calls)
            )

    def test_stop_kills_only_verified_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            layout.state.mkdir(parents=True, exist_ok=True)
            layout.pid_file.write_text("4242", encoding="utf-8")
            calls: list[list[str]] = []
            with patch.object(proxyctl, "health", return_value=None):
                with patch.object(
                    proxyctl, "listening_pid", return_value=4242
                ):
                    with patch.object(
                        proxyctl, "is_our_process",
                        side_effect=lambda pid, lay: pid == 4242,
                    ):
                        with patch.object(
                            proxyctl, "run_hidden",
                            side_effect=lambda cmd, **k: (
                                calls.append(cmd) or completed(0)
                            ),
                        ):
                            with redirect_stdout(io.StringIO()):
                                proxyctl.stop(layout)
            self.assertTrue(
                any(cmd[:2] == ["taskkill", "/PID"] for cmd in calls)
            )
            self.assertFalse(layout.pid_file.exists())


class InstallerTests(unittest.TestCase):
    def test_update_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            before = layout.versions_file.read_bytes()
            buffer = io.StringIO()
            with patch.object(
                installer, "resolve_vscode",
                return_value={
                    "version": "9.9.9", "commit": "c", "url": "u",
                    "sha256": "s",
                },
            ):
                with patch.object(
                    installer, "resolve_extension_version",
                    return_value="9.9.9",
                ):
                    with patch(
                        "urllib.request.urlopen",
                        return_value=_urlopen_stub(b'{"version": "9.9.9"}'),
                    ):
                        with redirect_stdout(buffer):
                            installer.update_pins(
                                layout, dry_run=True
                            )
            self.assertEqual(layout.versions_file.read_bytes(), before)
            self.assertIn("would write pins", buffer.getvalue())

    def test_update_dry_run_pins_extension_from_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            with patch.object(
                installer, "resolve_vscode",
                return_value={
                    "version": "1.0", "commit": "c", "url": "u",
                    "sha256": "s",
                },
            ):
                with patch.object(
                    installer, "resolve_extension_version",
                    return_value="7.7.7",
                ):
                    with patch(
                        "urllib.request.urlopen",
                        return_value=_urlopen_stub(b'{"version": "9.9.9"}'),
                    ):
                        pins = installer.update_pins(layout)
            self.assertEqual(
                pins["artifacts"]["claude_code_extension"]["version"],
                "7.7.7",
            )
            self.assertEqual(
                pins["artifacts"]["claude_code_cli"]["version"], "9.9.9"
            )

    def test_ensure_runtime_restores_from_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            pins = json.loads(layout.versions_file.read_text("utf-8"))
            pins["artifacts"]["python"] = {
                "version": "3.14.7", "download": "https://x/py.zip",
                "sha256": "ab" * 32,
            }
            layout.versions_file.write_text(
                json.dumps(pins), encoding="utf-8"
            )
            archive = Path(tmp) / "py.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("python.exe", b"exe")
                zf.writestr(
                    "python314._pth",
                    "python314.zip\n.\n\n# import site\n#import site\n",
                )
            with patch.object(
                installer, "download", return_value=archive
            ):
                with redirect_stdout(io.StringIO()):
                    exe = installer.ensure_runtime(layout)
            self.assertEqual(exe, layout.python_exe)
            self.assertTrue(layout.python_exe.exists())
            pth = (layout.python / "python314._pth").read_text("utf-8")
            self.assertIn("import site", pth)
            self.assertNotIn("#import site", pth)
            # Present runtime is a no-op (no download).
            with patch.object(installer, "download") as dl:
                installer.ensure_runtime(layout)
            dl.assert_not_called()


    def test_resolve_vscode_parses_api(self) -> None:
        payload = json.dumps(
            {
                "productVersion": "1.138.0",
                "version": "7debcd0e",
                "url": "https://example/vscode.exe",
                "sha256hash": "ab" * 32,
            }
        ).encode("utf-8")

        class Fake:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return payload

        with patch("urllib.request.urlopen", return_value=Fake()):
            resolved = installer.resolve_vscode(PINS, "latest")
        self.assertEqual(resolved["version"], "1.138.0")
        self.assertEqual(resolved["sha256"], "ab" * 32)

    def test_ensure_extension_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            want = PINS["artifacts"]["claude_code_extension"]["version"]
            buffer = io.StringIO()
            with patch.object(
                installer, "code_cmd", return_value=Path("code.cmd")
            ):
                with patch.object(
                    installer, "extension_versions",
                    side_effect=["2.1.0", want],
                ):
                    with patch.object(
                        installer, "_install_extension",
                        return_value="box moved",
                    ):
                        with patch.object(installer.time, "sleep"):
                            with redirect_stdout(buffer):
                                have = installer.ensure_extension(
                                    layout, Path(tmp) / "Code.exe"
                                )
            self.assertEqual(have, want)
            self.assertIn("installed", buffer.getvalue())

    def test_extension_versions_parses(self) -> None:
        out = (
            "anthropic.claude-code@2.1.278\n"
            "ms-python.python@2024.1.0\n"
        )
        with patch.object(
            installer, "run_batch", return_value=completed(0, out)
        ):
            have = installer.extension_versions(
                Path("code.cmd"), "anthropic.claude-code"
            )
        self.assertEqual(have, "2.1.278")

    def test_cli_version_parses(self) -> None:
        with patch.object(
            installer, "claude_cmd", return_value=Path("claude.cmd")
        ):
            with patch.object(
                installer, "run_batch",
                return_value=completed(0, "2.1.278 (Claude Code)\n"),
            ):
                self.assertEqual(installer.cli_version(), "2.1.278")

    def test_npm_cmd_skips_portable_trees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            portable = Path(tmp) / "DeepCC" / "portable" / "npm.cmd"
            portable.parent.mkdir(parents=True)
            portable.write_text("@echo off\n", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "PROGRAMFILES": str(Path(tmp) / "none"),
                    "PROGRAMFILES(X86)": str(Path(tmp) / "none86"),
                },
                clear=False,
            ):
                with patch(
                    "vsdseek.installer.shutil.which",
                    return_value=str(portable),
                ):
                    self.assertIsNone(installer.npm_cmd())


class AutostartTests(unittest.TestCase):
    """The logon task is a schtasks/PowerShell contract; mock the calls."""

    def _fields(self, layout: Layout, *, status: str = "Ready") -> str:
        return (
            "FolderName:                             \\\n"
            f"TaskName:                               \\{autostart.TASK_NAME}\n"
            f"Status:                                 {status}\n"
            f"Task To Run:                            "
            f"{layout.pythonw_exe} \"{layout.root / 'app.py'}\" proxy "
            f"--root \"{layout.root}\" --quiet\n"
            "Schedule Type:                          At logon time\n"
        )

    def test_enable_registers_task_and_clears_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            layout.python.mkdir(parents=True, exist_ok=True)
            layout.pythonw_exe.write_bytes(b"exe")
            startup = Path(tmp) / "Startup"
            startup.mkdir()
            legacy = startup / autostart.LEGACY_SHORTCUT
            legacy.write_bytes(b"old")

            def fake_run(cmd, **kwargs):
                if "schtasks" in " ".join(cmd) and "/query" in cmd:
                    return completed(0, self._fields(layout))
                return completed(0, "")

            buffer = io.StringIO()
            with patch.object(
                autostart, "startup_dir", return_value=startup
            ):
                with patch.object(
                    autostart, "run_hidden", side_effect=fake_run
                ):
                    with redirect_stdout(buffer):
                        ok = autostart.enable(layout)
            self.assertTrue(ok)
            self.assertFalse(legacy.exists())
            self.assertIn("enabled", buffer.getvalue())

    def test_status_reports_disabled_when_query_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            with patch.object(
                autostart, "run_hidden",
                return_value=completed(1, ""),
            ):
                state = autostart.status(layout)
            self.assertFalse(state["enabled"])
            self.assertFalse(state["running"])

    def test_status_flags_foreign_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            out = self._fields(layout).replace(
                str(layout.pythonw_exe), r"C:\Other\pythonw.exe"
            )
            with patch.object(
                autostart, "run_hidden", return_value=completed(0, out)
            ):
                state = autostart.status(layout)
            self.assertTrue(state["enabled"])
            self.assertFalse(state["target_ok"])

    def test_status_detects_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            with patch.object(
                autostart, "run_hidden",
                return_value=completed(
                    0, self._fields(layout, status="Running")
                ),
            ):
                state = autostart.status(layout)
            self.assertTrue(state["running"])
            self.assertTrue(state["target_ok"])

    def test_enable_without_runtime_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            with patch.object(autostart, "run_hidden") as runner:
                with redirect_stdout(io.StringIO()):
                    ok = autostart.enable(layout)
            self.assertFalse(ok)
            runner.assert_not_called()

    def test_disable_deletes_task(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return completed(1, "")

        with patch.object(autostart, "run_hidden", side_effect=fake_run):
            with patch.object(autostart, "legacy_shortcut",
                              return_value=None):
                self.assertTrue(autostart.disable(quiet=True))
        self.assertTrue(
            any("schtasks" in cmd and "/delete" in cmd for cmd in calls)
        )


class WinutilTests(unittest.TestCase):
    def test_hidden_flags_and_powershell_encoding(self) -> None:
        from vsdseek import winutil

        kwargs = winutil.nt_no_window_kwargs({"timeout": 5})
        if winutil.os.name == "nt":
            self.assertTrue(
                kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
            )
            self.assertTrue(
                kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
            )
        self.assertEqual(kwargs["timeout"], 5)
        argv = winutil.powershell_hidden_argv("Get-Date")
        self.assertIn("-Command", argv)
        self.assertTrue(
            argv[-1].startswith(
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            )
        )


class CliTests(unittest.TestCase):
    def test_apply_deepseek_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            order: list[str] = []
            with patch.object(
                installer, "ensure_all",
                side_effect=lambda lay, **k: order.append("install"),
            ):
                with patch.object(
                    switcher, "apply_deepseek",
                    side_effect=lambda lay, **k: order.append("switch"),
                ):
                    with patch.object(
                        proxyctl, "start",
                        side_effect=lambda lay, **k: order.append("start"),
                    ):
                        with patch.object(
                            cli, "_after_switch",
                            side_effect=lambda lay, ch: order.append("after"),
                        ):
                            rc = cli.main(
                                ["apply", "deepseek", "--root", str(
                                    layout.root)]
                            )
            self.assertEqual(rc, 0)
            self.assertEqual(
                order, ["install", "start", "switch", "after"]
            )

    def test_apply_starts_proxy_before_writing_settings(self) -> None:
        """A failed proxy start must leave the previous settings intact."""
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            with patch.object(installer, "ensure_all"):
                with patch.object(
                    proxyctl, "start",
                    side_effect=SystemExit("port busy"),
                ):
                    with patch.object(switcher, "apply_deepseek") as apply:
                        with self.assertRaises(SystemExit):
                            cli.main(
                                [
                                    "apply", "deepseek", "--no-install",
                                    "--root", str(layout.root),
                                ]
                            )
            apply.assert_not_called()

    def test_apply_no_install_skips_ensure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            with patch.object(installer, "ensure_all") as ensure:
                with patch.object(switcher, "apply_deepseek"):
                    with patch.object(proxyctl, "start"):
                        with patch.object(cli, "_after_switch"):
                            cli.main(
                                [
                                    "apply", "deepseek", "--no-install",
                                    "--root", str(layout.root),
                                ]
                            )
            ensure.assert_not_called()

    def test_restore_stops_proxy_and_autostart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            with patch.object(switcher, "apply_anthropic"):
                with patch.object(proxyctl, "stop") as stop:
                    with patch.object(
                        autostart, "disable"
                    ) as disable:
                        with patch.object(
                            installer, "vscode_exe", return_value=None
                        ):
                            rc = cli.main(
                                ["restore", "--root", str(layout.root)]
                            )
            self.assertEqual(rc, 0)
            stop.assert_called_once()
            disable.assert_called_once()

    def test_open_launch_uses_clean_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            exe = Path(tmp) / "Code.exe"
            exe.write_bytes(b"exe")
            with patch.object(
                installer, "vscode_exe", return_value=exe
            ):
                with patch.object(
                    cli, "_vscode_running", return_value=False
                ):
                    with patch.dict(
                        "os.environ",
                        {
                            "ELECTRON_RUN_AS_NODE": "1",
                            "VSCODE_CLI_DATA_DIR": "x",
                            "ANTHROPIC_BASE_URL": "x",
                            "CLAUDE_CONFIG_DIR": "x",
                            "KEEP_ME": "yes",
                        },
                        clear=False,
                    ):
                        with patch.object(cli.subprocess, "Popen") as popen:
                            cli._after_switch(layout, True)
            env = popen.call_args.kwargs["env"]
            self.assertEqual(popen.call_args.args[0], [str(exe)])
            self.assertNotIn("ELECTRON_RUN_AS_NODE", env)
            self.assertNotIn("VSCODE_CLI_DATA_DIR", env)
            self.assertNotIn("ANTHROPIC_BASE_URL", env)
            self.assertNotIn("CLAUDE_CONFIG_DIR", env)
            self.assertEqual(env.get("KEEP_ME"), "yes")

    def test_apply_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            claude = Path(tmp) / "settings.json"
            with patch.object(switcher, "claude_settings_path",
                              return_value=claude):
                with patch.object(
                    switcher, "vscode_settings_path",
                    return_value=Path(tmp) / "vscode.json",
                ):
                    with patch.object(
                        config, "read_env_key", return_value="test-key"
                    ):
                        rc = cli.main(
                            [
                                "apply", "deepseek", "--dry-run",
                                "--no-install", "--root", str(layout.root),
                            ]
                        )
            self.assertEqual(rc, 0)
            self.assertFalse(claude.exists())


class DoctorTests(unittest.TestCase):
    def _settings(self, tmp: str, port: int) -> Path:
        path = Path(tmp) / "claude-settings.json"
        path.write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_AUTH_TOKEN": "test-key",
                        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_red_when_vscode_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            buffer = io.StringIO()
            with patch.object(installer, "vscode_exe", return_value=None):
                with patch.object(
                    installer, "cli_version", return_value=None
                ):
                    with patch.object(
                        config, "read_env_key",
                        side_effect=SystemExit("no key"),
                    ):
                        with patch.object(
                            switcher, "provider_of",
                            return_value="anthropic",
                        ):
                            with redirect_stdout(buffer):
                                rc = doctor.run(layout)
            self.assertEqual(rc, 1)
            self.assertIn("FAIL VS Code", buffer.getvalue())

    def test_green_when_all_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            layout.runtime.mkdir(parents=True, exist_ok=True)
            layout.python.mkdir(parents=True, exist_ok=True)
            layout.python_exe.write_bytes(b"exe")
            settings = self._settings(tmp, PORT)
            buffer = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    autostart, "status",
                    return_value={
                        "enabled": True, "path": Path(tmp) / "s.lnk",
                        "target_ok": True,
                    },
                ))
                stack.enter_context(patch.object(
                    installer, "vscode_exe",
                    return_value=Path(tmp) / "Code.exe",
                ))
                stack.enter_context(patch.object(
                    installer, "vscode_version",
                    return_value=PINS["artifacts"]["vscode"]["version"],
                ))
                stack.enter_context(patch.object(
                    installer, "extension_versions",
                    return_value=PINS["artifacts"]["claude_code_extension"]
                    ["version"],
                ))
                stack.enter_context(patch.object(
                    installer, "cli_version",
                    return_value=PINS["artifacts"]["claude_code_cli"]
                    ["version"],
                ))
                stack.enter_context(patch.object(
                    config, "read_env_key", return_value="test-key"
                ))
                stack.enter_context(patch.object(
                    switcher, "claude_settings_path",
                    return_value=settings,
                ))
                stack.enter_context(patch.object(
                    switcher, "provider_of", return_value="deepseek"
                ))
                stack.enter_context(patch.object(
                    proxyctl, "status",
                    return_value={
                        "port": PORT,
                        "health": {
                            "pid": 1, "version": doctor.__version__,
                        },
                        "ours": True,
                        "listener_pid": 1,
                        "pid_file": 1,
                        "pid_file_verified": True,
                    },
                ))
                stack.enter_context(redirect_stdout(buffer))
                rc = doctor.run(layout)
            self.assertEqual(rc, 0, buffer.getvalue())
            self.assertIn("ALL GREEN", buffer.getvalue())

    def test_warns_when_proxy_version_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            layout.python.mkdir(parents=True, exist_ok=True)
            layout.python_exe.write_bytes(b"exe")
            settings = self._settings(tmp, PORT)
            buffer = io.StringIO()
            with (
                patch.object(
                    autostart, "status",
                    return_value={
                        "enabled": True, "path": Path(tmp) / "s.lnk",
                        "target_ok": True,
                    },
                ),
                patch.object(
                    installer, "vscode_exe",
                    return_value=Path(tmp) / "Code.exe",
                ),
                patch.object(
                    installer, "vscode_version",
                    return_value=PINS["artifacts"]["vscode"]["version"],
                ),
                patch.object(
                    installer, "extension_versions",
                    return_value=PINS["artifacts"]["claude_code_extension"]
                    ["version"],
                ),
                patch.object(
                    installer, "cli_version",
                    return_value=PINS["artifacts"]["claude_code_cli"]
                    ["version"],
                ),
                patch.object(
                    config, "read_env_key", return_value="test-key"
                ),
                patch.object(
                    switcher, "claude_settings_path",
                    return_value=settings,
                ),
                patch.object(
                    switcher, "provider_of", return_value="deepseek"
                ),
                patch.object(
                    proxyctl, "status",
                    return_value={
                        "port": PORT,
                        "health": {"pid": 1, "version": "0.0.1"},
                        "ours": True,
                        "listener_pid": 1,
                        "pid_file": 1,
                        "pid_file_verified": True,
                    },
                ),
            ):
                with redirect_stdout(buffer):
                    rc = doctor.run(layout)
            self.assertEqual(rc, 0)
            self.assertIn("running v0.0.1", buffer.getvalue())


class PackagingTests(unittest.TestCase):
    def test_launchers_use_vendored_python(self) -> None:
        for name, needle in (
            ("VsDSeek.bat", 'app.py" apply deepseek'),
            ("VsDSeek-restore.bat", 'app.py" restore'),
            ("VsDSeek-doctor.bat", 'app.py" doctor'),
        ):
            text = (APP_ROOT / name).read_text(encoding="utf-8-sig")
            self.assertIn("runtime\\python\\python.exe", text)
            self.assertIn(needle, text)

    def test_pins_cover_local_route(self) -> None:
        for key in (
            "vscode", "claude_code_extension", "claude_code_cli", "python",
        ):
            self.assertIn(key, PINS["artifacts"])
        self.assertEqual(PINS["artifacts"]["vscode"]["target"],
                         "win32-x64-user")
        self.assertEqual(
            PINS["artifacts"]["claude_code_extension"]["id"],
            "anthropic.claude-code",
        )

    def test_runtime_python_present(self) -> None:
        self.assertTrue((APP_ROOT / "runtime" / "python" / "python.exe")
                        .exists())

    def test_no_secret_in_pins(self) -> None:
        text = (APP_ROOT / "versions.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-", text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
