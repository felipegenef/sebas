"""server.py tools/call failures: house payloads, never a traceback.

The stale-server window — this server process started (usually with a system
interpreter) before the voice runtime finished installing — must not leak a
formatted traceback to the MCP client when the engine's modules are not
importable. Every failure comes back as a 'status'/'problem'/'next_step'
payload, the house shape the agent can act on. Nothing is spawned and no
engine is imported: the handlers under test are stubs that raise on demand.
Runs against the voice MCP checkout located by SEBAS_MCP_ROOT (skipped when
it is not set).
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest import mock

# Host-truth import plumbing: os.path strings, never pathlib — the CWD
# guard's simulation flips pathlib (test_no_cwd_artifacts.py).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "plugin"))

from _support import mcp_root


class ServerPayloadTest(unittest.TestCase):
    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import server
        import tools
        self.server = server
        self.tools = tools

    def call(self, handler):
        """One tools/call through server.handle; returns the tool payload and
        the raw JSON-RPC line (stdout is captured, never the console)."""
        buf = io.StringIO()
        with mock.patch.dict(self.tools.HANDLERS, {"boom": handler}), \
             mock.patch.object(sys, "stdout", buf):
            self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                "params": {"name": "boom", "arguments": {}}})
        out = buf.getvalue().strip().splitlines()
        self.assertEqual(len(out), 1)          # exactly one JSON-RPC reply
        msg = json.loads(out[0])
        self.assertEqual(msg["id"], 7)
        payload = json.loads(msg["result"]["content"][0]["text"])
        return payload, out[0]

    def test_engine_import_error_is_a_house_payload_not_a_traceback(self):
        def boom(**_args):
            raise ImportError("No module named 'soundfile'")
        payload, raw = self.call(boom)
        self.assertEqual(payload["status"], "internal_error")
        self.assertIn("problem", payload)
        self.assertIn("next_step", payload)
        self.assertNotIn("Traceback", raw)     # the whole point
        self.assertNotIn("File \"", raw)
        self.assertIn("not importable", payload["problem"])
        step = payload["next_step"].lower()
        self.assertTrue("reload" in step or "restart" in step)

    def test_any_failure_is_a_house_payload_without_a_traceback(self):
        def boom(**_args):
            raise RuntimeError("kaboom")
        payload, raw = self.call(boom)
        self.assertEqual(payload["status"], "internal_error")
        self.assertIn("RuntimeError", payload["problem"])
        self.assertIn("next_step", payload)
        self.assertNotIn("Traceback", raw)
        self.assertNotIn("File \"", raw)

    def test_type_error_stays_invalid_arguments(self):
        def boom(**_args):
            raise TypeError("unexpected keyword 'nope'")
        payload, _raw = self.call(boom)
        self.assertEqual(payload["status"], "invalid_arguments")
        self.assertIn("next_step", payload)

    def test_a_success_payload_passes_through_unchanged(self):
        def boom(**_args):
            return {"status": "ok", "played": True, "next_step": "Continue."}
        payload, _raw = self.call(boom)
        self.assertEqual(payload, {"status": "ok", "played": True,
                                   "next_step": "Continue."})


if __name__ == "__main__":
    unittest.main()
