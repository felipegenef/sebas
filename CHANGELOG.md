# Changelog

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
