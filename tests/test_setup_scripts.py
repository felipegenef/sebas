"""The setup/launch scripts: source-level contracts nothing can unit-test.

Spawning a shell is off-limits to the suite (host-independence rule), so the
script BEHAVIORS the first-run feature depends on are pinned as source
contracts here — they are the mutation net for the manual-review surface: the
fresh-machine interpreter fallback that makes the 'installing' state reachable
before any venv exists, the torn-venv self-heal, the download safety checks,
and the one data-dir rule every implementation must share. The scripts are
located through SEBAS_MCP_ROOT like every other MCP test (skipped when unset).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from _support import mcp_root


class _ScriptsCase(unittest.TestCase):
    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        self.run_sh = (root / "run.sh").read_text(encoding="utf-8")
        self.setup_sh = (root / "setup.sh").read_text(encoding="utf-8")
        self.setup_ps1 = (root / "setup.ps1").read_text(encoding="utf-8")


class FreshMachineFallbackTest(_ScriptsCase):
    """run.sh must start the server BEFORE the venv exists (the blocker)."""

    def test_venv_interpreter_is_preferred_but_never_required(self):
        self.assertIn('PY="$DATA/venv/bin/python"', self.run_sh)
        # The venv is checked BEFORE the fallback is consulted.
        self.assertLess(self.run_sh.index('[ ! -x "$PY" ]'),
                        self.run_sh.index("command -v python3.13"))
        # The fresh-machine fallback: newest system interpreter first — the
        # same fallback config.ts uses on Windows.
        self.assertIn("command -v python3.13 || command -v python3.12 "
                      "|| command -v python3", self.run_sh)
        # The old hard abort on a missing venv is gone.
        self.assertNotIn('voice: venv missing. Run:', self.run_sh)

    def test_no_python_at_all_still_fails_loudly(self):
        self.assertIn("exit 1", self.run_sh)
        self.assertIn("no python3 on PATH", self.run_sh)


class TornVenvSelfHealTest(_ScriptsCase):
    """A torn venv (dir without an interpreter) must be recreated, never
    skipped over — in both installers."""

    def test_setup_sh_guards_on_the_interpreter_and_recreates(self):
        self.assertIn('[ ! -x "$VENV/bin/python" ]', self.setup_sh)
        self.assertNotIn('[ ! -d "$VENV" ]', self.setup_sh)  # the old, broken key
        self.assertIn('rm -rf "$VENV"', self.setup_sh)
        # The removal happens BEFORE the recreation.
        self.assertLess(self.setup_sh.index('rm -rf "$VENV"'),
                        self.setup_sh.index('"$PY" -m venv "$VENV"'))

    def test_setup_ps1_mirrors_the_same_key(self):
        self.assertIn("Test-Path $VenvPy", self.setup_ps1)
        self.assertIn("Remove-Item -Recurse -Force $Venv", self.setup_ps1)
        self.assertLess(self.setup_ps1.index("Remove-Item -Recurse -Force $Venv"),
                        self.setup_ps1.index("& py $ver -m venv $Venv"))


class DownloadSafetyTest(_ScriptsCase):
    """An HTTP error body must never become a model file."""

    def test_setup_sh_uses_curl_fail_and_a_minimum_size(self):
        self.assertIn("curl -sSLf -o", self.setup_sh)
        self.assertNotIn("curl -sSL -o", self.setup_sh)  # the unguarded form
        self.assertIn('"$(wc -c < "$target.part")" -ge 1024', self.setup_sh)
        # The size check runs BEFORE the rename that makes it look complete.
        self.assertLess(self.setup_sh.index("wc -c"),
                        self.setup_sh.index('mv -f "$target.part"'))

    def test_setup_ps1_mirrors_curl_fail_and_the_minimum_size(self):
        self.assertIn("-sSLf -o", self.setup_ps1)
        self.assertNotIn("-sSL -o", self.setup_ps1)
        self.assertIn(".Length -ge 1024", self.setup_ps1)
        self.assertLess(self.setup_ps1.index(".Length -ge 1024"),
                        self.setup_ps1.index("Move-Item -Force $partial"))


class DataDirNormalizationTest(_ScriptsCase):
    """The one data-dir rule, with the TS/Python .strip() semantics."""

    def _assert_trimmed(self, text: str) -> None:
        self.assertIn('${XDG_DATA_HOME#"${XDG_DATA_HOME%%[![:space:]]*}"}', text)
        self.assertIn('${DATA_HOME%"${DATA_HOME##*[![:space:]]}"}', text)

    def test_both_shells_trim_xdg_and_detect_the_legacy_dir_with_e(self):
        for text in (self.run_sh, self.setup_sh):
            self._assert_trimmed(text)
            # -e, not -d: existence of ANY entry, like existsSync/Path.exists.
            self.assertIn('[ ! -e "$DATA" ] && [ -e "$DATA_HOME/voz" ]', text)
            self.assertNotIn('[ -d "$DATA_HOME/voz" ]', text)

    def test_powershell_trims_too_and_uses_test_path(self):
        self.assertIn("$env:XDG_DATA_HOME.Trim()", self.setup_ps1)
        self.assertIn('Test-Path (Join-Path $DataHome "voz")', self.setup_ps1)


if __name__ == "__main__":
    unittest.main()
