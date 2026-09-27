/**
 * Sebas — the electronic butler, packaged as an OpenCode plugin.
 *
 * Three jobs, wired explicitly:
 *   1. Inject the butler persona and voice rules into every agent-loop model
 *      request (session "context" hook) — this replaces copying the text into
 *      each user's AGENTS.md by hand.
 *   2. Wire the voice MCP (stdio JSON-RPC) through `mcp.transform`, so
 *      its tool surface — speak, voice/identity tools and the notification
 *      tools — appears in OpenCode unchanged. The transform is ALWAYS
 *      registered (replayable: V2 re-runs it on every registry rebuild) and
 *      applies a strict never-clobber rule: the plugin only (re-)registers
 *      when no `voice` entry exists or the entry is its own earlier one; a
 *      user-configured `voice` entry is left completely untouched.
 *   3. Keep identity/config in the plugin's `storage`, seeded once from the
 *      voice server's existing JSON files (read-only migration).
 *
 * Everything it wires together is located through configuration (see
 * config.ts); nothing here assumes a platform or a machine layout.
 */
import { existsSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { Plugin } from "@opencode/plugin"
import {
  parseOptions,
  resolveNotifyPath,
  resolveVoiceCommand,
  resolveVoiceDir,
  type SebasOptions,
} from "./config"
import {
  DEFAULT_IDENTITY,
  DEFAULT_VOICE,
  readVoiceServerFiles,
  resolveIdentity,
  resolveDataDir,
  STORAGE_IDENTITY_KEY,
  STORAGE_VOICE_KEY,
  type Identity,
  type IdentitySource,
  type VoiceConfig,
} from "./identity"
import { BUTLER_INSTRUCTIONS, INSTRUCTIONS_MARKER } from "./instructions"
import {
  createDiagnostics,
  resolveDiagnosticsPath,
} from "./diagnostics"
import {
  applyVoiceTransform,
  VOICE_MCP_NAME,
  type VoiceWireInput,
} from "./mcp"

/** Plugin id as it appears in OpenCode's plugin registry. */
export const PLUGIN_ID = "sebas"

/** One-line error description for logs (message only — never a stack). */
function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * The plugin package root (the directory holding package.json), found from
 * this module's own location so relative-layout discovery works whether the
 * plugin runs from src/ directly or from the built dist/.
 */
function findPluginRoot(): string {
  let dir = dirname(fileURLToPath(import.meta.url))
  for (let up = 0; up < 4; up += 1) {
    if (existsSync(join(dir, "package.json"))) return dir
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return dirname(fileURLToPath(import.meta.url))
}

/** The JSON value shape `storage` accepts (mirrors the plugin storage type). */
type Json = string | number | boolean | null | Json[] | { [key: string]: Json }

/**
 * Plain-JSON projections for `storage`, which accepts only JSON values.
 * Exported so tests can pin the projection — a field missing here is silently
 * dropped from the seeded storage JSON and lost on the next read.
 */
export function identityJson(value: Identity): Json {
  return {
    butlerName: value.butlerName,
    language: value.language,
    mainUser: value.mainUser,
    users: [...value.users],
    people: [...value.people],
    // Carried verbatim so its presence semantics survive the round trip:
    // null = never set (absent), "" = an explicit clear (a present key).
    formOfAddress: value.formOfAddress,
  }
}

function voiceJson(value: VoiceConfig): Json {
  return { voice: value.voice, speed: value.speed, play: value.play }
}

/**
 * Load identity/voice config into plugin storage. Storage wins when present;
 * otherwise plugin options seed it, then the voice server's legacy JSON files
 * (the documented migration source — read-only). Returns what won, for logs.
 */
async function loadIdentity(ctx: Plugin.Context, options: SebasOptions): Promise<IdentitySource> {
  const stored = await ctx.storage.get(STORAGE_IDENTITY_KEY)
  const storedVoice = await ctx.storage.get(STORAGE_VOICE_KEY)

  const legacy = options.migrateIdentity === false
    ? { identity: DEFAULT_IDENTITY, voice: DEFAULT_VOICE, found: { users: false, voice: false } }
    : readVoiceServerFiles(resolveDataDir(process.env))

  const resolved = resolveIdentity({ stored, storedVoice, options, legacy })

  if (stored === undefined || storedVoice === undefined) {
    if (stored === undefined) await ctx.storage.set(STORAGE_IDENTITY_KEY, identityJson(resolved.identity))
    if (storedVoice === undefined) await ctx.storage.set(STORAGE_VOICE_KEY, voiceJson(resolved.voice))
  }
  return resolved.source
}

export default Plugin.define({
  id: PLUGIN_ID,
  async setup(ctx) {
    // Diagnostics first: every line below lands on the console AND in the
    // Sebas data dir's plugin.log (runtime-resolved — see diagnostics.ts).
    const diagnostics = createDiagnostics({ filePath: resolveDiagnosticsPath(process.env) })
    const log = diagnostics.log

    // ---- plugin options ----
    const { options, warnings } = parseOptions((ctx.options ?? {}) as Record<string, unknown>)
    for (const warning of warnings) log("warn", warning)

    const pluginRoot = findPluginRoot()
    const disposals: Array<() => Promise<void>> = []

    // ---- identity/config in plugin storage (migration source: voice server's JSON) ----
    try {
      const identitySource = await loadIdentity(ctx, options)
      log("info", `identity/config loaded from ${identitySource} into plugin storage`)
    } catch (error) {
      log("warn", `identity/config seeding failed (${describeError(error)}); continuing`)
    }

    // ---- instructions hook: the butler persona and rules ----
    try {
      const hook = await ctx.session.hook("context", (event) => {
        // Idempotent: a second registration must not stack a second copy.
        const present = event.system.some(
          (part) => part.metadata?.[INSTRUCTIONS_MARKER] === true,
        )
        if (present) return
        event.system.push({
          type: "text",
          text: BUTLER_INSTRUCTIONS,
          metadata: { [INSTRUCTIONS_MARKER]: true },
        })
      })
      disposals.push(hook.dispose)
      log("info", "butler instructions hook registered")
    } catch (error) {
      log("warn", `instructions hook registration failed (${describeError(error)})`)
    }

    // ---- voice MCP: external state resolved ONCE, at setup ----
    // The transform callback below captures these values. Per the V2 docs,
    // captured inputs are not watched and `reload()` is only for when they
    // CHANGE — they cannot change within a process, so no reload is called.
    const voiceDir = resolveVoiceDir({ options, env: process.env, pluginRoot })
    for (const warning of voiceDir.warnings) log("warn", warning)
    const launch =
      voiceDir.status === "ok" ? resolveVoiceCommand({ options, voiceDir: voiceDir.value }) : undefined
    if (voiceDir.status === "ok") {
      log("info", `voice MCP located via ${voiceDir.source} (option → env → layout precedence)`)
    } else {
      log("warn", `${voiceDir.problem}. ${voiceDir.nextStep}`)
    }

    const notify = resolveNotifyPath({ options, env: process.env, pluginRoot })
    for (const warning of notify.warnings) log("warn", warning)
    const notifyPath = notify.status === "ok" ? notify.value : undefined
    if (notify.status === "ok") {
      log("info", `notify path available via ${notify.source}; notification cards enabled`)
    } else {
      log("warn", `${notify.problem}. ${notify.nextStep}`)
    }

    // Only-present keys all the way down: V2 rejects present-but-undefined.
    const input: VoiceWireInput = {
      ...(launch !== undefined ? { launch } : {}),
      ...(notifyPath !== undefined ? { notifyPath } : {}),
      ...(options.mcpProtocol !== undefined ? { protocol: options.mcpProtocol } : {}),
      ...(options.codemode !== undefined ? { codemode: options.codemode } : {}),
    }

    // ---- voice MCP transform: ALWAYS registered (replayable) ----
    // Never gated on a setup-time ctx.mcp.list() check: V2 rebuilds the
    // registry by replaying every active transform onto a fresh value, so the
    // callback must re-apply itself on every replay. It is cheap, synchronous
    // and must never throw — a throwing transform silently disables the whole
    // plugin, MCP servers included (applyVoiceTransform swallows and reports).
    try {
      const registration = await ctx.mcp.transform((editor) => {
        const outcome = applyVoiceTransform(editor, input)
        switch (outcome.branch) {
          case "registered":
            log("info", "transform run: 'voice' absent from the editor; registered ours")
            break
          case "refreshed":
            log("info", "transform run: 'voice' present and ours; re-registered (replay)")
            break
          case "skipped-user-entry":
            log("info", "transform run: 'voice' present and NOT ours (user configuration wins); left untouched")
            break
          case "skipped-no-launch":
            log("info", "transform run: 'voice' absent and no launch recipe resolved; nothing registered")
            break
          case "failed":
            log("warn", `transform run failed (${outcome.reason}); caught, plugin stays loaded`)
            break
        }
      })
      disposals.push(registration.dispose)
      log("info", "voice MCP transform registered (unconditional, replayable)")
    } catch (error) {
      log("warn", `mcp.transform registration failed (${describeError(error)})`)
    }

    // ---- post-registration view: forces the first replay, records the result ----
    try {
      const servers = await ctx.mcp.list()
      const entry = servers.data.find((server) => server.name === VOICE_MCP_NAME)
      if (entry === undefined) {
        log("warn", "post-registration: ctx.mcp.list() has no 'voice' (the transform did not land)")
      } else {
        const detail = "error" in entry.status ? ` (${entry.status.error})` : ""
        log("info", `post-registration: ctx.mcp.list() shows 'voice' with status ${entry.status.status}${detail}`)
      }
    } catch (error) {
      log("warn", `post-registration ctx.mcp.list() failed (${describeError(error)})`)
    }

    return async () => {
      await Promise.all(disposals.map((dispose) => dispose()))
    }
  },
})
