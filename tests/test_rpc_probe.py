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
play:false, and the probe refuses to run beside a live daemon socket.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _support import ROOT, mcp_root

PROBE = ROOT / "scripts" / "rpc_probe.py"

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


if __name__ == "__main__":
    unittest.main()
