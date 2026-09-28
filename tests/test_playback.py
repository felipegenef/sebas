"""Playback goes through the voice daemon: exact wire format.

A fake daemon — bound through the SAME transport abstraction the adapter
uses (notify/transport.py) — stands in for the real one, so the tests verify
the protocol line without ever connecting to a live daemon and without
playing anything. The fake binds the flavor this Python build uses: the
AF_UNIX socket on POSIX, the loopback TCP + token fallback otherwise — so
the wire is proven on every CI leg.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Host-truth import plumbing: os.path strings, never pathlib — the CWD
# guard's simulation flips pathlib (test_no_cwd_artifacts.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "plugin"))
sys.path.insert(0, _HERE)   # for _support

import notify.linux as linux
from _support import serve_one

notify_transport = linux._transport   # the adapter's own transport module


class PlaybackProtocolTest(unittest.TestCase):
    def _serve_once(self, data: Path, reply: dict | None = None):
        """One-shot fake daemon through the transport abstraction; returns
        (received, server_thread)."""
        return serve_one(data, reply or {"status": "ok", "played": True},
                         transport=notify_transport)

    def test_button_sends_confirmed_full_playback(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "sebas"
            received, thread = self._serve_once(data)
            reply = linux.play_via_daemon("Full message", context="Agente X",
                                          data=data, timeout=5)
            thread.join(5)
        self.assertEqual(reply["status"], "ok")
        self.assertEqual(received["payload"],
                         {"op": "speak", "text": "Full message",
                          "confirmed": True, "play": True,
                          "context": "Agente X"})

    def test_context_is_omitted_when_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "sebas"
            received, thread = self._serve_once(data)
            linux.play_via_daemon("Full message", data=data, timeout=5)
            thread.join(5)
        self.assertEqual(received["payload"],
                         {"op": "speak", "text": "Full message",
                          "confirmed": True, "play": True})

    def test_unreachable_daemon_is_a_payload_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            reply = linux.play_via_daemon("Full message",
                                          data=Path(tmp) / "missing", timeout=2)
        self.assertEqual(reply["status"], "daemon_unreachable")
        self.assertIn("next_step", reply)

    def test_socket_path_is_the_one_resolved_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
                # nothing exists yet -> the new data dir
                self.assertEqual(linux.socket_path(),
                                 Path(tmp) / "sebas" / "engine.sock")
                # a pre-1.0 legacy dir is auto-detected and used as-is
                (Path(tmp) / "voz").mkdir()
                self.assertEqual(linux.socket_path(),
                                 Path(tmp) / "voz" / "engine.sock")
                # once the new dir exists it wins
                (Path(tmp) / "sebas").mkdir()
                self.assertEqual(linux.socket_path(),
                                 Path(tmp) / "sebas" / "engine.sock")

    def test_socket_path_without_xdg_uses_the_default_data_home(self):
        env = dict(os.environ)
        env.pop("XDG_DATA_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            sock = linux.socket_path()
        self.assertEqual(sock.name, "engine.sock")
        self.assertEqual(sock.parent.parent, Path.home() / ".local" / "share")


if __name__ == "__main__":
    unittest.main()
