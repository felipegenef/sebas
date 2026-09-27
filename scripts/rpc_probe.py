#!/usr/bin/env python3
"""rpc_probe.py — CI helper: drives the voice MCP server over stdio JSON-RPC.

Why this exists: the install smoke test must prove the INSTALLED artifact
speaks the MCP protocol before anything reaches a user. The probe starts the
server exactly the way the plugin launches it, writes two requests to its
stdin (voice_status, then speak) and checks the two replies against the
contract the plugin relies on:

  * --mode installing  the runtime is absent: both replies must say
                       {"status": "installing", ...} — the clear 'not yet'
                       answer a first OpenCode load shows while the plugin
                       installs the runtime in the background.
  * --mode ok          the runtime is installed: voice_status must answer
                       {"status": "ok"} with the model present, and speak must
                       produce a real .wav while reporting "played": false.

Host-independent by design: stdlib only, no imports from the plugin, no
machine paths baked in. The server command after `--` is what gets spawned
(`bash run.sh` on POSIX, `<venv>/Scripts/python.exe server.py` on Windows —
exactly what plugin/src/config.ts resolves).

Safety rules this helper enforces on itself:
  * NEVER plays audio. `play: false` is hardcoded in the speak request and is
    not configurable — the probe only ever proves synthesis (a .wav file).
  * NEVER touches a live daemon socket. The child runs with XDG_DATA_HOME /
    HOME / USERPROFILE pointed at the scratch data dir it is given, and the
    probe refuses to run at all when that dir already contains an
    engine.sock (a daemon is listening there).
  * Only the process it spawned is ever terminated (on read timeout).

Usage:
    python3 scripts/rpc_probe.py --mode installing --data-home <scratch> -- \
        python3 <voice-dir>/server.py
    python3 scripts/rpc_probe.py --mode ok --data-home <scratch> -- \
        bash <voice-dir>/run.sh

Exit codes: 0 = contract held, 1 = contract broken, 2 = usage/environment.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# The two requests every probe sends. ids are matched against the replies so
# a stray line can never satisfy the wrong check.
_SPEAK_TEXT = "This is a CI probe of the voice runtime."
_SPEAK_CONTEXT = "CI install smoke test (synthesis only, never played)"


def _requests() -> list[dict]:
    """The probe's whole conversation: status first, then a dry-run speak."""
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "voice_status", "arguments": {}}},
        # play:false is the safety rule of this file: a dry run generates the
        # .wav and never touches a speaker (the daemon's queue treats dry runs
        # as never-voice calls for exactly this reason).
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "speak", "arguments": {
             "text": _SPEAK_TEXT, "context": _SPEAK_CONTEXT, "play": False}}},
    ]


def _data_dir(data_home: Path) -> Path:
    """The data dir the server will resolve (mirrors voice/core.py): new
    installs use <data home>/sebas, a pre-1.0 legacy <data home>/voz is used
    when only it exists."""
    current = data_home / "sebas"
    legacy = data_home / "voz"
    if not current.exists() and legacy.exists():
        return legacy
    return current


def _child_env(data_home: Path) -> dict:
    """Environment for the server process. HOME and USERPROFILE also point at
    the scratch dir so nothing — not even a resolution bug — can wander into
    the real user profile; cards are off (empty SEBAS_NOTIFY_PATH) so the
    probe can never raise a desktop notification."""
    env = dict(os.environ)
    scratch = str(data_home)
    env["XDG_DATA_HOME"] = scratch
    env["HOME"] = scratch
    env["USERPROFILE"] = scratch
    env["SEBAS_NOTIFY_PATH"] = ""
    return env


def _reader(stream, out: "queue.Queue", name: str) -> None:
    """Moves every stdout line onto a queue so replies can be awaited with a
    timeout (a hung server fails loudly instead of freezing the job)."""
    try:
        for line in stream:
            out.put(line)
    finally:
        out.put(None)                      # EOF sentinel


def _payload(reply: dict) -> dict:
    """The tool payload the server wraps in result.content[0].text (the
    standard MCP text envelope server.py produces)."""
    inner = reply.get("result", {}).get("content", [{}])[0].get("text", "")
    return json.loads(inner)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("installing", "ok"), required=True,
                    help="'installing' = runtime absent; 'ok' = runtime complete")
    ap.add_argument("--data-home", default=(os.environ.get("XDG_DATA_HOME")
                                           or "").strip() or None,
                    help="scratch data home (default: $XDG_DATA_HOME)")
    ap.add_argument("--timeout", type=float, default=600,
                    help="seconds to wait for each reply (default: 600; the "
                         "first synthesis loads a ~300 MB model)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="server command after --, e.g. -- bash run.sh")
    args = ap.parse_args()

    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        print("rpc_probe: no server command (put it after --)", file=sys.stderr)
        return 2
    if not args.data_home:
        print("rpc_probe: no data home: pass --data-home or set XDG_DATA_HOME",
              file=sys.stderr)
        return 2

    data_home = Path(args.data_home).expanduser().resolve()
    data = _data_dir(data_home)
    if (data / "engine.sock").exists():
        # The one place this helper could hurt a real install: talking to (or
        # spawning next to) the user's live daemon. Refuse outright.
        print(f"rpc_probe: refusing to run: {data / 'engine.sock'} exists "
              "(a live daemon socket). Point --data-home at a scratch dir.",
              file=sys.stderr)
        return 2

    print(f"rpc_probe: mode={args.mode} data={data}")
    print(f"rpc_probe: server: {cmd}")

    err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=err_file, text=True, encoding="utf-8", env=_child_env(data_home))

    lines: "queue.Queue" = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, lines, "stdout"),
                     daemon=True).start()

    replies: dict[int, dict] = {}
    try:
        for req in _requests():
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        proc.stdin.flush()

        want = {req["id"] for req in _requests()}
        while want - replies.keys():
            try:
                line = lines.get(timeout=args.timeout)
            except queue.Empty:
                print(f"rpc_probe: FAILED: no reply for id(s) "
                      f"{sorted(want - replies.keys())} within {args.timeout}s",
                      file=sys.stderr)
                return 1
            if line is None:
                print(f"rpc_probe: FAILED: server closed stdout after "
                      f"{len(replies)} reply(ies)", file=sys.stderr)
                return 1
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue                       # never JSON: not a reply
            rid = msg.get("id")
            if rid in want:
                replies[rid] = msg
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass

    # The server exits on stdin EOF; wait briefly, and only ever terminate the
    # process THIS probe started (never anything else on the machine).
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.terminate()

    problems: list[str] = []
    results: dict[int, dict] = {}
    for rid in sorted(replies):
        msg = replies[rid]
        if "error" in msg:
            problems.append(f"id={rid}: JSON-RPC error {msg['error']}")
            continue
        try:
            results[rid] = _payload(msg)
        except (IndexError, KeyError, json.JSONDecodeError) as e:
            problems.append(f"id={rid}: unreadable result envelope ({e!r})")

    status_payload = results.get(1)
    speak_payload = results.get(2)
    if status_payload is not None:
        print("--- voice_status ---")
        print(json.dumps(status_payload, ensure_ascii=False, indent=2))
    if speak_payload is not None:
        print("--- speak (play:false) ---")
        print(json.dumps(speak_payload, ensure_ascii=False, indent=2))

    if args.mode == "installing":
        for name, payload in (("voice_status", status_payload),
                              ("speak", speak_payload)):
            if payload is None:
                problems.append(f"{name}: no payload")
            elif payload.get("status") != "installing":
                problems.append(f"{name}: expected status 'installing', got "
                                f"{payload.get('status')!r}")
    else:
        if status_payload is None:
            problems.append("voice_status: no payload")
        else:
            if status_payload.get("status") != "ok":
                problems.append("voice_status: expected status 'ok', got "
                                f"{status_payload.get('status')!r}")
            if status_payload.get("model") != "ok":
                problems.append("voice_status: model is not present ('ok'), "
                                f"got {status_payload.get('model')!r}")
        if speak_payload is None:
            problems.append("speak: no payload")
        else:
            if speak_payload.get("status") != "ok":
                problems.append("speak: expected status 'ok', got "
                                f"{speak_payload.get('status')!r}")
            if speak_payload.get("played") is not False:
                problems.append("speak: played must be false (the probe never "
                                f"plays), got {speak_payload.get('played')!r}")
            wav = speak_payload.get("wav")
            path = Path(wav) if wav else None
            if not wav:
                problems.append("speak: reply carries no 'wav' path")
            elif not path.is_file():
                problems.append(f"speak: wav file does not exist: {wav}")
            elif path.stat().st_size < 1024:
                problems.append(f"speak: wav file is implausibly small "
                                f"({path.stat().st_size} bytes): {wav}")

    if problems:
        for p in problems:
            print(f"rpc_probe: FAILED: {p}", file=sys.stderr)
        err_file.seek(0)
        tail = err_file.read().strip()
        if tail:
            print("--- server stderr ---", file=sys.stderr)
            print(tail[-4000:], file=sys.stderr)
        return 1

    print(f"rpc_probe: OK (mode={args.mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
