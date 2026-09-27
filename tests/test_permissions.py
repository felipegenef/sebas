"""Permission-tool tests: notification_status and notification_request.

Everything the probes touch is mocked per OS (linux/darwin/win32): no
subprocess is really run, no D-Bus session is used to show anything, no
daemon socket is opened, no audio and — the point of the guard tests —
notification_request never writes an OS setting (Linux is guidance only).
The one loose test asserts just what this box reports for Linux.

Runs against the voice MCP checkout located by SEBAS_MCP_ROOT (skipped when it
is not set).
"""
from __future__ import annotations

import inspect
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from _support import mcp_root


class _McpCase(unittest.TestCase):
    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import tools
        from voice import permissions
        self.tools = tools
        self.permissions = permissions


def _which(*names):
    """`_has` stub: only `names` are on PATH."""
    return lambda binary: binary in names


def _runner(answers):
    """`_run_rc` stub from {argv_head: (code, out)}; (None, '') otherwise."""
    def fake(argv):
        return answers.get(argv[0], (None, ""))
    return fake


class RegistrationTest(_McpCase):
    def test_schema_registers_both_tools(self):
        by_name = {entry["name"]: entry for entry in self.tools.SCHEMA}
        for name in ("notification_status", "notification_request"):
            self.assertIn(name, by_name)
            entry = by_name[name]
            self.assertTrue(entry["description"])
            self.assertEqual(entry["inputSchema"]["type"], "object")
            self.assertEqual(entry["inputSchema"].get("properties"), {})
            self.assertNotIn("required", entry["inputSchema"])

    def test_handlers_register_both_tools(self):
        self.assertIs(self.tools.HANDLERS.get("notification_status"),
                      self.tools.notification_status)
        self.assertIs(self.tools.HANDLERS.get("notification_request"),
                      self.tools.notification_request)

    def test_handlers_take_no_arguments(self):
        for fn in (self.tools.notification_status,
                   self.tools.notification_request):
            params = inspect.signature(fn).parameters
            self.assertEqual(list(params), [])
            for param in params.values():
                self.assertIn(param.kind,
                              (inspect.Parameter.POSITIONAL_ONLY,
                               inspect.Parameter.POSITIONAL_OR_KEYWORD))

    def test_unexpected_argument_raises_typeerror(self):
        # The server maps this TypeError to the 'invalid_arguments' payload.
        with self.assertRaises(TypeError):
            self.tools.notification_status(bogus=True)
        with self.assertRaises(TypeError):
            self.tools.notification_request(bogus=True)


class StatusShapeTest(_McpCase):
    def _report(self, system):
        perms = self.permissions
        with mock.patch.object(perms, "os_name", return_value=system), \
             mock.patch.object(perms, "_cards",
                               return_value={"available": True,
                                             "source": "test",
                                             "detail": "ok"}), \
             mock.patch.object(perms, "_linux_report", return_value={
                 "backend": {"available": True}, "permission": {"state": "x"},
                 "dnd": {"state": "y"}}), \
             mock.patch.object(perms, "_darwin_report", return_value={
                 "backend": {"available": True}, "permission": {"state": "x"},
                 "dnd": {"state": "y"}}), \
             mock.patch.object(perms, "_win_report", return_value={
                 "backend": {"available": True}, "permission": {"state": "x"},
                 "dnd": {"state": "y"}}):
            return self.tools.notification_status()

    def test_payload_shape_on_every_known_os(self):
        for system in ("linux", "darwin", "win32"):
            report = self._report(system)
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["os"], system)
            for key in ("backend", "cards", "permission", "dnd"):
                self.assertIn(key, report)
            self.assertTrue(report["next_step"])
            self.assertNotIn("problem", report)      # problem only when not ok

    def test_unknown_platform_says_unknown_with_explanation(self):
        report = self._report("sunos5")
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["os"], "sunos5")
        self.assertTrue(report["problem"])
        self.assertTrue(report["next_step"])

    def test_probe_crash_returns_internal_error_payload(self):
        perms = self.permissions

        def boom():
            raise RuntimeError("probe exploded")

        with mock.patch.object(perms, "os_name", return_value="linux"), \
             mock.patch.object(perms, "_linux_report", side_effect=boom):
            report = self.tools.notification_status()
        self.assertEqual(report["status"], "internal_error")
        self.assertIn("probe exploded", report["problem"])
        self.assertTrue(report["next_step"])


class LinuxProbeTest(_McpCase):
    def test_backend_reports_gi_notify_and_gdbus(self):
        perms = self.permissions
        with mock.patch.object(perms, "_gi_notify_available", return_value=True), \
             mock.patch.object(perms, "_has", return_value=True):
            backend = perms._linux_backend()
        self.assertTrue(backend["available"])
        self.assertIn("python3-gi", backend["name"])
        self.assertIn("available", backend["detail"])

    def test_backend_unavailable_without_gi_and_gdbus(self):
        perms = self.permissions
        with mock.patch.object(perms, "_gi_notify_available", return_value=False), \
             mock.patch.object(perms, "_has", return_value=False):
            backend = perms._linux_backend()
        self.assertFalse(backend["available"])
        self.assertIn("missing", backend["detail"])

    def test_dnd_off_when_gnome_show_banners_true(self):
        perms = self.permissions
        answers = {"gsettings": (0, "true")}
        with mock.patch.object(perms, "_run_rc", side_effect=_runner(answers)):
            dnd = perms._linux_dnd()
        self.assertEqual(dnd["state"], "off")
        self.assertIn("show-banners", dnd["source"])

    def test_dnd_on_when_gnome_show_banners_false(self):
        perms = self.permissions
        answers = {"gsettings": (0, "false")}
        with mock.patch.object(perms, "_run_rc", side_effect=_runner(answers)):
            dnd = perms._linux_dnd()
        self.assertEqual(dnd["state"], "on")
        self.assertTrue(dnd["detail"])

    def test_dnd_from_xfce_when_gsettings_absent(self):
        perms = self.permissions

        def fake(argv):
            if argv[0] == "xfconf-query":
                return 0, "true"
            return None, ""

        with mock.patch.object(perms, "_run_rc", side_effect=fake):
            dnd = perms._linux_dnd()
        self.assertEqual(dnd["state"], "on")
        self.assertIn("xfconf", dnd["source"])

    def test_dnd_unknown_never_guessed(self):
        perms = self.permissions
        with mock.patch.object(perms, "_run_rc",
                               side_effect=_runner({})):    # nothing readable
            dnd = perms._linux_dnd()
        self.assertEqual(dnd["state"], "unknown")
        self.assertIn("GNOME", dnd["detail"])
        self.assertIn("XFCE", dnd["detail"])

    def test_permission_is_not_required_with_per_app_caveat(self):
        permission = self.permissions._linux_permission()
        self.assertEqual(permission["state"], "not_required")
        self.assertIn("Settings > Notifications", permission["detail"])

    def test_real_box_report_is_coherent(self):
        # Loose on purpose: this runs the real read-only probes of this box.
        report = self.tools.notification_status()
        self.assertEqual(report["os"], "linux")
        self.assertEqual(report["status"], "ok")
        self.assertIn(report["dnd"]["state"], ("on", "off", "unknown"))
        self.assertIn(report["permission"]["state"],
                      ("not_required", "unknown"))
        self.assertIsInstance(report["cards"]["available"], bool)
        self.assertTrue(report["next_step"])


class DarwinProbeTest(_McpCase):
    def test_backend_chain_prefers_terminal_notifier(self):
        perms = self.permissions
        with mock.patch.object(perms, "_has", return_value=True):
            backend = perms._darwin_backend()
        self.assertTrue(backend["available"])
        self.assertIn("terminal-notifier -> herald", backend["detail"])

    def test_backend_unavailable_without_helpers(self):
        perms = self.permissions
        with mock.patch.object(perms, "_has", return_value=False):
            backend = perms._darwin_backend()
        self.assertFalse(backend["available"])
        self.assertIn("osascript", backend["detail"])

    def test_permission_granted_from_diagnose_exit_0(self):
        perms = self.permissions
        with mock.patch.object(perms, "_has",
                               side_effect=_which("terminal-notifier")), \
             mock.patch.object(perms, "_run_rc",
                               side_effect=_runner({"terminal-notifier":
                                                    (0, "authorization: ok")})):
            permission = perms._darwin_permission()
        self.assertEqual(permission["state"], "granted")

    def test_permission_denied_from_diagnose_exit_3(self):
        perms = self.permissions
        with mock.patch.object(perms, "_has",
                               side_effect=_which("terminal-notifier")), \
             mock.patch.object(perms, "_run_rc",
                               side_effect=_runner({"terminal-notifier":
                                                    (3, "")})):
            permission = perms._darwin_permission()
        self.assertEqual(permission["state"], "denied")

    def test_permission_unknown_without_diagnose_or_clear_answer(self):
        perms = self.permissions
        with mock.patch.object(perms, "_has", return_value=False):
            permission = perms._darwin_permission()
        self.assertEqual(permission["state"], "unknown")
        self.assertIn("UNUserNotificationCenter", permission["detail"])
        with mock.patch.object(perms, "_has",
                               side_effect=_which("terminal-notifier")), \
             mock.patch.object(perms, "_run_rc",
                               side_effect=_runner({"terminal-notifier":
                                                    (4, "")})):
            permission = perms._darwin_permission()
        self.assertEqual(permission["state"], "unknown")
        self.assertTrue(permission["detail"])

    def test_dnd_is_unknown_with_explanation(self):
        dnd = self.permissions._darwin_dnd()
        self.assertEqual(dnd["state"], "unknown")
        self.assertIn("Focus", dnd["detail"])


class WindowsProbeTest(_McpCase):
    def test_backend_reports_powershell_and_burnttoast(self):
        perms = self.permissions
        with mock.patch.object(perms, "_has", return_value=True), \
             mock.patch.object(perms, "_burnttoast_available", return_value=True):
            backend = perms._win_backend()
        self.assertTrue(backend["available"])
        self.assertTrue(backend["burnttoast"])
        self.assertIn("BurntToast", backend["detail"])

    def test_backend_without_burnttoast_falls_back_to_raw_winrt(self):
        perms = self.permissions
        with mock.patch.object(perms, "_has", return_value=True), \
             mock.patch.object(perms, "_burnttoast_available", return_value=False):
            backend = perms._win_backend()
        self.assertTrue(backend["available"])
        self.assertIn("raw WinRT", backend["detail"])

    def test_permission_follows_toastenabled_switch(self):
        perms = self.permissions
        cases = {1: "granted", 0: "denied", None: "unknown"}
        for value, expected in cases.items():
            with mock.patch.object(perms, "_read_registry", return_value=value):
                permission = perms._win_permission()
            self.assertEqual(permission["state"], expected)
            self.assertTrue(permission["detail"])

    def test_dnd_unknown_mentions_focus_settings_uri(self):
        dnd = self.permissions._win_dnd()
        self.assertEqual(dnd["state"], "unknown")
        self.assertIn("ms-settings:quiethours", dnd["detail"])

    def test_burnttoast_detected_on_module_path(self):
        perms = self.permissions
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            module_dir = Path(tmp) / "BurntToast"
            module_dir.mkdir()
            with mock.patch.dict(os.environ,
                                 {"PSModulePath": tmp + os.pathsep + "/nowhere"}):
                self.assertTrue(perms._burnttoast_available())
            with mock.patch.dict(os.environ, {"PSModulePath": "/nowhere"}):
                self.assertFalse(perms._burnttoast_available())


class CardsProbeTest(_McpCase):
    def test_cards_follow_the_notify_bridge(self):
        perms = self.permissions
        with mock.patch("voice.notify_bridge.resolve",
                        return_value={"status": "ok", "path": "/x"}):
            self.assertTrue(perms._cards()["available"])
        with mock.patch("voice.notify_bridge.resolve",
                        return_value={"status": "unavailable",
                                      "problem": "SEBAS_NOTIFY_PATH is not set"}):
            cards = perms._cards()
        self.assertFalse(cards["available"])
        self.assertEqual(cards["problem"], "SEBAS_NOTIFY_PATH is not set")

    def test_cards_probe_failure_is_reported_not_raised(self):
        perms = self.permissions

        def boom():
            raise RuntimeError("bridge down")

        with mock.patch("voice.notify_bridge.resolve", side_effect=boom):
            cards = perms._cards()
        self.assertFalse(cards["available"])
        self.assertIn("bridge down", cards["problem"])

    def test_next_step_names_the_missing_piece(self):
        perms = self.permissions
        ready = {"backend": {"available": True},
                 "permission": {"state": "granted"},
                 "dnd": {"state": "off"},
                 "cards": {"available": True}}
        self.assertIn("ready", perms._next_step(ready))
        denied = dict(ready, permission={"state": "denied"})
        self.assertIn("notification_request", perms._next_step(denied))
        bridgeless = dict(ready, cards={"available": False,
                                        "problem": "SEBAS_NOTIFY_PATH is not set"})
        self.assertIn("SEBAS_NOTIFY_PATH", perms._next_step(bridgeless))
        dnd_on = dict(ready, dnd={"state": "on"})
        self.assertIn("Do-Not-Disturb", perms._next_step(dnd_on))
        no_backend = dict(ready, backend={"available": False})
        self.assertIn("install", perms._next_step(no_backend).lower())


class RequestLinuxGuardTest(_McpCase):
    def test_linux_request_is_guidance_only_and_never_runs_anything(self):
        """THE guard: notification_request writes nothing on Linux — it runs
        no process at all, only returns the manual steps."""
        perms = self.permissions
        with mock.patch.object(perms, "os_name", return_value="linux"), \
             mock.patch.object(perms, "subprocess") as sub, \
             mock.patch.object(perms, "_run_rc") as run_rc, \
             mock.patch.object(perms, "_run") as run, \
             mock.patch.object(perms, "_open_uri") as open_uri, \
             mock.patch.object(perms, "_launch") as launch:
            result = self.tools.notification_request()
        for seam in (sub.run, sub.Popen, run_rc, run, open_uri, launch):
            seam.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "guidance")
        self.assertIs(result["settings_written"], False)
        self.assertTrue(result["guidance"])
        self.assertLessEqual(len(result["guidance"]), 5)
        self.assertTrue(result["next_step"])

    def test_request_never_claims_or_performs_a_write_on_any_os(self):
        perms = self.permissions
        for system in ("linux", "darwin", "win32"):
            with mock.patch.object(perms, "os_name", return_value=system), \
                 mock.patch.object(perms, "_has", return_value=False), \
                 mock.patch.object(perms, "_run_rc") as run_rc, \
                 mock.patch.object(perms, "_run") as run, \
                 mock.patch.object(perms, "_launch") as launch, \
                 mock.patch.object(perms, "_open_uri",
                                   return_value={"status": "ok", "uri": "x"}) as opener:
                result = self.tools.notification_request()
            run_rc.assert_not_called()       # request never probes/reads state
            run.assert_not_called()
            self.assertIs(result.get("settings_written"), False)
            self.assertTrue(result["next_step"])
            for call in list(launch.call_args_list) + list(opener.call_args_list):
                argv = call.args[0] if call.args else call.kwargs.get("uri")
                joined = " ".join(argv) if isinstance(argv, list) else str(argv)
                for forbidden in ("gsettings set", "dconf write", "reg add",
                                  "Set-ItemProperty", "defaults write"):
                    self.assertNotIn(forbidden, joined)


class RequestDarwinTest(_McpCase):
    def test_herald_triggers_the_standard_prompt(self):
        perms = self.permissions
        with mock.patch.object(perms, "os_name", return_value="darwin"), \
             mock.patch.object(perms, "_has", return_value=True), \
             mock.patch.object(perms, "_launch",
                               return_value={"status": "ok"}) as launch:
            result = self.tools.notification_request()
        launch.assert_called_once_with(["herald", "request-permission"])
        self.assertEqual(result["action"], "prompt_triggered")
        self.assertEqual(result["helper"], "herald")
        self.assertIs(result["settings_written"], False)

    def test_without_helper_the_settings_pane_opens(self):
        perms = self.permissions
        with mock.patch.object(perms, "os_name", return_value="darwin"), \
             mock.patch.object(perms, "_has", return_value=False), \
             mock.patch.object(perms, "_open_uri",
                               return_value={"status": "ok",
                                             "uri": perms.MACOS_SETTINGS_URI}) as opener:
            result = self.tools.notification_request()
        opener.assert_called_once_with(perms.MACOS_SETTINGS_URI)
        self.assertEqual(result["action"], "settings_opened")
        self.assertIn("System Settings > Notifications", result["next_step"])
        self.assertIs(result["settings_written"], False)

    def test_legacy_settings_uri_is_the_fallback(self):
        perms = self.permissions
        with mock.patch.object(perms, "os_name", return_value="darwin"), \
             mock.patch.object(perms, "_has", return_value=False), \
             mock.patch.object(perms, "_open_uri",
                               side_effect=[{"status": "error", "problem": "x"},
                                            {"status": "ok",
                                             "uri": perms.MACOS_SETTINGS_URI_LEGACY}]) as opener:
            result = self.tools.notification_request()
        self.assertEqual(opener.call_count, 2)
        opener.assert_any_call(perms.MACOS_SETTINGS_URI_LEGACY)
        self.assertEqual(result["action"], "settings_opened")

    def test_unavailable_when_nothing_can_be_opened(self):
        perms = self.permissions
        with mock.patch.object(perms, "os_name", return_value="darwin"), \
             mock.patch.object(perms, "_has", return_value=False), \
             mock.patch.object(perms, "_open_uri",
                               return_value={"status": "error",
                                             "problem": "no launcher"}):
            result = self.tools.notification_request()
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["problem"])
        self.assertTrue(result["next_step"])
        self.assertIs(result["settings_written"], False)


class RequestWindowsTest(_McpCase):
    def test_opens_ms_settings_notifications_and_mentions_focus(self):
        perms = self.permissions
        with mock.patch.object(perms, "os_name", return_value="win32"), \
             mock.patch.object(perms, "_open_uri",
                               return_value={"status": "ok",
                                             "uri": perms.WIN_SETTINGS_URI}) as opener:
            result = self.tools.notification_request()
        opener.assert_called_once_with(perms.WIN_SETTINGS_URI)
        self.assertEqual(result["action"], "settings_opened")
        self.assertIn("Focus Assist", result["next_step"])
        self.assertIn("ms-settings:quiethours", result["next_step"])
        self.assertIs(result["settings_written"], False)

    def test_unavailable_when_the_pane_would_not_open(self):
        perms = self.permissions
        with mock.patch.object(perms, "os_name", return_value="win32"), \
             mock.patch.object(perms, "_open_uri",
                               return_value={"status": "error",
                                             "problem": "no launcher"}):
            result = self.tools.notification_request()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("ms-settings:notifications", result["next_step"])

    def test_unknown_platform_reports_unknown(self):
        perms = self.permissions
        with mock.patch.object(perms, "os_name", return_value="plan9"):
            result = self.tools.notification_request()
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["problem"])
        self.assertIs(result["settings_written"], False)

    def test_request_crash_returns_internal_error_payload(self):
        perms = self.permissions

        def boom():
            raise RuntimeError("request exploded")

        with mock.patch.object(perms, "os_name", return_value="linux"), \
             mock.patch.object(perms, "_linux_request", side_effect=boom):
            result = self.tools.notification_request()
        self.assertEqual(result["status"], "internal_error")
        self.assertIn("request exploded", result["problem"])
        self.assertIs(result["settings_written"], False)


class SeamTest(_McpCase):
    def test_run_rc_reads_stdout_and_swallows_failures(self):
        perms = self.permissions
        done = types.SimpleNamespace(returncode=0, stdout=" true \n")
        with mock.patch.object(perms.subprocess, "run", return_value=done):
            self.assertEqual(perms._run_rc(["gsettings", "get"]), (0, "true"))
            self.assertEqual(perms._run(["gsettings", "get"]), "true")
        done_fail = types.SimpleNamespace(returncode=1, stdout="")
        with mock.patch.object(perms.subprocess, "run", return_value=done_fail):
            self.assertIsNone(perms._run(["gsettings", "get"]))
        with mock.patch.object(perms.subprocess, "run",
                               side_effect=OSError("no such binary")):
            self.assertEqual(perms._run_rc(["nope"]), (None, ""))
            self.assertIsNone(perms._run(["nope"]))

    def test_read_registry_is_read_only_and_defensive(self):
        perms = self.permissions
        calls = []
        fake_key = mock.MagicMock()
        fake_key.__enter__ = lambda s: fake_key
        fake_key.__exit__ = lambda s, *a: False

        def open_key(root, path, reserved, access):
            calls.append((path, access))
            return fake_key

        def query(key, name):
            if name == "Enabled":
                return 1, 4
            raise FileNotFoundError(name)

        fake_winreg = types.SimpleNamespace(
            HKEY_CURRENT_USER="HKCU", KEY_READ="KEY_READ",
            OpenKey=open_key, QueryValueEx=query)
        with mock.patch.dict(sys.modules, {"winreg": fake_winreg}):
            self.assertEqual(perms._read_registry("some\\path", "Enabled"), 1)
            self.assertIsNone(perms._read_registry("some\\path", "Missing"))
        self.assertTrue(calls)
        self.assertTrue(all(access == "KEY_READ" for _path, access in calls))

    def test_open_uri_launches_only_the_platform_launcher(self):
        perms = self.permissions
        with mock.patch.object(perms.sys, "platform", "darwin"), \
             mock.patch.object(perms.subprocess, "Popen") as popen:
            self.assertEqual(perms._open_uri("x-test:uri")["status"], "ok")
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["open", "x-test:uri"])

        with mock.patch.object(perms.sys, "platform", "win32"), \
             mock.patch.object(perms.os, "startfile", create=True) as startfile:
            self.assertEqual(perms._open_uri("ms-settings:notifications")["status"],
                             "ok")
        startfile.assert_called_once_with("ms-settings:notifications")

        with mock.patch.object(perms.sys, "platform", "linux"):
            result = perms._open_uri("x-test:uri")
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["problem"])

    def test_open_uri_reports_errors_without_raising(self):
        perms = self.permissions
        with mock.patch.object(perms.sys, "platform", "darwin"), \
             mock.patch.object(perms.subprocess, "Popen",
                               side_effect=OSError("no display")):
            result = perms._open_uri("x-test:uri")
        self.assertEqual(result["status"], "error")
        self.assertIn("no display", result["problem"])

    def test_gbool_parses_only_booleans(self):
        perms = self.permissions
        self.assertIs(perms._gbool("true"), True)
        self.assertIs(perms._gbool("False"), False)
        self.assertIsNone(perms._gbool("garbage"))
        self.assertIsNone(perms._gbool(None))


if __name__ == "__main__":
    unittest.main()
