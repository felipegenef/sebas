"""The gi fallback: cards must appear even when THIS interpreter has no gi.

Why: the voice MCP runs inside its venv, and a venv sees no distro
site-packages — `import gi` (python3-gi) fails there even though the system
python3 has it (the live bug: no card ever appeared). notify/linux.py then
launches the SAME card through the packaged runner (notify/__main__.py)
under the system python3 — shutil.which("python3"), never the venv's own
interpreter.

Everything here is host-independent and silent: import, shutil.which and
subprocess are mocked, NO process is ever really spawned, no D-Bus, no
notification server, no daemon socket, no audio. What is pinned: the exact
argv of the fallback launcher, the in-process path staying in-process, the
degradation to the documented 'unavailable' payload without a system
python3, the venv-interpreter exclusion, and the card contract (one button,
close-once, dry run never plays) inside the fallback runner itself.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

# Host-truth import plumbing: os.path strings, never pathlib — the CWD
# guard's simulation flips pathlib (test_no_cwd_artifacts.py).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "plugin"))

import notify.linux as linux
from _support import ACTION_KEY, fake_gi

card_runner = importlib.import_module("notify.__main__")

GI_FIX = "the system python3 provides gi; the fallback launcher covers it"
SYSTEM_PYTHON = "/usr/systembin/python3"     # a stand-in, never executed
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(linux.__file__)))


def no_gi():
    """`import gi` fails in-process — the venv condition under test."""
    return mock.patch.dict(sys.modules, {"gi": None})


class SystemPythonResolutionTest(unittest.TestCase):
    """shutil.which("python3"), and NEVER the venv's own interpreter."""

    def test_venv_interpreter_is_filtered_out_of_the_lookup(self):
        venv_bin = "/venv/bin"
        system_bin = "/usr/systembin"

        def which(binary, path=None):
            for entry in (path or "").split(os.pathsep):
                if entry in (venv_bin, system_bin):
                    return os.path.join(entry, binary)   # first hit wins
            return None

        with mock.patch.dict(os.environ,
                             {"PATH": venv_bin + os.pathsep + system_bin}), \
             mock.patch.object(linux.sys, "executable",
                               os.path.join(venv_bin, "python3")), \
             mock.patch.object(linux.sys, "prefix", "/venv"), \
             mock.patch.object(linux.sys, "base_prefix", "/usr"), \
             mock.patch.object(linux.shutil, "which", side_effect=which):
            found = linux._system_python()
        self.assertEqual(found, os.path.join(system_bin, "python3"))

    def test_without_a_venv_the_plain_lookup_is_used(self):
        def which(binary, path=None):
            return "/usr/bin/python3" if "/usr/bin" in (path or "") else None

        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}), \
             mock.patch.object(linux.sys, "executable", "/usr/bin/python3"), \
             mock.patch.object(linux.sys, "prefix", "/usr"), \
             mock.patch.object(linux.sys, "base_prefix", "/usr"), \
             mock.patch.object(linux.shutil, "which", side_effect=which):
            found = linux._system_python()
        self.assertEqual(found, "/usr/bin/python3")

    def test_no_python3_on_path_is_none(self):
        with mock.patch.object(linux.shutil, "which", return_value=None):
            self.assertIsNone(linux._system_python())


class GiMissingFallbackTest(unittest.TestCase):
    """show_card() with no gi: the same card through the system python3."""

    def test_spawn_uses_the_system_interpreter_and_the_packaged_runner(self):
        with no_gi(), \
             mock.patch.object(linux.shutil, "which",
                               return_value=SYSTEM_PYTHON) as which, \
             mock.patch.object(linux.subprocess, "Popen") as popen:
            result = linux.show_card("Mensagem do agente — ç", butler="Sebas",
                                     language="pt-br", context="Agente X",
                                     urgency="critical", play=False)
        self.assertEqual(result["status"], "shown")
        self.assertEqual(result["via"], "system-python")
        self.assertEqual(result["button"], "Ouvir mensagem")
        which.assert_called_once()                # resolved with shutil.which
        self.assertEqual(which.call_args.args[0], "python3")

        argv = popen.call_args.args[0]
        self.assertEqual(argv[:3], [SYSTEM_PYTHON, "-m", "notify"])
        self.assertEqual(json.loads(argv[3]), {
            "text": "Mensagem do agente — ç", "butler": "Sebas",
            "language": "pt-br", "context": "Agente X",
            "urgency": "critical", "play": False})

        kwargs = popen.call_args.kwargs            # detached, stdio discarded
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["stdin"], linux.subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], linux.subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], linux.subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], PLUGIN_ROOT)
        self.assertEqual(kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0],
                         PLUGIN_ROOT)              # the packaged notify/ wins

    def test_gi_present_stays_in_process_and_never_spawns(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch.object(linux.subprocess, "Popen") as popen, \
                 mock.patch.object(linux.shutil, "which") as which, \
                 mock.patch.object(linux, "play_via_daemon"):
                result = linux.show_card("Full agent message.", play=False)
                self.assertEqual(result["status"], "shown")
                self.assertEqual(result["via"], "in-process")
                notify_mod.created[0].invoke(ACTION_KEY)  # close the card
        popen.assert_not_called()
        which.assert_not_called()

    def test_missing_system_python3_degrades_to_unavailable(self):
        with no_gi(), \
             mock.patch.object(linux.shutil, "which", return_value=None), \
             mock.patch.object(linux.subprocess, "Popen") as popen:
            result = linux.show_card("Full agent message.", play=False)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("python3-gi", result["problem"])
        self.assertIn(GI_FIX, result["next_step"])
        popen.assert_not_called()                 # nothing was launched

    def test_launcher_start_failure_is_unavailable_too(self):
        with no_gi(), \
             mock.patch.object(linux.shutil, "which",
                               return_value=SYSTEM_PYTHON), \
             mock.patch.object(linux.subprocess, "Popen",
                               side_effect=OSError("boom")):
            result = linux.show_card("Full agent message.", play=False)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("boom", result["problem"])
        self.assertIn(GI_FIX, result["next_step"])

    def test_the_runner_itself_never_falls_back_again(self):
        """Recursion guard: a runner without gi reports 'unavailable' and
        never spawns a second runner."""
        with no_gi(), \
             mock.patch.object(linux.subprocess, "Popen") as popen:
            result = linux.show_card_blocking("Full agent message.", play=False)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("python3-gi", result["problem"])
        popen.assert_not_called()


class FallbackRunnerContractTest(unittest.TestCase):
    """notify/__main__.py: the card contract inside the runner process."""

    def _start(self, spec):
        """Runs runner main() on a thread; returns (thread, exit codes)."""
        codes = []
        thread = threading.Thread(
            target=lambda: codes.append(card_runner.main([json.dumps(spec)])),
            name="card-runner-test", daemon=True)
        thread.start()
        return thread, codes

    @staticmethod
    def _wait_created(notify_mod, seconds=5.0):
        deadline = time.monotonic() + seconds
        while not notify_mod.created and time.monotonic() < deadline:
            time.sleep(0.01)
        return notify_mod.created

    def test_one_button_click_plays_once_and_closes_once(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch.object(linux, "play_via_daemon",
                                   return_value={"status": "ok"}) as play:
                thread, codes = self._start(
                    {"text": "Full agent message.", "butler": "Sebas",
                     "language": "pt-br", "context": "Agente X",
                     "urgency": "critical", "play": True})
                created = self._wait_created(notify_mod)
                self.assertTrue(created, "the runner created no card")
                card = created[0]
                self.assertEqual(len(card.actions), 1)      # ONE button
                self.assertEqual(card.actions[0][0], ACTION_KEY)
                self.assertEqual(card.actions[0][1], "Ouvir mensagem")
                card.invoke(ACTION_KEY)                     # the click plays
                card.invoke(ACTION_KEY)                     # double click
                thread.join(5)
            play.assert_called_once()
            self.assertEqual(play.call_args.args[0], "Full agent message.")
            self.assertEqual(play.call_args.kwargs["context"], "Agente X")
        self.assertFalse(thread.is_alive())
        self.assertEqual(codes, [0])                        # shown -> exit 0
        self.assertEqual(card.show_calls, 1)                # never re-banner
        self.assertEqual(card.close_calls, 1)               # close-once

    def test_dry_run_closes_without_touching_the_daemon(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch.object(linux, "play_via_daemon") as play:
                thread, codes = self._start({"text": "Full agent message.",
                                             "play": False})
                card = self._wait_created(notify_mod)[0]
                card.invoke(ACTION_KEY)
                thread.join(5)
            play.assert_not_called()                        # a true dry run
        self.assertFalse(thread.is_alive())
        self.assertEqual(codes, [0])
        self.assertEqual(card.close_calls, 1)

    def test_self_close_timer_still_closes_without_playback(self):
        with fake_gi() as (notify_mod, glib):
            with mock.patch.object(linux, "play_via_daemon") as play:
                thread, codes = self._start({"text": "Full agent message.",
                                             "play": True})
                card = self._wait_created(notify_mod)[0]
                seconds, fire = glib.timers[0]              # the self-close
                self.assertEqual(seconds, linux.CARD_TIMEOUT_SECONDS)
                fire()
                thread.join(5)
            play.assert_not_called()
        self.assertEqual(codes, [0])
        self.assertEqual(card.show_calls, 1)
        self.assertEqual(card.close_calls, 1)

    def test_bad_argv_and_empty_text_are_payloads_not_crashes(self):
        self.assertEqual(card_runner.main([]), 2)
        self.assertEqual(card_runner.main(["not json"]), 2)
        self.assertEqual(card_runner.main(["[]"]), 2)       # not a dict
        self.assertEqual(card_runner.main([json.dumps({"text": "   "})]), 1)


if __name__ == "__main__":
    unittest.main()
