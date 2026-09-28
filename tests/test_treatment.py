"""Form of address: how the user likes to be called.

Why: the butler addresses the user exactly as the saved `form_of_address`
says ("senhor", "senhora", "doutor", "chefe"… or the complete vocative like
"senhor Alex") and the spoken greeting uses the same string VERBATIM;
empty/unset falls back to the NEUTRAL default (the plain first name in every
language — never a gendered treatment) and '' clears back to it.
Every test runs against a TEMPORARY users.json/config.json — the real
configuration is never read or written, the daemon socket is never touched,
and no audio is ever played.

Runs against the voice MCP checkout located by SEBAS_MCP_ROOT (skipped when it
is not set).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Host-truth import plumbing: os.path strings, never pathlib — the CWD
# guard's simulation flips pathlib (test_no_cwd_artifacts.py).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "plugin"))

from _support import mcp_root


class _TreatmentCase(unittest.TestCase):
    """voice identity functions against a throwaway config dir: nothing on
    this machine is read, written or played."""

    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import tools
        from voice import core
        self.tools = tools
        self.core = core
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        for attr, name in (("USERS", "users.json"), ("CONFIG", "config.json")):
            patcher = mock.patch.object(core, attr, base / name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_language(self, language: str) -> None:
        """Language straight into the (temporary) users file, no engine work."""
        users = self.core.load_users()
        users["language"] = language
        self.core.save_users(users)


class GreetingTest(_TreatmentCase):
    """core.user_greeting(): form_of_address verbatim, else the neutral
    default (the plain first name)."""

    def test_form_of_address_is_used_verbatim(self):
        self.tools.set_user_name("Alex Doe", form_of_address="senhor Alex")
        self.assertEqual(self.core.user_greeting(), "senhor Alex")

    def test_form_already_containing_the_name_is_not_doubled(self):
        # The form IS the vocative: nothing is appended to it, ever.
        self.tools.set_user_name("Alex Doe",
                                 form_of_address="senhor Alex Doe")
        self.assertEqual(self.core.user_greeting(), "senhor Alex Doe")

    def test_form_without_a_registered_name_still_applies(self):
        users = self.core.load_users()
        users["form_of_address"] = "chefe"
        self.core.save_users(users)
        self.assertEqual(self.core.user_greeting(), "chefe")

    def test_default_greeting_is_neutral_per_language(self):
        self.tools.set_user_name("Alex Doe")
        self.set_language("pt-br")
        self.assertEqual(self.core.user_greeting(), "Alex")
        self.set_language("en-us")
        self.assertEqual(self.core.user_greeting(), "Alex")

    def test_default_greeting_never_guesses_gender_from_the_name(self):
        # The fresh-install bug: pt-br used to prefix "Senhor " to ANY name.
        self.set_language("pt-br")
        self.tools.set_user_name("Natália")
        self.assertEqual(self.core.user_greeting(), "Natália")
        self.set_language("en-us")
        self.assertEqual(self.core.user_greeting(), "Natália")

    def test_no_language_carries_a_gendered_default(self):
        for lang, template in self.core.GREETING.items():
            self.assertEqual(template, "{first}", lang)
            self.assertNotIn("senhor", template.lower())

    def test_cleared_form_restores_the_neutral_default(self):
        self.set_language("pt-br")
        self.tools.set_user_name("Alex Doe", form_of_address="chefe")
        self.assertEqual(self.core.user_greeting(), "chefe")
        self.tools.set_user_name("Alex Doe", form_of_address="")
        self.assertEqual(self.core.user_greeting(), "Alex")

    def test_whitespace_only_form_behaves_as_unset(self):
        self.set_language("pt-br")
        self.tools.set_user_name("Alex Doe", form_of_address="   ")
        self.assertEqual(self.core.user_greeting(), "Alex")

    def test_no_name_and_no_form_gives_empty_greeting(self):
        self.assertEqual(self.core.user_greeting(), "")


class GetUserNamePayloadTest(_TreatmentCase):
    """tools.get_user_name(): the field travels in the payload with an
    example of what the greeting sounds like right now."""

    def test_payload_carries_the_saved_form(self):
        self.tools.set_user_name("Alex Doe", form_of_address="doutor")
        payload = self.tools.get_user_name()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["form_of_address"], "doutor")

    def test_payload_reports_unset_and_cleared_as_none(self):
        self.assertIsNone(self.tools.get_user_name()["form_of_address"])
        self.tools.set_user_name("Alex Doe", form_of_address="chefe")
        self.tools.set_user_name("Alex Doe", form_of_address="")
        self.assertIsNone(self.tools.get_user_name()["form_of_address"])

    def test_greeting_example_sounds_like_the_greeting(self):
        self.tools.set_user_name("Alex Doe", form_of_address="senhor Alex")
        self.assertEqual(self.tools.get_user_name()["greeting_example"],
                         "senhor Alex")

    def test_greeting_example_falls_back_to_the_default(self):
        self.set_language("pt-br")
        self.tools.set_user_name("Alex Doe")
        self.assertEqual(self.tools.get_user_name()["greeting_example"],
                         "Alex")

    def test_greeting_example_when_nothing_is_set(self):
        self.set_language("pt-br")
        self.assertEqual(self.tools.get_user_name()["greeting_example"],
                         "<first name>")
        self.set_language("en-us")
        self.assertEqual(self.tools.get_user_name()["greeting_example"],
                         "<first name>")

    def test_house_payload_shape_is_intact(self):
        payload = self.tools.get_user_name()
        for key in ("status", "butler_name", "language", "main_user", "users",
                    "people", "form_of_address", "greeting_example",
                    "next_step"):
            self.assertIn(key, payload)
        self.assertTrue(payload["next_step"])

    def test_next_step_says_to_ask_when_no_identity_is_saved(self):
        step = self.tools.get_user_name()["next_step"].lower()
        self.assertIn("ask once", step)          # never repeat the question
        self.assertIn("never assume", step)      # never infer gender/name
        self.assertIn("neutral", step)           # neutral until it is known


class SetUserNameTest(_TreatmentCase):
    """tools.set_user_name(..., form_of_address=...): None keeps, a string
    saves (stripped), '' clears; the house payload style is unchanged."""

    def test_saves_the_form_stripped_and_persists(self):
        payload = self.tools.set_user_name("Alex Doe",
                                           form_of_address="  chefe  ")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["form_of_address"], "chefe")
        self.assertEqual(self.core.load_users()["form_of_address"], "chefe")

    def test_omitted_or_none_keeps_the_current_value(self):
        self.tools.set_user_name("Alex Doe", form_of_address="chefe")
        self.tools.set_user_name("Alex Doe")              # omitted
        self.assertEqual(self.core.load_users()["form_of_address"], "chefe")
        self.tools.set_user_name("Sam Doe", main=False, form_of_address=None)
        self.assertEqual(self.core.load_users()["form_of_address"], "chefe")

    def test_empty_string_clears_back_to_the_default(self):
        self.set_language("pt-br")
        self.tools.set_user_name("Alex Doe", form_of_address="chefe")
        payload = self.tools.set_user_name("Alex Doe", form_of_address="")
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(self.core.load_users()["form_of_address"])
        self.assertEqual(self.core.user_greeting(), "Alex")

    def test_whitespace_only_string_clears_too(self):
        self.tools.set_user_name("Alex Doe", form_of_address="chefe")
        self.tools.set_user_name("Alex Doe", form_of_address="   ")
        self.assertFalse(self.core.load_users()["form_of_address"])

    def test_empty_name_is_rejected_with_no_side_effects(self):
        payload = self.tools.set_user_name("  ", form_of_address="chefe")
        self.assertEqual(payload["status"], "invalid_name")
        self.assertEqual(payload["problem"], "empty name")
        self.assertTrue(payload["next_step"])
        users = self.core.load_users()
        self.assertEqual(users["users"], [])
        self.assertFalse(users["form_of_address"])

    def test_house_payload_style_is_intact(self):
        ok = self.tools.set_user_name("Alex Doe", form_of_address="chefe")
        self.assertEqual(ok["status"], "ok")
        self.assertTrue(ok["next_step"])
        self.assertEqual(ok["main_user"], "Alex Doe")
        bad = self.tools.set_user_name(None)
        self.assertEqual(bad["status"], "invalid_name")
        self.assertTrue(bad["next_step"])


class SchemaTest(_TreatmentCase):
    """Agents discover the field through the schema, so it must be there."""

    def _entry(self, name):
        matches = [e for e in self.tools.SCHEMA if e["name"] == name]
        self.assertEqual(len(matches), 1, name)
        return matches[0]

    def test_set_user_name_documents_form_of_address(self):
        entry = self._entry("set_user_name")
        props = entry["inputSchema"]["properties"]
        self.assertIn("form_of_address", props)
        self.assertEqual(props["form_of_address"]["type"], "string")
        self.assertTrue(props["form_of_address"]["description"])
        self.assertEqual(entry["inputSchema"]["required"], ["name"])
        self.assertIn("form_of_address", entry["description"])

    def test_get_user_name_documents_form_and_example(self):
        entry = self._entry("get_user_name")
        self.assertIn("form_of_address", entry["description"])
        self.assertIn("greeting_example", entry["description"])

    def test_schema_says_to_ask_and_never_assume(self):
        for name in ("get_user_name", "set_user_name"):
            desc = self._entry(name)["description"].lower()
            self.assertIn("ask", desc, name)
            self.assertIn("never assume", desc, name)
            self.assertIn("neutral", desc, name)


if __name__ == "__main__":
    unittest.main()
