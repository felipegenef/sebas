/**
 * Registration of the voice MCP inside OpenCode.
 *
 * WHY `mcp.transform` and not native plugin tools: the voice server is already a
 * complete stdio JSON-RPC MCP (schemas, handlers, the daemon bridge and the
 * notification-card flow live in its Python code). Registering the server
 * through `ctx.mcp.transform` exposes its whole tool surface — `speak`,
 * `set_voice`, `list_voices`, `voice_status`, `measure_rtf`, `warmup`,
 * `get_user_name`, `set_user_name`, `set_butler_name`, `set_language`, and the
 * notification tools — unchanged, with zero reimplementation and one source of
 * truth. Re-implementing those tools in TypeScript here would fork the tool
 * surface and lose the queue/card semantics the Python side already encodes.
 */
import type { Mcp } from "@opencode/plugin"
// `MCPEditor` is the exact editor `ctx.mcp.transform` hands its callback; it is
// published under the package's subpath exports rather than the root, and this
// is a type-only import (erased at build time).
import type { MCPEditor } from "@opencode/plugin/promise/mcp"
import type { Launch, McpProtocol } from "./config"

/** The MCP server name OpenCode registers the voice server under. */
export const VOICE_MCP_NAME = "voice"

/**
 * Environment variable passed to the voice server: the directory that CONTAINS
 * the notify/ package. The server's own run.sh already honours this variable,
 * so the plugin simply supplies the resolved value.
 */
export const NOTIFY_ENV = "SEBAS_NOTIFY_PATH"

export interface VoiceServerInput {
  readonly launch: Launch
  readonly notifyPath?: string
  readonly protocol?: McpProtocol
  readonly codemode?: boolean
}

/**
 * Build the local MCP server config for the voice server.
 *
 * ONLY PRESENT KEYS. OpenCode V2 validates every transform payload against
 * `Mcp.ServerConfig`, whose optional fields are encoded as `optionalKey`s
 * (see @opencode/schema Mcp.LocalConfig): a key that is PRESENT with an
 * `undefined` value fails schema validation, and a failing transform
 * silently disables the whole plugin — MCP servers included. So an unset
 * option must produce an ABSENT key, never `key: undefined`. Every optional
 * key below is spread conditionally and checked against `undefined`
 * explicitly, including values that arrive as present-but-undefined from
 * callers. All keys set here (`type`, `command`, `cwd`, `environment`,
 * `codemode`, `protocol`) are legal on the `type: "local"` union member.
 */
export function buildVoiceServerConfig(input: VoiceServerInput): Mcp.ServerConfig {
  const config: Mcp.ServerConfig = {
    type: "local",
    command: [...input.launch.command],
    ...(input.launch.cwd !== undefined ? { cwd: input.launch.cwd } : {}),
    ...(input.notifyPath !== undefined ? { environment: { [NOTIFY_ENV]: input.notifyPath } } : {}),
    ...(input.codemode !== undefined ? { codemode: input.codemode } : {}),
    ...(input.protocol !== undefined ? { protocol: input.protocol } : {}),
  }
  return config
}

/**
 * Order-insensitive structural equality for server configs, used to tell the
 * plugin's OWN registration apart from a user-configured entry ("is this
 * entry ours?"). A present key with an `undefined` value counts as absent,
 * matching the key-presence model of {@link buildVoiceServerConfig}.
 */
export function sameServerConfig(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    return a.every((item, index) => sameServerConfig(item, b[index]))
  }
  const recordA = a as Record<string, unknown>
  const recordB = b as Record<string, unknown>
  const keysA = Object.keys(recordA).filter((key) => recordA[key] !== undefined)
  const keysB = Object.keys(recordB).filter((key) => recordB[key] !== undefined)
  if (keysA.length !== keysB.length) return false
  return keysA.every(
    (key) => recordB[key] !== undefined && sameServerConfig(recordA[key], recordB[key]),
  )
}

/** What one replayable transform run did — the diagnostics subject. */
export type TransformOutcome =
  /** No entry existed: `editor.set` registered the plugin's config. */
  | { readonly branch: "registered" }
  /** The entry present was ours: `editor.set` re-applied the same config. */
  | { readonly branch: "refreshed" }
  /** A user-configured entry (not ours) is present: no writes, user wins. */
  | { readonly branch: "skipped-user-entry" }
  /** No entry and no launch recipe resolved: nothing to register. */
  | { readonly branch: "skipped-no-launch" }
  /** A plugin API call threw: caught and reported, never rethrown. */
  | { readonly branch: "failed"; readonly reason: string }

/**
 * What this plugin wrote earlier in this PROCESS. A replay must recognize its
 * own earlier registration even when the resolved launch recipe has changed
 * since — the first-run runtime setup flips Windows from `python server.py` to
 * the venv interpreter mid-session, and that update is the plugin refreshing
 * itself, never clobbering a user entry (a user's entry can never equal a
 * config only this plugin ever wrote).
 */
export interface TransformMemory {
  registered?: Mcp.ServerConfig
}

/**
 * Body of the `ctx.mcp.transform` callback — the REPLAYABLE registration.
 *
 * V2 rebuilds the MCP registry "by replaying every active transform in
 * registration order onto a fresh value" whenever it is marked changed, so
 * this runs again and again on config reloads and must stay cheap and
 * repeatable (the V2 plugins guide: "Keep transforms cheap and repeatable").
 * It is also entirely synchronous and MUST NOT throw: a throwing transform
 * disables the whole plugin, MCP servers included. Hence the blanket
 * try/catch that converts any failure into a logged `{branch: "failed"}`.
 *
 * Ownership rule (user configuration always wins): when the editor already
 * holds a `voice` entry that is not byte-identical to the config the plugin
 * registers — nor to the config this plugin wrote earlier in this process
 * (see {@link TransformMemory}) — the transform writes NOTHING: the user's
 * `command`, `cwd`, `protocol`, `codemode` and every other key stay exactly as
 * written. The plugin only (re-)registers when the entry is absent or is its
 * own earlier registration (same config, or its recorded one while the launch
 * recipe refreshes mid-session).
 */
export function applyVoiceTransform(editor: MCPEditor, input: VoiceWireInput, memory?: TransformMemory): TransformOutcome {
  try {
    const existing = editor.get(VOICE_MCP_NAME)
    const config =
      input.launch === undefined
        ? undefined
        : buildVoiceServerConfig({
            launch: input.launch,
            ...(input.notifyPath !== undefined ? { notifyPath: input.notifyPath } : {}),
            ...(input.protocol !== undefined ? { protocol: input.protocol } : {}),
            ...(input.codemode !== undefined ? { codemode: input.codemode } : {}),
          })
    const ours =
      existing !== undefined &&
      config !== undefined &&
      (sameServerConfig(existing, config) ||
        (memory !== undefined &&
          memory.registered !== undefined &&
          sameServerConfig(existing, memory.registered)))
    if (existing !== undefined && !ours) return { branch: "skipped-user-entry" }
    if (config === undefined) return { branch: "skipped-no-launch" }
    editor.set(VOICE_MCP_NAME, config)
    if (memory !== undefined) memory.registered = config
    return { branch: existing === undefined ? "registered" : "refreshed" }
  } catch (error) {
    return { branch: "failed", reason: error instanceof Error ? error.message : String(error) }
  }
}

/**
 * Like {@link VoiceServerInput}, but the launch recipe is only needed when there
 * is no voice entry yet to update: an existing entry keeps its own command/cwd
 * and must never be relaunched by the plugin.
 */
export interface VoiceWireInput extends Omit<VoiceServerInput, "launch"> {
  readonly launch?: Launch
}

/** What happened to the session's voice MCP entry — the startup log's subject. */
export type WireOutcome =
  | { readonly status: "registered" }
  | { readonly status: "merged" }
  | { readonly status: "untouched"; readonly reason: string }

/**
 * Wire the voice server into the session's MCP config (the `mcp.transform`
 * editor), one of three ways:
 *
 *   - no entry yet: register a fresh one (only when a launch recipe resolved);
 *   - an existing user-configured entry: merge ONLY the notify env key in, so
 *     notification cards work on machines where the user already configured
 *     voice themselves — run.sh defaults an unset `SEBAS_NOTIFY_PATH` to empty;
 *   - otherwise leave the entry alone and say why.
 *
 * The existing entry is the user's word: `command`, `cwd`, `protocol`,
 * `codemode`, `disabled`, any key they wrote — and every other environment key
 * — survive byte-identical. Mutations go through `editor.update`, which edits
 * the entry in place for THIS session only; the config file on disk is never
 * rewritten. The function is idempotent: a transform re-run on config reload
 * sees `SEBAS_NOTIFY_PATH` already present and does nothing.
 *
 * NOTE: the live transform callback is {@link applyVoiceTransform}, which
 * applies the stricter never-clobber rule — a user-configured entry is left
 * completely untouched (not even the notify key is merged). This function
 * keeps the gentler merge-only-one-key policy available and pinned by tests;
 * wire it back in only where merging into a user entry is deliberately wanted.
 */
export function wireVoiceServer(editor: MCPEditor, input: VoiceWireInput): WireOutcome {
  const existing = editor.get(VOICE_MCP_NAME)

  if (existing === undefined) {
    if (input.launch === undefined) {
      return { status: "untouched", reason: "the voice MCP could not be located to register" }
    }
    editor.set(
      VOICE_MCP_NAME,
      buildVoiceServerConfig({
        launch: input.launch,
        notifyPath: input.notifyPath,
        protocol: input.protocol,
        codemode: input.codemode,
      }),
    )
    return { status: "registered" }
  }

  // Existing entry from here on: never clobber, only ensure the notify path.
  if (input.notifyPath === undefined) {
    return { status: "untouched", reason: "no notify path could be resolved; spoken-notice fallback" }
  }
  // `environment` exists only on local stdio servers; a remote voice server runs
  // elsewhere and gets no local env from us.
  if (existing.type !== "local") {
    return { status: "untouched", reason: "a remote server gets no local environment" }
  }
  // Key PRESENCE, not truthiness: an explicit empty `SEBAS_NOTIFY_PATH` is a
  // deliberate clear by the user and wins over the plugin's resolution.
  if (existing.environment !== undefined && Object.hasOwn(existing.environment, NOTIFY_ENV)) {
    return { status: "untouched", reason: `its environment already sets ${NOTIFY_ENV} (the user's value wins)` }
  }

  const merged = { ...(existing.environment ?? {}), [NOTIFY_ENV]: input.notifyPath }
  editor.update(VOICE_MCP_NAME, (config) => {
    if (config.type === "local") config.environment = merged
  })
  return { status: "merged" }
}
