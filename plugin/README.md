# `@felipegenef/opencode-sebas` — package docs

The OpenCode plugin package for **Sebas**, the electronic butler
(user-facing docs: [`../README.md`](../README.md)). One package ships the whole
product: the butler persona, the `voice` MCP and its text-to-speech daemon, the
system notification cards, and the configuration resolution.

What the plugin wires on load:

1. **The butler persona and voice rules**, injected into every session:
   silent executor vs. cordial butler, speak-before-reply, the queue formula
   with the notification card, voice-off means text only, and the portability
   convention. Identity is never part of the text — names, language and form of
   address are configuration, read at runtime through the tools.
2. **The `voice` MCP** (a stdio JSON-RPC server) through `mcp.transform`, with
   its tool surface unchanged: `speak`, the voice tools, the identity tools and
   the notification tools (12 in total). It is registered **only** when no MCP
   named `voice` is configured — a server you configured yourself is left
   completely alone (command, cwd, environment and every other key stay exactly
   as written). Registration is session-only; the config file is never rewritten.
3. **Identity and voice config in plugin `storage`** (butler name, user names,
   people, form of address, language, voice, speed), seeded once from the
   pre-1.0 standalone voice MCP's files when present (read-only import).
4. **The voice runtime, installed automatically on first load.** The Python
   venv and the ~300 MB Kokoro weights are created in the background by the
   packaged `setup.sh` (Linux/macOS) or `setup.ps1` (Windows) — no manual
   step. When the install finishes, the `voice` MCP is reloaded and speech is
   available **without any restart**; until then `speak` and `voice_status`
   answer `{"status": "installing"}` with the same explanation — servable on
   all three operating systems from the first second, because before the venv
   exists the server starts with a system Python (`python3.13` → `python3.12`
   → `python3`). The plugin keeps watching the install (fast at first, then
   once every ~45 s) until the runtime is ready; only past a two-hour bound
   does it stop — logging that OpenCode must be restarted when the setup
   finishes. Setup output goes to `setup.log` in the data directory.

> **⚠️ An OpenCode restart is required** to load this plugin or to apply any
> change to its registration, options or the `voice` MCP. Nothing takes effect
> mid-session — warn the user before restarting. (The first-run voice runtime
> install above is the exception: it completes in the background and the voice
> MCP reloads itself when it is done.)

## Layout

```
plugin/
├── index.js        package-root entry (re-exports dist/index.js)
├── dist/           built plugin (bun + tsc)
├── mcp/voice/      the voice MCP: server.py, run.sh, setup.sh, setup.ps1, tools.py
└── notify/         notification card package: linux.py, macos.py, windows.py
```

Both moving parts ship **inside** this package, so one install brings
everything. They are located through a documented precedence — **plugin option
→ environment variable → relative layout** — and nothing is hardcoded to one
machine. Between them, the daemon's transport is chosen at runtime by
`mcp/voice/voice/transport.py` (mirrored byte-for-byte in
`notify/transport.py`): an AF_UNIX socket at `<data>/engine.sock` where the
Python build has AF_UNIX, otherwise a loopback TCP socket on `127.0.0.1`
guarded by a random token (`<data>/engine.port` + `<data>/engine.token`, both
0600) — so Windows builds without AF_UNIX reach the daemon all the same:

| Knob | Where | Default | Meaning |
|---|---|---|---|
| `voiceDir` | plugin option | — | Directory containing the voice MCP (`server.py`) |
| `voiceCommand` | plugin option | `["bash","run.sh"]` on POSIX (`run.sh` prefers the venv interpreter and falls back to a system `python3` until it exists); on Windows the venv interpreter (`<data>/venv/Scripts/python.exe`) with `server.py` once the voice setup ran, else `["python","server.py"]` | Full launch command for the voice MCP |
| `notifyPath` | plugin option | — | Directory that **contains** the `notify/` package |
| `mcpProtocol` | plugin option | auto-negotiate | `legacy` \| `auto` \| `2026-07-28` |
| `codemode` | plugin option | `false` | Expose the tools through Code Mode instead of plain tools |
| `migrateIdentity` | plugin option | `true` | One-time read-only import of the pre-1.0 identity files |
| `identity` / `voice` | plugin option | — | Seed the identity / voice profile on first load |
| `SEBAS_MCP_DIR` | environment | — | Same as `voiceDir` |
| `SEBAS_NOTIFY_PATH` | environment | — | Same as `notifyPath` (the voice server reads this variable itself) |
| `XDG_DATA_HOME` | environment | `~/.local/share` | Where the data directory lives (`…/sebas`) |

Relative layout fallbacks, checked in order:

- voice MCP: `<plugin>/mcp/voice`, then `<plugin>/../mcp/voice`
- notify/: `<plugin>/notify` first, then `<plugin>/../notify`

A bad explicit path warns and falls through to the next source. If nothing is
found, the plugin still injects the persona and logs what to set — nothing
breaks.

Options go in the object form of a plugin entry:

```jsonc
{
  "plugins": [
    {
      "package": "@felipegenef/opencode-sebas",
      "options": {
        "voiceDir": "~/tools/voice-mcp",
        "notifyPath": "~/tools/sebas-cards",
        "codemode": false
      }
    }
  ]
}
```

## Identity and configuration (plugin storage)

Identity is configuration, never shared instructions: butler name, user names,
people, spoken language, form of address, plus the voice profile (voice, speed,
play). The plugin stores them under two `storage` keys: `identity` and `voice`.

The **form of address** is how the user likes to be called — free text used
**verbatim** as the vocative: a treatment ("sir", "doctor", "boss") or the
complete form ("Mr. Alex"). Unset or empty falls back to the neutral greeting
(the plain first name — never gendered). In the merge it runs on key presence:
an absent key falls through to the lower layer, while a stored `""` is an
explicit clear and wins.

**First run:** with no identity saved, the butler asks once — name, how to be
called, and language (English / Portuguese) — saves the answers, and never
asks again. Until then the address is cordial but neutral: no gendered
treatment, and gender is never guessed from a name.

Load order (each layer only fills what the previous one left unset):

1. **plugin storage** — whatever a previous run stored;
2. **`identity` / `voice` plugin options** — a seed written in the config;
3. **the pre-1.0 standalone files** — the migration source, when found;
4. built-in defaults.

The pre-1.0 import is read-only and one-time: the legacy identity and voice
files (auto-detected in the legacy data directory) are never modified, moved or
deleted. Set `"migrateIdentity": false` to skip it entirely.

## Quiet mode

Disable the MCP and the plugin goes quiet — persona only, no audio, no cards:

```jsonc
{
  "mcp": {
    "voice": { "enabled": false }
  }
}
```

The plugin honors that entry and never registers over it.

## The notification cards

The card exists for one purpose: a message that arrives **while** another is
still being spoken is **parked** — never played automatically — and a system
card carries the full text with a single Listen button. A message that arrives
when nothing is being spoken just plays. The button plays the full message in
turn through the voice daemon and closes the card; cards never re-banner and
never overlap. Cards come from the `notify/` package the plugin locates
(`notifyPath` / `SEBAS_NOTIFY_PATH`); without it, `speak` falls back to the
spoken short notice and the chat confirmation. See
[`../docs/notify-adapters.md`](../docs/notify-adapters.md) for the per-OS
adapters.

## Diagnostics

Every log line goes to the console **and** to **`plugin.log`** in the data
directory — `$XDG_DATA_HOME/sebas`, default `~/.local/share/sebas` — beside the
voice server's own files. Each line is prefixed with the plugin id in brackets
(`[sebas]`), so that is all you need to grep for. The location is resolved at
runtime; nothing machine-specific is hardcoded.

What the file records, in order:

- plugin setup start and any invalid-option warnings;
- where identity/config came from (storage / options / legacy / defaults);
- how the voice MCP and the notify path resolved — **option → env → layout**,
  which one won, and whether notification cards are available;
- every MCP transform run: whether `voice` was already configured, whether that
  entry was the plugin's own or the user's, and which branch ran — registered,
  refreshed (replay), user entry left untouched, nothing to register, or a
  caught error;
- the automatic voice-runtime install: ready, installing in the background, or
  failed with a reason — plus the `voice` MCP reload when it completes. The
  installer's own output lands beside this file in **`setup.log`**;
- the post-registration view of `voice` from `ctx.mcp.list()` (its connection
  status).

The file is trimmed to its **last 200 lines** on every write, so it stays small
across restarts. A registration problem shows up here first.

## Migrating from the standalone voice MCP (pre-1.0)

Before 1.0, the voice MCP was installed on its own and registered by hand. The
plugin replaces that install. About five minutes plus two restarts:

1. **Install the plugin** (see the root README) and restart.
2. **Remove the hand-written MCP entry** for the standalone voice server from
   the `mcp` section of your OpenCode config. Required: the plugin never
   overwrites a server named `voice` that you configured — if your entry stays,
   the plugin stands down and you keep running the old copy.
3. **Voice runtime:** if this machine never ran the standalone server, nothing
   to run — the first load installs the voice runtime automatically (a few
   minutes, ~300 MB, progress in `plugin.log` / `setup.log`). If it did, there
   is likewise nothing to do: the legacy data directory is auto-detected and
   reused, model and all. The packaged setup (`bash <plugin>/mcp/voice/setup.sh`,
   Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File
   <plugin>\mcp\voice\setup.ps1` — the automatic setup prefers `pwsh`
   (PowerShell 7) whenever it is present and falls back to `powershell`)
   remains as the offline/troubleshooting path.
4. **Restart OpenCode.**
5. **Verify.** The startup log carries a `[sebas]` line saying the voice MCP was
   registered and whether notification cards are enabled; `voice_status` answers
   from there.

Your identity and voice files are untouched — the import is read-only, and the
same code reads the same values. **Rollback:** re-add the manual MCP entry,
remove the plugin from `plugins`, restart. Everything was left in place, so the
old setup comes back exactly as it was.

## Uninstall

1. Remove the entry from the `plugins` array in `opencode.json(c)`.
2. Restart OpenCode.
3. Done. Nothing global was installed. Plugin `storage` stays behind but is
   inert; the model weights and the Python environment live in the data
   directory (`~/.local/share/sebas`) and can be deleted with it.

## Development

```bash
bun install        # local to plugin/
bun run build      # dist/index.js + type declarations
bun run check      # tsc --noEmit
bun test           # unit tests (temp dirs only: no OpenCode, no audio, no daemon)
```

Tests never touch a live system: path resolution runs against throwaway
directory layouts, the migration tests only read their own temp files, and no
test plays audio or contacts the voice daemon.

The same suites run in CI on Ubuntu, Windows and macOS, together with an
install smoke that packs the npm tarball, runs the platform installer and
talks JSON-RPC to the installed server (`ci` workflow in `.github/workflows/`).
Add tests for anything the smoke would otherwise be the first to catch.

## License

MIT
