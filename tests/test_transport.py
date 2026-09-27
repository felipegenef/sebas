"""voice/transport.py and its notify mirror: the daemon transport, checked.

What is pinned here (host-independent — every test runs on Linux, macOS and
Windows CI alike):

  * flavor selection: "unix" where the build has AF_UNIX (POSIX keeps exactly
    its old transport), "tcp" fallback when AF_UNIX is missing — and the "this
    Python build has no AF_UNIX socket support" payload ONLY when NEITHER
    flavor can work;
  * the tcp bind is loopback-only (127.0.0.1, never 0.0.0.0) on an ephemeral
    port published in <data>/engine.port, with the random token in
    <data>/engine.token — both written 0600 (mode check: POSIX);
  * the token is the FIRST line of a tcp connection; wrong or missing token
    gets the house payload and never a served request;
  * the unix wire has NO handshake byte added: one JSON line in, one out;
  * 'unreachable' stays an OSError on every flavor (the callers' payload
    semantics), and endpoint files of the other flavor are cleaned up so no
    client can ever find two live endpoints;
  * plugin/notify/transport.py is byte-identical with the voice copy (the
    notify package is loaded standalone and cannot import the voice tree).

Run:  python3 -m unittest tests.test_transport -v
"""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))
sys.path.insert(0, str(ROOT / "plugin" / "mcp" / "voice"))

from _support import af_unix_removed, attr_removed, serve_one  # noqa: E402
from notify import transport as notify_transport  # noqa: E402
from voice import transport as voice_transport  # noqa: E402


class MirrorParityTest(unittest.TestCase):
    """The notify copy must stay byte-identical with the voice copy."""

    def test_files_are_byte_identical(self):
        voice = (ROOT / "plugin" / "mcp" / "voice" / "voice" /
                 "transport.py").read_bytes()
        notify = (ROOT / "plugin" / "notify" / "transport.py").read_bytes()
        self.assertEqual(voice, notify,
                         "notify/transport.py drifted from voice/transport.py")


class FlavorSelectionTest(unittest.TestCase):
    """Which flavor a Python build uses — and where the AF_UNIX payload may
    appear at all."""

    def test_unix_where_af_unix_exists(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("this Python build has no AF_UNIX (tcp flavor)")
        self.assertEqual(voice_transport.kind(), "unix")
        self.assertEqual(notify_transport.kind(), "unix")
        self.assertIsNone(voice_transport.unsupported_payload())

    def test_tcp_fallback_when_af_unix_is_missing(self):
        with af_unix_removed():
            self.assertEqual(voice_transport.kind(), "tcp")
            self.assertEqual(notify_transport.kind(), "tcp")
            # THE guard: the AF_UNIX payload must NOT be reachable here —
            # the tcp fallback keeps the daemon usable (the CI Windows bug).
            self.assertIsNone(voice_transport.unsupported_payload())
            self.assertIsNone(notify_transport.unsupported_payload())

    def test_unsupported_payload_only_when_neither_flavor_works(self):
        with af_unix_removed(), attr_removed(socket, "AF_INET"):
            self.assertIsNone(voice_transport.kind())
            for transport in (voice_transport, notify_transport):
                payload = transport.unsupported_payload(status="error")
                self.assertEqual(payload["status"], "error")
                self.assertIn("AF_UNIX", payload["problem"])
                self.assertIn("3.9+", payload["next_step"])
                self.assertTrue(payload["next_step"])

    def test_unsupported_payload_keeps_the_callers_status(self):
        with af_unix_removed(), attr_removed(socket, "AF_INET"):
            self.assertEqual(voice_transport.unsupported_payload()["status"],
                             "daemon_error")


class TcpBindTest(unittest.TestCase):
    """The tcp listener: loopback ONLY, ephemeral port published 0600, token
    random and published 0600."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "sebas"
        self._forced = af_unix_removed()
        self._forced.__enter__()
        self.addCleanup(self._forced.__exit__, None, None, None)

    def test_bind_is_loopback_only_and_publishes_the_endpoint(self):
        listener = voice_transport.bind_listener(self.data)
        self.addCleanup(listener.sock.close)
        self.assertEqual(listener.kind, "tcp")
        host, port = listener.sock.getsockname()
        self.assertEqual(host, "127.0.0.1")      # never 0.0.0.0
        self.assertGreater(port, 0)
        self.assertFalse(voice_transport.unix_socket(self.data).exists())
        self.assertEqual(voice_transport.port_file(self.data).read_text().strip(),
                         str(port))
        self.assertEqual(voice_transport.token_file(self.data).read_text().strip(),
                         listener.token)
        self.assertGreaterEqual(len(listener.token), 32)
        self.assertEqual(voice_transport.endpoint(self.data, "tcp"),
                         f"127.0.0.1:{port}")

    def test_endpoint_files_are_0600(self):
        if os.name == "nt":
            self.skipTest("POSIX file modes only (Windows chmod carries just "
                          "the read-only bit)")
        listener = voice_transport.bind_listener(self.data)
        self.addCleanup(listener.sock.close)
        for path in (voice_transport.port_file(self.data),
                     voice_transport.token_file(self.data)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)

    def test_bind_removes_stale_other_flavor_artifacts(self):
        stale_sock = voice_transport.unix_socket(self.data)
        self.data.mkdir(parents=True)
        stale_sock.write_bytes(b"")
        listener = voice_transport.bind_listener(self.data)
        self.addCleanup(listener.sock.close)
        self.assertFalse(stale_sock.exists())

    def test_remove_endpoints_clears_all_three(self):
        listener = voice_transport.bind_listener(self.data)
        listener.sock.close()
        self.data.joinpath("engine.sock").write_bytes(b"")   # stale too
        voice_transport.remove_endpoints(self.data)
        for name in ("engine.sock", "engine.port", "engine.token"):
            self.assertFalse((self.data / name).exists(), name)


class UnixBindTest(unittest.TestCase):
    """The unix listener keeps exactly its old endpoint (POSIX unchanged)."""

    def setUp(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("this Python build has no AF_UNIX (tcp flavor)")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "sebas"

    def test_bind_publishes_only_the_socket(self):
        listener = voice_transport.bind_listener(self.data)
        self.addCleanup(listener.sock.close)
        self.assertEqual(listener.kind, "unix")
        self.assertEqual(listener.token, "")
        self.assertEqual(listener.sock.getsockname(),
                         str(voice_transport.unix_socket(self.data)))
        self.assertFalse(voice_transport.port_file(self.data).exists())
        self.assertFalse(voice_transport.token_file(self.data).exists())

    def test_bind_removes_stale_tcp_artifacts(self):
        self.data.mkdir(parents=True)
        voice_transport.port_file(self.data).write_text("12345")
        voice_transport.token_file(self.data).write_text("stale")
        listener = voice_transport.bind_listener(self.data)
        self.addCleanup(listener.sock.close)
        self.assertFalse(voice_transport.port_file(self.data).exists())
        self.assertFalse(voice_transport.token_file(self.data).exists())


class HandshakeTest(unittest.TestCase):
    """The tcp first-line token: accepted once, rejected otherwise with the
    house payload — and never a served request."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "sebas"
        self._forced = af_unix_removed()
        self._forced.__enter__()
        self.addCleanup(self._forced.__exit__, None, None, None)

    def _raw_client(self):
        """A client that speaks tcp BY HAND (no token helper): for the
        rejection paths transport.connect would never take."""
        port = int(voice_transport.port_file(self.data).read_text().strip())
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.settimeout(5)
        srv.connect(("127.0.0.1", port))
        return srv

    def test_token_first_line_round_trip(self):
        received, thread = serve_one(self.data, {"status": "ok"},
                                     transport=voice_transport)
        with voice_transport.connect(self.data, timeout=5) as s:
            s.sendall(b'{"op":"ping"}\n')
            buf = b""
            while not buf.endswith(b"\n"):
                buf += s.recv(4096)
        thread.join(5)
        self.assertEqual(json.loads(buf), {"status": "ok"})
        self.assertEqual(received["payload"], {"op": "ping"})
        self.assertNotIn("refused", received)

    def test_wrong_token_gets_the_house_payload(self):
        received, thread = serve_one(self.data, {"status": "ok"},
                                     transport=voice_transport)
        with self._raw_client() as s:
            s.sendall(b"wrong-token\n")
            buf = b""
            while not buf.endswith(b"\n"):
                buf += s.recv(4096)
        thread.join(5)
        reply = json.loads(buf)
        self.assertEqual(reply["status"], "daemon_error")
        self.assertIn("token", reply["problem"])
        self.assertTrue(reply["next_step"])
        self.assertEqual(received.get("refused"), reply)   # never served
        self.assertNotIn("payload", received)

    def test_missing_token_line_is_rejected(self):
        received, thread = serve_one(self.data, {"status": "ok"},
                                     transport=voice_transport)
        with self._raw_client() as s:
            s.sendall(b"\n")                    # empty first line
            buf = b""
            while not buf.endswith(b"\n"):
                buf += s.recv(4096)
        thread.join(5)
        self.assertEqual(json.loads(buf)["status"], "daemon_error")
        self.assertNotIn("payload", received)

    def test_authorize_reads_nothing_on_unix(self):
        class ExplodingConn:
            def recv(self, n):
                raise AssertionError("unix authorize must not read the wire")

        listener = voice_transport.Listener(None, "unix", "")
        refused, leftover = voice_transport.authorize(ExplodingConn(), listener)
        self.assertIsNone(refused)
        self.assertEqual(leftover, b"")


class UnixWireUnchangedTest(unittest.TestCase):
    """POSIX byte-for-byte: no handshake line, the request line is the FIRST
    thing on the wire."""

    def setUp(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("this Python build has no AF_UNIX (tcp flavor)")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "sebas"

    def test_first_bytes_are_the_request_line(self):
        received, thread = serve_one(self.data, {"status": "ok"},
                                     transport=voice_transport)
        with voice_transport.connect(self.data, timeout=5) as s:
            s.sendall(b'{"op":"ping"}\n')
            s.recv(4096)
        thread.join(5)
        self.assertEqual(received["raw"], b'{"op":"ping"}\n')


class UnreachableTest(unittest.TestCase):
    """Per flavor: a missing endpoint is an OSError — the callers' shared
    'unreachable' semantics, never a crash."""

    def test_unreachable_raises_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "sebas"
            with self.assertRaises(OSError):
                voice_transport.connect(data, timeout=1)
        with af_unix_removed():
            with tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp) / "sebas"
                with self.assertRaises(OSError):
                    voice_transport.connect(data, timeout=1)

    def test_corrupt_port_file_is_oserror(self):
        with af_unix_removed():
            with tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp) / "sebas"
                data.mkdir()
                voice_transport.port_file(data).write_text("not-a-port")
                voice_transport.token_file(data).write_text("t")
                with self.assertRaises(OSError):
                    voice_transport.connect(data, timeout=1)


class EndpointDescriptionTest(unittest.TestCase):
    """endpoint() names the flavor actually in use (payloads and logs)."""

    def test_unix_endpoint_is_the_socket_path(self):
        data = Path("/data/sebas")
        self.assertEqual(voice_transport.endpoint(data, "unix"),
                         str(data / "engine.sock"))
        self.assertEqual(voice_transport.unix_socket(data),
                         data / voice_transport.UNIX_SOCK_NAME)

    def test_tcp_endpoint_is_loopback_port(self):
        data = Path("/data/sebas")
        self.assertEqual(voice_transport.endpoint(data, "tcp"),
                         "127.0.0.1:? (engine.port)")
        self.assertEqual(voice_transport.port_file(data).name,
                         voice_transport.PORT_FILE_NAME)
        self.assertEqual(voice_transport.token_file(data).name,
                         voice_transport.TOKEN_FILE_NAME)


class FakeDaemonBothFlavorsTest(unittest.TestCase):
    """serve_one — the fake daemon the adapter tests lean on — works through
    both flavors and records exactly one JSON line."""

    def test_round_trip_on_the_native_flavor(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "sebas"
            received, thread = serve_one(data, {"status": "ok", "played": True},
                                         transport=notify_transport)
            with notify_transport.connect(data, timeout=5) as s:
                s.sendall(b'{"op":"speak"}\n')
                s.recv(4096)
            thread.join(5)
            self.assertEqual(received["payload"], {"op": "speak"})

    def test_round_trip_without_af_unix(self):
        with af_unix_removed(), tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "sebas"
            received, thread = serve_one(data, {"status": "ok", "played": True},
                                         transport=notify_transport)
            with notify_transport.connect(data, timeout=5) as s:
                s.sendall(b'{"op":"speak"}\n')
                s.recv(4096)
            thread.join(5)
            self.assertEqual(received["payload"], {"op": "speak"})


if __name__ == "__main__":
    unittest.main()
