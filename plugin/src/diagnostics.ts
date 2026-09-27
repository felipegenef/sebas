/**
 * Diagnostics for the plugin: every log line goes to the console AND to a
 * small log file beside the voice server's own data — the Sebas data dir
 * (`<data home>/sebas`, default `~/.local/share/sebas`; a pre-1.0 legacy
 * location is auto-detected — see `identity.resolveDataDir`), the same
 * directory that holds `users.json` and the voice profile.
 *
 * Why a file: the plugin's console lines can be invisible depending on how
 * OpenCode captures plugin stdout, and a silent plugin is undebuggable. The
 * file is the user's ground truth for "did the voice MCP register, and why
 * (not)?".
 *
 * The file holds the same `[sebas] ...` lines the console gets and is
 * trimmed to the last {@link DIAGNOSTICS_MAX_LINES} lines on every append, so
 * it stays small across restarts and replay storms. Writing diagnostics must
 * NEVER break the plugin: every filesystem and echo call is swallowed on
 * failure.
 */
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { resolveDataDir } from "./identity"

/** File name inside the Sebas data dir. */
export const DIAGNOSTICS_FILE = "plugin.log"

/** The log file keeps only the newest lines (trimmed on every append). */
export const DIAGNOSTICS_MAX_LINES = 200

/** Prefix every line carries — the greppable marker in console and file. */
export const LOG_PREFIX = "[sebas]"

export interface Diagnostics {
  /** Resolved location of the log file (runtime; never hardcoded). */
  readonly path: string
  /** Append one `[sebas] ...` line to console and file. Never throws. */
  log(level: "info" | "warn", message: string): void
}

/**
 * Resolve the diagnostics file path at runtime: the Sebas data dir (see
 * `identity.resolveDataDir` — `<data home>/sebas`, default
 * `~/.local/share/sebas`, legacy location auto-detected) plus
 * {@link DIAGNOSTICS_FILE}.
 */
export function resolveDiagnosticsPath(
  env: Record<string, string | undefined>,
  home?: string,
): string {
  return join(resolveDataDir(env, home), DIAGNOSTICS_FILE)
}

/**
 * Create the diagnostics writer. `echo` is the console sink (injectable so
 * tests stay quiet); it defaults to `console.warn`/`console.log`.
 */
export function createDiagnostics(options: {
  filePath: string
  maxLines?: number
  echo?: (line: string, level: "info" | "warn") => void
}): Diagnostics {
  const filePath = options.filePath
  const maxLines = options.maxLines ?? DIAGNOSTICS_MAX_LINES
  const echo = options.echo ?? ((line: string, level: "info" | "warn") => {
    if (level === "warn") console.warn(line)
    else console.log(line)
  })

  const append = (line: string): void => {
    mkdirSync(dirname(filePath), { recursive: true })
    appendFileSync(filePath, `${line}\n`, "utf-8")
    // Trim: read, keep the newest maxLines non-empty lines, rewrite.
    const lines = readFileSync(filePath, "utf-8").split("\n")
    while (lines.length > 0 && lines[lines.length - 1] === "") lines.pop()
    if (lines.length <= maxLines) return
    writeFileSync(filePath, `${lines.slice(-maxLines).join("\n")}\n`, "utf-8")
  }

  return {
    path: filePath,
    log(level, message) {
      const line = `${LOG_PREFIX} ${message}`
      try {
        echo(line, level)
      } catch {
        // console failure is never fatal
      }
      try {
        append(line)
      } catch {
        // the log file is best-effort: an unwritable path must not break the plugin
        try {
          echo(`${LOG_PREFIX} diagnostics file could not be written`, "warn")
        } catch {
          // nothing more we can do
        }
      }
    },
  }
}
