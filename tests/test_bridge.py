"""SEBAS_NOTIFY_PATH fallbacks: unset or invalid -> the old spoken flow.

The bridge is the only place the voice MCP learns about the notify/ package,
so every failure mode must come back as a payload (never an exception) so
the caller can fall back to the spoken short notice + chat confirmation.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from _support import PLUGIN_ROOT, mcp_root


class BridgeTest(unittest.TestCase):
    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from voice import notify_bridge
        self.bridge = notify_bridge

    def test_unset_env_is_unavailable(self):
        env = dict(os.environ)
        env.pop("SEBAS_NOTIFY_PATH", None)
        with mock.patch.dict(os.environ, env, clear=True):
            result = self.bridge.show_card("Full message", play=False)
            self.assertFalse(self.bridge.available())
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("SEBAS_NOTIFY_PATH", result["problem"])
        self.assertIn("fall back", result["next_step"].lower())

    def test_invalid_path_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SEBAS_NOTIFY_PATH": tmp}):
                result = self.bridge.show_card("Full message", play=False)
                self.assertFalse(self.bridge.available())
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("notify", result["problem"])

    def test_valid_package_forwards_the_contract_kwargs(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "notify"
            pkg.mkdir()
            (pkg / "__init__.py").write_text(
                "def show_card(text, *, butler='Sebas', language='en-us', "
                "context=None, urgency='critical', play=True):\n"
                "    return {'status': 'shown', 'echo': {'text': text, "
                "'butler': butler, 'language': language, 'context': context, "
                "'urgency': urgency, 'play': play}}\n",
                encoding="utf-8")
            with mock.patch.dict(os.environ, {"SEBAS_NOTIFY_PATH": tmp}):
                self.assertTrue(self.bridge.available())
                result = self.bridge.show_card("Full message", butler="Sebas",
                                               language="pt-br", context="Agente X",
                                               urgency="critical", play=True)
        self.assertEqual(result["status"], "shown")
        self.assertEqual(result["echo"],
                         {"text": "Full message", "butler": "Sebas",
                          "language": "pt-br", "context": "Agente X",
                          "urgency": "critical", "play": True})

    def test_broken_package_is_a_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "notify"
            pkg.mkdir()
            (pkg / "__init__.py").write_text(
                "def show_card(*args, **kwargs):\n"
                "    raise RuntimeError('boom')\n",
                encoding="utf-8")
            with mock.patch.dict(os.environ, {"SEBAS_NOTIFY_PATH": tmp}):
                result = self.bridge.show_card("Full message", play=False)
        self.assertEqual(result["status"], "error")
        self.assertIn("boom", result["problem"])

    def test_the_real_notify_package_resolves(self):
        # Integration check against the notify/ package shipped inside the
        # plugin: empty text is rejected before any card is created, so
        # nothing reaches the desktop.
        with mock.patch.dict(os.environ, {"SEBAS_NOTIFY_PATH": str(PLUGIN_ROOT)}):
            self.assertTrue(self.bridge.available())
            result = self.bridge.show_card("", play=False)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["problem"], "empty text")


if __name__ == "__main__":
    unittest.main()
