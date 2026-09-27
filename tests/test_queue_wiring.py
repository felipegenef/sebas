"""Queue wiring in the voice MCP: the card replaces the spoken short notice.

Busy-at-arrival semantics: only a message that arrives WHILE another voice
request is being processed is parked (card, no auto-play); an idle daemon
plays right away with no card and no notice.

Runs against the voice MCP checkout located by SEBAS_MCP_ROOT (skipped when it
is not set). Everything the tests touch is mocked: the daemon request, the
engine, the notification bridge, synthesis and playback — no socket, no
audio.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from _support import mcp_root

CARD_REPLY = {
    "status": "awaiting_confirmation",
    "card": True,
    "notification_only": True,
    "text": "Full message",
    "context": "Agente X",
    "played": False,
    "next_step": ("This message arrived while another message was being "
                  "spoken, so it was PARKED: nothing was played and it will "
                  "NOT be played automatically."),
}
OLD_AWAITING = {
    "status": "awaiting_confirmation",
    "notification_only": True,
    "next_step": "Ask the user and wait for the answer.",
}


class _McpCase(unittest.TestCase):
    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import tools
        from voice import daemon
        self.tools = tools
        self.daemon = daemon


class SpeakCardWiringTest(_McpCase):
    def test_parked_message_shows_card_no_spoken_notice(self):
        captured = {}

        def fake_request(payload):
            captured["payload"] = payload
            return dict(CARD_REPLY)

        with mock.patch.object(self.tools.notify_bridge, "available", return_value=True), \
             mock.patch.object(self.tools.notify_bridge, "show_card",
                               return_value={"status": "shown"}) as show, \
             mock.patch.object(self.tools, "_daemon_request", side_effect=fake_request), \
             mock.patch.object(self.tools.engine, "speak",
                               side_effect=AssertionError("spoken notice must not happen")), \
             mock.patch.object(self.tools.core, "load_config", return_value={"play": True}), \
             mock.patch.object(self.tools.core, "get_butler_name", return_value="Sebas"), \
             mock.patch.object(self.tools.core, "get_language", return_value="pt-br"):
            result = self.tools.speak("Full message", play=True, context="Agente X")

        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["card"], "shown")
        self.assertIn("Do NOT ask", result["next_step"])
        self.assertIn("NOT be played automatically", result["next_step"])
        self.assertTrue(captured["payload"]["card"])
        self.assertFalse(captured["payload"]["confirmed"])
        self.assertEqual(captured["payload"]["text"], "Full message")
        show.assert_called_once()
        self.assertEqual(show.call_args.kwargs["text"], "Full message")
        self.assertEqual(show.call_args.kwargs["butler"], "Sebas")
        self.assertEqual(show.call_args.kwargs["language"], "pt-br")
        self.assertEqual(show.call_args.kwargs["context"], "Agente X")
        self.assertEqual(show.call_args.kwargs["urgency"], "critical")

    def test_card_unavailable_keeps_the_old_flow(self):
        with mock.patch.object(self.tools.notify_bridge, "available", return_value=False), \
             mock.patch.object(self.tools, "_daemon_request",
                               side_effect=AssertionError("card mode must not be requested")), \
             mock.patch.object(self.tools.engine, "speak",
                               return_value=dict(OLD_AWAITING)) as old:
            result = self.tools.speak("Full message", play=True, context="Agente X")

        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertNotIn("card", result)
        self.assertEqual(result["next_step"], OLD_AWAITING["next_step"])
        old.assert_called_once()
        self.assertFalse(old.call_args.kwargs["confirmed"])

    def test_card_failure_falls_back_to_the_spoken_notice(self):
        calls = []

        def fake_request(payload):
            calls.append(payload)
            if payload.get("notice_only"):
                return {"status": "awaiting_confirmation",
                        "notification_only": True, "played": True,
                        "next_step": "Ask the user and wait for the answer."}
            return dict(CARD_REPLY)

        with mock.patch.object(self.tools.notify_bridge, "available", return_value=True), \
             mock.patch.object(self.tools.notify_bridge, "show_card",
                               return_value={"status": "unavailable",
                                             "problem": "bridge error"}), \
             mock.patch.object(self.tools, "_daemon_request", side_effect=fake_request), \
             mock.patch.object(self.tools.core, "load_config", return_value={"play": True}), \
             mock.patch.object(self.tools.engine, "speak",
                               side_effect=AssertionError(
                                   "the full text must never be re-sent unconfirmed")):
            result = self.tools.speak("Full message", play=True, context="Agente X")

        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["card"], "unavailable")
        self.assertEqual(result["card_problem"], "bridge error")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["card"])                  # the parked request
        self.assertTrue(calls[1]["notice_only"])           # short notice only
        self.assertFalse(calls[1]["confirmed"])
        self.assertEqual(calls[1]["text"], "Full message")

    def test_confirmed_call_never_consults_the_card(self):
        with mock.patch.object(self.tools.notify_bridge, "available",
                               side_effect=AssertionError("must not be consulted")), \
             mock.patch.object(self.tools.engine, "speak",
                               return_value={"status": "ok", "played": True}) as speak:
            result = self.tools.speak("Full message", play=True, confirmed=True)

        speak.assert_called_once()
        self.assertTrue(speak.call_args.kwargs["confirmed"])
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("card", result)

    def test_unreachable_daemon_falls_back_to_engine(self):
        with mock.patch.object(self.tools.notify_bridge, "available", return_value=True), \
             mock.patch.object(self.tools, "_daemon_request", return_value=None), \
             mock.patch.object(self.tools.core, "load_config", return_value={"play": True}), \
             mock.patch.object(self.tools.engine, "speak",
                               return_value=dict(OLD_AWAITING)) as old:
            result = self.tools.speak("Full message", play=True, context="Agente X")

        old.assert_called_once()
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertNotIn("card", result)


class DaemonCardModeTest(_McpCase):
    """The daemon decides on busy-at-arrival: park, short notice, or play."""

    def test_busy_with_card_parks_without_synthesis(self):
        with mock.patch.object(self.daemon.core, "generate",
                               side_effect=AssertionError("must not synthesize")), \
             mock.patch.object(self.daemon.core, "play_file",
                               side_effect=AssertionError("must not play")):
            reply = self.daemon._handle("speak", {"text": "Full message", "card": True,
                                                  "context": "Agente X"}, busy=True)
        self.assertEqual(reply["status"], "awaiting_confirmation")
        self.assertTrue(reply["card"])
        self.assertTrue(reply["notification_only"])
        self.assertEqual(reply["text"], "Full message")
        self.assertFalse(reply["played"])
        self.assertIn("Do NOT ask", reply["next_step"])

    def test_idle_at_arrival_plays_the_full_message_no_card(self):
        with mock.patch.object(self.daemon.core, "load_config", return_value={"play": True}), \
             mock.patch.object(self.daemon.core, "generate",
                               return_value=("/tmp/x.wav", 22050, 1.0, "kokoro test")) as gen, \
             mock.patch.object(self.daemon.core, "play_file", return_value=True) as play:
            reply = self.daemon._handle("speak", {"text": "Full message", "card": True,
                                                  "play": True}, busy=False)
        self.assertEqual(reply["status"], "ok")
        self.assertEqual(gen.call_args.args[0], "Full message")
        self.assertTrue(reply["played"])
        play.assert_called_once()
        self.assertNotIn("card", reply)
        self.assertNotIn("next_step", reply)

    def test_busy_without_card_speaks_the_short_notice(self):
        with mock.patch.object(self.daemon.core, "get_language", return_value="pt-br"), \
             mock.patch.object(self.daemon.core, "user_greeting", return_value=""), \
             mock.patch.object(self.daemon.core, "load_config", return_value={"play": True}), \
             mock.patch.object(self.daemon.core, "generate",
                               return_value=("/tmp/x.wav", 22050, 1.0, "kokoro test")) as gen, \
             mock.patch.object(self.daemon.core, "play_file", return_value=True):
            reply = self.daemon._handle("speak", {"text": "Full message",
                                                  "context": "Agente X",
                                                  "play": True}, busy=True)
        self.assertEqual(reply["status"], "awaiting_confirmation")
        self.assertTrue(reply["notification_only"])
        self.assertNotIn("card", reply)
        notice = gen.call_args.args[0]
        self.assertNotEqual(notice, "Full message")
        self.assertIn("confirme", notice.lower())

    def test_confirmed_while_busy_plays_the_full_message_in_turn(self):
        with mock.patch.object(self.daemon.core, "load_config", return_value={"play": True}), \
             mock.patch.object(self.daemon.core, "generate",
                               return_value=("/tmp/x.wav", 22050, 1.0, "kokoro test")) as gen, \
             mock.patch.object(self.daemon.core, "play_file", return_value=True):
            reply = self.daemon._handle("speak", {"text": "Full message", "card": True,
                                                  "confirmed": True, "play": True},
                                        busy=True)
        self.assertEqual(reply["status"], "ok")
        self.assertEqual(gen.call_args.args[0], "Full message")
        self.assertTrue(reply["played"])
        self.assertNotIn("card", reply)

    def test_notice_only_never_synthesizes_or_plays_the_full_text(self):
        """notice_only forces the short notice REGARDLESS of busy state: an
        idle daemon must never play a parked message unconfirmed. The spy on
        core.play_file (side_effect AssertionError) makes any playback of the
        full text's wav an automatic failure."""
        generated = {}                                  # wav path -> text

        def generate(text, cfg):
            path = f"/tmp/notice_{len(generated)}.wav"
            generated[path] = text
            return (path, 22050, 1.0, "kokoro test")

        def play_file(path):
            if generated.get(str(path)) == "Full message":
                raise AssertionError("the full text must never be played unconfirmed")
            return True

        for busy in (False, True):
            with self.subTest(busy=busy), \
                 mock.patch.object(self.daemon.core, "load_config",
                                   return_value={"play": True}), \
                 mock.patch.object(self.daemon.core, "get_language",
                                   return_value="pt-br"), \
                 mock.patch.object(self.daemon.core, "user_greeting",
                                   return_value=""), \
                 mock.patch.object(self.daemon.core, "generate",
                                   side_effect=generate), \
                 mock.patch.object(self.daemon.core, "play_file",
                                   side_effect=play_file):
                reply = self.daemon._handle("speak", {"text": "Full message",
                                                      "context": "Agente X",
                                                      "play": True,
                                                      "confirmed": False,
                                                      "notice_only": True},
                                            busy=busy)
            self.assertEqual(reply["status"], "awaiting_confirmation")
            self.assertTrue(reply["notification_only"])
            self.assertNotIn("Full message", generated.values())   # never made
            notice = list(generated.values())[-1]
            self.assertIn("confirme", notice.lower())              # the notice


if __name__ == "__main__":
    unittest.main()
