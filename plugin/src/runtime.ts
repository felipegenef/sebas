/**
 * First-run voice runtime setup, automatic and non-blocking.
 *
 * The voice MCP needs two things that are NOT shipped in the package: the
 * Python venv (`<data>/venv`) and the Kokoro weights (`<data>/models/kokoro`,
 * ~300 MB). `mcp/voice/setup.sh` / `setup.ps1` create them; this module makes
 * that first run happen BY ITSELF on Linux, macOS and Windows:
 *
 *   1. detection — the runtime is "ready" when the venv interpreter AND both
 *      model files exist (the common case after the first run: a no-op);
 *   2. install — when incomplete, the platform installer is spawned DETACHED
 *      and unblocking (the plugin never waits for a 300 MB download), with its
 *      output landing in `<data>/setup.log` beside the plugin's own log;
 *   3. concurrency — `<data>/setup.lock` (holding the installer's pid) keeps
 *      parallel OpenCode sessions from ever running two setups; a stale lock
 *      (no such pid, or older than {@link LOCK_STALE_MS}) is reclaimed;
 *   4. completion — {@link watchVoiceRuntime} polls the same detection and
 *      fires `onReady` once, so the caller can `ctx.mcp.reload()` and get
 *      speech without any restart. It watches fast, then slow, until the
 *      runtime is ready — bounded by the lock-staleness horizon, and never
 *      silent about what happens on the bound.
 *
 * Everything here is failure-contained: a missing installer, an unwritable
 * data dir or a spawn error comes back as a house-style `{problem, nextStep}`
 * payload and never throws into plugin setup. Nothing is hardcoded to one
 * machine: platform, environment and every filesystem probe are injectable
 * inputs, which is also what keeps the tests offline (no download ever, no
 * real installer ever).
 */
import { spawn as nodeSpawn } from "node:child_process"
import {
  closeSync,
  constants,
  existsSync,
  ftruncateSync,
  mkdirSync,
  openSync,
  readFileSync,
  statSync,
  unlinkSync,
  writeSync,
} from "node:fs"
import { delimiter, join } from "node:path"

/** Lock file in the data dir: holds the pid of the running installer. */
export const RUNTIME_LOCK_FILE = "setup.lock"

/** Installer stdout/stderr lands here, next to the plugin's own log. */
export const RUNTIME_LOG_FILE = "setup.log"

/**
 * A lock older than this is stale even when its pid looks alive: a setup that
 * has not finished in two hours is wedged (a healthy pip install + 300 MB
 * download finishes well under that). Dead pids are reclaimed immediately.
 */
export const LOCK_STALE_MS = 2 * 60 * 60 * 1000

/** Completion check cadence and bounds: fast while a quick finish is likely,
 *  slow once the install is clearly long — and always bounded. */
export const WATCH_INTERVAL_MS = 5_000

/** A setup still going after this long is a SLOW one (a ~300 MB download plus
 *  pip can take well over twenty minutes): drop to the slow cadence instead of
 *  giving up. */
export const WATCH_FAST_MS = 20 * 60 * 1000

/** Slow-poll cadence for a slow install: 30–60 s, cheap on both sides. */
export const WATCH_SLOW_INTERVAL_MS = 45 * 1000

/** Hard bound: the lock-staleness horizon (2 h). Beyond it the setup lock is
 *  stale by definition, so the watcher stops and says so EXPLICITLY — an
 *  honest "restart OpenCode when it finishes", never a silent degradation. */
export const WATCH_TIMEOUT_MS = LOCK_STALE_MS

/** Filesystem probe (injectable so tests never touch a real install). */
export type ExistsFn = (path: string) => boolean

/** One `[sebas]` diagnostics line, without the prefix (diagnostics adds it). */
export type RuntimeLog = (level: "info" | "warn", message: string) => void

/** Per-platform layout of the pieces the installers create (core.py parity). */
export interface VoiceRuntimePaths {
  /** The venv interpreter: `venv/bin/python` (POSIX) / `Scripts\python.exe`. */
  readonly venvPython: string
  readonly onnx: string
  readonly voices: string
}

/**
 * The runtime layout inside the resolved data dir. Windows venvs keep the
 * interpreter at `venv/Scripts/python.exe`, POSIX venvs at `venv/bin/python` —
 * the same rule `config.resolveVoiceCommand` and `voice/core.venv_python` use.
 */
export function voiceRuntimePaths(dataDir: string, platform: NodeJS.Platform = process.platform): VoiceRuntimePaths {
  return {
    venvPython: platform === "win32"
      ? join(dataDir, "venv", "Scripts", "python.exe")
      : join(dataDir, "venv", "bin", "python"),
    onnx: join(dataDir, "models", "kokoro", "kokoro-v1.0.onnx"),
    voices: join(dataDir, "models", "kokoro", "voices-v1.0.bin"),
  }
}

export interface VoiceRuntimeState {
  /** True when the venv interpreter and BOTH model files exist. */
  readonly ready: boolean
  /** Human labels of the missing pieces (empty when ready). */
  readonly missing: readonly string[]
}

/** What the runtime still needs — empty means complete (the common case). */
export function inspectVoiceRuntime(input: {
  dataDir: string
  platform?: NodeJS.Platform
  exists?: ExistsFn
}): VoiceRuntimeState {
  const exists = input.exists ?? existsSync
  const paths = voiceRuntimePaths(input.dataDir, input.platform ?? process.platform)
  const missing: string[] = []
  if (!exists(paths.venvPython)) missing.push("venv")
  if (!exists(paths.onnx)) missing.push("kokoro-v1.0.onnx")
  if (!exists(paths.voices)) missing.push("voices-v1.0.bin")
  return { ready: missing.length === 0, missing }
}

export function isVoiceRuntimeReady(input: {
  dataDir: string
  platform?: NodeJS.Platform
  exists?: ExistsFn
}): boolean {
  return inspectVoiceRuntime(input).ready
}

/**
 * The PowerShell to drive `setup.ps1` with: `pwsh` (PowerShell 7) when it is on
 * PATH, otherwise `powershell` (Windows PowerShell, present on every Windows).
 * No shell lookup and no spawn: the PATH is scanned through the injected file
 * probe, so the choice is deterministic and testable from any host.
 */
export function findPowerShell(input: {
  env?: Record<string, string | undefined>
  exists?: ExistsFn
}): string {
  const exists = input.exists ?? existsSync
  const env = input.env ?? process.env
  const raw = (env["PATH"] ?? env["Path"] ?? "").trim()
  for (const dir of raw.split(delimiter)) {
    const trimmed = dir.trim()
    if (trimmed === "") continue
    if (exists(join(trimmed, "pwsh.exe")) || exists(join(trimmed, "pwsh"))) return "pwsh"
  }
  return "powershell"
}

export type InstallerResolution =
  | { readonly status: "ok"; readonly command: readonly string[] }
  | { readonly status: "skipped"; readonly problem: string; readonly nextStep: string }

/**
 * How to run the platform installer inside `voiceDir`:
 * POSIX `bash <voiceDir>/setup.sh`; Windows
 * `powershell|pwsh -NoProfile -ExecutionPolicy Bypass -File <voiceDir>\setup.ps1`.
 * A missing installer is a payload, never a spawn failure.
 */
export function resolveInstallerCommand(input: {
  voiceDir: string
  platform?: NodeJS.Platform
  env?: Record<string, string | undefined>
  exists?: ExistsFn
}): InstallerResolution {
  const exists = input.exists ?? existsSync
  const platform = input.platform ?? process.platform
  if (platform === "win32") {
    const script = join(input.voiceDir, "setup.ps1")
    if (!exists(script)) {
      return {
        status: "skipped",
        problem: `the voice runtime installer was not found (${script})`,
        nextStep: "Point the 'voiceDir' plugin option at the directory that contains setup.ps1, or run the PowerShell setup by hand and restart OpenCode.",
      }
    }
    return {
      status: "ok",
      command: [findPowerShell({ env: input.env, exists }), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
    }
  }
  const script = join(input.voiceDir, "setup.sh")
  if (!exists(script)) {
    return {
      status: "skipped",
      problem: `the voice runtime installer was not found (${script})`,
      nextStep: "Point the 'voiceDir' plugin option at the directory that contains setup.sh, or run the setup by hand and restart OpenCode.",
    }
  }
  return { status: "ok", command: ["bash", script] }
}

/** Minimal child surface the installer needs (real ChildProcess fits it). */
export interface SpawnedChild {
  readonly pid?: number
  unref(): void
  on(event: "error", listener: (error: Error) => void): unknown
}

export interface InstallerSpawnOptions {
  readonly cwd: string
  readonly detached: true
  /** ["ignore", fd, fd]: the installer's output goes to the setup log. */
  readonly stdio: readonly ["ignore", number, number]
}

export type SpawnFn = (
  command: string,
  args: readonly string[],
  options: InstallerSpawnOptions,
) => SpawnedChild

/**
 * The slice of `node:child_process.spawn` the default spawn uses (injectable
 * so the real child_process boundary is testable without a process).
 */
export type NodeSpawnFn = (
  command: string,
  args: readonly string[],
  options: { cwd: string; detached: boolean; stdio: readonly unknown[] },
) => SpawnedChild

/**
 * The real spawn, built over an injectable `child_process.spawn`: argv array
 * (never a shell string), `detached: true` (own process group, survives this
 * one) and the installer's stdio descriptors passed through unchanged.
 */
export function makeDefaultSpawn(nodeSpawnFn: NodeSpawnFn): SpawnFn {
  return (command, args, options) =>
    nodeSpawnFn(command, [...args], {
      cwd: options.cwd,
      detached: options.detached,
      stdio: [...options.stdio],
    })
}

const defaultSpawn: SpawnFn = makeDefaultSpawn(nodeSpawn as unknown as NodeSpawnFn)

/** Pid liveness probe: true when the process exists (EPERM counts as alive). */
export function isProcessAlive(pid: number, kill: (pid: number, signal: 0) => void = process.kill): boolean {
  try {
    kill(pid, 0)
    return true
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM"
  }
}

export type LockResult =
  | { readonly status: "acquired"; readonly lockPath: string }
  | { readonly status: "busy"; readonly lockPath: string; readonly pid: number | null }
  | { readonly status: "failed"; readonly problem: string; readonly nextStep: string }

/** Like {@link LockResult}, but an acquisition also owns the open descriptor
 *  the record was written through (the caller closes it). */
type LockResultWithFd =
  | { readonly status: "acquired"; readonly lockPath: string; readonly fd: number }
  | { readonly status: "busy"; readonly lockPath: string; readonly pid: number | null }
  | { readonly status: "failed"; readonly problem: string; readonly nextStep: string }

/**
 * Atomically create `lockPath` holding `pid`, returning the STILL-OPEN
 * descriptor the record was written through. The create is `O_EXCL` (never
 * follows a link) and the pid goes through that same descriptor — the path is
 * never re-opened for writing, so a symlink swap cannot redirect the write.
 * Mode 0600: the file holds a pid and nothing else.
 */
function claimLockFile(lockPath: string, pid: number): number {
  const fd = openSync(lockPath, "wx", 0o600)
  try {
    writeSync(fd, String(pid))
    return fd
  } catch (error) {
    closeSync(fd)
    throw error
  }
}

/** Overwrite the pid record on the open descriptor (at position 0, trimmed). */
function overwriteLockPid(fd: number, pid: number): void {
  const record = String(pid)
  writeSync(fd, record, 0)
  ftruncateSync(fd, Buffer.byteLength(record))
}

/**
 * Open flags for `<data>/setup.log`: create + append, and `O_NOFOLLOW` where
 * the platform exposes it (POSIX) so the log is never written through a
 * symlink. Mode 0600 keeps the transcript private.
 */
function setupLogFlags(): number {
  const noFollow = (constants as unknown as Record<string, number | undefined>)["O_NOFOLLOW"]
  return constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | (noFollow ?? 0)
}

/**
 * Claim `<data>/setup.lock` for one installer run (content: the pid) and keep
 * the descriptor open for the caller's pid updates.
 *
 * The create is atomic (`wx`): two sessions racing both lose but one wins. A
 * held lock is reclaimed when its pid is gone OR it is older than `staleMs`;
 * an unparseable/empty record (a crash between create and write) is judged by
 * age alone. Reclaim re-reads the record right before unlinking and gives up
 * when it changed, so the common race (another session reclaimed first) never
 * steals a live lock — and even in the worst interleaving the setup scripts
 * are idempotent, so a duplicate run is safe.
 */
function acquireSetupLockWithFd(input: {
  dataDir: string
  pid?: number
  now?: number
  staleMs?: number
  alive?: (pid: number) => boolean
  log?: RuntimeLog
}): LockResultWithFd {
  const pid = input.pid ?? process.pid
  const now = input.now ?? Date.now()
  const staleMs = input.staleMs ?? LOCK_STALE_MS
  const alive = input.alive ?? ((candidate: number) => isProcessAlive(candidate))
  const lockPath = join(input.dataDir, RUNTIME_LOCK_FILE)

  try {
    mkdirSync(input.dataDir, { recursive: true })
    return { status: "acquired", lockPath, fd: claimLockFile(lockPath, pid) }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
      return {
        status: "failed",
        problem: `the setup lock could not be created (${lockPath}): ${error instanceof Error ? error.message : String(error)}`,
        nextStep: "Run the voice setup by hand (setup.sh on POSIX, setup.ps1 on Windows) and restart OpenCode.",
      }
    }
  }

  // Held by someone else: reclaim only when provably stale.
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let record: string | null = null
    let ageMs = Number.POSITIVE_INFINITY
    try {
      record = readFileSync(lockPath, "utf-8").trim()
      ageMs = Math.max(0, now - statSync(lockPath).mtimeMs)
    } catch {
      // Vanished (released) or unreadable: fall through to the retry below.
    }
    const holder = record !== null && /^\d+$/.test(record) ? Number(record) : null
    const stale =
      record === null
        ? false // gone or unreadable mid-flight: the retry decides
        : holder === null
          ? ageMs > staleMs // empty/corrupt record: age is the only signal
          : !alive(holder) || ageMs > staleMs
    if (record !== null && !stale) {
      return { status: "busy", lockPath, pid: holder }
    }
    try {
      // Compare before unlinking: never remove a lock someone else just wrote.
      const again = readFileSync(lockPath, "utf-8").trim()
      if (again !== record) continue
      unlinkSync(lockPath)
      input.log?.("info", `reclaimed a stale ${RUNTIME_LOCK_FILE} (pid ${holder ?? "unknown"}, age ${Math.round(ageMs / 1000)}s)`)
    } catch {
      continue
    }
    try {
      return { status: "acquired", lockPath, fd: claimLockFile(lockPath, pid) }
    } catch {
      continue // someone won the race; loop reads the new record
    }
  }
  return { status: "busy", lockPath, pid: null }
}

/** Public acquisition: same result, descriptor already closed. */
export function acquireSetupLock(input: {
  dataDir: string
  pid?: number
  now?: number
  staleMs?: number
  alive?: (pid: number) => boolean
  log?: RuntimeLog
}): LockResult {
  const result = acquireSetupLockWithFd(input)
  if (result.status === "acquired") {
    try {
      closeSync(result.fd)
    } catch {
      // the descriptor is already gone; nothing more to do
    }
  }
  return result
}

/**
 * Drop the lock again. With `pid` given the file is removed only when it still
 * holds that pid (never a concurrent session's lock); without it the file is
 * removed outright — for "the runtime is complete, whatever is left is
 * leftover". Returns whether a file was removed.
 */
export function releaseSetupLock(input: { dataDir: string; pid?: number }): boolean {
  const lockPath = join(input.dataDir, RUNTIME_LOCK_FILE)
  try {
    if (input.pid !== undefined) {
      const record = readFileSync(lockPath, "utf-8").trim()
      if (record !== String(input.pid)) return false
    }
    unlinkSync(lockPath)
    return true
  } catch {
    return false
  }
}

export type VoiceRuntimeResult =
  /** The venv and the model are both there: nothing to do (the common case). */
  | { readonly status: "ready" }
  /** The installer is running (spawned here or by a parallel session). */
  | {
      readonly status: "installing"
      /** "spawned here" vs "another session holds the lock". */
      readonly detail: string
      readonly setupLog: string
      readonly lockPath: string
    }
  /** Could not start at all: house-style payload, plugin setup continues. */
  | { readonly status: "failed"; readonly problem: string; readonly nextStep: string }

/**
 * Make sure the voice runtime is installed, without ever blocking the caller:
 * a complete runtime returns `ready` and does nothing; an incomplete one gets
 * the platform installer spawned DETACHED (new session, output to
 * `<data>/setup.log`, `unref`d) and returns `installing` at once. The lock
 * keeps parallel sessions from double-installing; a spawn failure releases it
 * and comes back as a `failed` payload.
 */
export async function ensureVoiceRuntime(input: {
  dataDir: string
  voiceDir: string
  log: RuntimeLog
  platform?: NodeJS.Platform
  env?: Record<string, string | undefined>
  spawn?: SpawnFn
  exists?: ExistsFn
  alive?: (pid: number) => boolean
  now?: number
  staleMs?: number
  pid?: number
}): Promise<VoiceRuntimeResult> {
  const { dataDir, voiceDir, log } = input
  const platform = input.platform ?? process.platform
  const exists = input.exists ?? existsSync

  const state = inspectVoiceRuntime({ dataDir, platform, exists })
  if (state.ready) {
    // Housekeeping: a finished install leaves its lock behind with a dead
    // pid. The runtime is complete, so whatever lock is there is leftover —
    // removing this file of ours keeps the data dir clean. The common path
    // costs nothing (usually the file is absent).
    releaseSetupLock({ dataDir })
    return { status: "ready" }
  }

  const installer = resolveInstallerCommand({ voiceDir, platform, env: input.env, exists })
  if (installer.status === "skipped") {
    return { status: "failed", problem: installer.problem, nextStep: installer.nextStep }
  }

  const lock = acquireSetupLockWithFd({
    dataDir,
    ...(input.pid !== undefined ? { pid: input.pid } : {}),
    ...(input.now !== undefined ? { now: input.now } : {}),
    ...(input.staleMs !== undefined ? { staleMs: input.staleMs } : {}),
    ...(input.alive !== undefined ? { alive: input.alive } : {}),
    log,
  })
  if (lock.status === "failed") {
    return { status: "failed", problem: lock.problem, nextStep: lock.nextStep }
  }
  const setupLog = join(dataDir, RUNTIME_LOG_FILE)
  if (lock.status === "busy") {
    return {
      status: "installing",
      detail: `another session holds the ${RUNTIME_LOCK_FILE}${lock.pid !== null ? ` (pid ${lock.pid})` : ""}; its setup is shared`,
      setupLog,
      lockPath: lock.lockPath,
    }
  }

  const lockFd = lock.fd
  const spawnFn = input.spawn ?? defaultSpawn
  const selfPid = input.pid ?? process.pid
  let fd: number | null = null
  try {
    mkdirSync(dataDir, { recursive: true })
    fd = openSync(setupLog, setupLogFlags(), 0o600)
    const child = spawnFn(installer.command[0] ?? "bash", installer.command.slice(1), {
      cwd: voiceDir,
      detached: true,
      stdio: ["ignore", fd, fd],
    })
    // The child owns the lock from here: its pid is what "a setup is running"
    // means for every other session (and what staleness checks for liveness).
    // Overwriting our own record through the SAME descriptor the record was
    // created with — never a re-open by path, so a symlink swap cannot
    // redirect the write; and never a release-and-reacquire, which would open
    // a window for a second session to claim it.
    const childPid = child.pid ?? selfPid
    try {
      overwriteLockPid(lockFd, childPid)
    } catch {
      // the lock still holds our own pid and staleness reclaims it all the same
    }
    child.on("error", (error) => {
      log("warn", `voice runtime setup failed to start (${error.message}); run the setup by hand (setup.sh / setup.ps1) and restart OpenCode`)
      releaseSetupLock({ dataDir, pid: childPid })
    })
    child.unref()
  } catch (error) {
    releaseSetupLock({ dataDir, pid: selfPid })
    return {
      status: "failed",
      problem: `the voice runtime setup could not be started (${error instanceof Error ? error.message : String(error)})`,
      nextStep: "Run the voice setup by hand (setup.sh on POSIX, setup.ps1 on Windows) and restart OpenCode.",
    }
  } finally {
    for (const handle of [fd, lockFd]) {
      if (handle === null) continue
      try {
        closeSync(handle)
      } catch {
        // the descriptor is already gone; nothing more to do
      }
    }
  }

  return {
    status: "installing",
    detail: `setup started in the background (missing: ${state.missing.join(", ")})`,
    setupLog,
    lockPath: lock.lockPath,
  }
}

/** Injectable timers (tests drive ticks by hand; production uses the DOM-less
 *  Node timers). */
export interface WatchTimers {
  setTimeout(handler: () => void, ms: number): unknown
  clearTimeout(handle: unknown): void
}

export interface VoiceRuntimeWatch {
  /** Stop polling. Idempotent; also runs on plugin disposal. */
  stop(): void
}

/**
 * Poll the runtime until it turns ready, then fire `onReady` ONCE (the caller
 * reloads the voice MCP — the relaunch picks up the installed runtime, so no
 * restart is needed). Cadence adapts to the install: {@link WATCH_INTERVAL_MS}
 * while a quick finish is likely ({@link WATCH_FAST_MS}), then
 * {@link WATCH_SLOW_INTERVAL_MS} until ready — a slow ~300 MB download is
 * never abandoned. The hard bound is {@link WATCH_TIMEOUT_MS} (the
 * lock-staleness horizon); on it the watcher logs EXPLICITLY that OpenCode
 * must be restarted when the setup finishes — an honest degradation, never a
 * silent one. Every failure — a throwing probe or a throwing `onReady` — is
 * contained and logged; the watcher never throws.
 */
export function watchVoiceRuntime(input: {
  dataDir: string
  log: RuntimeLog
  onReady: () => void | Promise<void>
  platform?: NodeJS.Platform
  exists?: ExistsFn
  isReady?: () => boolean
  /** Fast cadence (ms) while the install is young. */
  intervalMs?: number
  /** How long the fast cadence lasts before slowing down (ms). */
  fastMs?: number
  /** Slow cadence (ms) once the install is clearly long. */
  slowIntervalMs?: number
  /** Hard bound (ms): the watcher stops here and says a restart is needed. */
  timeoutMs?: number
  timers?: WatchTimers
}): VoiceRuntimeWatch {
  const intervalMs = input.intervalMs ?? WATCH_INTERVAL_MS
  const fastMs = input.fastMs ?? WATCH_FAST_MS
  const slowIntervalMs = input.slowIntervalMs ?? WATCH_SLOW_INTERVAL_MS
  const timeoutMs = input.timeoutMs ?? WATCH_TIMEOUT_MS
  const timers = input.timers ?? {
    setTimeout: (handler: () => void, ms: number) => setTimeout(handler, ms),
    clearTimeout: (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  }
  const ready = input.isReady ?? (() => isVoiceRuntimeReady({
    dataDir: input.dataDir,
    ...(input.platform !== undefined ? { platform: input.platform } : {}),
    ...(input.exists !== undefined ? { exists: input.exists } : {}),
  }))

  const started = Date.now()
  let handle: unknown = null
  let stopped = false

  const stop = (): void => {
    if (stopped) return
    stopped = true
    if (handle !== null) timers.clearTimeout(handle)
    handle = null
  }

  const tick = (): void => {
    handle = null
    if (stopped) return
    try {
      if (ready()) {
        stop()
        input.log("info", "voice runtime ready (venv and model complete)")
        try {
          void Promise.resolve(input.onReady()).catch((error) => {
            input.log("warn", `voice runtime ready but the voice MCP reload failed (${error instanceof Error ? error.message : String(error)})`)
          })
        } catch (error) {
          input.log("warn", `voice runtime ready but the voice MCP reload failed (${error instanceof Error ? error.message : String(error)})`)
        }
        return
      }
    } catch (error) {
      input.log("warn", `voice runtime readiness check failed (${error instanceof Error ? error.message : String(error)})`)
    }
    const elapsed = Date.now() - started
    if (elapsed >= timeoutMs) {
      stop()
      // The honest degradation D2 asks for: say so, never degrade silently.
      input.log("warn", `voice runtime still installing — restart OpenCode when it finishes (watched ${Math.round(elapsed / 60000)} minutes; progress in ${RUNTIME_LOG_FILE} in the Sebas data dir, or run the setup by hand: setup.sh / setup.ps1)`)
      return
    }
    // Young install: fast cadence. A slow one (long download, slow pip) keeps
    // being watched at the slow cadence until ready or the hard bound.
    handle = timers.setTimeout(tick, elapsed >= fastMs ? slowIntervalMs : intervalMs)
  }

  handle = timers.setTimeout(tick, intervalMs)
  return { stop }
}
