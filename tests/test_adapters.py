"""Tests for the macOS and Windows notification adapters.

Everything is mocked: no subprocess is spawned, no socket is opened, no audio
is played and the live voice daemon is never touched. The adapters target
macOS/Windows and are never executed on this Linux machine — these tests
validate command construction, the click flow and the daemon payload shape.

Run:  python3 -m unittest tests.test_adapters -v   (or pytest tests/test_adapters.py)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from _support import af_unix_removed, serve_one  # noqa: E402


def _load(name: str, relpath: str):
    """Load an adapter module straight from its file.

    Why: notify/__init__.py is owned by another part of the project and may
    import platform code; the adapters must be testable standalone.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


macos = _load("notify_macos_under_test", "plugin/notify/macos.py")
windows = _load("notify_windows_under_test", "plugin/notify/windows.py")


class ContractTests(unittest.TestCase):
    """Both modules must match the agreed show_card contract exactly."""

    EXPECTED = [
        ("text", None),
        ("butler", "Sebas"),
        ("language", "en-us"),
        ("context", None),
        ("urgency", "critical"),
        ("play", True),
    ]

    def _check(self, module):
        import inspect
        sig = inspect.signature(module.show_card)
        params = list(sig.parameters.values())
        self.assertEqual([p.name for p in params], [n for n, _ in self.EXPECTED])
        for param, (_, default) in zip(params, self.EXPECTED):
            kind = inspect.Parameter.KEYWORD_ONLY if param.name != "text" \
                else inspect.Parameter.POSITIONAL_OR_KEYWORD
            self.assertEqual(param.kind, kind, param.name)
            if param.name != "text":
                self.assertEqual(param.default, default, param.name)
        self.assertIn("Ouvir mensagem", module.show_card.__doc__)
        self.assertIn("NEVER re-shown", module.show_card.__doc__)

    def test_macos_signature(self):
        self._check(macos)

    def test_windows_signature(self):
        self._check(windows)

    def test_labels_and_titles_per_language(self):
        for module in (macos, windows):
            self.assertEqual(module._label("en-us"), "Listen")
            self.assertEqual(module._label("pt-br"), "Ouvir mensagem")
            self.assertEqual(module._label("pt"), "Ouvir mensagem")     # alias
            self.assertEqual(module._label("en"), "Listen")             # alias
            self.assertEqual(module._label("de"), "Listen")             # fallback
            self.assertEqual(module._title("Sebas", "en-us"), "Sebas - message")
            self.assertEqual(module._title("Sebas", "pt-br"), "Sebas - mensagem")
            self.assertEqual(module._title("", "en-us"), "Sebas - message")

    def test_daemon_payload_shape(self):
        for module in (macos, windows):
            self.assertEqual(module._daemon_payload("olá"),
                             {"op": "speak", "text": "olá",
                              "confirmed": True, "play": True})

    def test_daemon_socket_matches_data_layout(self):
        """Both adapters resolve the daemon socket through the one rule
        (notify/paths.py): <data dir>/engine.sock, pre-1.0 legacy dir
        auto-detected."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
                for module in (macos, windows):
                    self.assertEqual(module._daemon_socket(),
                                     Path(tmp) / "sebas" / "engine.sock")
                (Path(tmp) / "voz").mkdir()          # legacy install
                for module in (macos, windows):
                    self.assertEqual(module._daemon_socket(),
                                     Path(tmp) / "voz" / "engine.sock")
                (Path(tmp) / "sebas").mkdir()        # new dir wins
                for module in (macos, windows):
                    self.assertEqual(module._daemon_socket(),
                                     Path(tmp) / "sebas" / "engine.sock")


class DaemonSendTests(unittest.TestCase):
    """One JSON line per request; failures are payloads, never exceptions.

    The fake daemon is a REAL listener bound through the same transport
    abstraction the adapters use (notify/transport.py), so these tests prove
    the wire over the AF_UNIX flavor AND over the loopback TCP + token
    fallback — the two flavors the CI Windows Python exercises. The
    'unreachable' semantics are asserted per transport."""

    def _round_trip(self, module, reply: dict, text: str):
        """One send against a one-shot fake daemon; returns (result, received)."""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "sebas"
            received, thread = serve_one(data, reply, transport=module._transport)
            result = module._send_play_request(text, timeout=5, data=data)
            thread.join(5)
        return result, received

    def _assert_unreachable(self, module):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "sebas"          # nothing is listening there
            result = module._send_play_request("hi", timeout=0.5, data=data)
        self.assertEqual(result["status"], "error")
        self.assertIn("unreachable", result["problem"])
        self.assertTrue(result["next_step"])

    def test_send_ok(self):
        for module in (macos, windows):
            with self.subTest(module=module.__name__):
                result, received = self._round_trip(
                    module, {"status": "ok", "played": True}, "mensagem completa")
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["played"])
                line = received["raw"].decode()
                self.assertTrue(line.endswith("\n"))
                self.assertEqual(json.loads(line),
                                 {"op": "speak", "text": "mensagem completa",
                                  "confirmed": True, "play": True})

    def test_send_ok_without_af_unix(self):
        """The CI Windows Python: no AF_UNIX, the loopback TCP + token
        transport carries the same request and the same reply."""
        with af_unix_removed():
            for module in (macos, windows):
                with self.subTest(module=module.__name__):
                    result, received = self._round_trip(
                        module, {"status": "ok", "played": True}, "mensagem completa")
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(json.loads(received["raw"]),
                                     {"op": "speak", "text": "mensagem completa",
                                      "confirmed": True, "play": True})

    def test_send_unreachable_is_error_payload(self):
        for module in (macos, windows):
            with self.subTest(module=module.__name__):
                self._assert_unreachable(module)

    def test_send_unreachable_is_error_payload_without_af_unix(self):
        with af_unix_removed():
            for module in (macos, windows):
                with self.subTest(module=module.__name__):
                    self._assert_unreachable(module)

    def test_send_non_ok_reply_is_error_payload(self):
        for module in (macos, windows):
            with self.subTest(module=module.__name__):
                result, _ = self._round_trip(
                    module, {"status": "generation_error", "problem": "x"}, "hi")
                self.assertEqual(result["status"], "error")
                self.assertTrue(result["next_step"])

    def test_send_socket_failure_is_an_error_payload_not_an_exception(self):
        def boom(*a, **k):
            raise OSError("socket layer exploded")

        with mock.patch.object(socket, "socket", boom):
            for module in (macos, windows):
                with self.subTest(module=module.__name__):
                    result = module._send_play_request("hi", timeout=0.1)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["next_step"])


class MacosCommandTests(unittest.TestCase):
    def test_terminal_notifier_command(self):
        cmd = macos.build_terminal_notifier_command(
            "/fake/terminal-notifier", text="Olá [urgente]", title="Sebas - mensagem",
            subtitle="agente X", label="Ouvir mensagem", group="sebas-card-abc")
        self.assertEqual(cmd[0], "/fake/terminal-notifier")
        self.assertEqual(cmd.count("-action"), 1)              # exactly ONE button
        self.assertEqual(cmd[cmd.index("-action") + 1], "Ouvir mensagem")
        self.assertEqual(cmd[cmd.index("-title") + 1], "Sebas - mensagem")
        self.assertEqual(cmd[cmd.index("-subtitle") + 1], "agente X")
        self.assertEqual(cmd[cmd.index("-group") + 1], "sebas-card-abc")
        self.assertEqual(cmd[cmd.index("-timeout") + 1], str(macos.AUTO_CLOSE_SECONDS))
        # NSUserDefaults plist escaping applies to the first character only
        self.assertEqual(cmd[cmd.index("-message") + 1], "Olá [urgente]")

    def test_terminal_notifier_command_escapes_leading_metachar(self):
        self.assertEqual(macos._tn_escape("[ZOMG] fire"), "\\[ZOMG] fire")
        self.assertEqual(macos._tn_escape("(hi)"), "\\(hi)")
        self.assertEqual(macos._tn_escape("plain"), "plain")
        self.assertEqual(macos._tn_escape(""), "")
        cmd = macos.build_terminal_notifier_command(
            "tn", text="{dict}", title="t", subtitle=None, label="Listen", group="g")
        self.assertEqual(cmd[cmd.index("-message") + 1], "\\{dict}")
        self.assertNotIn("-subtitle", cmd)                      # omitted when None

    def test_herald_command_urgency(self):
        critical = macos.build_herald_command(
            "herald", text="m", title="t", subtitle="s", label="Listen",
            urgency="critical")
        normal = macos.build_herald_command(
            "herald", text="m", title="t", subtitle=None, label="Listen",
            urgency="normal")
        self.assertEqual(critical[critical.index("--level") + 1], "timeSensitive")
        self.assertEqual(normal[normal.index("--level") + 1], "active")
        self.assertEqual(critical[critical.index("--actions") + 1], "Listen")
        self.assertEqual(critical[critical.index("--timeout") + 1],
                         str(macos.AUTO_CLOSE_SECONDS))
        self.assertNotIn("--subtitle", normal)


class MacosClickFlowTests(unittest.TestCase):
    def _proc(self, output: str):
        return SimpleNamespace(stdout=io.StringIO(output))

    def test_button_click_plays_full_text_and_closes(self):
        with mock.patch.object(macos, "_send_play_request",
                               return_value={"status": "ok"}) as send, \
             mock.patch.object(macos, "_close_group") as close:
            result = macos._watch_terminal_notifier(
                self._proc("Listen\n"), binary="tn", text="full message here",
                label="Listen", group="sebas-card-1", play=True)
        self.assertEqual(result["status"], "ok")
        send.assert_called_once_with("full message here")
        close.assert_called_once_with("tn", "sebas-card-1")      # closes after playback

    def test_timeout_autocloses_without_playing(self):
        with mock.patch.object(macos, "_send_play_request",
                               side_effect=AssertionError("must not play")) as send, \
             mock.patch.object(macos, "_close_group") as close:
            result = macos._watch_terminal_notifier(
                self._proc("@TIMEOUT\n"), binary="tn", text="t",
                label="Listen", group="sebas-card-2", play=True)
        self.assertEqual(result["status"], "closed")
        send.assert_not_called()
        close.assert_called_once_with("tn", "sebas-card-2")      # auto-close

    def test_body_click_or_close_never_plays(self):
        for outcome in ("@ACTIONCLICKED", "@CLOSED"):
            with mock.patch.object(macos, "_send_play_request",
                                   side_effect=AssertionError("must not play")), \
                 mock.patch.object(macos, "_close_group") as close:
                result = macos._watch_terminal_notifier(
                    self._proc(outcome + "\n"), binary="tn", text="t",
                    label="Listen", group="g", play=True)
            self.assertEqual(result["status"], "closed")
            close.assert_not_called()                          # macOS already closed it

    def test_dry_run_never_touches_daemon(self):
        with mock.patch.object(macos, "_send_play_request",
                               side_effect=AssertionError("must not connect")):
            result = macos._handle_click("t", play=False)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["dry_run"])

    def test_dry_run_click_still_closes_card(self):
        with mock.patch.object(macos, "_close_group") as close:
            result = macos._watch_terminal_notifier(
                self._proc("Listen\n"), binary="tn", text="t",
                label="Listen", group="g", play=False)          # dry: no daemon
        self.assertTrue(result["dry_run"])
        close.assert_called_once_with("tn", "g")

    def test_daemon_failure_returns_error_with_next_step(self):
        with mock.patch.object(macos, "_send_play_request",
                               return_value={"status": "error",
                                             "problem": "daemon down",
                                             "next_step": "start the daemon"}):
            result = macos._handle_click("t", play=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["problem"], "daemon down")
        self.assertEqual(result["next_step"], "start the daemon")


class MacosShowCardTests(unittest.TestCase):
    def test_shows_with_terminal_notifier(self):
        fake_proc = SimpleNamespace(stdout=io.StringIO(""))
        with mock.patch.object(macos, "find_terminal_notifier",
                               return_value="/fake/tn"), \
             mock.patch.object(macos, "find_herald"), \
             mock.patch.object(macos, "_probe_daemon",
                               return_value={"status": "ok"}), \
             mock.patch.object(macos, "_close_group"), \
             mock.patch.object(macos.subprocess, "Popen",
                               return_value=fake_proc) as popen:
            result = macos.show_card("mensagem", butler="Sebas",
                                     language="pt-br", context="agente X",
                                     urgency="critical", play=True)
        self.assertEqual(result["status"], "shown")
        self.assertEqual(result["backend"], "terminal-notifier")
        self.assertEqual(result["button"], "Ouvir mensagem")
        self.assertEqual(result["title"], "Sebas - mensagem")
        self.assertTrue(result["group"].startswith("sebas-card-"))
        self.assertEqual(result["daemon"], {"status": "ok"})
        argv = popen.call_args[0][0]
        self.assertEqual(argv.count("-action"), 1)
        self.assertEqual(argv[argv.index("-action") + 1], "Ouvir mensagem")

    def test_falls_back_to_herald(self):
        fake_proc = SimpleNamespace(stdout=io.StringIO(""))
        with mock.patch.object(macos, "find_terminal_notifier", return_value=None), \
             mock.patch.object(macos, "find_herald", return_value="/fake/herald"), \
             mock.patch.object(macos, "_probe_daemon",
                               return_value={"status": "ok"}), \
             mock.patch.object(macos, "_close_group"), \
             mock.patch.object(macos.subprocess, "Popen",
                               return_value=fake_proc) as popen:
            result = macos.show_card("t", urgency="critical")
        self.assertEqual(result["status"], "shown")
        self.assertEqual(result["backend"], "herald")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[argv.index("--level") + 1], "timeSensitive")

    def test_no_tool_is_error_with_next_step(self):
        with mock.patch.object(macos, "find_terminal_notifier", return_value=None), \
             mock.patch.object(macos, "find_herald", return_value=None):
            result = macos.show_card("t")
        self.assertEqual(result["status"], "error")
        self.assertIn("action button", result["problem"])
        self.assertIn("terminal-notifier", result["next_step"])

    def test_dry_run_never_probes_daemon(self):
        fake_proc = SimpleNamespace(stdout=io.StringIO(""))
        with mock.patch.object(macos, "find_terminal_notifier",
                               return_value="/fake/tn"), \
             mock.patch.object(macos, "_probe_daemon",
                               side_effect=AssertionError("must not touch daemon")), \
             mock.patch.object(macos, "_close_group"), \
             mock.patch.object(macos.subprocess, "Popen", return_value=fake_proc):
            result = macos.show_card("t", play=False)
        self.assertEqual(result["status"], "shown")
        self.assertTrue(result["dry_run"])

    def test_usage_errors_are_payloads(self):
        self.assertEqual(macos.show_card("   ")["status"], "error")
        self.assertEqual(macos.show_card("t", urgency="loud")["status"], "error")

    def test_low_urgency_is_normal(self):
        """The dispatcher's 'low' level must not fail; macOS treats it as normal."""
        fake_proc = SimpleNamespace(stdout=io.StringIO(""))
        with mock.patch.object(macos, "find_terminal_notifier",
                               return_value="/fake/tn"), \
             mock.patch.object(macos, "_probe_daemon",
                               return_value={"status": "ok"}), \
             mock.patch.object(macos, "_close_group"), \
             mock.patch.object(macos.subprocess, "Popen", return_value=fake_proc):
            result = macos.show_card("t", urgency="low")
        self.assertEqual(result["status"], "shown")
        self.assertEqual(result["urgency"], "normal")


class WindowsXmlTests(unittest.TestCase):
    def test_toast_xml_one_button_and_label(self):
        xml = windows.build_toast_xml(
            title="Sebas - message", subtitle="agent X", body="hello & <bye>",
            label="Listen", uri="sebascard://play/abc", urgent=False)
        self.assertEqual(xml.count("<action "), 1)
        self.assertIn('content="Listen"', xml)
        self.assertIn('activationType="protocol"', xml)
        self.assertIn('afterActivationBehavior="default"', xml)   # closes on click
        self.assertNotIn("scenario", xml)                        # normal urgency
        self.assertIn("hello &amp; &lt;bye&gt;", xml)            # XML escaping
        self.assertIn("<text>agent X</text>", xml)

    def test_toast_xml_urgent(self):
        xml = windows.build_toast_xml(
            title="t", subtitle=None, body="b", label="Ouvir mensagem",
            uri="sebascard://play/abc", urgent=True)
        self.assertIn('scenario="urgent"', xml)                  # critical mapping
        self.assertIn('duration="long"', xml)
        self.assertIn('content="Ouvir mensagem"', xml)

    def test_ps_quoting(self):
        self.assertEqual(windows._ps_quote("it's"), "'it''s'")
        self.assertEqual(windows._ps_quote("a\nb"), "'a\nb'")

    def test_toast_xml_escapes_quotes_in_attributes(self):
        """saxutils.escape alone leaves `"` raw: it must never break out of an
        attribute value (title/subtitle/label/URI)."""
        xml = windows.build_toast_xml(
            title='He said "hi"', subtitle='sub "x"', body='b "y"',
            label='Say "yes"', uri='sebascard://play/tok"en', urgent=False)
        self.assertIn("&quot;", xml)
        content = re.search(r'content="([^"]*)"', xml).group(1)
        arguments = re.search(r'arguments="([^"]*)"', xml).group(1)
        self.assertEqual(content, "Say &quot;yes&quot;")
        self.assertEqual(arguments, "sebascard://play/tok&quot;en")
        self.assertIn("He said &quot;hi&quot;", xml)       # text nodes too
        self.assertNotIn('He said "hi"', xml)


class WindowsScriptTests(unittest.TestCase):
    def _script(self, urgent):
        return windows.build_powershell_script(
            title="Sebas - message", subtitle=None, body="msg",
            label="Listen", uri="sebascard://play/abc", urgent=urgent)

    def test_burnttoast_block(self):
        script = self._script(urgent=True)
        self.assertIn("Import-Module BurntToast", script)
        self.assertIn("New-BTButton -Content 'Listen'", script)
        self.assertIn("-ActivationType Protocol", script)
        self.assertIn("-ExpirationTime (Get-Date).AddSeconds(300)", script)
        self.assertIn("-Urgent", script)                         # critical mapping
        self.assertIn("catch {", script)                         # raw fallback
        self.assertIn(windows.POWERSHELL_APP_ID, script)

    def test_normal_has_no_urgent_flags(self):
        script = self._script(urgent=False)
        self.assertNotIn("-Urgent", script)
        self.assertNotIn("ToastNotificationPriority]::High", script)
        self.assertNotIn("scenario=\"urgent\"", script)

    def test_raw_fallback_sets_priority_for_critical(self):
        script = self._script(urgent=True)
        self.assertIn("ToastNotificationPriority]::High", script)
        self.assertIn("CreateToastNotifier", script)


class WindowsClickFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ,
                                  {"SEBAS_CARD_TOKEN_DIR": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_uri_carries_a_token_never_the_text(self):
        text = 'tudo bem, "amigo" & <fim> — ação'
        token = windows._mint_token(text)
        uri = windows.card_uri(token)
        self.assertTrue(uri.startswith("sebascard://play/"))
        self.assertEqual(uri[len("sebascard://play/"):], token)
        self.assertNotIn(text, uri)                # text never in the URI

    def test_play_uri_sends_full_text(self):
        uri = windows.card_uri(windows._mint_token("full message"))
        with mock.patch.object(windows, "_send_play_request",
                               return_value={"status": "ok"}) as send:
            result = windows._play_from_uri(uri)
        self.assertEqual(result["status"], "ok")
        send.assert_called_once_with("full message")

    def test_dry_uri_never_touches_daemon_or_the_store(self):
        uri = windows.card_uri(windows._new_token(), dry=True)
        with mock.patch.object(windows, "_send_play_request",
                               side_effect=AssertionError("must not connect")), \
             mock.patch.object(socket, "socket",
                               side_effect=AssertionError("must not connect")):
            result = windows._play_from_uri(uri)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["dry_run"])
        self.assertEqual(os.listdir(self._tmp.name), [])   # zero side effects

    def test_bad_uri_is_error_payload(self):
        result = windows._play_from_uri("https://example.com/x")
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["next_step"])

    def test_handle_click_dry_run(self):
        with mock.patch.object(windows, "_send_play_request",
                               side_effect=AssertionError("must not connect")):
            result = windows._handle_click("t", play=False)
        self.assertTrue(result["dry_run"])


class WindowsShowCardTests(unittest.TestCase):
    def setUp(self):
        # show_card mints a capability token even with everything downstream
        # mocked. The store must land in a throwaway dir: the default falls
        # back to the OS temp dir, which can resolve as relatively as the CWD
        # itself (see test_no_cwd_artifacts.py).
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ,
                                  {"SEBAS_CARD_TOKEN_DIR": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shows_and_registers_handler(self):
        with mock.patch.object(windows, "_ensure_protocol_handler",
                               return_value={"status": "ok"}) as reg, \
             mock.patch.object(windows, "_find_powershell", return_value="pwsh"), \
             mock.patch.object(windows, "_probe_daemon",
                               return_value={"status": "ok"}), \
             mock.patch.object(windows.subprocess, "Popen") as popen:
            result = windows.show_card("mensagem", language="pt-br",
                                       urgency="critical", context="agente X")
        self.assertEqual(result["status"], "shown")
        self.assertEqual(result["button"], "Ouvir mensagem")
        self.assertEqual(result["urgency"], "critical")
        reg.assert_called_once_with(write=True)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "pwsh")
        script = argv[-1]
        self.assertIn("-Command", argv)
        self.assertIn("'Ouvir mensagem'", script)
        self.assertIn("-Urgent", script)

    def test_dry_run_marks_uri_and_never_probes_daemon(self):
        with mock.patch.object(windows, "_ensure_protocol_handler",
                               return_value={"status": "ok"}), \
             mock.patch.object(windows, "_find_powershell", return_value="pwsh"), \
             mock.patch.object(windows, "_probe_daemon",
                               side_effect=AssertionError("must not touch daemon")), \
             mock.patch.object(windows.subprocess, "Popen") as popen:
            result = windows.show_card("t", play=False)
        self.assertEqual(result["status"], "shown")
        self.assertTrue(result["dry_run"])
        script = popen.call_args[0][0][-1]
        self.assertIn("sebascard://play/d/", script)              # dry marker

    def test_registration_failure_is_error(self):
        with mock.patch.object(windows, "_ensure_protocol_handler",
                               return_value={"status": "error",
                                             "problem": "registry blocked",
                                             "next_step": "register manually"}):
            result = windows.show_card("t")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["problem"], "registry blocked")
        self.assertEqual(result["next_step"], "register manually")

    def test_no_powershell_is_error(self):
        with mock.patch.object(windows, "_ensure_protocol_handler",
                               return_value={"status": "ok"}), \
             mock.patch.object(windows, "_find_powershell", return_value=None):
            result = windows.show_card("t")
        self.assertEqual(result["status"], "error")
        self.assertIn("PowerShell", result["problem"])

    def test_usage_errors_are_payloads(self):
        self.assertEqual(windows.show_card("")["status"], "error")
        self.assertEqual(windows.show_card("t", urgency="loud")["status"], "error")

    def test_low_urgency_is_normal(self):
        """The dispatcher's 'low' level must not fail; Windows treats it as normal."""
        with mock.patch.object(windows, "_ensure_protocol_handler",
                               return_value={"status": "ok"}), \
             mock.patch.object(windows, "_find_powershell", return_value="pwsh"), \
             mock.patch.object(windows, "_probe_daemon",
                               return_value={"status": "ok"}), \
             mock.patch.object(windows.subprocess, "Popen") as popen:
            result = windows.show_card("t", urgency="low")
        self.assertEqual(result["status"], "shown")
        self.assertEqual(result["urgency"], "normal")
        self.assertNotIn("-Urgent", popen.call_args[0][0][-1])


class CliTests(unittest.TestCase):
    def test_windows_cli_play_uri(self):
        uri = windows.card_uri(windows._new_token(), dry=True)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = windows.main(["--play-uri", uri])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "ok")

    def test_windows_cli_requires_text(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = windows.main([])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "error")

    def test_macos_cli_no_play(self):
        fake_proc = SimpleNamespace(stdout=io.StringIO(""))
        with mock.patch.object(macos, "find_terminal_notifier",
                               return_value="/fake/tn"), \
             mock.patch.object(macos, "_close_group"), \
             mock.patch.object(macos.subprocess, "Popen", return_value=fake_proc), \
             mock.patch.object(macos.time, "sleep"), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = macos.main(["--text", "hi", "--no-play"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "shown")
        self.assertTrue(payload["dry_run"])


if __name__ == "__main__":
    unittest.main()
