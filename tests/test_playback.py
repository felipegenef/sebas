"""Playback goes through the voice daemon socket: exact wire format.

A fake Unix socket stands in for the daemon, so the tests verify the
protocol line without ever connecting to the real engine.sock and without
playing anything.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

import notify.linux as linux


class PlaybackProtocolTest(unittest.TestCase):
    def _serve_once(self, sock_path: Path):
        """One-shot fake daemon; returns (received, server_thread)."""
        received = {}
        ready = threading.Event()

        def run():
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(sock_path))
            srv.listen(1)
            ready.set()
            conn, _ = srv.accept()
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            received["payload"] = json.loads(buf.decode())
            conn.sendall(b'{"status": "ok", "played": true}\n')
            conn.close()
            srv.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5))
        return received, thread

    def test_button_sends_confirmed_full_playback(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = Path(tmp) / "engine.sock"
            received, thread = self._serve_once(sock)
            reply = linux.play_via_daemon("Full message", context="Agente X",
                                          sock_path=sock, timeout=5)
            thread.join(5)
        self.assertEqual(reply["status"], "ok")
        self.assertEqual(received["payload"],
                         {"op": "speak", "text": "Full message",
                          "confirmed": True, "play": True,
                          "context": "Agente X"})

    def test_context_is_omitted_when_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = Path(tmp) / "engine.sock"
            received, thread = self._serve_once(sock)
            linux.play_via_daemon("Full message", sock_path=sock, timeout=5)
            thread.join(5)
        self.assertEqual(received["payload"],
                         {"op": "speak", "text": "Full message",
                          "confirmed": True, "play": True})

    def test_unreachable_daemon_is_a_payload_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            reply = linux.play_via_daemon("Full message",
                                          sock_path=Path(tmp) / "missing.sock",
                                          timeout=2)
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
