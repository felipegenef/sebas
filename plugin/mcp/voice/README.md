# MCP `voice` — the agent speaks before replying

> **Packaged copy.** This tree is the copy shipped INSIDE the Sebas plugin
> package (`<plugin>/mcp/voice`) and is the single source of truth for the
> voice MCP: the plugin launches it and injects `SEBAS_NOTIFY_PATH` (pointing
> at the plugin's own `notify/`) automatically. The standalone marketplace
> install is legacy — existing installs keep working, but new setups install
> the plugin only. The steps below still describe this server itself; where
> they mention registering the server in the OpenCode config by hand, know
> that the plugin does that for you.

Local text-to-speech for OpenCode. When active, the agent calls
`speak(text, context)` before writing the response: the user hears the answer
on the speaker and reads the text right after.

Speech follows the **configured language** — default English, changed with
`set_language` (`en` / `pt`, also en-us / pt-br) — whatever language the
conversation is written in.

## Engine

Kokoro 82M on CPU (Apache 2.0) — fast, tiny and GPU-free. RTF ~0.5 on a
modern CPU, so replies are spoken almost as fast as they are generated.

The default voice follows the configured language (`bm_george` for English,
`pm_santa` for pt-br). Suggested voices per language — `list_voices` reports
the full set:

| voice | language | gender | character |
|---|---|---|---|
| `bm_george` (default, English) | en | male | British |
| `am_adam` | en | male | American |
| `bf_emma` | en | female | British |
| `af_bella` | en | female | American |
| `pm_santa` (default, pt-br) | pt | male | deep, warm |
| `pm_alex` | pt | male | neutral, clear |
| `pf_dora` | pt | female | young |

## Queue semantics — one voice at a time

Two voices NEVER overlap: this daemon is the only source of audio and plays
one message per turn (generation and playback under the same turn).

- **Busy at arrival — card only on collision:** a `speak` request that arrives
  while another voice request is still being processed (generating or
  speaking) is PARKED — nothing is synthesized and it is NEVER played
  automatically later. A notification card carries the full text ("listen
  later if you want") and the request returns immediately
  (`awaiting_confirmation` with `card:"shown"`).
- **Idle at arrival:** the message plays right away, as-is — no card, no
  notice, no confirmation, even when the previous message just ended.
- **Dry runs are not voice calls:** `warmup`, `measure_rtf` and `speak` with
  `play:false` never park and never show a card.
- **User-initiated playback** (the card's Listen button or `confirmed:true`)
  always plays in turn behind whatever is playing — never parked, never
  re-carded; the button click IS the confirmation.
- **Card unavailable:** the old flow stays — a short spoken notice plus the
  chat confirmation (`awaiting_confirmation`), and the full message plays
  only after the user confirms.

Each call can carry a `context` (short label in the configured language, e.g.
"Agente do projeto X, terminando o build"); it identifies the message on the
card and in the old flow's short notice.

## Notification cards (`SEBAS_NOTIFY_PATH`)

When the queue is busy, `speak` no longer speaks a short notice: it shows a
system notification card (the message text plus one "Ouvir mensagem" /
"Listen" button) and answers `awaiting_confirmation` with `card:"shown"`.
Clicking the button plays the full message through this daemon and closes
the card; the card closes by itself after a few minutes and is never
re-shown.

The cards come from the `notify/` package shipped inside the Sebas plugin
(`<plugin>/notify`). The MCP finds that package through the
`SEBAS_NOTIFY_PATH` environment variable — the directory that CONTAINS
`notify/`. When this server runs under the plugin, the plugin sets that
variable itself; `run.sh` leaves it empty by default (export your own value
only when running the server outside the plugin). When the variable is unset
or points nowhere, the server falls back to the spoken short notice plus the
chat confirmation, exactly as before.

## Install

Under the Sebas plugin nothing is needed: the plugin installs this runtime
**automatically on first load** (this very script, in the background — a few
minutes, ~300 MB, output in `setup.log` next to the plugin log) and reloads
the voice server when it finishes, with no restart. The plugin keeps watching
the install the whole time — fast checks at first, then one every ~45 s for a
slow download — and until the venv exists `run.sh` starts this server with a
system Python (`python3.13` → `python3.12` → `python3`), so `speak` and
`voice_status` answer `{"status": "installing"}` right away instead of
failing. To install by hand —
offline installs, troubleshooting, or running this server standalone — use the
scripts directly; both are idempotent and safe to re-run at any time:

```bash
bash setup.sh     # venv + kokoro-onnx + weights (~300 MB)
python3 demo.py --status
python3 demo.py --text "Testing the voice." --voice pm_santa
```

Windows, from PowerShell (`setup.ps1` is the mirror of `setup.sh` — same data
directory, same packages, same weights; the automatic setup prefers `pwsh`,
PowerShell 7, whenever it is present):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

Playback needs nothing beyond the OS itself: macOS plays through the built-in
`afplay`, Linux through `pw-play`/`aplay`, Windows through `winsound` (with a
PowerShell fallback) — `ffplay` (from ffmpeg, when installed) is the last
resort everywhere.

Registration: when this copy runs under the Sebas plugin (the packaged-copy
header above), nothing is needed — the plugin launches this server and
injects `SEBAS_NOTIFY_PATH`. Registering the server by hand in the OpenCode
config is the legacy path. **An OpenCode restart is required** for the server
to load — warn the user first.

## MCP tools

- `speak(text, context, play=true, confirmed=false)` — synthesizes and plays. Call before replying.
- `set_voice(voice, speed)` — speed 0.5 to 2.0
- `list_voices()` — available voices and current configuration
- `voice_status()` — engine state
- `measure_rtf()` — this machine's real speed
- `warmup()` — loads the engine before the first real speech
- `get_user_name()` / `set_user_name(name, main, form_of_address)` — identity,
  stored per machine in the users config (never in the shared instructions):
  the user's name, the people around them and **`form_of_address`** — how the
  user likes to be called ("senhor", "senhora", "doutor", "chefe"… or the
  complete "senhor Alex"), used **verbatim** as the vocative by the persona
  and the spoken greeting. Empty/unset = the language default
  (`Senhor {first}` pt-br, `{first}` en-us); `form_of_address=""` clears it
  back to that default and omitting the argument keeps the current value.
  `get_user_name` also returns `greeting_example`: what the greeting sounds
  like right now
- `set_butler_name(name)` — renames the butler persona (default: Sebas)
- `set_language(language)` — sets the spoken language (default: English;
  'en' or 'pt', also en-us / pt-br); also switches the default voice to one
  that speaks the language well
- `notification_status()` — notification readiness: OS, backend, card
  availability (`SEBAS_NOTIFY_PATH`), permission state and DND/focus.
  Read-only; unknowable states are reported as `unknown`, never guessed
- `notification_request()` — opens the OS permission flow (macOS prompt or
  Settings pane, Windows `ms-settings:notifications`, Linux guidance only);
  never writes a desktop setting on any platform

## Architecture

One daemon process owns audio (synthesis + playback) and serves MCP instances
over a local socket; instances never load models themselves. Requests are
serialized through a turn lock and models are released after idle time.

## Files outside git

Resolved at runtime under the Sebas data dir (`<data home>/sebas`, default
`~/.local/share/sebas`; a pre-1.0 legacy location, `<data home>/voz`, is
auto-detected and used as-is when the new directory does not exist — no user
action needed), per machine:

- `venv` — venv
- `models` — Kokoro weights
- `config.json` — active voice profile (a pre-1.0 data dir keeps its
  original `voz.json` name; the server reads either, so saved settings are
  never lost)
- `users.json` — identity (butler name, user, people, form of address)
- `outputs` — generated `.wav` files
- `engine.sock` / `engine.lock` / `daemon.log` — daemon socket, start lock and log
- `setup.log` / `setup.lock` — automatic first-run setup output and its lock

To move a pre-1.0 install to the new location at your own pace: create the
new directory and move the files over (`venv`, `users.json`, the voice
profile) — `bash setup.sh` (Windows: `setup.ps1`) re-downloads the weights
into `models` if they are not moved along.
