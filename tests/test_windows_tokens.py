"""Windows click-URI security: capability tokens and registry writes.

What is pinned here:
  * the button URI carries a random single-use token, NEVER the message text;
  * unknown, expired and already-consumed tokens play nothing;
  * hostile URI strings (quotes, backticks, %, spaces, path separators) can
    reach neither a shell, a subprocess nor the daemon;
  * the token store is bounded (TTL, count, size);
  * the protocol handler registry is check-before-write, so repeat calls and
    dry runs have zero side effects.

Everything runs against a temp token store and a fake winreg module: no
registry, no subprocess, no socket, no daemon, no audio.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

# Host-truth import plumbing: os.path strings, never pathlib — the CWD
# guard's simulation flips pathlib (test_no_cwd_artifacts.py).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


windows = _load("notify_windows_tokens_under_test", "plugin/notify/windows.py")


class FakeKey:
    def __init__(self, registry, subkey):
        self.registry = registry
        self.subkey = subkey

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg(types.ModuleType):
    """Records every write; raises OSError for missing keys like winreg."""

    HKEY_CURRENT_USER = "HKEY_CURRENT_USER"
    REG_SZ = 1

    def __init__(self):
        super().__init__("winreg")
        self.values = {}
        self.writes = []

    def CreateKey(self, hive, subkey):
        self.values.setdefault(subkey, {})
        return FakeKey(self, subkey)

    def OpenKey(self, hive, subkey):
        if subkey not in self.values:
            raise OSError(2, "registry key not found")
        return FakeKey(self, subkey)

    def QueryValueEx(self, key, name):
        try:
            return (self.values[key.subkey][name], self.REG_SZ)
        except KeyError:
            raise OSError(2, "registry value not found")

    def SetValueEx(self, key, name, reserved, regtype, value):
        self.writes.append((key.subkey, name, value))
        self.values.setdefault(key.subkey, {})[name] = value


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ,
                                  {"SEBAS_CARD_TOKEN_DIR": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _files(self):
        return sorted(os.listdir(self._tmp.name))

    # ------------------------------------------------------------- rejection
    def test_unknown_token_is_rejected(self):
        with mock.patch.object(windows, "_send_play_request",
                               side_effect=AssertionError("must not play")) as send:
            result = windows._play_from_uri("sebascard://play/" + "A" * 32)
        self.assertEqual(result["status"], "error")
        self.assertIn("unknown", result["problem"])
        send.assert_not_called()

    def test_expired_token_is_rejected_and_deleted(self):
        now = windows.time.time()
        with mock.patch.object(windows.time, "time",
                               return_value=now - windows.TOKEN_TTL_SECONDS - 1):
            token = windows._mint_token("old message")
        with mock.patch.object(windows, "_send_play_request",
                               side_effect=AssertionError("must not play")):
            result = windows._play_from_uri(windows.card_uri(token))
        self.assertEqual(result["status"], "error")
        self.assertIn("expired", result["problem"])
        self.assertEqual(self._files(), [])          # expired file removed

    def test_token_is_single_use(self):
        token = windows._mint_token("play me once")
        with mock.patch.object(windows, "_send_play_request",
                               return_value={"status": "ok"}) as send:
            first = windows._play_from_uri(windows.card_uri(token))
            second = windows._play_from_uri(windows.card_uri(token))
        self.assertEqual(first["status"], "ok")
        send.assert_called_once_with("play me once")  # replay plays nothing
        self.assertEqual(second["status"], "error")
        self.assertIn("unknown", second["problem"])
        self.assertEqual(self._files(), [])           # consumed = deleted

    def test_two_clicks_on_one_token_play_exactly_once(self):
        """Two clicks racing on the SAME token URI: the atomic claim (rename)
        makes exactly one play and the other is refused — a double click can
        never speak the message twice."""
        token = windows._mint_token("play me once")
        gate = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def click():
            gate.wait(5)
            result = windows._play_from_uri(windows.card_uri(token))
            with lock:
                results.append(result)

        with mock.patch.object(windows, "_send_play_request",
                               return_value={"status": "ok"}) as send:
            threads = [threading.Thread(target=click, daemon=True)
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)

        send.assert_called_once_with("play me once")  # exactly one plays
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(r["status"] for r in results), ["error", "ok"])
        loser = next(r for r in results if r["status"] == "error")
        self.assertIn("unknown", loser["problem"])
        self.assertEqual(self._files(), [])           # consumed = deleted

    def test_race_loser_is_unknown_never_malformed_on_windows_rename(self):
        """A race loser is "unknown", NEVER "malformed" — with the Windows
        claim-race rename semantics (the windows-latest failure) simulated:
        MoveFileEx renames the file it OPENED, not the name it was looked up
        under, so the losing click's claim can land on the winner's in-flight
        .claim-*.json and move it away before the winner reads it. Whatever
        the OS does to a click whose token was already consumed, the answer
        is "unknown" on every platform; "malformed" is reserved for a token
        file that exists but is not a valid record."""
        token = windows._mint_token("play me once")
        real_rename = os.rename
        lock = threading.Lock()
        calls = []
        claimed = threading.Event()
        stolen = threading.Event()

        def windows_rename(src, dst):
            with lock:
                calls.append(None)
                first = len(calls) == 1
            if first:
                real_rename(src, dst)       # the winning claim
                claimed.set()
                stolen.wait(10)             # the move lands before the winner
                return                      # reads — the windows-latest window
            claimed.wait(10)                # the claim must exist first
            names = [n for n in os.listdir(self._tmp.name)
                     if n.startswith(".claim-")]
            real_rename(os.path.join(self._tmp.name, names[0]), dst)  # steal
            stolen.set()

        gate = threading.Barrier(2)
        results = []
        results_lock = threading.Lock()

        def click():
            gate.wait(5)
            result = windows._play_from_uri(windows.card_uri(token))
            with results_lock:
                results.append(result)

        with mock.patch.object(windows.os, "rename", windows_rename), \
             mock.patch.object(windows, "_send_play_request",
                               return_value={"status": "ok"}) as send:
            threads = [threading.Thread(target=click, daemon=True)
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)

        send.assert_called_once_with("play me once")   # exactly one plays
        self.assertEqual(len(results), 2)
        loser = next(r for r in results if r["status"] == "error")
        self.assertIn("unknown", loser["problem"])     # consumed = unknown
        self.assertNotIn("malformed", loser["problem"])  # never "malformed"
        self.assertEqual(self._files(), [])            # consumed = deleted

    def test_text_never_travels_in_the_uri(self):
        text = 'secret "message" with `backticks` & %vars% and spaces'
        uri = windows.card_uri(windows._mint_token(text))
        self.assertNotIn("secret", uri)
        self.assertNotIn(text, uri)
        self.assertNotIn(" ", uri)
        self.assertNotIn('"', uri)

    # ---------------------------------------------------- hostile URI grammar
    def test_hostile_uris_never_reach_a_shell_or_the_daemon(self):
        hostile = [
            'sebascard://play/abc"; start calc; "',
            'sebascard://play/`id`AAAAAAAA',
            'sebascard://play/%COMSPEC%/x',
            "sebascard://play/$(id)AAAAAAAA",
            'sebascard://play/../../evil',
            'sebascard://play/short',
            'sebascard://play/' + 'A' * 65,
            'sebascard://play/AAAA\nBBBB',
            'sebascard://play/d/abc"; start calc; "',
            'sebascard://play/..%5cwindows',
        ]
        for uri in hostile:
            with self.subTest(uri=uri), \
                 mock.patch.object(windows, "_send_play_request",
                                   side_effect=AssertionError("must not play")), \
                 mock.patch.object(windows.subprocess, "Popen",
                                   side_effect=AssertionError("must not spawn")), \
                 mock.patch.object(socket, "socket",
                                   side_effect=AssertionError("must not connect")):
                result = windows._play_from_uri(uri)
            self.assertEqual(result["status"], "error", uri)
            self.assertTrue(result["next_step"], uri)

    def test_card_uri_rejects_hostile_tokens(self):
        for token in ('abc"; calc; "', "`id`", "%COMSPEC%", "../evil",
                      "has space", "a" * 65, "short", "a\nb"):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    windows.card_uri(token)

    def test_cli_refuses_injected_extra_argv(self):
        """Handler mode is exactly `--play-uri <uri>`: extra argv (a `"%1"`
        quote breakout) is refused and nothing is played or shown."""
        hostile = 'sebascard://play/abc"; start calc; "'
        with mock.patch.object(windows, "_send_play_request",
                               side_effect=AssertionError("must not play")), \
             mock.patch.object(windows, "show_card",
                               side_effect=AssertionError("must not show")), \
             mock.patch.object(windows.subprocess, "Popen",
                               side_effect=AssertionError("must not spawn")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = windows.main(["--play-uri", hostile,
                                 "extra-arg", "--garbage", "%1"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "error")

    def test_cli_extra_argv_cannot_switch_a_valid_uri_into_card_mode(self):
        """Injected --text after --play-uri must never show a card: the whole
        invocation is refused, no fall-through to the CLI modes."""
        with mock.patch.object(windows, "_send_play_request",
                               side_effect=AssertionError("must not play")), \
             mock.patch.object(windows, "show_card",
                               side_effect=AssertionError("must not show")), \
             mock.patch.object(windows.subprocess, "Popen",
                               side_effect=AssertionError("must not spawn")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = windows.main(["--play-uri",
                                 windows.card_uri(windows._new_token(), dry=True),
                                 "--text", "injected card"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "error")

    def test_handler_mode_empty_uri_cannot_spoof_a_card(self):
        """The `"%1"` quote breakout: an empty --play-uri value plus an
        injected --text must never reach the show path (it used to fall
        through to card mode and spoof a toast with the attacker's text)."""
        with mock.patch.object(windows, "_send_play_request",
                               side_effect=AssertionError("must not play")), \
             mock.patch.object(windows, "show_card",
                               side_effect=AssertionError("must not show")), \
             mock.patch.object(windows.subprocess, "Popen",
                               side_effect=AssertionError("must not spawn")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = windows.main(["--play-uri", "",
                                 "--text", "spoofed message", "--no-play"])
        self.assertEqual(code, 1)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertIn("handler mode", result["problem"])

    # ----------------------------------------------------------- store bounds
    def test_oversized_text_is_rejected(self):
        with self.assertRaises(ValueError):
            windows._mint_token("x" * (windows.TOKEN_MAX_TEXT_BYTES + 1))
        result = windows.show_card("x" * (windows.TOKEN_MAX_TEXT_BYTES + 1),
                                   play=False)
        self.assertEqual(result["status"], "error")
        self.assertIn("too long", result["problem"])

    def test_store_count_is_bounded(self):
        for i in range(windows.TOKEN_MAX_COUNT + 10):
            windows._mint_token(f"message {i}")
        self.assertLessEqual(len(self._files()), windows.TOKEN_MAX_COUNT)

    def test_token_files_are_private(self):
        if os.name == "nt":
            self.skipTest("POSIX file modes only (Windows chmod carries just "
                          "the read-only bit)")
        token = windows._mint_token("private")
        mode = os.stat(Path(self._tmp.name) / f"{token}.json").st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_purge_never_deletes_an_inflight_claim(self):
        """A concurrent click's .claim-*.json is IN-FLIGHT: both purge loops
        (expired and oldest-over-keep) must leave it alone, or that single
        click is silently lost."""
        token = windows._mint_token("message")
        store = Path(self._tmp.name)
        claim = store / f".claim-{windows.secrets.token_hex(8)}.json"
        claim.write_text("{}", encoding="utf-8")
        old = windows.time.time() - windows.TOKEN_TTL_SECONDS - 1
        os.utime(claim, (old, old))          # looks expired to the TTL loop
        windows._purge_tokens(store, keep=0)  # and over the count limit too
        self.assertTrue(claim.exists())       # the click is still served
        self.assertFalse((store / f"{token}.json").exists())   # token purged

    def test_token_dir_is_private(self):
        if os.name == "nt":
            self.skipTest("POSIX file modes only (Windows chmod carries just "
                          "the read-only bit)")
        store = Path(self._tmp.name) / "store"
        with mock.patch.dict(os.environ, {"SEBAS_CARD_TOKEN_DIR": str(store)}):
            directory = windows._ensure_token_dir()
        self.assertEqual(directory, store)
        self.assertEqual(os.stat(store).st_mode & 0o077, 0)   # no group/other

    def test_token_dir_owned_by_another_user_is_refused(self):
        """Shared-TEMP pre-creation: a store owned by ANOTHER user is never
        used — no token is minted, nothing is written there, and the card
        refuses with a precise problem instead of losing clicks."""
        if not hasattr(os, "getuid"):
            self.skipTest("POSIX uid ownership only (Windows has no "
                          "st_uid/getuid; NTFS ACLs are the boundary there)")
        store = Path(self._tmp.name) / "store"
        store.mkdir()                              # pre-created "by them"
        with mock.patch.dict(os.environ, {"SEBAS_CARD_TOKEN_DIR": str(store)}):
            with mock.patch.object(windows.os, "getuid",
                                   return_value=os.getuid() + 1):
                with self.assertRaises(PermissionError):
                    windows._mint_token("message")
                with mock.patch.object(windows, "_ensure_protocol_handler",
                                       side_effect=AssertionError("must not register")), \
                     mock.patch.object(windows.subprocess, "Popen",
                                       side_effect=AssertionError("must not spawn")):
                    result = windows.show_card("message")
        self.assertEqual(result["status"], "error")
        self.assertIn("owned by uid", result["problem"])
        self.assertEqual(os.listdir(store), [])    # nothing written there


class RegistryTest(unittest.TestCase):
    """Check-before-write: dry runs and repeat calls never write the registry."""

    def setUp(self):
        self.winreg = FakeWinreg()
        patcher = mock.patch.dict(sys.modules, {"winreg": self.winreg})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _command_key(self):
        return rf"Software\Classes\{windows.SCHEME}\shell\open\command"

    def test_written_when_missing(self):
        result = windows._ensure_protocol_handler()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["changed"])
        self.assertTrue(self.winreg.writes)
        self.assertEqual(self.winreg.values[self._command_key()][""],
                         windows._handler_command())

    def test_check_before_write_when_current(self):
        key = self._command_key()
        self.winreg.values[key] = {"": windows._handler_command()}
        result = windows._ensure_protocol_handler()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["changed"])
        self.assertEqual(self.winreg.writes, [])      # zero side effects

    def test_rewritten_when_stale(self):
        self.winreg.values[self._command_key()] = {"": "old-command"}
        result = windows._ensure_protocol_handler()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["changed"])
        self.assertEqual(self.winreg.values[self._command_key()][""],
                         windows._handler_command())

    def test_read_only_mode_never_writes(self):
        result = windows._ensure_protocol_handler(write=False)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.winreg.writes, [])

    def test_dry_run_show_card_never_writes_the_registry(self):
        for existing in (None, "old-command", windows._handler_command()):
            with self.subTest(existing=existing):
                self.winreg.values.clear()
                self.winreg.writes.clear()
                if existing is not None:
                    self.winreg.values[self._command_key()] = {"": existing}
                with tempfile.TemporaryDirectory() as tmp, \
                     mock.patch.dict(os.environ, {"SEBAS_CARD_TOKEN_DIR": tmp}), \
                     mock.patch.object(windows, "_find_powershell",
                                       return_value="pwsh"), \
                     mock.patch.object(windows, "_probe_daemon",
                                       side_effect=AssertionError("no daemon")), \
                     mock.patch.object(windows.subprocess, "Popen"):
                    result = windows.show_card("dry run", play=False)
                    self.assertEqual(result["status"], "shown", existing)
                    self.assertEqual(self.winreg.writes, [], existing)
                    self.assertEqual(os.listdir(tmp), [], existing)  # no token


if __name__ == "__main__":
    unittest.main()
