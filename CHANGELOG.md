# Changelog

## 1.0.2 — 2026-09-27

**The butler asks who you are instead of assuming.** On a fresh install the
spoken greeting used to be gendered by default ("senhor" in Brazilian
Portuguese) — a wrong guess the moment anyone else used the machine.

- **First-interaction rubric.** When no identity is saved, the butler asks
  **once** — your name, how you like to be called (senhor, senhora, doctor,
  boss, or any form you choose) and which language to speak (English /
  Portuguese) — saves the answers (`set_user_name` / `set_language`) and never
  asks again.
- **Neutral until it knows.** Until those answers are saved the address stays
  cordial but neutral: the plain name or a neutral greeting, no gendered
  treatment in any language. Gender is never guessed from a name.
- **Neutral default greeting.** Without a saved form of address the spoken
  greeting is now the plain first name in every language (`{first}`); the
  treatment comes only from the saved `form_of_address`. Clearing the form
  restores that neutral default.
- The persona text, the `get_user_name` / `set_user_name` tool descriptions
  and their `next_step` strings all carry the same guidance: missing identity
  → ask, never assume.

**Windows voice transport: the daemon is reachable without AF_UNIX (loopback
TCP + token).** The 3-OS CI caught a real product bug: some Windows Python
builds (the GitHub `windows-latest` runner among them) ship without AF_UNIX
sockets, and the voice daemon was unreachable there at all — `speak` answered
`daemon_error`, and every Windows user without AF_UNIX would have lost the
one-voice-at-a-time daemon (the thing that guarantees two voices never
overlap).

- **One transport module, two flavors.** `voice/transport.py` (mirrored
  byte-for-byte in `notify/transport.py`) is now the only place that knows how
  clients reach the daemon: the AF_UNIX socket at `<data>/engine.sock` where
  the Python build has AF_UNIX — POSIX behavior unchanged, byte for byte —
  and, where it does not, a stream socket bound to `127.0.0.1` ONLY (never
  `0.0.0.0`) on an ephemeral port published in `<data>/engine.port`.
- **Token auth with the unix socket's parity.** A TCP port carries no file
  permissions, so the listening side writes a random token to
  `<data>/engine.token` and the client sends it as the FIRST line of every
  connection; a mismatch gets the usual house payload and is never served.
  Both files are written 0600 — same threat model as the unix socket: other
  local users on the same machine.
- **The "this Python build has no AF_UNIX socket support" error now appears
  only when neither transport can work** — with the TCP fallback it is not
  reachable on the CI Windows runner, and the daemon's R1 (one voice at a
  time) holds over the TCP transport exactly as over the unix socket.
- **Every caller goes through the one module**: the daemon (client and
  server), the notification-card click handlers on all three platforms and
  the CI probe (which refuses to run beside either live endpoint —
  `engine.sock` or `engine.port` — and exercises the TCP path on Windows
  automatically).
- **Tests pin it on all three CI legs**: flavor selection with AF_UNIX
  monkeypatched away, loopback-only bind, `engine.port`/`engine.token` modes
  0600, token rejection, the unix wire with no added handshake byte, adapter
  round trips over both flavors, and two concurrent requests playing one at a
  time through the TCP transport.

## 1.0.1 — 2026-09-27

**Automatic first-run voice setup on Linux, macOS and Windows: no manual step,
no extra restart.** Installing the plugin and restarting once is now the whole
install — the step that used to be run by hand is gone.

- **The voice runtime installs itself on first load.** The plugin detects an
  incomplete runtime (missing venv or model weights) and runs the packaged
  `setup.sh` / `setup.ps1` in the background, detached and non-blocking —
  OpenCode startup is never delayed. A few minutes for the one-time ~300 MB
  Kokoro download; progress lands in `setup.log` beside `plugin.log` in the
  Sebas data directory.
- **No restart when it finishes.** A completion watcher reloads the `voice` MCP
  the moment the runtime is ready, so speech becomes available mid-session —
  the relaunch picks up the installed venv interpreter automatically (Windows
  included). The watcher keeps polling through slow installs (fast at first,
  then once every ~45 s) instead of giving up early.
- **Graceful "installing" state on Linux, macOS and Windows.** While the setup
  runs, `speak` and `voice_status` answer `{"status": "installing", ...}` with
  a clear `next_step` — never a traceback — and no half-armed daemon is ever
  started. Before the venv exists the voice server starts with a system Python,
  so the state is servable on all three operating systems from the first
  second. Only past a two-hour bound does the watcher stop — and it logs then
  that OpenCode must be restarted when the setup finishes.
- **Parallel sessions share one setup.** A `setup.lock` in the data directory
  (holding the installer's pid) makes concurrent OpenCode sessions share a
  single install; stale locks are reclaimed automatically.
- **The manual commands remain** as the offline/troubleshooting path, and both
  installers now download to a `.part` file first, with `curl --fail` and a
  minimum-size check before the rename, so a failed download or an HTTP error
  page can never look complete. A torn venv (an interrupted creation) is now
  detected and recreated instead of aborting the setup.

## 1.0.0 — 2026-09-27

First public release of **Sebas**, the electronic butler for LLM-assisted work —
one OpenCode plugin that speaks your agents' messages, parks the ones that
collide, and keeps one voice at a time.

- **Voice daemon with a collision-proof queue.** Local Kokoro TTS (~80M
  parameters, Apache-2.0) on CPU — no GPU needed. One voice at a time, no
  overlap ever: sequential messages just speak in order, and a message arriving
  while another is being spoken is parked, never auto-played. First-run setup
  downloads the model (~300 MB) into the user's data directory.
- **Notification cards on Linux, macOS and Windows.** One card per parked
  message: the full text and a single Listen button (label localized per
  language — "Listen" in English, "Ouvir mensagem" in Portuguese). Clicking
  plays the message in turn through the voice daemon and closes the card; an
  untouched card auto-closes and is never re-shown. Linux uses freedesktop notifications (CRITICAL urgency bypasses
  Do-Not-Disturb), macOS a terminal-notifier → herald chain (time-sensitive),
  Windows BurntToast → raw WinRT toasts (urgent scenario). Click-to-play never
  touches the browser or a shell.
- **The butler persona, injected per session.** A silent, technical executor and
  a cordial, calm, precise butler who translates the agent's work into what the
  user needs to know — speak before replying, the queue formula, and voice-off
  means text only. No more copying persona text into `AGENTS.md` by hand.
- **Configurable identity, never hardcoded.** Butler name (default "Sebas"),
  user name(s) and related people, spoken language (default English) and a
  free-text **form of address** — how the user likes to be called, used verbatim
  and always cordial. A small voice set per language (switchable at runtime,
  speed 0.5–2.0); `set_language` switches both speech and greeting.
- **12 tools over one MCP (`voice`).** `speak`, `set_voice`, `list_voices`,
  `voice_status`, `measure_rtf`, `warmup`, `get_user_name`, `set_user_name`,
  `set_butler_name`, `set_language`, `notification_status`,
  `notification_request`.
- **Single-package install.** One line in `opencode.json(c)` registers the
  `voice` MCP, injects the persona and enables the cards. A user-configured MCP
  of the same name is never overwritten. Quiet mode: disable the `voice` MCP and
  the plugin goes persona-only — no audio, no cards.
- **The voice runtime works on Windows.** The PowerShell setup (`setup.ps1`)
  creates the venv and downloads the model, the plugin launches the stdio
  voice server from that venv, playback runs through `winsound` with a
  PowerShell `Media.SoundPlayer` fallback, and parked messages land on toast
  cards.
