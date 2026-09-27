/**
 * Option and path resolution for the Sebas plugin.
 *
 * The plugin ships the two pieces it wires together INSIDE this package:
 * the voice MCP (a stdio JSON-RPC server, under `<plugin>/mcp/voice`) and
 * the notify/ card package (under `<plugin>/notify`).
 * Nothing about them may be hardcoded to one machine — that is this project's
 * portability convention — so every location is resolved at runtime with a
 * documented precedence:
 *
 *   1. plugin options (the `options` object in opencode.json)
 *   2. environment variables
 *   3. well-known relative layouts (the vendored layout inside the plugin
 *      package first, then this repository's sibling layout)
 *
 * All functions here are pure with respect to the inputs they take (they do
 * read the filesystem to validate candidate paths, never to guess absolute
 * machine locations), which keeps them unit-testable without a live OpenCode.
 */
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { resolveDataDir } from "./identity"

/** Where the voice MCP tree is looked up when no option/env names it. */
export const ENV_MCP_DIR = "SEBAS_MCP_DIR"

/**
 * Where the directory CONTAINING the notify/ package is looked up. Same
 * variable the voice MCP's run.sh already honours, so one knob serves both.
 */
export const ENV_NOTIFY_PATH = "SEBAS_NOTIFY_PATH"

export type McpProtocol = "legacy" | "auto" | "2026-07-28"

const MCP_PROTOCOLS: readonly string[] = ["legacy", "auto", "2026-07-28"]

/**
 * Plugin options accepted in opencode.json under `plugins[].options`.
 * Unknown keys are ignored (with a warning) so a typo never breaks startup.
 */
export interface SebasOptions {
  /** Directory that contains the voice MCP (server.py / run.sh). */
  readonly voiceDir?: string
  /** Full command override for the voice MCP, e.g. a wrapper script. */
  readonly voiceCommand?: readonly string[]
  /** Directory that CONTAINS the notify/ card package. */
  readonly notifyPath?: string
  /** MCP protocol negotiation override for the voice server. */
  readonly mcpProtocol?: McpProtocol
  /** Expose the voice tools through Code Mode instead of as plain tools. */
  readonly codemode?: boolean
  /** Set false to skip the one-time legacy identity import (see identity.ts). */
  readonly migrateIdentity?: boolean
  /** Seed for the identity store on first load (see identity.ts). */
  readonly identity?: Record<string, unknown>
  /** Seed for the voice profile store on first load (see identity.ts). */
  readonly voice?: Record<string, unknown>
}

/** A launch recipe for a local MCP server: what to run and where. */
export interface Launch {
  readonly command: readonly string[]
  readonly cwd: string
}

/** Resolved external pieces, or a house-style problem payload when missing. */
export type Resolution<T> =
  | {
      readonly status: "ok"
      readonly value: T
      /** Which knob won: the plugin option, the environment, or the layout. */
      readonly source: ResolutionSource
      readonly warnings: readonly string[]
    }
  | { readonly status: "skipped"; readonly problem: string; readonly nextStep: string; readonly warnings: readonly string[] }

/** Where a resolution came from — reported verbatim in the diagnostics log. */
export type ResolutionSource = "option" | "env" | "layout"

/** Expand a leading `~` to the home directory; leave absolute paths alone. */
export function expandHome(path: string): string {
  const trimmed = path.trim()
  if (trimmed === "~") return homedir()
  if (trimmed.startsWith("~/")) return join(homedir(), trimmed.slice(2))
  return trimmed
}

/** Read `plugins[].options` defensively: wrong types are dropped, not fatal. */
export function parseOptions(raw: Record<string, unknown>): {
  options: SebasOptions
  warnings: string[]
} {
  const warnings: string[] = []
  const options: {
    -readonly [K in keyof SebasOptions]?: SebasOptions[K]
  } = {}

  const str = (key: keyof SebasOptions): string | undefined => {
    const value = raw[key as string]
    if (value === undefined) return undefined
    if (typeof value !== "string" || value.trim() === "") {
      warnings.push(`option '${String(key)}' must be a non-empty string; ignored`)
      return undefined
    }
    return value
  }

  const voiceDir = str("voiceDir")
  if (voiceDir !== undefined) options.voiceDir = voiceDir
  const notifyPath = str("notifyPath")
  if (notifyPath !== undefined) options.notifyPath = notifyPath

  const voiceCommand = raw["voiceCommand"]
  if (voiceCommand !== undefined) {
    if (
      Array.isArray(voiceCommand) &&
      voiceCommand.length > 0 &&
      voiceCommand.every((part) => typeof part === "string" && part !== "")
    ) {
      options.voiceCommand = voiceCommand as string[]
    } else {
      warnings.push("option 'voiceCommand' must be a non-empty array of strings; ignored")
    }
  }

  const protocol = raw["mcpProtocol"]
  if (protocol !== undefined) {
    if (typeof protocol === "string" && MCP_PROTOCOLS.includes(protocol)) {
      options.mcpProtocol = protocol as McpProtocol
    } else {
      warnings.push(`option 'mcpProtocol' must be one of ${MCP_PROTOCOLS.join(", ")}; ignored`)
    }
  }

  const codemode = raw["codemode"]
  if (codemode !== undefined) {
    if (typeof codemode === "boolean") options.codemode = codemode
    else warnings.push("option 'codemode' must be a boolean; ignored")
  }

  const migrateIdentity = raw["migrateIdentity"]
  if (migrateIdentity !== undefined) {
    if (typeof migrateIdentity === "boolean") options.migrateIdentity = migrateIdentity
    else warnings.push("option 'migrateIdentity' must be a boolean; ignored")
  }

  for (const key of ["identity", "voice"] as const) {
    const value = raw[key]
    if (value === undefined) continue
    if (typeof value === "object" && value !== null && !Array.isArray(value)) {
      options[key] = value as Record<string, unknown>
    } else {
      warnings.push(`option '${key}' must be an object; ignored`)
    }
  }

  return { options, warnings }
}

/** True when `dir` looks like the voice MCP tree (it serves from server.py). */
function isVoiceMcpDir(dir: string): boolean {
  return existsSync(join(dir, "server.py"))
}

/** True when `dir` contains a usable notify/ card package. */
function isNotifyRoot(dir: string): boolean {
  return existsSync(join(dir, "notify", "__init__.py"))
}

/**
 * Locate the voice MCP tree: option → env → relative layout.
 * Layout candidates are tried in order: the vendored copy inside the plugin
 * package (`<plugin>/mcp/voice`) first, then a sibling `mcp/voice` directory
 * (the repo layout) — the packaged copy must never be shadowed.
 */
export function resolveVoiceDir(input: {
  options: SebasOptions
  env: Record<string, string | undefined>
  pluginRoot: string
}): Resolution<string> {
  const { options, env, pluginRoot } = input
  const warnings: string[] = []

  const explicit = options.voiceDir ? expandHome(options.voiceDir) : undefined
  if (explicit && isVoiceMcpDir(explicit)) {
    return { status: "ok", value: resolve(explicit), source: "option", warnings }
  }
  if (explicit) {
    warnings.push(`option 'voiceDir' does not contain a voice MCP (server.py not found); ignored`)
  }

  const fromEnv = (env[ENV_MCP_DIR] ?? "").trim()
  if (fromEnv) {
    const expanded = expandHome(fromEnv)
    if (isVoiceMcpDir(expanded)) {
      return { status: "ok", value: resolve(expanded), source: "env", warnings }
    }
    warnings.push(`${ENV_MCP_DIR} does not contain a voice MCP (server.py not found); ignored`)
  }

  for (const candidate of [join(pluginRoot, "mcp", "voice"), join(dirname(pluginRoot), "mcp", "voice")]) {
    if (isVoiceMcpDir(candidate)) {
      return { status: "ok", value: resolve(candidate), source: "layout", warnings }
    }
  }

  return {
    status: "skipped",
    problem: "the voice MCP could not be located",
    nextStep:
      `Set the 'voiceDir' plugin option or the ${ENV_MCP_DIR} environment variable ` +
      "to the directory that contains the voice MCP (the one with server.py), then " +
      "restart OpenCode. Without it the plugin only injects the butler instructions.",
    warnings,
  }
}

/**
 * How to launch the voice MCP inside `voiceDir`: explicit command wins;
 * otherwise a per-platform default. POSIX runs `bash run.sh`, which resolves
 * the venv itself. Windows has no run.sh: launch the venv interpreter the
 * voice setup creates (`<data>/venv/Scripts/python.exe`, located through the
 * same `identity.resolveDataDir` rule as everything else) and fall back to
 * `python` on PATH when it does not exist — the server then prints the setup
 * hint. Nothing is hardcoded to one machine: `env` and `home` are injectable
 * inputs like every other resolution here.
 */
export function resolveVoiceCommand(input: {
  options: SebasOptions
  voiceDir: string
  platform?: NodeJS.Platform
  env?: Record<string, string | undefined>
  home?: string
}): Launch {
  const { options, voiceDir } = input
  const platform = input.platform ?? process.platform
  if (options.voiceCommand && options.voiceCommand.length > 0) {
    return { command: [...options.voiceCommand], cwd: voiceDir }
  }
  if (platform === "win32") {
    const venvPython = join(
      resolveDataDir(input.env ?? process.env, input.home ?? homedir()),
      "venv",
      "Scripts",
      "python.exe",
    )
    const command = existsSync(venvPython) ? [venvPython, "server.py"] : ["python", "server.py"]
    return { command, cwd: voiceDir }
  }
  return { command: ["bash", "run.sh"], cwd: voiceDir }
}

/**
 * Locate the directory that CONTAINS the notify/ card package: option → env →
 * layout. Layout candidates are tried in order: the vendored `<plugin>/notify`
 * first (so `pluginRoot` itself is the containing directory), then a
 * `notify/` next to plugin/ (the repo layout) — the packaged copy must never
 * be shadowed.
 */
export function resolveNotifyPath(input: {
  options: SebasOptions
  env: Record<string, string | undefined>
  pluginRoot: string
}): Resolution<string> {
  const { options, env, pluginRoot } = input
  const warnings: string[] = []

  const explicit = options.notifyPath ? expandHome(options.notifyPath) : undefined
  if (explicit && isNotifyRoot(explicit)) {
    return { status: "ok", value: resolve(explicit), source: "option", warnings }
  }
  if (explicit) {
    warnings.push("option 'notifyPath' does not contain the notify/ package; ignored")
  }

  const fromEnv = (env[ENV_NOTIFY_PATH] ?? "").trim()
  if (fromEnv) {
    const expanded = expandHome(fromEnv)
    if (isNotifyRoot(expanded)) {
      return { status: "ok", value: resolve(expanded), source: "env", warnings }
    }
    warnings.push(`${ENV_NOTIFY_PATH} does not contain the notify/ package; ignored`)
  }

  for (const candidate of [pluginRoot, dirname(pluginRoot)]) {
    if (isNotifyRoot(candidate)) {
      return { status: "ok", value: resolve(candidate), source: "layout", warnings }
    }
  }

  return {
    status: "skipped",
    problem: "the notify/ card package could not be located",
    nextStep:
      `Set the 'notifyPath' plugin option or the ${ENV_NOTIFY_PATH} environment ` +
      "variable to the directory that CONTAINS notify/, then restart OpenCode. " +
      "Without it speak() falls back to the spoken short notice and the chat confirmation.",
    warnings,
  }
}
