"""Two voices NEVER overlap: busy-at-arrival parking and one turn at a time.

Exercises the real arrival probe (_enter/_exit/_serve_voice) of voice/daemon.py
with a fake turn and faked synthesis/playback: no daemon socket, no model, no
audio. Rules under test:

  R1 no playback outside the daemon, ever (engine fallback included)
  R2 busy at arrival + card => parked, card shown, never auto-played
  R3 idle at arrival => plays right away, no card, no notice
  R4 confirmed=true / card click => waits its turn and plays, never re-carded
  R5 card unavailable + busy => old flow (short spoken notice + chat confirm)
  R6 race-free arrival probe: two arrivals together => one plays, one parks
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from _support import mcp_root


class _FlakyLock:
    """threading.Lock stand-in whose acquire() can fail on demand (the
    claim-leak regression: a failed acquire must undo the claim)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.fail_next = False

    def acquire(self, *args, **kwargs):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("acquire failed (injected)")
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        return self._lock.release()

    def locked(self):
        return self._lock.locked()


class _McpCase(unittest.TestCase):
    """Fresh fake turn state per test; faked voice stack; never real audio."""

    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import tools
        from voice import core, daemon, engine
        self.tools = tools
        self.daemon = daemon
        self.core = core
        self.engine = engine
        # Fake turn state: the tests drive the real probe against it.
        self._saved = (daemon._TURN, daemon._PLAN, daemon._INFLIGHT,
                       daemon._VOICE_INFLIGHT, daemon._WAITING)
        daemon._TURN = threading.Lock()
        daemon._PLAN = threading.Lock()
        daemon._INFLIGHT = {"n": 0}
        daemon._VOICE_INFLIGHT = {"n": 0}
        daemon._WAITING = {"n": 0}
        cfg = mock.patch.object(core, "load_config",
                                return_value={"play": True, "voice": None,
                                              "speed": 1.0})
        cfg.start()
        self.addCleanup(cfg.stop)
        # These tests pin queue semantics GIVEN a complete runtime. The
        # first-run 'installing' guard reads the machine's real data dir
        # otherwise (see test_installing_state.py for the guard itself), so it
        # is neutralized here: no machine state may leak into these tests.
        guard = mock.patch.object(core, "installing_payload", return_value=None)
        guard.start()
        self.addCleanup(guard.stop)

    def tearDown(self):
        (self.daemon._TURN, self.daemon._PLAN, self.daemon._INFLIGHT,
         self.daemon._VOICE_INFLIGHT, self.daemon._WAITING) = self._saved

    def fake_voice(self, entered: threading.Event | None = None,
                   release: threading.Event | None = None,
                   play_delay: float = 0.0) -> dict:
        """core.generate/core.play_file stand-ins that record and never touch
        audio. generate() sets `entered` and blocks until `release` (a long
        generation); play_file() keeps a non-reentrant guard so any overlap is
        counted. Returns the call record."""
        rec: dict = {"generated": [], "played": [], "overlaps": 0}
        playing = threading.Lock()

        def generate(text, cfg):
            rec["generated"].append(text)
            if entered is not None:
                entered.set()
            if release is not None and not release.wait(10):
                raise AssertionError("fake generate never released")
            return (f"/tmp/fake_{len(rec['generated'])}.wav", 22050, 1.0,
                    "kokoro fake")

        def play_file(path):
            got = playing.acquire(blocking=False)
            if not got:
                rec["overlaps"] += 1
            try:
                if play_delay:
                    time.sleep(play_delay)
                rec["played"].append(str(path))
                return True
            finally:
                if got:
                    playing.release()

        for name, fake in (("generate", generate), ("play_file", play_file)):
            patcher = mock.patch.object(self.core, name, new=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        return rec

    @staticmethod
    def speak_req(text: str, **extra) -> dict:
        req = {"op": "speak", "text": text, "play": True,
               "confirmed": False, "card": False,
               "config": {"play": True, "voice": None, "speed": 1.0}}
        req.update(extra)
        return req


class NoOverlapTest(_McpCase):
    # ------------------------------------------------------------ R3 + R2
    def test_idle_at_arrival_plays_no_card_no_notice(self):
        rec = self.fake_voice()
        reply = self.daemon._serve_voice(self.speak_req("Solo message", card=True))
        self.assertEqual(reply["status"], "ok")
        self.assertTrue(reply["played"])
        self.assertEqual(rec["generated"], ["Solo message"])   # no notice
        self.assertEqual(len(rec["played"]), 1)
        self.assertNotIn("card", reply)                        # no card

        # tools level: no card is shown and no notice is spoken.
        with mock.patch.object(self.tools.notify_bridge, "available",
                               return_value=True), \
             mock.patch.object(self.tools.notify_bridge, "show_card",
                               side_effect=AssertionError("no card when idle")), \
             mock.patch.object(self.tools, "_daemon_request",
                               return_value=dict(reply)), \
             mock.patch.object(self.tools.engine, "speak",
                               side_effect=AssertionError("no second speak")):
            result = self.tools.speak("Solo message", play=True, context="Agente X")
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("card", result)
        self.assertIn("speech was already heard", result["next_step"])

    def test_busy_at_arrival_parks_and_never_autoplays(self):
        entered, release = threading.Event(), threading.Event()
        rec = self.fake_voice(entered=entered, release=release)
        first = threading.Thread(
            target=self.daemon._serve_voice,
            args=(self.speak_req("First message"),), daemon=True)
        first.start()
        self.assertTrue(entered.wait(5))

        # Second request arrives while the first is still being processed.
        replies: list = []
        parked = threading.Thread(
            target=lambda: replies.append(self.daemon._serve_voice(
                self.speak_req("Parked message", card=True, context="Agente X"))),
            daemon=True)
        parked.start()
        parked.join(5)
        self.assertFalse(parked.is_alive(), "park reply must return immediately")
        release.set()
        first.join(10)

        self.assertEqual(len(replies), 1)
        reply = replies[0]
        self.assertEqual(reply["status"], "awaiting_confirmation")
        self.assertTrue(reply["card"])
        self.assertTrue(reply["notification_only"])
        self.assertEqual(reply["text"], "Parked message")   # full text for the card
        self.assertEqual(reply["context"], "Agente X")
        self.assertFalse(reply["played"])
        self.assertEqual(rec["generated"], ["First message"])  # never synthesized
        self.assertEqual(len(rec["played"]), 1)                # never played
        self.assertNotIn("Parked message", rec["generated"])

    def test_park_reply_carries_the_full_text_and_the_card_is_shown(self):
        entered, release = threading.Event(), threading.Event()
        self.fake_voice(entered=entered, release=release)
        first = threading.Thread(
            target=self.daemon._serve_voice,
            args=(self.speak_req("First message"),), daemon=True)
        first.start()
        self.assertTrue(entered.wait(5))

        with mock.patch.object(self.tools.notify_bridge, "available",
                               return_value=True), \
             mock.patch.object(self.tools.notify_bridge, "show_card",
                               return_value={"status": "shown"}) as show, \
             mock.patch.object(self.tools, "_daemon_request",
                               side_effect=self.daemon._serve_voice), \
             mock.patch.object(self.tools.engine, "speak",
                               side_effect=AssertionError("no spoken notice")), \
             mock.patch.object(self.tools.core, "get_butler_name", return_value="Sebas"), \
             mock.patch.object(self.tools.core, "get_language", return_value="pt-br"):
            result = self.tools.speak("Parked message", play=True, context="Agente X")

        release.set()
        first.join(10)
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["card"], "shown")
        self.assertIn("NOT be played automatically", result["next_step"])
        self.assertIn("Do NOT ask", result["next_step"])
        show.assert_called_once()
        self.assertEqual(show.call_args.kwargs["text"], "Parked message")

    # ---------------------------------------------------------------- R6
    def test_two_arrivals_together_one_plays_one_parks(self):
        for round_no in range(5):
            entered, release = threading.Event(), threading.Event()
            rec = self.fake_voice(entered=entered, release=release)
            gate = threading.Barrier(2)
            replies: list = []

            def arrive(text):
                gate.wait(5)
                replies.append(self.daemon._serve_voice(
                    self.speak_req(text, card=True)))

            a = threading.Thread(target=arrive, args=(f"race A {round_no}",),
                                 daemon=True)
            b = threading.Thread(target=arrive, args=(f"race B {round_no}",),
                                 daemon=True)
            a.start(), b.start()
            self.assertTrue(entered.wait(5), f"round {round_no}: nobody played")
            # Hold the winner busy until the OTHER arrival has decided, so the
            # outcome depends on the probe, never on thread timing.
            deadline = time.time() + 5
            while len(replies) < 1 and time.time() < deadline:
                time.sleep(0.01)
            release.set()
            a.join(10), b.join(10)

            self.assertEqual(len(replies), 2, f"round {round_no}: {replies}")
            played = [r for r in replies if r.get("status") == "ok"]
            parked = [r for r in replies if r.get("card") is True]
            self.assertEqual(len(played), 1, f"round {round_no}: {replies}")
            self.assertEqual(len(parked), 1, f"round {round_no}: {replies}")
            self.assertFalse(parked[0]["played"])
            self.assertEqual(parked[0]["notification_only"], True)
            self.assertIn(parked[0]["text"], ("race A " + str(round_no),
                                              "race B " + str(round_no)))
            self.assertEqual(len(rec["generated"]), 1, f"round {round_no}")
            self.assertEqual(len(rec["played"]), 1, f"round {round_no}")

    # ------------------------------- dry runs never cause cards (the R2 rule)
    def test_dry_run_in_flight_never_parks_a_real_message(self):
        """A request that will never be spoken (play=false / warmup) is NOT a
        voice call: the arriving real message waits its turn and plays — no
        card, no notice. Only two VOICE calls colliding get a card."""
        dry_runs = (
            self.speak_req("Dry run", play=False),
            {"op": "warmup", "config": {"play": True, "voice": None,
                                        "speed": 1.0}},
        )
        for dry in dry_runs:
            label = "play-false" if dry.get("play") is False else "warmup"
            with self.subTest(dry=label):
                entered, release = threading.Event(), threading.Event()
                rec = self.fake_voice(entered=entered, release=release)
                generating = threading.Thread(target=self.daemon._serve_voice,
                                              args=(dry,), daemon=True)
                generating.start()
                self.assertTrue(entered.wait(5))

                replies: list = []
                real = threading.Thread(
                    target=lambda: replies.append(self.daemon._serve_voice(
                        self.speak_req("Real message", card=True,
                                       context="Agente X"))),
                    daemon=True)
                real.start()
                real.join(0.2)
                self.assertTrue(real.is_alive(),
                                "the real message waits its turn (no overlap)")
                release.set()
                generating.join(10)
                real.join(10)

                self.assertEqual(len(replies), 1, label)
                reply = replies[0]
                self.assertEqual(reply["status"], "ok", label)  # plays in turn
                self.assertTrue(reply["played"], label)
                self.assertNotIn("card", reply, label)          # NO card
                self.assertFalse(reply["notification_only"], label)
                first = "Dry run" if label == "play-false" else "Engine warmup."
                self.assertEqual(rec["generated"], [first, "Real message"], label)
                self.assertEqual(len(rec["played"]), 1, label)  # dry never plays
                self.assertEqual(rec["overlaps"], 0, label)
                self.assertEqual(self.daemon._INFLIGHT["n"], 0, label)
                self.assertEqual(self.daemon._VOICE_INFLIGHT["n"], 0, label)

    def test_real_speak_in_flight_still_parks_the_next_one(self):
        """(the kept behavior) A VOICE call in flight + another voice call
        arriving = two voice calls collide: the second PARKS and gets a card."""
        entered, release = threading.Event(), threading.Event()
        rec = self.fake_voice(entered=entered, release=release)
        first = threading.Thread(
            target=self.daemon._serve_voice,
            args=(self.speak_req("First message"),), daemon=True)
        first.start()
        self.assertTrue(entered.wait(5))

        replies: list = []
        second = threading.Thread(
            target=lambda: replies.append(self.daemon._serve_voice(
                self.speak_req("Second message", card=True))),
            daemon=True)
        second.start()
        second.join(0.2)
        self.assertFalse(second.is_alive(), "the park reply must be immediate")
        release.set()
        first.join(10)

        self.assertEqual(len(replies), 1)
        reply = replies[0]
        self.assertEqual(reply["status"], "awaiting_confirmation")
        self.assertTrue(reply["card"])                       # a card, like before
        self.assertFalse(reply["played"])
        self.assertNotIn("Second message", rec["generated"])

    # ---------------------------------------------------------------- R4
    def test_confirmed_waits_its_turn_and_plays_in_order(self):
        entered, release = threading.Event(), threading.Event()
        rec = self.fake_voice(entered=entered, release=release)
        first = threading.Thread(
            target=self.daemon._serve_voice,
            args=(self.speak_req("First message"),), daemon=True)
        first.start()
        self.assertTrue(entered.wait(5))

        # Card button / chat confirmation: waits its turn, never parked.
        replies: list = []
        click = threading.Thread(
            target=lambda: replies.append(self.daemon._serve_voice(
                self.speak_req("Click message", card=True, confirmed=True))),
            daemon=True)
        click.start()
        click.join(0.2)
        self.assertTrue(click.is_alive(), "confirmed playback must wait its turn")
        release.set()
        first.join(10)
        click.join(10)

        self.assertEqual(rec["generated"], ["First message", "Click message"])
        self.assertEqual(len(rec["played"]), 2)          # serialized, in order
        self.assertEqual(rec["overlaps"], 0)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["status"], "ok")
        self.assertTrue(replies[0]["played"])
        self.assertNotIn("card", replies[0])             # never re-carded

    def test_confirmed_reply_carries_no_card(self):
        rec = self.fake_voice()
        reply = self.daemon._serve_voice(
            self.speak_req("Click message", card=True, confirmed=True))
        self.assertEqual(reply["status"], "ok")
        self.assertTrue(reply["played"])
        self.assertNotIn("card", reply)                  # never re-carded
        self.assertEqual(rec["generated"], ["Click message"])

    # ---------------------------------------------------------------- R1
    def test_engine_unreachable_never_plays_locally(self):
        with mock.patch.object(self.engine, "_daemon", return_value=None), \
             mock.patch.object(self.engine, "load_config",
                               return_value={"play": True}), \
             mock.patch.object(self.core, "generate",
                               return_value=("/tmp/fake.wav", 22050, 1.0,
                                             "kokoro fake")), \
             mock.patch.object(self.core, "play_file",
                               side_effect=AssertionError(
                                   "local playback is forbidden (two voices "
                                   "could overlap)")):
            result = self.engine.speak("Full message", play=True,
                                       context="Agente X")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["played"])
        self.assertIn("daemon", result["next_step"])
        self.assertIn("NOTHING was played", result["next_step"])

    def test_engine_unreachable_with_play_false_never_plays_either(self):
        with mock.patch.object(self.engine, "_daemon", return_value=None), \
             mock.patch.object(self.engine, "load_config",
                               return_value={"play": False}), \
             mock.patch.object(self.core, "generate",
                               return_value=("/tmp/fake.wav", 22050, 1.0,
                                             "kokoro fake")), \
             mock.patch.object(self.core, "play_file",
                               side_effect=AssertionError("no local playback")):
            result = self.engine.speak("Full message", play=False)
        self.assertFalse(result["played"])

    # ---------------------------------------------------------------- R5
    def test_card_unavailable_busy_keeps_the_old_flow(self):
        entered, release = threading.Event(), threading.Event()
        rec = self.fake_voice(entered=entered, release=release)
        first = threading.Thread(
            target=self.daemon._serve_voice,
            args=(self.speak_req("First message"),), daemon=True)
        first.start()
        self.assertTrue(entered.wait(5))

        results: list = []
        with mock.patch.object(self.tools.notify_bridge, "available",
                               return_value=False), \
             mock.patch.object(self.tools.engine, "_daemon",
                               side_effect=self.daemon._serve_voice), \
             mock.patch.object(self.tools.engine, "load_config",
                               return_value={"play": True}), \
             mock.patch.object(self.core, "get_language", return_value="pt-br"), \
             mock.patch.object(self.core, "user_greeting", return_value=""):
            old_flow = threading.Thread(
                target=lambda: results.append(
                    self.tools.speak("Full message", play=True, context="Agente X")),
                daemon=True)
            old_flow.start()
            old_flow.join(0.2)
            self.assertTrue(old_flow.is_alive(),
                            "the short notice waits for its turn (no overlap)")
            release.set()
            first.join(10)
            old_flow.join(10)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertTrue(result["notification_only"])
        self.assertNotIn("card", result)
        self.assertIn("Ask the user", result["next_step"])
        notice = rec["generated"][-1]
        self.assertNotEqual(notice, "Full message")       # short notice only
        self.assertIn("confirme", notice.lower())
        self.assertEqual(rec["overlaps"], 0)

    def test_card_failure_daemon_idle_never_plays_the_full_text(self):
        """Card fails and the daemon freed up meanwhile: the fallback speaks
        ONLY the short notice. The spy on core.play_file (side_effect
        AssertionError) makes any playback of the full text's wav an automatic
        failure — the parked message plays only after a user confirmation."""
        entered, release = threading.Event(), threading.Event()
        generated: dict = {}                              # wav path -> text
        spoken: list = []

        def generate(text, cfg):
            path = f"/tmp/fake_{len(generated)}.wav"
            generated[path] = text
            entered.set()
            if not release.wait(10):
                raise AssertionError("fake generate never released")
            return (path, 22050, 1.0, "kokoro fake")

        def play_file(path):
            text = generated.get(str(path), "")
            if text == "Parked message":
                raise AssertionError("the parked message must never play unconfirmed")
            spoken.append(text)
            return True

        first = threading.Thread(
            target=self.daemon._serve_voice,
            args=(self.speak_req("Background voice"),), daemon=True)

        def card_fails(**kwargs):
            release.set()          # the daemon frees up before the fallback
            first.join(10)
            return {"status": "unavailable", "problem": "bridge error"}

        with mock.patch.object(self.core, "generate", side_effect=generate), \
             mock.patch.object(self.core, "play_file", side_effect=play_file), \
             mock.patch.object(self.tools.notify_bridge, "available",
                               return_value=True), \
             mock.patch.object(self.tools.notify_bridge, "show_card",
                               side_effect=card_fails), \
             mock.patch.object(self.tools, "_daemon_request",
                               side_effect=self.daemon._serve_voice), \
             mock.patch.object(self.tools.core, "get_language",
                               return_value="pt-br"), \
             mock.patch.object(self.tools.core, "user_greeting",
                               return_value=""):
            first.start()
            self.assertTrue(entered.wait(5))
            result = self.tools.speak("Parked message", play=True,
                                      context="Agente X")

        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["card"], "unavailable")
        self.assertEqual(result["card_problem"], "bridge error")
        self.assertNotIn("Parked message", generated.values())  # never made
        self.assertEqual(spoken[0], "Background voice")     # the one in flight
        self.assertEqual(len(spoken), 2)                    # + the short notice
        self.assertIn("confirme", spoken[1].lower())
        self.assertEqual(self.daemon._INFLIGHT["n"], 0)     # turn fully drained

    # ------------------------------------------------- queue-wide guarantee
    def test_no_two_play_calls_overlap(self):
        rec = self.fake_voice(play_delay=0.01)
        gate = threading.Barrier(8)
        replies: list = []
        lock = threading.Lock()

        def arrive(i):
            gate.wait(5)
            reply = self.daemon._serve_voice(
                self.speak_req(f"message {i}", confirmed=True))
            with lock:
                replies.append(reply)

        threads = [threading.Thread(target=arrive, args=(i,), daemon=True)
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertEqual(len(replies), 8)
        self.assertTrue(all(r.get("status") == "ok" for r in replies))
        self.assertTrue(all(r.get("played") for r in replies))
        self.assertEqual(len(rec["generated"]), 8)
        self.assertEqual(len(rec["played"]), 8)
        self.assertEqual(rec["overlaps"], 0, "two play_file calls overlapped")
        # The turn is fully drained afterwards.
        self.assertEqual(self.daemon._INFLIGHT["n"], 0)
        self.assertFalse(self.daemon._TURN.locked())
        self.assertEqual(self.daemon._WAITING["n"], 0)

    def test_no_two_play_calls_overlap_with_dry_runs_in_the_mix(self):
        """Same regression with a mix of voice calls and dry runs: the turn
        still serializes EVERYTHING (R1), dry runs never play and never park
        anything, and every counter drains."""
        rec = self.fake_voice(play_delay=0.01)
        gate = threading.Barrier(8)
        replies: list = []
        lock = threading.Lock()

        def arrive(i):
            gate.wait(5)
            if i % 2:
                req = self.speak_req(f"dry {i}", play=False)     # never spoken
            else:
                req = self.speak_req(f"message {i}", confirmed=True)
            reply = self.daemon._serve_voice(req)
            with lock:
                replies.append(reply)

        threads = [threading.Thread(target=arrive, args=(i,), daemon=True)
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertEqual(len(replies), 8)
        self.assertTrue(all(r.get("status") == "ok" for r in replies))
        self.assertTrue(all(not r.get("card") for r in replies))
        self.assertEqual(len(rec["generated"]), 8)
        self.assertEqual(len(rec["played"]), 4)      # only the voice calls play
        self.assertEqual(rec["overlaps"], 0, "two play_file calls overlapped")
        self.assertEqual(self.daemon._INFLIGHT["n"], 0)
        self.assertEqual(self.daemon._VOICE_INFLIGHT["n"], 0)
        self.assertFalse(self.daemon._TURN.locked())
        self.assertEqual(self.daemon._WAITING["n"], 0)

    # ------------------------------------------------- claim-leak protection
    def test_acquire_failure_never_leaks_the_turn_claim(self):
        """If _TURN.acquire() raises in the wait path, the claim must be
        undone: a stuck _INFLIGHT would make every later arrival park
        forever. The failure is loud (daemon_error) and the queue survives."""
        entered, release = threading.Event(), threading.Event()
        self.fake_voice(entered=entered, release=release)
        flaky = _FlakyLock()
        self.daemon._TURN = flaky
        first = threading.Thread(
            target=self.daemon._serve_voice,
            args=(self.speak_req("First message"),), daemon=True)
        first.start()
        self.assertTrue(entered.wait(5))

        flaky.fail_next = True
        replies: list = []
        second = threading.Thread(
            target=lambda: replies.append(self.daemon._serve_voice(
                self.speak_req("Second message", confirmed=True))),
            daemon=True)
        second.start()
        second.join(5)
        self.assertFalse(second.is_alive(), "the failure must not hang")
        release.set()
        first.join(10)

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["status"], "daemon_error")   # loud
        # The claim was undone: nothing is stuck.
        self.assertEqual(self.daemon._INFLIGHT["n"], 0)
        self.assertEqual(self.daemon._VOICE_INFLIGHT["n"], 0)
        self.assertEqual(self.daemon._WAITING["n"], 0)
        self.assertFalse(flaky.locked())
        # And the queue still works: the next request plays, it does not park.
        reply = self.daemon._serve_voice(self.speak_req("Third message"))
        self.assertEqual(reply["status"], "ok")
        self.assertTrue(reply["played"])


if __name__ == "__main__":
    unittest.main()
