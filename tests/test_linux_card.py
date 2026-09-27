"""Linux card lifecycle: one show, one close, playback only on the button.

Everything is faked (gi, GLib, Notify, daemon socket call): no D-Bus, no
notification server, no audio. What is asserted is the behavior the phase
requires — single show (no re-banner), close after playback, close after the
timeout, and a dry run that never reaches the daemon.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

import notify.linux as linux
from _support import ACTION_KEY, FakeUrgency, fake_gi


class LinuxCardLifecycleTest(unittest.TestCase):
    def test_click_plays_full_text_then_closes(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch.object(linux, "play_via_daemon",
                                   return_value={"status": "ok", "played": True}) as play:
                result = linux.show_card("Full agent message.", butler="Sebas",
                                         language="pt-br", context="Agente X",
                                         play=True)
                self.assertEqual(result["status"], "shown")
                self.assertEqual(result["title"], "Sebas - mensagem")
                self.assertEqual(result["button"], "Ouvir mensagem")
                self.assertEqual(result["urgency"], "critical")
                card = notify_mod.created[0]
                card.invoke(ACTION_KEY)          # ActionInvoked(id, "listen")
                self.assertTrue(card.closed.wait(5))

            play.assert_called_once()
            self.assertEqual(play.call_args.args[0], "Full agent message.")
            self.assertEqual(play.call_args.kwargs["context"], "Agente X")
            self.assertEqual(card.show_calls, 1)    # no re-banner
            self.assertEqual(card.close_calls, 1)   # exactly one close

    def test_card_contents_urgency_and_single_button(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch.object(linux, "play_via_daemon"):
                linux.show_card("Full agent message.", butler="Sebas",
                                language="en-us", context="Agente X",
                                urgency="critical", play=False)
                card = notify_mod.created[0]
        self.assertEqual(card.summary, "Sebas - message")
        self.assertEqual(card.body, "<b>Agente X</b>\nFull agent message.")
        self.assertEqual(card.urgency, FakeUrgency.CRITICAL)
        self.assertEqual(card.timeout, 0)          # NOTIFY_EXPIRES_NEVER
        self.assertEqual(len(card.actions), 1)     # exactly ONE button
        self.assertEqual(card.actions[0][0], ACTION_KEY)
        self.assertEqual(card.actions[0][1], "Listen")

    def test_body_escapes_markup_and_keeps_context_first(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch.object(linux, "play_via_daemon"):
                linux.show_card("a < b & c", context="Agente <X>", play=False)
                card = notify_mod.created[0]
        self.assertEqual(card.body, "<b>Agente &lt;X&gt;</b>\na &lt; b &amp; c")

    def test_timeout_closes_without_playback(self):
        with fake_gi() as (notify_mod, glib):
            with mock.patch.object(linux, "play_via_daemon") as play:
                result = linux.show_card("Full agent message.", play=True)
                self.assertEqual(result["status"], "shown")
                card = notify_mod.created[0]
                seconds, fire = glib.timers[0]
                self.assertEqual(seconds, linux.CARD_TIMEOUT_SECONDS)
                fire()                             # the self-close timer
                self.assertTrue(card.closed.wait(5))
            play.assert_not_called()
            self.assertEqual(card.show_calls, 1)    # shown once, never re-shown
            self.assertEqual(card.close_calls, 1)

    def test_second_click_is_ignored_and_close_stays_single(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch.object(linux, "play_via_daemon",
                                   return_value={"status": "ok"}) as play:
                linux.show_card("Full agent message.", play=True)
                card = notify_mod.created[0]
                card.invoke(ACTION_KEY)
                self.assertTrue(card.closed.wait(5))
                card.invoke(ACTION_KEY)
            play.assert_called_once()
            self.assertEqual(card.close_calls, 1)

    def test_dry_run_never_touches_the_daemon(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch.object(linux, "play_via_daemon") as play:
                result = linux.show_card("Full agent message.", play=False)
                self.assertEqual(result["status"], "shown")
                self.assertFalse(result["play"])
                card = notify_mod.created[0]
                card.invoke(ACTION_KEY)
                self.assertTrue(card.closed.wait(5))
            play.assert_not_called()
            self.assertEqual(card.close_calls, 1)

    def test_missing_gi_is_an_error_payload(self):
        with mock.patch.dict(sys.modules, {"gi": None}):
            result = linux.show_card("Full agent message.", play=False)
        self.assertEqual(result["status"], "error")
        self.assertIn("python3-gi", result["problem"])

    def test_empty_text_is_rejected(self):
        result = linux.show_card("   ", play=False)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["problem"], "empty text")


class LinuxStackedCardsTest(unittest.TestCase):
    """Stacked cards must be independent; Notify.init is process-global."""

    def test_stacked_cards_get_independent_loops_and_single_init(self):
        with fake_gi() as (notify_mod, glib):
            with mock.patch.object(linux, "play_via_daemon"):
                first = linux.show_card("one", play=False)
                second = linux.show_card("two", play=False)
                self.assertEqual(first["status"], "shown")
                self.assertEqual(second["status"], "shown")
                self.assertEqual(len(glib.loops), 2)
                self.assertIsNot(glib.loops[0], glib.loops[1])
                self.assertEqual(len(glib.contexts), 2)
                self.assertIsNot(glib.contexts[0], glib.contexts[1])
                self.assertIs(glib.loops[0].context, glib.contexts[0])
                self.assertIs(glib.loops[1].context, glib.contexts[1])
        self.assertEqual(notify_mod.inits, 1)     # once per process
        self.assertEqual(notify_mod.uninits, 0)   # never per card

    def test_playback_failure_still_closes_the_card(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch.object(linux, "play_via_daemon",
                                   side_effect=RuntimeError("boom")):
                linux.show_card("Full agent message.", play=True)
                card = notify_mod.created[0]
                card.invoke(ACTION_KEY)
                self.assertTrue(card.closed.wait(5))
        self.assertEqual(card.close_calls, 1)     # closed despite the failure


if __name__ == "__main__":
    unittest.main()
