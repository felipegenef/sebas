"""Windows runtime support: the venv interpreter path and Windows playback.

Two platform rules live in voice/core.py and both must hold without a Windows
box at hand: where the venv interpreter lives (venv_python — Windows and POSIX
layouts) and the Windows playback chain
(winsound first, PowerShell Media.SoundPlayer fallback). The platform is
mocked (os.name / sys.platform) and every player is a stand-in — no test ever
plays audio, opens a speaker or starts a real shell. The POSIX players are
pinned too, so a Windows edit can never break Linux playback, and the macOS
player (afplay, built into macOS) is pinned the same way: first on darwin,
never on Linux. The daemon transport guard is exercised the same way the
notify adapters guard it: the "no AF_UNIX" payload only when NEITHER
transport works, the loopback TCP + token fallback where AF_UNIX is missing.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from _support import af_unix_removed, attr_removed, mcp_root, serve_one  # noqa: E402


def _voice_modules():
    """(core, daemon) from the voice MCP tree; None when SEBAS_MCP_ROOT is
    not set — same skip rule as the other voice-MCP suites."""
    root = mcp_root()
    if root is None:
        return None, None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from voice import core, daemon
    return core, daemon


class FakeWinsound(types.ModuleType):
    """winsound stand-in: records calls, never touches an audio device.
    Mirrors the real flag values so an accidental SND_ASYNC is visible."""

    SND_ASYNC = 0x0001
    SND_FILENAME = 0x00020000

    def __init__(self, fail: bool = False):
        super().__init__("winsound")
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    def PlaySound(self, name: str, flags: int) -> bool:
        self.calls.append((name, flags))
        if self.fail:
            raise RuntimeError("no audio device in the test")
        return True


class VenvPythonTest(unittest.TestCase):
    """venv_python: the ONE place that knows the venv layout per platform."""

    def setUp(self):
        self.core, _ = _voice_modules()
        if self.core is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")

    def test_posix_venv_interpreter(self):
        data = Path("/data/sebas")
        self.assertEqual(self.core.venv_python(data),
                         data / "venv" / "bin" / "python")

    def test_windows_venv_interpreter_via_sys_platform(self):
        data = Path("/data/sebas")
        expected = data / "venv" / "Scripts" / "python.exe"
        with mock.patch("sys.platform", "win32"):
            self.assertEqual(self.core.venv_python(data), expected)

    def test_windows_venv_interpreter_via_os_name(self):
        data = Path("/data/sebas")
        expected = data / "venv" / "Scripts" / "python.exe"
        with mock.patch.object(os, "name", "nt"):
            self.assertEqual(self.core.venv_python(data), expected)


class PlayFileWindowsTest(unittest.TestCase):
    """play_file on Windows: winsound (synchronous), then PowerShell."""

    def setUp(self):
        self.core, _ = _voice_modules()
        if self.core is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")

    def test_winsound_first_and_synchronous(self):
        """winsound plays and nothing else runs. No SND_ASYNC bit: the
        daemon's turn lock needs play_file to return only when the sound
        ENDED, or two voices could overlap."""
        fake = FakeWinsound()
        with mock.patch("sys.platform", "win32"), \
             mock.patch.dict(sys.modules, {"winsound": fake}), \
             mock.patch.object(subprocess, "run") as run:
            ok = self.core.play_file("/data/outputs/speak_1.wav")
        self.assertTrue(ok)
        self.assertEqual(fake.calls,
                         [("/data/outputs/speak_1.wav", FakeWinsound.SND_FILENAME)])
        self.assertEqual(fake.calls[0][1] & FakeWinsound.SND_ASYNC, 0)
        run.assert_not_called()

    def test_powershell_fallback_quotes_the_path_strictly(self):
        """winsound failing falls through to PowerShell Media.SoundPlayer:
        the path is a strict single-quoted literal (embedded quotes doubled),
        argv-passed to powershell — never a shell, never unquoted."""
        fake = FakeWinsound(fail=True)
        path = r"C:\Users\o'brien\voice message.wav"
        with mock.patch("sys.platform", "win32"), \
             mock.patch.dict(sys.modules, {"winsound": fake}), \
             mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            ok = self.core.play_file(path)
        self.assertTrue(ok)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4],
                         ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
        script = argv[4]
        self.assertIn("System.Media.SoundPlayer", script)
        self.assertIn("PlaySync", script)
        self.assertIn(r"'C:\Users\o''brien\voice message.wav'", script)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_missing_winsound_module_still_reaches_powershell(self):
        with mock.patch("sys.platform", "win32"), \
             mock.patch.dict(sys.modules, {"winsound": None}), \
             mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            ok = self.core.play_file("/data/outputs/speak_1.wav")
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[0][0], "powershell")

    def test_windows_failure_falls_through_to_the_posix_players(self):
        """Everything Windows-side fails: the POSIX players run after, kept
        exactly as they are, and the answer is False when none works."""
        fake = FakeWinsound(fail=True)
        path = "/data/outputs/speak_1.wav"
        with mock.patch("sys.platform", "win32"), \
             mock.patch.dict(sys.modules, {"winsound": fake}), \
             mock.patch.object(subprocess, "run", side_effect=OSError) as run:
            ok = self.core.play_file(path)
        self.assertFalse(ok)
        tried = [call.args[0] for call in run.call_args_list]
        self.assertEqual(tried[0][:4],
                         ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertEqual(tried[1:], [
            ["pw-play", path],
            ["aplay", "-q", path],
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
        ])


class PlayFilePosixTest(unittest.TestCase):
    """The POSIX players must survive the Windows work untouched."""

    def setUp(self):
        self.core, _ = _voice_modules()
        if self.core is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")

    def test_pw_play_first_then_aplay_then_ffplay(self):
        path = "/data/outputs/speak_1.wav"
        with mock.patch("sys.platform", "linux"), \
             mock.patch.object(subprocess, "run", side_effect=OSError) as run:
            ok = self.core.play_file(path)
        self.assertFalse(ok)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["pw-play", path],
            ["aplay", "-q", path],
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
        ])

    def test_first_working_player_wins(self):
        path = "/data/outputs/speak_1.wav"
        with mock.patch("sys.platform", "linux"), \
             mock.patch.object(subprocess, "run") as run:
            run.side_effect = [OSError, mock.Mock(returncode=0)]
            ok = self.core.play_file(path)
        self.assertTrue(ok)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [["pw-play", path], ["aplay", "-q", path]])


class PlayFileMacOSTest(unittest.TestCase):
    """play_file on macOS: afplay (built into macOS) leads the chain, then
    the same POSIX players as everywhere else."""

    def setUp(self):
        self.core, _ = _voice_modules()
        if self.core is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")

    def test_afplay_first_on_darwin(self):
        """afplay is the built-in macOS player — it runs FIRST there and,
        when it plays, nothing else is tried."""
        path = "/data/outputs/speak_1.wav"
        with mock.patch("sys.platform", "darwin"), \
             mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            ok = self.core.play_file(path)
        self.assertTrue(ok)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["afplay", path])

    def test_afplay_failure_falls_through_to_the_next_player(self):
        """A failing afplay moves on to the next player in the chain
        (pw-play) — one at a time, first success wins."""
        path = "/data/outputs/speak_1.wav"
        with mock.patch("sys.platform", "darwin"), \
             mock.patch.object(subprocess, "run") as run:
            run.side_effect = [OSError, mock.Mock(returncode=0)]
            ok = self.core.play_file(path)
        self.assertTrue(ok)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [["afplay", path], ["pw-play", path]])

    def test_posix_chain_order_is_pinned_after_afplay(self):
        """The macOS chain keeps the pinned POSIX order behind afplay:
        afplay -> pw-play -> aplay -> ffplay, and False when none works.
        Linux stays exactly pw-play -> aplay -> ffplay (PlayFilePosixTest):
        afplay never runs there."""
        path = "/data/outputs/speak_1.wav"
        with mock.patch("sys.platform", "darwin"), \
             mock.patch.object(subprocess, "run", side_effect=OSError) as run:
            ok = self.core.play_file(path)
        self.assertFalse(ok)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["afplay", path],
            ["pw-play", path],
            ["aplay", "-q", path],
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
        ])


class TransportGuardTest(unittest.TestCase):
    """The daemon's last-stop guard: the "no AF_UNIX" payload appears ONLY
    when NEITHER transport works. A build WITHOUT AF_UNIX is not a failure
    case — it speaks the loopback TCP + token fallback (voice/transport.py)
    and reaches the daemon normally. This pins the CI Windows bug: there
    `hasattr(socket, "AF_UNIX")` is False and the daemon must still work."""

    def setUp(self):
        self.core, self.daemon = _voice_modules()
        if self.daemon is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")

    def test_without_af_unix_the_guard_stays_silent(self):
        with af_unix_removed():
            self.assertIsNone(self.daemon._transport_unsupported())
            self.assertEqual(self.daemon.transport.kind(), "tcp")

    def test_without_af_unix_request_round_trips_over_tcp(self):
        """The Windows CI path end to end at the daemon client: request()
        reaches a daemon bound on the loopback TCP + token transport and
        never returns the AF_UNIX payload."""
        with af_unix_removed(), tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "sebas"
            received, thread = serve_one(data, {"status": "ok"},
                                         transport=self.daemon.transport)
            with mock.patch.object(self.daemon.core, "DATA", data), \
                 mock.patch.object(self.daemon, "ensure_running"):
                reply = self.daemon.request({"op": "ping"}, timeout=5)
            thread.join(5)
        self.assertEqual(reply, {"status": "ok"})
        self.assertEqual(received["payload"], {"op": "ping"})

    def test_request_returns_the_error_payload_when_neither_works(self):
        with af_unix_removed(), attr_removed(socket, "AF_INET"):
            reply = self.daemon.request({"op": "ping"})
        self.assertEqual(reply["status"], "daemon_error")
        self.assertIn("AF_UNIX", reply["problem"])
        self.assertIn("next_step", reply)

    def test_alive_is_false_and_no_daemon_is_spawned_when_neither_works(self):
        with af_unix_removed(), attr_removed(socket, "AF_INET"):
            with mock.patch.object(subprocess, "Popen") as popen:
                self.daemon.ensure_running()
            popen.assert_not_called()
            self.assertFalse(self.daemon._alive())

    def test_serve_refuses_with_a_clear_message_when_neither_works(self):
        with af_unix_removed(), attr_removed(socket, "AF_INET"):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(self.daemon, "LOG", Path(tmp) / "daemon.log"):
                    with self.assertRaises(RuntimeError) as caught:
                        self.daemon.serve()
        self.assertIn("AF_UNIX", str(caught.exception))
        self.assertIn("3.9+", str(caught.exception))

    def test_with_af_unix_the_guard_stays_silent(self):
        self.assertIsNone(self.daemon._transport_unsupported())


if __name__ == "__main__":
    unittest.main()
