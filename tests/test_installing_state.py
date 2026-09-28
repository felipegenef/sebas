"""The first-run 'installing' state: speak and voice_status answer clearly
while the voice runtime setup is still creating the venv or downloading the
weights, instead of a traceback.

Everything runs against throwaway directories — the real data dir is never
read — and nothing ever spawns: the daemon and engine paths are mocked and
asserted untouched. Runs against the voice MCP checkout located by
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

from _support import mcp_root


class _VoiceCase(unittest.TestCase):
    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import tools
        from voice import core, engine
        self.tools = tools
        self.core = core
        self.engine = engine
        # The runtime layout every check reads: a throwaway dir, never the
        # machine's real one. venv_python knows the per-platform layout (and
        # returns a PURE path — wrap it for real filesystem work). A
        # Windows-flavour wrap is a CWD-RELATIVE name on a POSIX host, so the
        # fixture runs with the CWD inside the throwaway dir: the wrapped
        # names materialize there — where runtime_missing's identical wrap
        # finds them — and never in the repository CWD (see
        # test_no_cwd_artifacts.py). The CWD is restored BEFORE the throwaway
        # dir is removed (addCleanup runs LIFO).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        outer_cwd = os.getcwd()
        os.chdir(self.data)
        self.addCleanup(os.chdir, outer_cwd)
        self.venv_py = Path(str(core.venv_python(self.data)))
        self.onnx = self.data / "models" / "kokoro" / "kokoro-v1.0.onnx"
        self.voices = self.data / "models" / "kokoro" / "voices-v1.0.bin"
        for patcher in (
                mock.patch.object(core, "DATA", self.data),
                mock.patch.object(core, "KOKORO_ONNX", self.onnx),
                mock.patch.object(core, "KOKORO_VOICES", self.voices)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_complete(self):
        for path in (self.venv_py, self.onnx, self.voices):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")


class RuntimeMissingTest(_VoiceCase):
    def test_complete_runtime_reports_nothing_missing(self):
        self.make_complete()
        self.assertEqual(self.core.runtime_missing(), [])

    def test_each_piece_is_named_when_absent(self):
        self.venv_py.parent.mkdir(parents=True, exist_ok=True)
        self.venv_py.write_bytes(b"")
        self.assertEqual(self.core.runtime_missing(),
                         ["kokoro-v1.0.onnx", "voices-v1.0.bin"])
        self.onnx.parent.mkdir(parents=True, exist_ok=True)
        self.onnx.write_bytes(b"")
        self.assertEqual(self.core.runtime_missing(), ["voices-v1.0.bin"])
        self.voices.parent.mkdir(parents=True, exist_ok=True)
        self.voices.write_bytes(b"")
        self.assertEqual(self.core.runtime_missing(), [])
        self.venv_py.unlink()                 # a half-broken venv is noticed
        self.assertEqual(self.core.runtime_missing(), ["venv"])

    def test_fresh_machine_reports_all_three(self):
        self.assertEqual(self.core.runtime_missing(),
                         ["venv", "kokoro-v1.0.onnx", "voices-v1.0.bin"])


class InstallingPayloadTest(_VoiceCase):
    def test_none_when_the_runtime_is_complete(self):
        self.make_complete()
        self.assertIsNone(self.core.installing_payload())

    def test_payload_is_house_style_with_a_portable_next_step(self):
        payload = self.core.installing_payload()
        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "installing")
        self.assertEqual(payload["missing"],
                         ["venv", "kokoro-v1.0.onnx", "voices-v1.0.bin"])
        step = payload["next_step"]
        self.assertIn("automatically", step)       # the plugin installs it
        self.assertIn("setup.log", step)           # where to watch progress
        self.assertIn("WITHOUT a restart", step)   # the whole point
        self.assertIn("setup.sh", step)            # the manual fallback
        self.assertIn("setup.ps1", step)
        self.assertNotIn(str(Path.home()), step)   # portability convention

    def test_speak_answers_installing_and_never_touches_the_daemon(self):
        with mock.patch.object(self.tools, "_daemon_request") as daemon_req, \
             mock.patch.object(self.tools.engine, "speak") as engine_speak:
            payload = self.tools.speak("Full message", play=True, context="X")
        self.assertEqual(payload["status"], "installing")
        daemon_req.assert_not_called()
        engine_speak.assert_not_called()

    def test_voice_status_answers_installing_without_a_probe(self):
        with mock.patch.object(self.tools.engine, "status") as engine_status:
            payload = self.tools.voice_status()
        self.assertEqual(payload["status"], "installing")
        engine_status.assert_not_called()

    def test_engine_speak_answers_installing_without_a_daemon(self):
        with mock.patch.object(self.engine, "_daemon") as daemon_req:
            payload = self.engine.speak("Full message")
        self.assertEqual(payload["status"], "installing")
        daemon_req.assert_not_called()

    def test_engine_status_answers_installing_without_a_daemon(self):
        with mock.patch.object(self.engine, "_daemon") as daemon_req:
            payload = self.engine.status()
        self.assertEqual(payload["status"], "installing")
        daemon_req.assert_not_called()

    def test_warmup_and_measure_rtf_pass_the_payload_through(self):
        with mock.patch.object(self.tools.engine, "speak") as engine_speak:
            warm = self.tools.warmup()
            rtf = self.tools.measure_rtf("x")
        self.assertEqual(warm["status"], "installing")
        self.assertEqual(rtf["status"], "installing")
        self.assertIn("next_step", warm)
        self.assertIn("next_step", rtf)
        engine_speak.assert_not_called()


class ReadyRuntimeTest(_VoiceCase):
    def test_voice_status_flows_through_when_complete(self):
        self.make_complete()
        with mock.patch.object(self.tools.engine, "status",
                               return_value={"engine": "stub"}):
            payload = self.tools.voice_status()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["engine"], "stub")
        self.assertIn("next_step", payload)

    def test_speak_reaches_the_normal_flow_when_complete(self):
        self.make_complete()
        with mock.patch.object(self.tools.notify_bridge, "available",
                               return_value=True), \
             mock.patch.object(self.tools, "_daemon_request",
                               return_value={"status": "ok", "played": True}), \
             mock.patch.object(self.tools.engine, "speak") as engine_speak:
            payload = self.tools.speak("Full message", play=True, context="X")
        self.assertEqual(payload["status"], "ok")
        engine_speak.assert_not_called()   # the card path owns the daemon


if __name__ == "__main__":
    unittest.main()
