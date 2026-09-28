"""Regression tests against the REAL GLib main loop — no Notify, no D-Bus.

QA found that stacked cards sharing the process default GLib context block
each other: card B's loop would not run until card A's loop quit, a click
could freeze every card's dispatch for the whole daemon timeout, and a quit
arriving before a blocked loop started was LOST (leaked thread). The
`_support.FakeGLibModule` gives every card an independent loop and therefore
masks that class of bug — so the loop/thread semantics are pinned here against
the real GLib, with a fake Notify module.

Hard rules for this file: real GLib ONLY. No Notify import, no D-Bus, no
notification server, no daemon socket, no audio.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest import mock

# Host-truth import plumbing: os.path strings, never pathlib — the CWD
# guard's simulation flips pathlib (test_no_cwd_artifacts.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "plugin"))
sys.path.insert(0, _HERE)   # for _support

import notify.linux as linux
from _support import ACTION_KEY, FakeNotifyModule

try:
    import gi  # noqa: F401  (GLib only: never import Notify here)
    from gi.repository import GLib as _GLib
except Exception:
    _GLib = None


def _make_card(notify_mod, *, play=True, text="agent message", timeout=300):
    return linux._Card(notify_mod, _GLib, text=text, title="Sebas - message",
                       body=text, button="Listen", urgency="critical",
                       context=None, play=play, timeout=timeout)


@unittest.skipIf(_GLib is None, "python3-gi / GLib not installed")
class RealGLibCardLifecycleTest(unittest.TestCase):
    def test_each_card_owns_its_own_context(self):
        notify_mod = FakeNotifyModule()
        a = _make_card(notify_mod)
        b = _make_card(notify_mod)
        self.assertIsNot(a._ctx, b._ctx)               # one context per card
        self.assertIsNot(a._ctx, _GLib.MainContext.default())
        self.assertIsNot(a._loop, b._loop)             # one loop per card
        # hash() of a GLib boxed type tracks the C pointer: these three must
        # be genuinely different contexts, not just different Python wrappers
        self.assertNotEqual(hash(a._ctx), hash(b._ctx))
        self.assertNotEqual(hash(a._ctx), hash(_GLib.MainContext.default()))

    def test_blocked_playback_does_not_freeze_other_cards(self):
        """Card A stuck in playback must not stop card B from closing.

        With the shared-default-context bug, B's 1s self-close timer never
        fires while A's loop holds the context, and the wait below fails."""
        release = threading.Event()

        def blocked_play(text, context=None, **kwargs):
            release.wait(10)                           # simulates a 900s wait
            return {"status": "ok", "played": True}

        notify_a, notify_b = FakeNotifyModule(), FakeNotifyModule()
        card_a = _make_card(notify_a, play=True, timeout=60)
        card_b = _make_card(notify_b, play=True, timeout=1)
        with mock.patch.object(linux, "play_via_daemon", side_effect=blocked_play):
            thread_a = threading.Thread(target=card_a.run, daemon=True)
            thread_b = threading.Thread(target=card_b.run, daemon=True)
            thread_a.start()
            thread_b.start()
            try:
                self.assertTrue(card_a.shown.wait(5))
                self.assertTrue(card_b.shown.wait(5))
                notify_a.created[0].invoke(ACTION_KEY)  # playback blocks A
                # B must still dispatch its own self-close timer:
                self.assertTrue(notify_b.created[0].closed.wait(5),
                                "card B did not close while A was in playback")
                self.assertEqual(notify_a.created[0].close_calls, 0)
            finally:
                release.set()
                self.assertTrue(notify_a.created[0].closed.wait(5))
                thread_a.join(5)
                thread_b.join(5)
        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())
        self.assertEqual(notify_a.created[0].close_calls, 1)
        self.assertEqual(notify_b.created[0].close_calls, 1)

    def test_quit_before_run_is_honored_and_leaks_no_thread(self):
        notify_mod = FakeNotifyModule()
        card = _make_card(notify_mod, play=False)
        card._close_and_quit()                         # before run() ever starts
        thread = threading.Thread(target=card.run, daemon=True)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive(),
                         "quit-before-run leaked the card thread")
        self.assertEqual(notify_mod.created, [])       # never shown either

    def test_close_between_flag_check_and_run_is_not_lost(self):
        """The racy window: a close that arrives after the pre-run flag check
        must still reach the loop (an attached idle source is never lost,
        unlike a bare loop.quit() before run())."""
        notify_mod = FakeNotifyModule()
        card = _make_card(notify_mod, play=False)
        real_loop = card._loop

        class RacingLoop:
            def run(self):
                card._close_and_quit()                 # arrives in the window
                return real_loop.run()

            def quit(self):
                real_loop.quit()

        card._loop = RacingLoop()
        thread = threading.Thread(target=card.run, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(),
                         "close in the check/run window leaked the thread")
        self.assertEqual(notify_mod.created[0].close_calls, 1)

    def test_close_exactly_once_under_double_click(self):
        notify_mod = FakeNotifyModule()
        card = _make_card(notify_mod, play=True)
        with mock.patch.object(linux, "play_via_daemon",
                               return_value={"status": "ok"}) as play:
            thread = threading.Thread(target=card.run, daemon=True)
            thread.start()
            self.assertTrue(card.shown.wait(5))
            notification = notify_mod.created[0]
            notification.invoke(ACTION_KEY)
            notification.invoke(ACTION_KEY)            # double click
            self.assertTrue(notification.closed.wait(5))
            thread.join(5)
        play.assert_called_once()
        self.assertEqual(notification.close_calls, 1)  # close() exactly once
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
