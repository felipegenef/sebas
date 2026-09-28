/**
 * Unit tests for the automatic first-run voice runtime setup.
 *
 * Everything is offline by construction: no test downloads anything, no test
 * spawns a real installer — `spawn` is a recording mock, filesystem probes are
 * injectable, and the lock tests run against throwaway temp dirs. What is
 * pinned here: readiness detection per platform, the exact installer command
 * per platform (including the powershell/pwsh choice), lock acquisition and
 * stale reclaim, the `ctx.mcp.reload()` completion path, and containment of
 * every installer failure.
 */
import { afterAll, describe, expect, mock, test } from "bun:test"
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { delimiter, join } from "node:path"
import {
  acquireSetupLock,
  ensureVoiceRuntime,
  findPowerShell,
  inspectVoiceRuntime,
  isVoiceRuntimeReady,
  LOCK_STALE_MS,
  makeDefaultSpawn,
  releaseSetupLock,
  resolveInstallerCommand,
  RUNTIME_LOCK_FILE,
  RUNTIME_LOG_FILE,
  voiceRuntimePaths,
  WATCH_FAST_MS,
  WATCH_INTERVAL_MS,
  WATCH_SLOW_INTERVAL_MS,
  WATCH_TIMEOUT_MS,
  watchVoiceRuntime,
  type InstallerSpawnOptions,
  type NodeSpawnFn,
  type SpawnedChild,
  type WatchTimers,
} from "../src/runtime"

const tmpRoots: string[] = []

function makeRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "sebas-runtime-"))
  tmpRoots.push(root)
  return root
}

function touch(path: string): void {
  mkdirSync(join(path, ".."), { recursive: true })
  writeFileSync(path, "")
}

/** Creates the pieces the installers would leave behind (empty stand-ins). */
function installRuntime(dataDir: string, platform: NodeJS.Platform): void {
  const paths = voiceRuntimePaths(dataDir, platform)
  touch(paths.venvPython)
  touch(paths.onnx)
  touch(paths.voices)
}

function makeVoiceDir(root: string, platform: NodeJS.Platform): string {
  const voiceDir = join(root, "voice")
  mkdirSync(voiceDir, { recursive: true })
  touch(join(voiceDir, platform === "win32" ? "setup.ps1" : "setup.sh"))
  touch(join(voiceDir, platform === "win32" ? "setup.sh" : "setup.ps1"))
  return voiceDir
}

/**
 * The "no group/other bits" (0600) contract is POSIX-only: Windows chmod
 * carries just the read-only bit, so `mode` cannot express it there (the
 * Python suite skips the same way — "POSIX file modes only (Windows chmod
 * carries just the read-only bit)"). On win32 the file must exist as a
 * regular file (statSync throws when it does not); on POSIX the mode is
 * pinned exactly. Never weaker on POSIX.
 */
function expectPrivateFile(path: string): void {
  const stat = statSync(path)
  if (process.platform === "win32") {
    expect(stat.isFile()).toBe(true)
    return
  }
  expect(stat.mode & 0o077).toBe(0)
}

interface SpawnCall {
  command: string
  args: readonly string[]
  options: InstallerSpawnOptions
}

/** Recording spawn mock plus the fake child it hands back. Never a process.
 *  `onSpawn` runs inside the call (between record and return) so a test can
 *  simulate an attacker interleaving with the spawn itself. */
function makeSpawn(overrides: Partial<SpawnedChild> & { pid?: number; throwError?: Error; onSpawn?: () => void } = {}) {
  const calls: SpawnCall[] = []
  const listeners: Array<(error: Error) => void> = []
  const unrefCalls: number[] = []
  const spawn = (command: string, args: readonly string[], options: InstallerSpawnOptions): SpawnedChild => {
    if (overrides.throwError !== undefined) throw overrides.throwError
    calls.push({ command, args, options })
    overrides.onSpawn?.()
    const child: SpawnedChild = {
      pid: overrides.pid ?? 4242,
      unref: () => {
        unrefCalls.push(1)
      },
      on: (_event, listener) => {
        listeners.push(listener)
        return child
      },
    }
    return child
  }
  return {
    spawn,
    calls,
    unrefCalls,
    emitError(error: Error): void {
      for (const listener of listeners) listener(error)
    },
  }
}

/** Deterministic timers: the test decides when each tick fires and sees the
 *  delay every tick was scheduled with. */
function makeTimers(): {
  timers: WatchTimers
  tick: () => void
  pending: () => number
  delays: () => number[]
} {
  const queue: Array<{ id: number; handler: () => void }> = []
  const scheduled: number[] = []
  let nextId = 1
  return {
    timers: {
      setTimeout(handler: () => void, ms: number): unknown {
        const id = nextId
        nextId += 1
        queue.push({ id, handler })
        scheduled.push(ms)
        return id
      },
      clearTimeout(handle: unknown): void {
        const index = queue.findIndex((entry) => entry.id === handle)
        if (index >= 0) queue.splice(index, 1)
      },
    },
    tick(): void {
      const next = queue.shift()
      if (next !== undefined) next.handler()
    },
    pending(): number {
      return queue.length
    },
    delays(): number[] {
      return [...scheduled]
    },
  }
}

const quietLog = (): void => {}

afterAll(() => {
  for (const root of tmpRoots) rmSync(root, { recursive: true, force: true })
})

describe("runtime detection", () => {
  test("POSIX: ready only with venv/bin/python and BOTH model files", () => {
    const dataDir = makeRoot()
    installRuntime(dataDir, "linux")
    expect(inspectVoiceRuntime({ dataDir, platform: "linux" })).toEqual({ ready: true, missing: [] })
    expect(isVoiceRuntimeReady({ dataDir, platform: "linux" })).toBe(true)

    rmSync(voiceRuntimePaths(dataDir, "linux").onnx)
    expect(inspectVoiceRuntime({ dataDir, platform: "linux" })).toEqual({
      ready: false,
      missing: ["kokoro-v1.0.onnx"],
    })
    expect(isVoiceRuntimeReady({ dataDir, platform: "linux" })).toBe(false)
  })

  test("macOS behaves exactly like Linux (same POSIX layout)", () => {
    const dataDir = makeRoot()
    installRuntime(dataDir, "darwin")
    expect(inspectVoiceRuntime({ dataDir, platform: "darwin" }).ready).toBe(true)
    rmSync(voiceRuntimePaths(dataDir, "darwin").venvPython)
    expect(inspectVoiceRuntime({ dataDir, platform: "darwin" })).toEqual({
      ready: false,
      missing: ["venv"],
    })
  })

  test("win32 probes venv/Scripts/python.exe — never the POSIX interpreter", () => {
    const dataDir = makeRoot()
    const probed: string[] = []
    inspectVoiceRuntime({
      dataDir,
      platform: "win32",
      exists: (path) => {
        probed.push(path)
        return false
      },
    })
    expect(probed).toEqual([
      join(dataDir, "venv", "Scripts", "python.exe"),
      join(dataDir, "models", "kokoro", "kokoro-v1.0.onnx"),
      join(dataDir, "models", "kokoro", "voices-v1.0.bin"),
    ])
    expect(probed).not.toContain(join(dataDir, "venv", "bin", "python"))
  })

  test("fresh machine reports every missing piece at once", () => {
    const dataDir = makeRoot()
    expect(inspectVoiceRuntime({ dataDir, platform: "linux" })).toEqual({
      ready: false,
      missing: ["venv", "kokoro-v1.0.onnx", "voices-v1.0.bin"],
    })
  })

  test("voices-v1.0.bin alone being missing counts as incomplete", () => {
    const dataDir = makeRoot()
    installRuntime(dataDir, "win32")
    rmSync(voiceRuntimePaths(dataDir, "win32").voices)
    expect(inspectVoiceRuntime({ dataDir, platform: "win32" })).toEqual({
      ready: false,
      missing: ["voices-v1.0.bin"],
    })
  })
})

describe("installer command per platform", () => {
  test("POSIX: bash <voiceDir>/setup.sh", () => {
    const root = makeRoot()
    const voiceDir = makeVoiceDir(root, "linux")
    expect(resolveInstallerCommand({ voiceDir, platform: "linux" })).toEqual({
      status: "ok",
      command: ["bash", join(voiceDir, "setup.sh")],
    })
  })

  test("win32: powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1", () => {
    const root = makeRoot()
    const voiceDir = makeVoiceDir(root, "win32")
    expect(
      resolveInstallerCommand({
        voiceDir,
        platform: "win32",
        env: { PATH: "/usr/bin" },
        exists: (path) => path === join(voiceDir, "setup.ps1"),
      }),
    ).toEqual({
      status: "ok",
      command: ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", join(voiceDir, "setup.ps1")],
    })
  })

  test("win32 prefers pwsh when PowerShell 7 is on PATH", () => {
    const root = makeRoot()
    const voiceDir = makeVoiceDir(root, "win32")
    const result = resolveInstallerCommand({
      voiceDir,
      platform: "win32",
      env: { Path: join(root, "bin") },
      exists: (path) => path === join(voiceDir, "setup.ps1") || path === join(root, "bin", "pwsh.exe"),
    })
    expect(result.status).toBe("ok")
    if (result.status !== "ok") throw new Error("unreachable")
    expect(result.command[0]).toBe("pwsh")
    expect(result.command.slice(1)).toEqual([
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      join(voiceDir, "setup.ps1"),
    ])
  })

  test("findPowerShell scans every PATH entry and falls back to powershell", () => {
    const exists = (path: string): boolean => path === join("d", "pwsh")
    // PATH is built with the platform delimiter — the exact one findPowerShell
    // splits by — so the scan contract is pinned on win32 (";") as on POSIX (":").
    expect(findPowerShell({ env: { PATH: ["a", "b", "d"].join(delimiter) }, exists })).toBe("pwsh")
    expect(findPowerShell({ env: { PATH: ["a", "b", "c"].join(delimiter) }, exists })).toBe("powershell")
    expect(findPowerShell({ env: { PATH: "" }, exists })).toBe("powershell")
  })

  test("a missing installer is a payload with next_step — never a spawn", () => {
    const root = makeRoot()
    const voiceDir = join(root, "nowhere")
    mkdirSync(voiceDir, { recursive: true })
    for (const platform of ["linux", "win32"] as const) {
      const result = resolveInstallerCommand({ voiceDir, platform, exists: () => false })
      expect(result.status).toBe("skipped")
      if (result.status !== "skipped") throw new Error("unreachable")
      expect(result.problem).toContain("installer was not found")
      expect(result.nextStep).toContain("by hand")
    }
  })
})

describe("setup lock", () => {
  test("acquisition writes the pid; a live holder is never stolen", () => {
    const dataDir = makeRoot()
    const first = acquireSetupLock({ dataDir, pid: 111, alive: () => true })
    expect(first.status).toBe("acquired")
    expect(readFileSync(join(dataDir, RUNTIME_LOCK_FILE), "utf-8")).toBe("111")

    const second = acquireSetupLock({ dataDir, pid: 222, alive: () => true })
    expect(second.status).toBe("busy")
    if (second.status !== "busy") throw new Error("unreachable")
    expect(second.pid).toBe(111)
    expect(readFileSync(join(dataDir, RUNTIME_LOCK_FILE), "utf-8")).toBe("111")
  })

  test("a dead pid is stale and reclaimed", () => {
    const dataDir = makeRoot()
    acquireSetupLock({ dataDir, pid: 111, alive: () => true })
    const reclaimed = acquireSetupLock({ dataDir, pid: 222, alive: () => false })
    expect(reclaimed.status).toBe("acquired")
    expect(readFileSync(join(dataDir, RUNTIME_LOCK_FILE), "utf-8")).toBe("222")
  })

  test("a very old lock is reclaimed even when its pid still looks alive", () => {
    const dataDir = makeRoot()
    acquireSetupLock({ dataDir, pid: 111, alive: () => true })
    const reclaimed = acquireSetupLock({
      dataDir,
      pid: 222,
      now: Date.now() + LOCK_STALE_MS + 1,
      alive: () => true,
    })
    expect(reclaimed.status).toBe("acquired")
    expect(readFileSync(join(dataDir, RUNTIME_LOCK_FILE), "utf-8")).toBe("222")
  })

  test("an empty/corrupt record is judged by age alone", () => {
    const dataDir = makeRoot()
    acquireSetupLock({ dataDir, pid: 111, alive: () => true })
    writeFileSync(join(dataDir, RUNTIME_LOCK_FILE), "")
    const fresh = acquireSetupLock({ dataDir, pid: 222, now: Date.now(), alive: () => true })
    expect(fresh.status).toBe("busy")
    const old = acquireSetupLock({
      dataDir,
      pid: 333,
      now: Date.now() + LOCK_STALE_MS + 1,
      alive: () => true,
    })
    expect(old.status).toBe("acquired")
  })

  test("release removes only its own lock (pid compare)", () => {
    const dataDir = makeRoot()
    acquireSetupLock({ dataDir, pid: 111, alive: () => true })
    expect(releaseSetupLock({ dataDir, pid: 222 })).toBe(false)
    expect(readFileSync(join(dataDir, RUNTIME_LOCK_FILE), "utf-8")).toBe("111")
    expect(releaseSetupLock({ dataDir, pid: 111 })).toBe(true)
    expect(releaseSetupLock({ dataDir, pid: 111 })).toBe(false) // already gone
  })
})

describe("ensureVoiceRuntime", () => {
  test("complete runtime: ready, NOT a single spawn, leftover lock cleaned", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "linux")
    installRuntime(dataDir, "linux")
    acquireSetupLock({ dataDir, pid: 999, alive: () => false }) // leftover
    const spy = makeSpawn()
    const result = await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "linux",
      spawn: spy.spawn,
    })
    expect(result).toEqual({ status: "ready" })
    expect(spy.calls).toEqual([])
    expect(releaseSetupLock({ dataDir })).toBe(false) // already cleaned
  })

  test("POSIX install: spawns bash setup.sh detached with output to setup.log", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "linux")
    const spy = makeSpawn({ pid: 4242 })
    const result = await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "linux",
      spawn: spy.spawn,
      pid: 111,
    })
    expect(result.status).toBe("installing")
    if (result.status !== "installing") throw new Error("unreachable")
    expect(result.detail).toContain("venv")
    expect(result.setupLog).toBe(join(dataDir, RUNTIME_LOG_FILE))
    expect(result.lockPath).toBe(join(dataDir, RUNTIME_LOCK_FILE))

    expect(spy.calls.length).toBe(1)
    const call = spy.calls[0]
    if (call === undefined) throw new Error("unreachable")
    expect(call.command).toBe("bash")
    expect(call.args).toEqual([join(voiceDir, "setup.sh")])
    expect(call.options.detached).toBe(true)
    expect(call.options.cwd).toBe(voiceDir)
    expect(call.options.stdio[0]).toBe("ignore")
    expect(call.options.stdio[1]).toBe(call.options.stdio[2])
    expect(spy.unrefCalls.length).toBe(1)
    // The lock now names the INSTALLER's pid, so other sessions probe it.
    expect(readFileSync(join(dataDir, RUNTIME_LOCK_FILE), "utf-8")).toBe("4242")
  })

  test("win32 install: spawns the PowerShell installer", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "win32")
    const spy = makeSpawn()
    await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "win32",
      env: { PATH: "/usr/bin" },
      spawn: spy.spawn,
      exists: (path) => path === join(voiceDir, "setup.ps1"),
      pid: 111,
    })
    const call = spy.calls[0]
    if (call === undefined) throw new Error("unreachable")
    expect(call.command).toBe("powershell")
    expect(call.args).toEqual([
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      join(voiceDir, "setup.ps1"),
    ])
  })

  test("a parallel session holds the lock: installing, and nothing is spawned", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "linux")
    acquireSetupLock({ dataDir, pid: 999, alive: () => true })
    const spy = makeSpawn()
    const result = await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "linux",
      spawn: spy.spawn,
      pid: 111,
      alive: () => true,
    })
    expect(result.status).toBe("installing")
    if (result.status !== "installing") throw new Error("unreachable")
    expect(result.detail).toContain("another session")
    expect(spy.calls).toEqual([])
  })

  test("installer failure is contained: failed payload, lock released", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "linux")
    const spy = makeSpawn({ throwError: new Error("ENOENT bash") })
    const result = await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "linux",
      spawn: spy.spawn,
      pid: 111,
    })
    expect(result.status).toBe("failed")
    if (result.status !== "failed") throw new Error("unreachable")
    expect(result.problem).toContain("ENOENT bash")
    expect(result.nextStep).toContain("by hand")
    // The lock was released: a later attempt can claim it.
    expect(acquireSetupLock({ dataDir, pid: 222, alive: () => true }).status).toBe("acquired")
  })

  test("an async spawn error is logged and releases the lock, never throws", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "linux")
    const spy = makeSpawn({ pid: 4242 })
    const lines: string[] = []
    await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: (_level, message) => {
        lines.push(message)
      },
      platform: "linux",
      spawn: spy.spawn,
      pid: 111,
    })
    spy.emitError(new Error("spawn bash ENOENT"))
    expect(lines.some((line) => line.includes("failed to start"))).toBe(true)
    expect(releaseSetupLock({ dataDir, pid: 4242 })).toBe(false) // already released
  })

  test("a missing installer never creates a lock or a log", async () => {
    const dataDir = makeRoot()
    const voiceDir = join(dataDir, "voice")
    mkdirSync(voiceDir, { recursive: true })
    const spy = makeSpawn()
    const result = await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "linux",
      spawn: spy.spawn,
      exists: (path) => !path.endsWith("setup.sh") && !path.endsWith("python") && !path.endsWith(".onnx") && !path.endsWith(".bin"),
    })
    expect(result.status).toBe("failed")
    expect(spy.calls).toEqual([])
    expect(acquireSetupLock({ dataDir, pid: 5, alive: () => true }).status).toBe("acquired")
  })
})

describe("watchVoiceRuntime", () => {
  test("turning ready fires onReady once — this is where ctx.mcp.reload() lives", async () => {
    const dataDir = makeRoot()
    const { timers, tick, pending } = makeTimers()
    let ready = false
    let reloaded = 0
    // The exact production wiring: onReady awaits ctx.mcp.reload().
    const ctx = {
      mcp: {
        reload: mock(async () => {
          reloaded += 1
        }),
      },
    }
    const watch = watchVoiceRuntime({
      dataDir,
      log: quietLog,
      isReady: () => ready,
      onReady: async () => {
        await ctx.mcp.reload()
      },
      timers,
      intervalMs: 5,
      timeoutMs: 60_000,
    })

    tick() // still installing
    expect(reloaded).toBe(0)
    ready = true
    tick()
    await Promise.resolve()
    expect(reloaded).toBe(1)
    expect(ctx.mcp.reload).toHaveBeenCalledTimes(1)
    expect(pending()).toBe(0) // stopped after firing: no second reload
    watch.stop() // idempotent
  })

  test("never ready: bounded polling gives up with a warn and stops", () => {
    const dataDir = makeRoot()
    const { timers, tick, pending } = makeTimers()
    const lines: Array<{ level: string; message: string }> = []
    const watch = watchVoiceRuntime({
      dataDir,
      log: (level, message) => {
        lines.push({ level, message })
      },
      isReady: () => false,
      onReady: () => {
        throw new Error("must not fire")
      },
      timers,
      intervalMs: 5,
      timeoutMs: 0, // exhausted on the first tick
    })
    tick()
    expect(pending()).toBe(0)
    expect(lines.some((line) => line.level === "warn" && line.message.includes("setup.log"))).toBe(true)
    watch.stop()
  })

  test("still installing before the bound: keeps polling without firing", () => {
    const dataDir = makeRoot()
    const { timers, tick, pending } = makeTimers()
    let fired = 0
    watchVoiceRuntime({
      dataDir,
      log: quietLog,
      isReady: () => false,
      onReady: () => {
        fired += 1
      },
      timers,
      intervalMs: 5,
      timeoutMs: 60_000,
    })
    tick()
    tick()
    expect(fired).toBe(0)
    expect(pending()).toBe(1) // still scheduled
  })

  test("a throwing or rejecting onReady is contained and logged", async () => {
    const dataDir = makeRoot()
    const lines: string[] = []
    const log = (_level: "info" | "warn", message: string): void => {
      lines.push(message)
    }
    const syncTimers = makeTimers()

    watchVoiceRuntime({
      dataDir,
      log,
      isReady: () => true,
      onReady: () => {
        throw new Error("reload exploded")
      },
      timers: syncTimers.timers,
    })
    syncTimers.tick()
    expect(lines.some((line) => line.includes("reload failed"))).toBe(true)

    const asyncTimers = makeTimers()
    watchVoiceRuntime({
      dataDir,
      log,
      isReady: () => true,
      onReady: () => Promise.reject(new Error("reload rejected")),
      timers: asyncTimers.timers,
    })
    asyncTimers.tick()
    await Promise.resolve()
    await Promise.resolve()
    expect(lines.filter((line) => line.includes("reload failed")).length).toBe(2)
  })

  test("stop() cancels the pending tick", () => {
    const { timers, pending } = makeTimers()
    const watch = watchVoiceRuntime({
      dataDir: makeRoot(),
      log: quietLog,
      isReady: () => true,
      onReady: () => {},
      timers,
    })
    expect(pending()).toBe(1)
    watch.stop()
    expect(pending()).toBe(0)
  })
})

describe("watchVoiceRuntime: cadence and bounds", () => {
  test("a slow install drops to the slow cadence and is still watched to completion", () => {
    const dataDir = makeRoot()
    const { timers, tick, pending, delays } = makeTimers()
    let ready = false
    let fired = 0
    watchVoiceRuntime({
      dataDir,
      log: quietLog,
      isReady: () => ready,
      onReady: () => {
        fired += 1
      },
      timers,
      intervalMs: 5,
      fastMs: 0, // the fast window is over from the start
      slowIntervalMs: 45_000,
      timeoutMs: 2 * 60 * 60 * 1000,
    })
    tick() // not ready: the NEXT check is scheduled at the slow cadence
    expect(delays()).toEqual([5, 45_000])
    expect(pending()).toBe(1) // still watched — a slow download is never abandoned
    ready = true
    tick()
    expect(fired).toBe(1) // slow polling still completes without a restart
  })

  test("inside the fast window the fast cadence stays", () => {
    const { timers, tick, delays } = makeTimers()
    watchVoiceRuntime({
      dataDir: makeRoot(),
      log: quietLog,
      isReady: () => false,
      onReady: () => {},
      timers,
      intervalMs: 5,
      fastMs: 60_000,
      slowIntervalMs: 45_000,
      timeoutMs: 2 * 60 * 60 * 1000,
    })
    tick()
    expect(delays()).toEqual([5, 5])
  })

  test("the hard bound logs the restart degradation explicitly — never silent", () => {
    const { timers, tick, pending } = makeTimers()
    const lines: Array<{ level: string; message: string }> = []
    watchVoiceRuntime({
      dataDir: makeRoot(),
      log: (level, message) => {
        lines.push({ level, message })
      },
      isReady: () => false,
      onReady: () => {
        throw new Error("must not fire")
      },
      timers,
      intervalMs: 5,
      timeoutMs: 0, // exhausted on the first tick
    })
    tick()
    expect(pending()).toBe(0)
    const warn = lines.find((line) => line.level === "warn")
    if (warn === undefined) throw new Error("the bound must warn")
    expect(warn.message).toContain("restart OpenCode when it finishes")
    expect(warn.message).toContain("setup.log")
  })

  test("constants: the hard bound IS the lock-staleness horizon; slow cadence is 30–60 s", () => {
    expect(WATCH_TIMEOUT_MS).toBe(LOCK_STALE_MS) // 2 h, one horizon
    expect(WATCH_SLOW_INTERVAL_MS).toBeGreaterThanOrEqual(30_000)
    expect(WATCH_SLOW_INTERVAL_MS).toBeLessThanOrEqual(60_000)
    expect(WATCH_INTERVAL_MS).toBeLessThan(WATCH_FAST_MS) // fast first, then slow
  })
})

describe("defaultSpawn — the real child_process boundary", () => {
  function makeNodeSpawn(): {
    nodeSpawn: NodeSpawnFn
    calls: Array<{ command: string; args: readonly string[]; options: { cwd: string; detached: boolean; stdio: readonly unknown[] } }>
    child: SpawnedChild
    unrefCalls: number[]
  } {
    const calls: Array<{ command: string; args: readonly string[]; options: { cwd: string; detached: boolean; stdio: readonly unknown[] } }> = []
    const unrefCalls: number[] = []
    const child: SpawnedChild = {
      pid: 7,
      unref: () => {
        unrefCalls.push(1)
      },
      on: () => child,
    }
    const nodeSpawn: NodeSpawnFn = (command, args, options) => {
      calls.push({ command, args: [...args], options: { ...options, stdio: [...options.stdio] } })
      return child
    }
    return { nodeSpawn, calls, child, unrefCalls }
  }

  test("argv array (never a shell string), detached: true, stdio descriptors through", () => {
    const { nodeSpawn, calls, child } = makeNodeSpawn()
    const returned = makeDefaultSpawn(nodeSpawn)("bash", ["setup.sh"], {
      cwd: "/voice",
      detached: true,
      stdio: ["ignore", 3, 3],
    })
    expect(returned).toBe(child) // unref() will reach the REAL child
    expect(calls.length).toBe(1)
    const call = calls[0]
    if (call === undefined) throw new Error("unreachable")
    expect(call.command).toBe("bash")
    expect(Array.isArray(call.args)).toBe(true) // argv array, no shell
    expect(call.args).toEqual(["setup.sh"])
    expect("shell" in call.options).toBe(false) // no shell option, ever
    expect(call.options.detached).toBe(true) // a mutation detached:true→false dies here
    expect(call.options.cwd).toBe("/voice")
    expect(call.options.stdio).toEqual(["ignore", 3, 3])
  })

  test("ensureVoiceRuntime through the default spawn: log fd stdio, then unref()", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "linux")
    const { nodeSpawn, calls, unrefCalls } = makeNodeSpawn()
    const result = await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "linux",
      spawn: makeDefaultSpawn(nodeSpawn),
      pid: 111,
    })
    expect(result.status).toBe("installing")
    expect(unrefCalls.length).toBe(1) // detached work must not hold the process
    const call = calls[0]
    if (call === undefined) throw new Error("unreachable")
    expect(call.options.detached).toBe(true)
    expect(call.options.stdio[0]).toBe("ignore")
    expect(typeof call.options.stdio[1]).toBe("number") // the setup.log descriptor
    expect(call.options.stdio[1]).toBe(call.options.stdio[2])
  })
})

describe("symlink hardening and file modes", () => {
  test("the child-pid update goes through the wx descriptor — a symlink swap cannot redirect it", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "linux")
    const victim = join(makeRoot(), "victim")
    writeFileSync(victim, "precious")
    const spy = makeSpawn({
      pid: 4242,
      onSpawn: () => {
        // The attacker swaps setup.lock for a symlink pointing at a victim
        // file in the window between lock creation and the pid update.
        const lockPath = join(dataDir, RUNTIME_LOCK_FILE)
        unlinkSync(lockPath)
        symlinkSync(victim, lockPath)
      },
    })
    const result = await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "linux",
      spawn: spy.spawn,
      pid: 111,
    })
    expect(result.status).toBe("installing")
    // The record was updated through the descriptor the lock was CREATED with:
    // the path is never re-opened, so the victim file is untouched.
    expect(readFileSync(victim, "utf-8")).toBe("precious")
  })

  test.if(process.platform !== "win32")("setup.log is opened O_NOFOLLOW: a symlinked log is refused, never written", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "linux")
    const victim = join(makeRoot(), "victim-log")
    writeFileSync(victim, "precious")
    symlinkSync(victim, join(dataDir, RUNTIME_LOG_FILE))
    const spy = makeSpawn()
    const result = await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "linux",
      spawn: spy.spawn,
      pid: 111,
    })
    expect(result.status).toBe("failed") // contained: refuse, never write through
    expect(readFileSync(victim, "utf-8")).toBe("precious")
    expect(spy.calls).toEqual([]) // nothing was spawned through the link
  })

  test("setup.lock and setup.log are created without group/other bits (0600)", async () => {
    const dataDir = makeRoot()
    const voiceDir = makeVoiceDir(dataDir, "linux")
    const spy = makeSpawn()
    await ensureVoiceRuntime({
      dataDir,
      voiceDir,
      log: quietLog,
      platform: "linux",
      spawn: spy.spawn,
      pid: 111,
    })
    for (const name of [RUNTIME_LOCK_FILE, RUNTIME_LOG_FILE]) {
      expectPrivateFile(join(dataDir, name))
    }
  })

  test("acquisition claims the lock 0600 as well", () => {
    const dataDir = makeRoot()
    const lock = acquireSetupLock({ dataDir, pid: 111, alive: () => true })
    expect(lock.status).toBe("acquired")
    expectPrivateFile(join(dataDir, RUNTIME_LOCK_FILE))
  })
})
