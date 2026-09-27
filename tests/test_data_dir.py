"""The one data-dir resolution: current dir, legacy fallback, config names.

Everything the voice engine stores — daemon socket, venv, weights, identity,
voice profile, logs — derives from the single resolved data dir, and the
resolution must behave identically in the voice server (voice/core.py) and in
the card adapters (notify/paths.py). These tests pin that rule and the voice
profile filename rule (`config.json`; a pre-1.0 dir keeps `voz.json`), so an
existing install never loses its settings and no path is ever hardcoded.

Nothing here touches the real data dir: every case runs under a temporary
XDG_DATA_HOME. The core/ cases run against the voice MCP checkout located by
SEBAS_MCP_ROOT (skipped when it is not set).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

import notify.paths as paths  # noqa: E402  (card side of the same rule)
from _support import mcp_root  # noqa: E402


class DataDirRuleTest(unittest.TestCase):
    """notify/paths.data_dir and voice/core.resolve_data_dir, same rule."""

    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from voice import core
        self.core = core

    def _both(self, tmp: str) -> tuple[Path, bool]:
        """The rule resolved by both implementations, asserted equal."""
        env = {"XDG_DATA_HOME": tmp}
        core_dir = self.core.resolve_data_dir(env, home=str(Path(tmp) / "home"))
        card_dir = paths.data_dir(env, home=str(Path(tmp) / "home"))
        self.assertEqual((str(core_dir[0]), core_dir[1]),
                         (str(card_dir[0]), card_dir[1]))
        return core_dir

    def test_fresh_machine_uses_the_new_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, is_legacy = self._both(tmp)
        self.assertEqual(data, Path(tmp) / "sebas")
        self.assertFalse(is_legacy)

    def test_legacy_only_install_is_auto_detected_and_used_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "voz").mkdir()
            data, is_legacy = self._both(tmp)
        self.assertEqual(data, Path(tmp) / "voz")
        self.assertTrue(is_legacy)

    def test_the_new_dir_wins_once_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "voz").mkdir()
            (Path(tmp) / "sebas").mkdir()
            data, is_legacy = self._both(tmp)
        self.assertEqual(data, Path(tmp) / "sebas")
        self.assertFalse(is_legacy)

    def test_empty_xdg_falls_back_to_the_default_data_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = str(Path(tmp) / "home")
            data, is_legacy = self.core.resolve_data_dir({"XDG_DATA_HOME": "  "}, home=home)
        self.assertEqual(data, Path(home) / ".local" / "share" / "sebas")
        self.assertFalse(is_legacy)

    def test_profile_file_name_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, is_legacy = self.core.resolve_data_dir(
                {"XDG_DATA_HOME": tmp}, home=str(Path(tmp) / "home"))
            self.assertEqual(self.core.config_file(data, is_legacy).name, "config.json")
            (Path(tmp) / "voz").mkdir()
            data, is_legacy = self.core.resolve_data_dir(
                {"XDG_DATA_HOME": tmp}, home=str(Path(tmp) / "home"))
            self.assertEqual(self.core.config_file(data, is_legacy).name, "voz.json")

    def test_every_storage_path_derives_from_the_one_data_dir(self):
        core = self.core
        self.assertEqual(core.CONFIG, core.config_file(core.DATA, core.DATA_LEGACY))
        self.assertEqual(core.MODELS, core.DATA / "models")
        self.assertEqual(core.OUTPUTS, core.DATA / "outputs")
        self.assertEqual(core.USERS, core.DATA / "users.json")
        # English names everywhere — never Portuguese, not even for legacy.
        for path in (core.MODELS, core.OUTPUTS, core.USERS):
            self.assertNotIn("modelos", str(path))
            self.assertNotIn("saidas", str(path))

    def test_daemon_socket_derives_from_the_same_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_DATA_HOME": tmp}
            data, _ = self.core.resolve_data_dir(env, home=str(Path(tmp) / "home"))
            self.assertEqual(paths.daemon_socket(env, home=str(Path(tmp) / "home")),
                             data / "engine.sock")


class ProfileFileRuleTest(unittest.TestCase):
    """load_config/save_config: config.json, and never lose saved settings."""

    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from voice import core
        self.core = core

    def _patched(self, base: Path, *, legacy: bool):
        """Points core at a throwaway data dir (nothing real is read/written)."""
        config = base / ("voz.json" if legacy else "config.json")
        for attr, value in (("DATA", base), ("CONFIG", config),
                            ("DATA_LEGACY", legacy), ("USERS", base / "users.json"),
                            ("MODELS", base / "models"), ("OUTPUTS", base / "outputs")):
            patcher = mock.patch.object(self.core, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_new_dir_reads_and_writes_config_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._patched(base, legacy=False)
            cfg = self.core.load_config()
            cfg["speed"] = 1.5
            self.core.save_config(cfg)
            self.assertTrue((base / "config.json").is_file())
            self.assertFalse((base / "voz.json").exists())
            self.assertEqual(self.core.load_config()["speed"], 1.5)

    def test_legacy_dir_keeps_its_pre_1_0_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._patched(base, legacy=True)
            cfg = self.core.load_config()
            cfg["voice"] = "pm_santa"
            self.core.save_config(cfg)
            self.assertTrue((base / "voz.json").is_file())
            self.assertFalse((base / "config.json").exists())
            self.assertEqual(self.core.load_config()["voice"], "pm_santa")

    def test_moved_legacy_profile_is_still_read(self):
        """Someone moved an old dir by hand: voz.json sits in the new dir and
        its settings must still load (never lose the voice settings)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "voz.json").write_text('{"speed": 1.75}', encoding="utf-8")
            self._patched(base, legacy=False)
            self.assertEqual(self.core.load_config()["speed"], 1.75)

    def test_config_json_wins_over_a_leftover_legacy_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "voz.json").write_text('{"speed": 1.75}', encoding="utf-8")
            (base / "config.json").write_text('{"speed": 1.25}', encoding="utf-8")
            self._patched(base, legacy=False)
            self.assertEqual(self.core.load_config()["speed"], 1.25)


if __name__ == "__main__":
    unittest.main()
