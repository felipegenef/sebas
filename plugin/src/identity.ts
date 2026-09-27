/**
 * Identity and voice configuration for the butler: who speaks, who is served,
 * in which language and voice, and how the user likes to be called
 * (`formOfAddress`, the verbatim vocative).
 *
 * Identity is configuration, never hardcoded in shared instructions — the
 * injected persona (instructions.ts) has no names in it and the agent reads
 * them with the `get_user_name` tool. This module keeps the plugin-side store
 * (OpenCode's per-plugin `storage`) that holds those values.
 *
 * Today the voice MCP remains the runtime owner of identity: its
 * `set_user_name` / `set_butler_name` / `set_language` / `set_voice` tools
 * write the voice server's JSON files. This store is the plugin-side mirror seeded from
 * the same source once (read-only migration), so the plugin store can become
 * authoritative when the Python package is vendored into the plugin layout.
 * Nothing here ever writes to or deletes the voice server's files.
 */
import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

export const STORAGE_IDENTITY_KEY = "identity"
export const STORAGE_VOICE_KEY = "voice"

/** Spoken languages the voice server supports. */
export const LANGUAGES: Readonly<Record<string, string>> = {
  "en": "en-us",
  "en-us": "en-us",
  "pt": "pt-br",
  "pt-br": "pt-br",
}

/** Who speaks and who is served. null = not set (the voice default applies). */
export interface Identity {
  readonly butlerName: string | null
  readonly language: string | null
  readonly mainUser: string | null
  readonly users: readonly string[]
  readonly people: readonly string[]
  /**
   * How the user likes to be called — free text used VERBATIM as the vocative:
   * a treatment ("senhor", "senhora", "doutor", "chefe"…) or the complete
   * form ("senhor Alex"). `null` = never set (absent in a merge: falls
   * through to the lower layer); `""` = explicitly CLEARED (a present key:
   * wins over lower layers, like normalizeVoiceConfig's "absent = undefined"
   * presence model). Both mean "use the language default greeting" at runtime.
   */
  readonly formOfAddress: string | null
}

/**
 * Active voice profile (mirrors the voice server's profile file:
 * `config.json`, or its pre-1.0 `voz.json` in a legacy data dir). Resolved shape:
 * every field is concrete — this is what plugin storage holds. `voice: null`
 * means "no explicit voice" (the engine default applies).
 */
export interface VoiceConfig {
  readonly voice: string | null
  readonly speed: number
  readonly play: boolean
}

/**
 * A parsed voice profile in which an ABSENT field (`undefined`) means "no
 * opinion": the key was missing or invalid in the source. This distinction is
 * the whole point — merges run on key presence, never on value ≠ default, so
 * a stored `speed: 1.0` must beat a legacy `speed: 1.5` even though 1.0 is
 * the default value. Filling defaults here would make the two indistinguishable
 * and silently resurrect legacy values (the "storage wins" rule breaks).
 * `voice: null` is allowed and means "no explicit voice", like VoiceConfig.
 */
export interface VoiceConfigPatch {
  readonly voice?: string | null
  readonly speed?: number
  readonly play?: boolean
}

export const DEFAULT_IDENTITY: Identity = {
  butlerName: null,
  language: null,
  mainUser: null,
  users: [],
  people: [],
  formOfAddress: null,
}

/** The bottom merge layer: values used for keys no source sets. */
export const DEFAULT_VOICE: VoiceConfig = { voice: null, speed: 1.0, play: true }

/** "en" / "en-us" → "en-us"; unknown values → null (never guess). */
export function normalizeLanguage(raw: unknown): string | null {
  if (typeof raw !== "string") return null
  return LANGUAGES[raw.trim().toLowerCase()] ?? null
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null
}

/**
 * String where "" carries meaning (an explicit clear): a present string stays
 * present after trimming — `""` and whitespace become `""` (cleared, a
 * present key that wins in merges) — while missing or non-string values stay
 * absent (`null`, falls through). This is the Identity-side twin of
 * normalizeVoiceConfig's "absent = undefined" model.
 */
function asPresentString(value: unknown): string | null {
  return typeof value === "string" ? value.trim() : null
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((entry) => asString(entry))
    .filter((entry): entry is string => entry !== null)
}

/** Tolerant parser for identity JSON (any source: storage, options, files). */
export function normalizeIdentity(raw: unknown): Identity {
  const data = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>
  return {
    butlerName: asString(data["butlerName"] ?? data["butler_name"]),
    language: normalizeLanguage(data["language"]),
    mainUser: asString(data["mainUser"] ?? data["main_user"]),
    users: asStringList(data["users"]),
    people: asStringList(data["people"]),
    formOfAddress: asPresentString(data["formOfAddress"] ?? data["form_of_address"]),
  }
}

/**
 * Tolerant parser for the voice profile. Accepts the legacy `kokoro_voice`
 * key the voice server itself migrates, so both files parse the same way.
 *
 * Returns a PATCH, not a filled config: missing or invalid keys stay absent
 * (`undefined`) instead of being filled with defaults, so the merge layer can
 * tell "explicitly set to the default value" (wins) from "never set" (falls
 * through to the lower layer).
 */
export function normalizeVoiceConfig(raw: unknown): VoiceConfigPatch {
  const data = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>
  const speed = typeof data["speed"] === "number" && Number.isFinite(data["speed"])
    ? data["speed"]
    : undefined
  return {
    // null (unset/invalid voice) becomes absent so an empty voice never
    // overrides a lower layer; VoiceConfig.voice keeps null as its "unset".
    voice: asString(data["voice"] ?? data["kokoro_voice"]) ?? undefined,
    speed,
    play: typeof data["play"] === "boolean" ? data["play"] : undefined,
  }
}

/**
 * Overlay `patch` on `base`. Identity marks "unset" as null/empty list (its
 * representation has no other room for absence), so a set string or a
 * non-empty list in patch always wins — even at a value equal to the default.
 * `formOfAddress` runs on key presence like the voice patch: `null` (absent)
 * falls through, while a present value wins — including `""`, which is an
 * explicit clear ("no form of address", the language default returns) and
 * must beat lower layers so cleared storage never resurrects a legacy form.
 */
export function mergeIdentity(base: Identity, patch: Identity): Identity {
  return {
    butlerName: patch.butlerName ?? base.butlerName,
    language: patch.language ?? base.language,
    mainUser: patch.mainUser ?? base.mainUser,
    users: patch.users.length > 0 ? patch.users : base.users,
    people: patch.people.length > 0 ? patch.people : base.people,
    formOfAddress: patch.formOfAddress ?? base.formOfAddress,
  }
}

/**
 * Overlay `patch` on `base` on KEY PRESENCE: a patch key wins whenever it is
 * set (`undefined` = absent), including when its value equals the default —
 * that is what "storage wins" means. `voice: null` marks "no explicit voice"
 * and never overrides.
 */
export function mergeVoice(base: VoiceConfig, patch: VoiceConfigPatch): VoiceConfig {
  return {
    voice: patch.voice ?? base.voice,
    speed: patch.speed !== undefined ? patch.speed : base.speed,
    play: patch.play !== undefined ? patch.play : base.play,
  }
}

/** Directory name of a pre-1.0 install (auto-detection only — see below). */
const LEGACY_DIR_NAME = "voz"

/**
 * The Sebas data directory (where users.json and the voice profile live),
 * resolved at runtime the same way the voice server resolves it (its
 * voice/core.py is the authority): `<data home>/sebas` (default
 * `~/.local/share/sebas`), with XDG_DATA_HOME when set. A pre-1.0 legacy
 * location (`<data home>/voz`) is auto-detected and used AS-IS when the new
 * directory does not exist — a pre-1.0 legacy location, auto-detected, no
 * user action needed. Never hardcoded.
 */
export function resolveDataDir(
  env: Record<string, string | undefined>,
  home: string = homedir(),
): string {
  const dataHome = (env["XDG_DATA_HOME"] ?? "").trim() || join(home, ".local", "share")
  const current = join(dataHome, "sebas")
  if (existsSync(current)) return current
  const legacy = join(dataHome, LEGACY_DIR_NAME)
  if (existsSync(legacy)) return legacy
  return current
}

export interface VoiceServerFiles {
  readonly identity: Identity
  /** Parsed as a patch: only the keys the file actually sets contribute. */
  readonly voice: VoiceConfigPatch
  /** Which voice-server files were actually found and parsed. */
  readonly found: { readonly users: boolean; readonly voice: boolean }
}

function readJson(path: string): Record<string, unknown> | null {
  if (!existsSync(path)) return null
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, "utf-8"))
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
  } catch {
    return null // a corrupt file is treated as absent, never fatal
  }
}

/**
 * Read the voice server's existing config files (users.json and the voice
 * profile). STRICTLY READ-ONLY: this is the documented migration source; the
 * files are never modified, moved or deleted by the plugin.
 *
 * Profile file name rule (mirrors the server's config_file): `config.json` is
 * the current name and wins; a pre-1.0 `voz.json` is read when config.json is
 * missing (a legacy dir keeps that name, or someone moved an old dir by
 * hand) — voice settings are never lost.
 */
export function readVoiceServerFiles(dataDir: string): VoiceServerFiles {
  const users = readJson(join(dataDir, "users.json"))
  const voice = readJson(join(dataDir, "config.json"))
    ?? readJson(join(dataDir, `${LEGACY_DIR_NAME}.json`))
  return {
    identity: normalizeIdentity(users ?? {}),
    voice: normalizeVoiceConfig(voice ?? {}),
    found: { users: users !== null, voice: voice !== null },
  }
}

export type IdentitySource = "storage" | "options" | "legacy" | "defaults"

export interface ResolvedIdentity {
  readonly identity: Identity
  readonly voice: VoiceConfig
  /** Which input won the merge — reported for logs and support. */
  readonly source: IdentitySource
}

/**
 * Merge order (each layer seeds only what the layer above left absent):
 * legacy voice server's files (read-only migration source) → plugin options → plugin
 * storage (authoritative) → built-in defaults. Presence-based throughout —
 * a stored value equal to the default still beats an options/legacy value
 * ("storage wins"). The reported source is the topmost layer that was present.
 */
export function resolveIdentity(input: {
  stored?: unknown
  storedVoice?: unknown
  options: { identity?: Record<string, unknown>; voice?: Record<string, unknown> }
  legacy: VoiceServerFiles
}): ResolvedIdentity {
  const { stored, storedVoice, options, legacy } = input

  let identity = DEFAULT_IDENTITY
  let voice = DEFAULT_VOICE
  let source: IdentitySource = "defaults"

  if (legacy.found.users || legacy.found.voice) {
    identity = mergeIdentity(identity, legacy.identity)
    voice = mergeVoice(voice, legacy.voice)
    source = "legacy"
  }
  if (options.identity !== undefined) {
    identity = mergeIdentity(identity, normalizeIdentity(options.identity))
    source = "options"
  }
  if (options.voice !== undefined) {
    voice = mergeVoice(voice, normalizeVoiceConfig(options.voice))
    source = "options"
  }
  if (stored !== undefined || storedVoice !== undefined) {
    identity = mergeIdentity(identity, normalizeIdentity(stored ?? {}))
    voice = mergeVoice(voice, normalizeVoiceConfig(storedVoice ?? {}))
    source = "storage"
  }

  return { identity, voice, source }
}
