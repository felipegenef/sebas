"""Windows stdio encoding: the ANSI-codepage regression that spoke the
copyright symbol aloud.

The bug: on Windows the server spoke a greeting like "my name is …" with
the © symbol read aloud ("símbolo de copyright" in pt-br) in the middle.
Cause: server.py never
reconfigured stdio, so a host that spawns it without PYTHONUTF8 /
PYTHONIOENCODING (every Windows host that starts it as an MCP child) leaves
stdin/stdout on the system ANSI codepage — cp1252. JSON-RPC is always UTF-8,
so 'é' (bytes C3 A9) decoded as the TWO characters 'Ã©', and the lone '©'
traveled to espeak, which reads it in pt-br as "símbolo de copyright". The
corruption is one-way INTO the engine: the reply re-encoded through cp1252
can accidentally reproduce the original UTF-8 bytes, so the bug is visible
only in what the server DID with the text — the tests below look there.

Three levels of proof, no audio and no engine anywhere:

  * in-process  main() hand a cp1252 TextIOWrapper over the real request
                bytes (exactly the Windows condition) and must still READ
                UTF-8: the echoed accented method comes back intact;
  * guarded     streams without TextIOWrapper.reconfigure (StringIO — the
                same captured streams the payload tests use) must not break
                startup;
  * subprocess  the server spawned in the portable replica of the Windows
                environment (PYTHONIOENCODING=cp1252 — the child reports
                cp1252 for ALL three streams, pinned first) answers in UTF-8
                bytes that decode strictly, byte for byte. This suite runs
                on windows-latest too, so the real runner re-proves it.
Runs against the voice MCP checkout located by SEBAS_MCP_ROOT (skipped when
it is not set).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

# Host-truth import plumbing: os.path strings, never pathlib — the CWD
# guard's simulation flips pathlib (test_no_cwd_artifacts.py).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "plugin"))

from _support import mcp_root  # noqa: E402

# The echoed phrase: a natural self-introduction ("my name is Sebas").
# `é` alone exposes the cp1252 mojibake pair ('Ã©'); the trailing '€' is
# the byte-level sentinel — its mojibake pair holds 0x82, which cp1252
# cannot encode. The echo rides in the -32601 unknown-method message,
# which quotes the method verbatim — a round trip that touches no engine.
ACCENTED_METHOD = "meu nome é Sebas €"


def _request(rid: int) -> bytes:
    """One JSON-RPC request line, exactly as a UTF-8 host sends it: the
    accented characters are literal UTF-8 bytes, never \\u escapes."""
    return (json.dumps({"jsonrpc": "2.0", "id": rid, "method": ACCENTED_METHOD},
                       ensure_ascii=False) + "\n").encode("utf-8")


class InProcessAnsiStdioTest(unittest.TestCase):
    """main() starts with the three streams on cp1252 (the Windows spawn
    condition) and must still read the wire as UTF-8."""

    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import server
        self.server = server

    def test_main_decodesutf8_after_a_cp1252_start(self):
        """A cp1252 stdin that carries the UTF-8 request is reconfigured to
        UTF-8 before the first line is read: the reply quotes the accented
        method intact — never 'Ã©', never a lone '©' (the spoken bug)."""
        stdin = io.TextIOWrapper(io.BytesIO(_request(2)), encoding="cp1252")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), \
             mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sys, "stderr", err):
            self.server.main()

        self.assertEqual(stdin.encoding, "utf-8")   # reconfigure ran over ANSI
        replies = [json.loads(l) for l in out.getvalue().strip().splitlines()]
        self.assertEqual(len(replies), 1)           # exactly one -32601 reply
        msg = replies[0]
        self.assertEqual(msg["id"], 2)
        self.assertEqual(msg["error"]["code"], -32601)
        message = msg["error"]["message"]
        self.assertIn(ACCENTED_METHOD, message)
        self.assertNotIn("Ã©", message)             # the misread pair
        self.assertNotIn("©", message)              # what espeak spoke aloud


class StdioReconfigureIsGuardedTest(unittest.TestCase):
    """_use_utf8_stdio must survive streams that cannot be reconfigured:
    captured streams (StringIO — what the payload tests install as stdout)
    and pre-3.7 interpreters simply gain nothing and never crash startup."""

    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import server
        self.server = server

    def test_stringio_streams_start_without_reconfigure(self):
        with mock.patch.object(sys, "stdin", io.StringIO()), \
             mock.patch.object(sys, "stdout", io.StringIO()), \
             mock.patch.object(sys, "stderr", io.StringIO()):
            self.server._use_utf8_stdio()           # must NOT raise

    def test_streams_without_reconfigure_attribute_are_survived(self):
        class _Stub:                                # pre-3.7-like: no method
            encoding = "cp1252"

        stubs = [_Stub(), _Stub(), _Stub()]
        with mock.patch.object(sys, "stdin", stubs[0]), \
             mock.patch.object(sys, "stdout", stubs[1]), \
             mock.patch.object(sys, "stderr", stubs[2]):
            self.server._use_utf8_stdio()
        self.assertTrue(all(s.encoding == "cp1252" for s in stubs))


def _ansi_child_env() -> dict:
    """The portable replica of the Windows spawn environment: piped stdio in
    the system ANSI codepage. Windows builds its pipe wrappers with the ANSI
    page (cp1252 on US-locale hosts, GitHub windows-latest among them)
    whenever PYTHONUTF8/PYTHONIOENCODING are absent; PYTHONIOENCODING=cp1252
    reproduces exactly that condition on every host, deterministically."""
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONLEGACYWINDOWSSTDIO", None)
    env["PYTHONIOENCODING"] = "cp1252"
    return env


@unittest.skipIf(
    (os.environ.get("SEBAS_MCP_ROOT") or "").strip() == "",
    "set SEBAS_MCP_ROOT to the voice MCP checkout")
class SubprocessWindowsSpawnTest(unittest.TestCase):
    """The real server.py, spawned the way an MCP host spawns it, in the
    Windows ANSI-stdio environment, over raw pipes (no text/encoding help
    from the parent — the parent must only write and read BYTES)."""

    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        self.server_py = root / "server.py"

    def test_the_child_environment_really_starts_in_the_ansi_codepage(self):
        """Pins the simulation first: with PYTHONIOENCODING=cp1252 the child
        reports cp1252 for ALL THREE streams — on every host — which is the
        exact condition a Windows spawn produces without the env vars."""
        done = subprocess.run(
            [sys.executable, "-c",
             "import sys; print(sys.stdin.encoding,"
             " sys.stdout.encoding, sys.stderr.encoding)"],
            capture_output=True, text=True, env=_ansi_child_env(),
            timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["cp1252", "cp1252", "cp1252"])

    def test_spawned_server_answers_and_replies_decode_as_utf8(self):
        """initialize + the accented echo through raw pipes: the fixed server
        reconfigures BOTH directions, so the reply bytes decode strictly as
        UTF-8. Without the fix the initialize reply's em-dashes land on the
        pipe as cp1252 bytes (0x96) and strict decoding FAILS here — the
        same corruption the user heard aloud."""
        line1 = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {}})
        line2 = json.dumps({"jsonrpc": "2.0", "id": 2, "method": ACCENTED_METHOD})
        requests = (line1 + "\n" + line2 + "\n").encode("utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(self.server_py)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=_ansi_child_env())
        out, err = proc.communicate(requests, timeout=60)
        self.assertEqual(proc.returncode, 0, err)

        try:
            text = out.decode("utf-8")
        except UnicodeDecodeError as e:
            self.fail(f"the server replied in encoded bytes, not UTF-8 ({e})")
        replies = [json.loads(l) for l in text.strip().splitlines()]
        self.assertEqual(len(replies), 2)

        init = replies[0]
        self.assertEqual(init["id"], 1)
        self.assertEqual(init["result"]["serverInfo"]["name"], "voice")
        # The instructions the reply quotes are house text with non-ASCII
        # em-dashes — the write direction is UTF-8 too.
        self.assertIn("—", init["result"]["instructions"])

        echo = replies[1]
        self.assertEqual(echo["id"], 2)
        self.assertEqual(echo["error"]["code"], -32601)
        self.assertIn(ACCENTED_METHOD, echo["error"]["message"])


if __name__ == "__main__":
    unittest.main()
