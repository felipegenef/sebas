<p align="center">
  <img src="assets/logo.png" alt="Sebas — the electronic butler for LLM-assisted work" width="240">
</p>

# Sebas

> **The electronic butler for LLM-assisted work.** When an agent finishes and has
> something to tell you, Sebas speaks the message out loud on your speaker — and
> when a second message arrives while the first is still being spoken, it is
> **parked** onto a system notification card with a single **Listen** button
> instead of talking over it. **One voice at a time: two voices never overlap.**
>
> - **Speaks your agents' messages** through a local text-to-speech daemon —
>   Kokoro (~80M parameters, Apache-2.0) on **CPU**, no GPU needed.
> - **Parks colliding messages** onto a notification card: the full text plus a
>   single **Listen** button — the label follows your configured language
>   ("Listen" in English, "Ouvir mensagem" in Portuguese). Click it and the
>   message plays in turn, then the card closes and never re-appears.
> - **One package installs everything** — the `voice` MCP (12 tools), the butler
>   persona, the notification cards and the configuration. One line in
>   `opencode.json(c)`.

## What it does

Sebas is four things in one OpenCode plugin:

1. **A voice.** A local daemon synthesizes speech with Kokoro on the CPU and
   plays it on your speaker. Each language ships a small voice set (Portuguese:
   `pm_santa`, `pm_alex`, `pf_dora`; English: `bm_george`, `am_adam`, `bf_emma`,
   `af_bella`), switchable at runtime, speaking speed 0.5–2.0.
2. **A queue with one rule.** One voice at a time. A message that arrives while
   another is still being spoken is parked onto a card; a message that arrives
   when the speaker is free just plays. No overlap, ever.
3. **A butler persona.** The plugin injects it into every session: the executor
   is silent and technical, the butler is cordial, calm and precise — it
   translates what the agent did into what you need to know, before you have to
   ask. When the voice is off, the butler speaks in text only: no audio, no
   cards, no interruptions.
4. **Configurable identity.** The butler's name (default **Sebas**), your name
   and the people around you, the spoken language (default **English**), and a
   free-text **form of address** — how you like to be called, used verbatim and
   always cordial. Nothing is hardcoded; everything is set in conversation or
   seeded in the config.

The card flow:

```
agent finishes ──► speak(message)
                     │
                     ├─ speaker is free ──────────► the message is spoken. Done.
                     │
                     └─ a voice is still playing ─► the message is PARKED
                                                    │
                                                    ▼
                                       system notification card
                                       ┌──────────────────────────────┐
                                       │ Build finished — 240 tests   │
                                       │ passed in 12s.               │
                                       │                              │
                                       │ [ Listen ]                   │
                                       └──────────────────────────────┘
                                       click ──► plays in turn through the
                                       voice daemon, then closes. Never
                                       re-shown. Cards never overlap.
```

Click-to-play always goes through the voice daemon — never the browser, never a
shell.

## Requirements

- **OpenCode V2** (the plugin uses the V2 plugin API).
- **A speaker.** That is the point.
- **Python 3** (3.12 or 3.13) and about **300 MB of disk** for the TTS model,
  plus a small virtual environment for the voice server. The first load
  installs both automatically — the model is downloaded once.
- Per operating system:
  - **Linux** — fully tested. Cards go through freedesktop notifications
    (libnotify); CRITICAL urgency bypasses Do-Not-Disturb.
  - **macOS** — cards need `terminal-notifier` (or `herald`, e.g. via Homebrew)
    and notification permission for that tool. Voice playback needs nothing
    to install: it plays through the built-in `afplay`.
  - **Windows 10 1803+** — the full experience works: voice, toast cards and
    the notification permission flow. The voice setup runs in PowerShell
    (`setup.ps1`) and needs Python 3.12/3.13 from python.org or the `py`
    launcher. Cards go through PowerShell toasts: BurntToast when the module
    is installed, raw WinRT toast XML otherwise. The daemon talks to the
    servers over an AF_UNIX socket (Python 3.9+ has it) — see
    [Troubleshooting](#troubleshooting) for the one path-length caveat.

Notification adapters and their trade-offs are documented in
[`docs/notify-adapters.md`](docs/notify-adapters.md).

## Install

One line in your OpenCode config. The first load installs the voice runtime
**automatically** — a few minutes for the one-time ~300 MB model download, no
manual step and no second restart.

1. **Add the plugin** to the `plugins` array in `opencode.json` or
   `opencode.jsonc` — globally (`~/.config/opencode/`) or per project
   (`.opencode/`):

   ```jsonc
   {
     "$schema": "https://opencode.ai/config.json",
     "plugins": ["@felipegenef/opencode-sebas"]
   }
   ```

   That is the whole install. The plugin automatically registers its `voice`
   MCP, injects the butler persona and enables the notification cards. It never
   overwrites an MCP server you configured yourself — see
   [`plugin/README.md`](plugin/README.md).

2. **Restart OpenCode.** Loading or changing a plugin requires a restart;
   nothing takes effect mid-session. (Warn anyone who is mid-work.)

3. **That is all.** On this first load the plugin installs the voice runtime in
   the background: it creates the Python environment and downloads the Kokoro
   model (~300 MB) into Sebas's data directory (`$XDG_DATA_HOME/sebas`, default
   `~/.local/share/sebas` — the same rule on every operating system), using the
   packaged `setup.sh` on Linux/macOS and `setup.ps1` on Windows. Progress is
   logged to `plugin.log` and `setup.log` there. **When it finishes, speech is
   available immediately — the voice server reloads itself, no restart needed.**
   Until then `speak` and `voice_status` answer `{"status": "installing"}` with
   the same explanation instead of an error — on Linux, macOS and Windows alike:
   while the venv does not exist yet, the server starts with a system Python
   (`python3.13`, then `python3.12`, then `python3`; Windows falls back to
   `python`), so the installing state is answered from the very first second.
   The plugin keeps watching the whole install — quick checks at first, then one
   every ~45 s for a slow download — until the runtime is ready. Only past a
   two-hour horizon does it stop watching, and it says so in the log: restart
   OpenCode when the setup finishes.

4. **Verify**: open a session and ask "introduce yourself and say one sentence
   out loud". Sebas answers in text and speaks. For the machine-level check,
   ask it to run `voice_status` — it reports the engine state — or `list_voices`
   to see the voices available in your language.

**Offline install or troubleshooting?** The setup can always be run by hand —
both scripts are idempotent, safe to re-run at any time, and do exactly what
the automatic first run does:

```bash
bash <voice-mcp-dir>/setup.sh
```

Windows, from PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <voice-mcp-dir>\setup.ps1
```

The automatic setup prefers PowerShell 7 (`pwsh`) whenever it is installed and
falls back to Windows PowerShell (`powershell`) otherwise — nothing to choose.

From a source checkout `<voice-mcp-dir>` is `plugin/mcp/voice`. For a package
install, the exact resolved path is printed in the startup log — see
[Troubleshooting](#troubleshooting) for where the log lives. If the automatic
setup cannot start (no Python, no network), the plugin logs the reason and
`voice_status` points at these very commands.

### Make it quiet

Disable the `voice` MCP and Sebas goes quiet: the persona stays, audio and cards
stop.

```jsonc
{
  "mcp": {
    "voice": { "enabled": false }
  }
}
```

Restart to apply. The plugin honors this entry and stands down — voice-only
features are gone, the butler still writes.

### Uninstall

1. Remove the entry from the `plugins` array in your OpenCode config.
2. Restart OpenCode.
3. Optional: delete the data directory (model weights and the Python
   environment) if you no longer need them — `rm -rf ~/.local/share/sebas`
   (Windows: delete the same folder inside your user profile's
   `.local\share`).

Nothing else was installed globally.

## Configuration

Everything is set in conversation — the butler reads and writes its own config
through its tools — or seeded once in the plugin options
(see [`plugin/README.md`](plugin/README.md) for the full option table).

| What | Default | Set with |
|---|---|---|
| Butler name | `Sebas` | `set_butler_name` |
| Your name, the people around you | unset | `set_user_name` |
| **Form of address** — how you like to be called (a treatment such as "sir" or "doctor", or a complete form such as "Mr. Alex"), used verbatim and always cordial | language default greeting | `set_user_name` (`form_of_address`) |
| Spoken language | English | `set_language` |
| Voice and speed (0.5–2.0) | `bm_george` (English) / `pm_santa` (Portuguese) | `set_voice` (list first with `list_voices`) |

Everything the user sees or hears follows that language: the card title and
button, the spoken short notices, the spoken greeting and the form of address
fallback. English and Brazilian Portuguese ship today (`notify/` and
`voice/core.py` hold the string tables — adding a language means adding the
dictionaries and a voice prefix).

The 12 tools of the `voice` MCP:

| Tool | What it is for |
|---|---|
| `speak` | Speaking a message — agents call it before every user-facing reply |
| `set_voice` | Choosing the voice and the speaking speed |
| `list_voices` | Listing the voices and the current configuration |
| `voice_status` | Engine state — the first stop when speech misbehaves |
| `warmup` | Loading the engine into memory before the first real speech |
| `measure_rtf` | Measuring this machine's synthesis speed (RTF < 1 = faster than real time) |
| `get_user_name` | Reading identity: butler name, user, people, language, form of address |
| `set_user_name` | Setting your name, the people around you, and the form of address |
| `set_butler_name` | Renaming the butler |
| `set_language` | Switching the spoken language |
| `notification_status` | OS, notification backend, permission state, Do-Not-Disturb / Focus |
| `notification_request` | Asking for notification permission (prompt on macOS, settings pane on Windows, guidance on Linux) |

## How the queue works

Five rules, in force everywhere:

1. **No overlapping audio, ever.** The daemon is the only speaker, one turn at a
   time.
2. **A card only on collision.** A message arriving while another is being
   processed is parked — never auto-played — and a card carries the full text
   ("listen later if you want"). The request returns immediately.
3. **Sequential messages just speak**, in order. No card, no notice.
4. **User-initiated playback** (the card button, or an explicit confirmation)
   always plays in turn behind whatever is playing. Never re-carded, never
   overlapping.
5. **No card available?** The old flow takes over: a short spoken notice and a
   chat confirmation.

Card semantics: one card per parked message, the full text and exactly one
button; the click plays the message in turn through the daemon and closes the
card; an untouched card closes by itself after about five minutes and is never
re-shown. Cards stack — one per parked message — and each plays in its own turn.

## Troubleshooting

The three-platform install (Linux, Windows, macOS) is verified in CI on every
push — see [CI](#ci) — so a setup failure on one OS is caught before release.

**Nothing is spoken.** Ask Sebas for `voice_status`. If it answers
`{"status": "installing"}`, the voice runtime is still being set up — speech
appears by itself when it finishes (no restart). If the engine is missing after
that, the automatic setup failed: check `setup.log` in the data dir and run the
setup by hand (Install step 3). Check your speaker and volume outside OpenCode
first.

**Windows: the daemon cannot reach the voice server.** The daemon and the
notification adapters talk over an AF_UNIX socket, which Windows supports from
10 1803+ with Python 3.9+ (the setup installs 3.12/3.13 anyway). Socket paths
are limited to about 108 characters: when your user profile path is very long,
set `XDG_DATA_HOME` to a short directory (for example `D:\sebas`) **before**
the first load (or before running the setup by hand), so `<data>/engine.sock`
stays under the limit.

**No notification cards.** Ask Sebas for `notification_status`, then
`notification_request`. On macOS the card tool needs notification permission
(System Settings → Notifications) and `terminal-notifier` or `herald` installed;
on Windows toasts need the interactive desktop session; on Linux a
freedesktop-compatible notification server.

**A change did not take effect.** An OpenCode restart is required to load or
change a plugin — including its options and the `voice` MCP. Nothing applies
mid-session.

**Diagnostics.** Every line the plugin logs also lands in
`~/.local/share/sebas/plugin.log` (`$XDG_DATA_HOME/sebas`): what was resolved,
from where, every registration decision and every step of the automatic
voice-runtime install. The installer's own output lands beside it in
`setup.log`. Start there.

More detail: [`plugin/README.md`](plugin/README.md) · adapter research:
[`docs/notify-adapters.md`](docs/notify-adapters.md) · release history:
[`CHANGELOG.md`](CHANGELOG.md).

## CI

Two workflows run on GitHub-hosted runners:

- **`ci`** (every push to `main`, every pull request) — on **Ubuntu, Windows
  and macOS**: the Python and bun test suites, then the real install smoke —
  `npm pack` the plugin, install the tarball into a temp prefix, run the
  platform installer (`setup.sh` / `setup.ps1`) against a scratch data dir,
  and talk JSON-RPC to the installed voice server to prove `voice_status`
  answers `ok` and `speak` produces a `.wav` (never played: `play:false`).
  The Kokoro model is cached between runs, so the ~300 MB download happens
  once per OS instead of once per run.
- **`release`** (tags `v*`, or manually as a rehearsal) — runs the same test
  matrix as a gate, then publishes to npm with a repository `NPM_TOKEN`
  secret. Manual runs publish nothing: they stop at `npm publish --dry-run`.

The three-OS install above is what the [Install](#install) section describes;
if it works there, it installs on the runner's real Windows and macOS too.

## License

[MIT](LICENSE)

> [!NOTE]
> **Maintainer note — the `undefined` workaround.** OpenCode V2 plugin
> transforms currently reject objects that carry a key explicitly set to
> `undefined` (optional keys are decoded strictly), and a failing transform
> silently drops **everything** the plugin registered — the `voice` MCP
> included. Sebas therefore builds every object it hands to the plugin API
> with **only the keys that actually have values**, and wraps each plugin API
> call in a `try/catch` that writes a diagnostic line to `plugin.log`. Other
> plugins have hit the same behavior and worked around it the same way. As
> soon as upstream accepts present-but-undefined optional keys, we will
> simplify this code path and drop the guard. For users this is **fully
> transparent**: no configuration change, no behavior change, no action
> required. This note exists so the maintainers remember why the defensive
> code is there.
