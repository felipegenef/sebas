"""scripts/rpc_probe.py, machine-checked: the CI stdio probe must itself be
trustworthy before CI leans on it.

Two levels of proof, no audio and no 300 MB model anywhere:
  * the 'installing' path runs the REAL voice server against a fresh scratch
    data dir (the exact first-load window the README documents);
  * the 'ok' path is driven through a stub server that speaks the real
    result envelope, so the probe's assertions (status, model, wav on disk,
    played:false) are exercised end-to-end — synthesis itself is proven only
    in CI, where the Kokoro weights are cached. The suite says so rather than
    pretending otherwise.
Safety invariants checked here too: the speak request always carries
play:false, and the probe refuses to run beside a live daemon endpoint
(engine.sock or engine.port — the names mirror voice/transport.py and the
parity is pinned below).
The launch-command construction is machine-checked as well (mocked Popen,
exact argv): the probe must resolve <data>/sebas/venv/Scripts/python.exe on
Windows and <data>/sebas/venv/bin/python on POSIX — the same interpreter
plugin/src/config.ts and voice/core.py resolve at runtime — with a system
interpreter only while the venv is absent. A hand-built CI path that missed
the 'sebas' segment once broke the Windows install smoke for exactly this
reason; the argv assertions are its regression guard.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import ROOT, mcp_root

PROBE = ROOT / "scripts" / "rpc_probe.py"

sys.path.insert(0, str(ROOT / "scripts"))
import rpc_probe  # noqa: E402  the unit under test (stdlib only)

# The stub server: replies to voice_status and speak with the same envelope
# server.py produces ({result: {content: [{text: <payload json>}]}}), records
# every request it received, and writes a real file for the 'wav' path.
_STUB = r'''
import json, os, sys
from pathlib import Path

scratch = Path(os.environ["STUB_SCRATCH"])
received = scratch / "received.jsonl"
wav = scratch / "stub.wav"
wav.write_bytes(b"RIFF" + b"\0" * 4096)

def send(rid, payload):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": False}}) + "\n")
    sys.stdout.flush()

override = json.loads(os.environ.get("STUB_OVERRIDE") or "{}")
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    with received.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    name = (req.get("params") or {}).get("name")
    if name == "voice_status":
        payload = {"status": "ok", "model": "ok", "engine": "stub"}
    else:
        payload = {"status": "ok", "wav": str(wav), "played": False}
    payload.update(override.get(name or "", {}))
    send(req.get("id"), payload)
'''


class ProbeAgainstRealServerTest(unittest.TestCase):
    """The 'installing' path against the real server.py (stdlib, no runtime)."""

    def setUp(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        self.server = root / "server.py"

    def test_fresh_data_dir_answers_installing(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, str(PROBE), "--mode", "installing",
                 "--data-home", tmp, "--timeout", "120", "--",
                 sys.executable, str(self.server)],
                capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('"status": "installing"', done.stdout)
        self.assertIn("rpc_probe: OK", done.stdout)

    def test_voice_dir_launches_the_server_the_plugin_way(self):
        """--voice-dir against the real server.py: the resolved command must
        start the server and answer 'installing' on a fresh data dir (the
        venv is absent there, so the system-interpreter fallback runs it —
        the same first-load window the plugin resolves at runtime)."""
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, str(PROBE), "--mode", "installing",
                 "--data-home", tmp, "--timeout", "120", "--voice-dir",
                 str(self.server.parent)],
                capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('"status": "installing"', done.stdout)
        self.assertIn("rpc_probe: OK", done.stdout)


class ProbeAgainstStubServerTest(unittest.TestCase):
    """The 'ok' assertions and the safety rules, through a canned server."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.stub = self.tmp / "stub_server.py"
        self.stub.write_text(_STUB, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def run_probe(self, mode="ok", override=None, data_home=None):
        import os
        env = dict(os.environ,
                   STUB_SCRATCH=str(self.tmp),
                   STUB_OVERRIDE=json.dumps(override or {}))
        return subprocess.run(
            [sys.executable, str(PROBE), "--mode", mode, "--data-home",
             str(data_home or self.tmp / "data"), "--timeout", "60", "--",
             sys.executable, str(self.stub)],
            capture_output=True, text=True, env=env)

    def test_ok_mode_passes_and_the_speak_request_never_plays(self):
        done = self.run_probe()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("rpc_probe: OK", done.stdout)
        # The safety invariant of the probe: play:false reached the server.
        sent = [json.loads(l) for l in
                (self.tmp / "received.jsonl").read_text().splitlines()]
        speak = next(r for r in sent if r["params"]["name"] == "speak")
        self.assertIs(speak["params"]["arguments"]["play"], False)

    def test_played_true_is_a_contract_breach(self):
        done = self.run_probe(override={"speak": {"played": True}})
        self.assertEqual(done.returncode, 1)
        self.assertIn("played must be false", done.stderr)

    def test_missing_wav_is_a_contract_breach(self):
        done = self.run_probe(override={"speak": {"wav": str(self.tmp / "nope.wav")}})
        self.assertEqual(done.returncode, 1)
        self.assertIn("does not exist", done.stderr)

    def test_missing_model_is_a_contract_breach(self):
        done = self.run_probe(override={"voice_status": {"model": "missing"}})
        self.assertEqual(done.returncode, 1)
        self.assertIn("model is not present", done.stderr)

    def test_installing_mode_rejects_an_ok_reply(self):
        done = self.run_probe(mode="installing")
        self.assertEqual(done.returncode, 1)
        self.assertIn("expected status 'installing'", done.stderr)

    def test_refuses_to_probe_beside_a_live_daemon_socket(self):
        data = self.tmp / "data"
        (data / "sebas").mkdir(parents=True)
        (data / "sebas" / "engine.sock").write_bytes(b"")
        done = self.run_probe(data_home=data)
        self.assertEqual(done.returncode, 2)
        self.assertIn("refusing to run", done.stderr)

    def test_refuses_to_probe_beside_a_live_tcp_daemon_endpoint(self):
        """The loopback TCP fallback publishes engine.port — a live daemon
        there must stop the probe just as engine.sock does."""
        data = self.tmp / "data"
        (data / "sebas").mkdir(parents=True)
        (data / "sebas" / "engine.port").write_text("12345")
        done = self.run_probe(data_home=data)
        self.assertEqual(done.returncode, 2)
        self.assertIn("refusing to run", done.stderr)
        self.assertIn("engine.port", done.stderr)


class EndpointNamesMirrorTheTransportTest(unittest.TestCase):
    """rpc_probe is stdlib-only (it must never import the plugin), so it
    mirrors the daemon endpoint file names by hand — like it mirrors
    venv_python and the data-dir rule. The parity is machine-checked here
    against voice/transport.py so the two can never drift."""

    def test_refusal_names_match_the_transport_module(self):
        root = mcp_root()
        if root is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from voice import transport
        source = PROBE.read_text(encoding="utf-8")
        for name in (transport.UNIX_SOCK_NAME, transport.PORT_FILE_NAME):
            self.assertIn(f'"{name}"', source,
                          f"rpc_probe must refuse on a live {name}")


class CommandConstructionTest(unittest.TestCase):
    """The Windows install-smoke regression guard: the launch argv must be
    built EXACTLY as the plugin resolves it at runtime (plugin/src/config.ts
    resolveVoiceCommand + voice/core.py venv_python) — the venv interpreter
    under the RESOLVED data dir, 'sebas' segment included. The literals mirror
    the windows-latest job layout (D:\\a\\sebas\\sebas\\.ci-data): a hand-built
    CI path that skipped the 'sebas' segment left Popen pointing at nothing."""

    WIN_DATA_HOME = r"D:\a\sebas\sebas\.ci-data"
    WIN_VOICE_DIR = (r"D:\a\sebas\sebas\.ci-prefix\node_modules"
                     r"\@felipegenef\opencode-sebas\mcp\voice")
    POSIX_DATA_HOME = "/home/runner/work/sebas/sebas/.ci-data"
    POSIX_VOICE_DIR = ("/home/runner/work/sebas/sebas/.ci-prefix/node_modules"
                       "/@felipegenef/opencode-sebas/mcp/voice")

    def test_windows_argv_is_venv_python_exe_with_backslashes(self):
        with mock.patch.object(rpc_probe, "_exists", return_value=True):
            cmd = rpc_probe.resolve_server_command(
                self.WIN_DATA_HOME, self.WIN_VOICE_DIR,
                windows=True, system_python="python")
        self.assertEqual(
            cmd,
            [self.WIN_DATA_HOME + r"\sebas\venv\Scripts\python.exe",
             self.WIN_VOICE_DIR + r"\server.py"])
        self.assertNotIn("/", cmd[0])          # Windows separators throughout
        self.assertNotIn("-c", cmd)            # never a -c wrapper

    def test_posix_argv_is_venv_bin_python(self):
        with mock.patch.object(rpc_probe, "_exists", return_value=True):
            cmd = rpc_probe.resolve_server_command(
                self.POSIX_DATA_HOME, self.POSIX_VOICE_DIR,
                windows=False, system_python="python3")
        self.assertEqual(
            cmd,
            [self.POSIX_DATA_HOME + "/sebas/venv/bin/python",
             self.POSIX_VOICE_DIR + "/server.py"])
        self.assertNotIn("\\", cmd[0])

    def test_missing_venv_falls_back_to_the_system_interpreter(self):
        for windows in (True, False):
            with self.subTest(windows=windows), \
                 mock.patch.object(rpc_probe, "_exists", return_value=False):
                cmd = rpc_probe.resolve_server_command(
                    self.WIN_DATA_HOME if windows else self.POSIX_DATA_HOME,
                    self.WIN_VOICE_DIR if windows else self.POSIX_VOICE_DIR,
                    windows=windows, system_python="sys-python")
            self.assertEqual(cmd[0], "sys-python")   # only when venv is absent
            self.assertTrue(cmd[1].endswith("server.py"))


class ProbeSpawnsTheResolvedCommandTest(unittest.TestCase):
    """Mocked Popen through main(): what resolve_server_command builds is
    EXACTLY what gets spawned, on both platform flavours."""

    def _spawned_argv(self, windows: bool):
        with tempfile.TemporaryDirectory() as tmp:
            data_home = Path(tmp) / "data"
            voice_dir = Path(tmp) / "voice"
            voice_dir.mkdir()
            (voice_dir / "server.py").write_text("# stub\n", encoding="utf-8")
            proc = mock.MagicMock()
            proc.stdout = iter(())       # EOF at once: no reply, probe fails loud
            proc.stdin = io.StringIO()
            argv = ["rpc_probe", "--mode", "installing",
                    "--data-home", str(data_home), "--voice-dir",
                    str(voice_dir), "--timeout", "5"]
            with mock.patch.object(rpc_probe.subprocess, "Popen",
                                   return_value=proc) as popen, \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(rpc_probe, "_is_windows",
                                   return_value=windows), \
                 mock.patch.object(rpc_probe, "_exists", return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                rc = rpc_probe.main()
            with mock.patch.object(rpc_probe, "_exists", return_value=True):
                expected = rpc_probe.resolve_server_command(
                    data_home.expanduser().resolve(),
                    voice_dir.expanduser().resolve(),
                    windows=windows, system_python=sys.executable)
        self.assertEqual(rc, 1)          # the stub answers nothing: fails loud
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], expected)
        return expected

    def test_windows_spawn_is_venv_python_exe(self):
        argv = self._spawned_argv(windows=True)
        self.assertIn("\\sebas\\venv\\Scripts\\python.exe", argv[0])
        self.assertNotIn("/", argv[0])
        self.assertTrue(argv[1].endswith("\\server.py"))

    def test_posix_spawn_is_venv_bin_python(self):
        argv = self._spawned_argv(windows=False)
        self.assertIn("/sebas/venv/bin/python", argv[0])
        self.assertNotIn("\\", argv[0])
        self.assertTrue(argv[1].endswith("/server.py"))


class CommandLineContractTest(unittest.TestCase):
    """--voice-dir (the supported path) and -- cmd (the explicit override)
    are mutually exclusive; one of the two is required."""

    def _run_main(self, argv):
        with mock.patch.object(sys, "argv", ["rpc_probe"] + argv), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            rc = rpc_probe.main()
        return rc, err.getvalue()

    def test_voice_dir_and_command_are_mutually_exclusive(self):
        rc, err = self._run_main(["--mode", "ok", "--data-home", "x",
                                  "--voice-dir", "v", "--", "python", "s.py"])
        self.assertEqual(rc, 2)
        self.assertIn("not both", err)

    def test_one_of_voice_dir_or_command_is_required(self):
        rc, err = self._run_main(["--mode", "ok", "--data-home", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("no server", err)


if __name__ == "__main__":
    unittest.main()
