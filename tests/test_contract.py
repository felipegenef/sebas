"""The show_card contract every backend must satisfy, plus card strings.

The same contract is what the voice MCP bridge calls and what the macOS and
Windows adapters implement: signature check here catches drift without
touching gi, D-Bus, the daemon socket or the speaker.
"""
from __future__ import annotations

import importlib
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

import notify
import notify.linux  # noqa: F401  (backend module; gi only loads on show)
from _support import fake_gi

CONTRACT_DEFAULTS = {"butler": "Sebas", "language": "en-us", "context": None,
                     "urgency": "critical", "play": True}


class ShowCardContractTest(unittest.TestCase):
    def _assert_signature(self, fn):
        params = inspect.signature(fn).parameters
        self.assertEqual(list(params)[0], "text")
        for name, default in CONTRACT_DEFAULTS.items():
            self.assertIn(name, params)
            self.assertEqual(params[name].kind, inspect.Parameter.KEYWORD_ONLY, name)
            self.assertEqual(params[name].default, default, name)

    def test_dispatcher_signature(self):
        self._assert_signature(notify.show_card)

    def test_linux_signature(self):
        self._assert_signature(notify.linux.show_card)

    def test_other_backends_match_when_present(self):
        found = 0
        for name in ("notify.macos", "notify.windows"):
            try:
                module = importlib.import_module(name)
            except Exception:
                continue
            self._assert_signature(module.show_card)
            found += 1
        if not found:
            self.skipTest("no macOS/Windows backend in this checkout yet")

    def test_dispatcher_accepts_documented_kwargs(self):
        # empty text short-circuits before any platform call: proves the
        # keyword contract with zero side effects
        result = notify.show_card("", butler="Sebas", language="pt-br",
                                  context="Agente X", urgency="normal", play=False)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["problem"], "empty text")

    def test_full_call_with_documented_kwargs(self):
        with fake_gi() as (notify_mod, _glib):
            with mock.patch("notify.linux.play_via_daemon") as play:
                result = notify.show_card("Full message", butler="Jeeves",
                                          language="pt-br", context="Agente X",
                                          urgency="critical", play=False)
        self.assertEqual(result["status"], "shown")
        self.assertEqual(result["title"], "Jeeves - mensagem")
        play.assert_not_called()

    def test_unknown_platform_is_unavailable(self):
        with mock.patch.object(notify.sys, "platform", "plan9"):
            result = notify.show_card("Full message", play=False)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("fall back", result["next_step"])

    def test_dispatcher_normalizes_urgency_before_dispatch(self):
        """urgency='bogus' must never reach a backend raw (Linux would show a
        CRITICAL card, macOS/Windows would error)."""
        urgencies = []

        def fake_show_card(text, **kwargs):
            urgencies.append(kwargs["urgency"])
            return {"status": "shown"}

        fake_module = SimpleNamespace(show_card=fake_show_card)
        with mock.patch.object(notify.importlib, "import_module",
                               return_value=fake_module):
            self.assertEqual(notify.show_card("m", urgency="bogus",
                                              play=False)["status"], "shown")
            notify.show_card("m", urgency=" NORMAL ", play=False)
            notify.show_card("m", urgency="low", play=False)   # 3rd level kept
        self.assertEqual(urgencies, ["critical", "normal", "low"])


class StringsTest(unittest.TestCase):
    def test_en_us(self):
        s = notify.strings_for("en-us", "Sebas")
        self.assertEqual(s["title"], "Sebas - message")
        self.assertEqual(s["button"], "Listen")

    def test_pt_br(self):
        s = notify.strings_for("pt-br", "Sebas")
        self.assertEqual(s["title"], "Sebas - mensagem")
        self.assertEqual(s["button"], "Ouvir mensagem")

    def test_aliases_defaults_and_butler(self):
        self.assertEqual(notify.strings_for("pt")["language"], "pt-br")
        self.assertEqual(notify.strings_for("de")["language"], "en-us")
        self.assertEqual(notify.strings_for("en-us", " Jeeves ")["title"],
                         "Jeeves - message")
        self.assertEqual(notify.strings_for("en-us", "")["title"], "Sebas - message")

    def test_urgency_mapping(self):
        self.assertEqual(notify.LINUX_URGENCY["critical"], "CRITICAL")
        self.assertEqual(notify.LINUX_URGENCY["normal"], "NORMAL")
        self.assertEqual(notify.normalize_urgency("critical"), "critical")
        self.assertEqual(notify.normalize_urgency("NORMAL"), "normal")
        self.assertEqual(notify.normalize_urgency("weird"), "critical")
        self.assertEqual(notify.normalize_urgency(None), "critical")


if __name__ == "__main__":
    unittest.main()
