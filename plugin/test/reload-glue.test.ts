/**
 * The completion-watcher → `ctx.mcp.reload()` glue, exactly as the plugin
 * entry point wires it (index.ts): a mock `ctx` whose `mcp.reload()` relaunches
 * the voice MCP, the real watcher underneath, and the entry point's dispose
 * semantics (`disposed` flag + `watch.stop()`). What is pinned: turning ready
 * triggers EXACTLY ONE reload — never one per tick — and after disposal there
 * are ZERO reloads, even if readiness appears afterwards. Deterministic
 * timers: no test waits on real time, and no test touches a real data dir.
 */
import { afterAll, describe, expect, mock, test } from "bun:test"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { watchVoiceRuntime, type VoiceRuntimeWatch, type WatchTimers } from "../src/runtime"

const tmpRoots: string[] = []

function makeRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "sebas-reload-glue-"))
  tmpRoots.push(root)
  return root
}

afterAll(() => {
  for (const root of tmpRoots) rmSync(root, { recursive: true, force: true })
})

/** Deterministic timers: the test decides when each tick fires. */
function makeTimers(): { timers: WatchTimers; tick: () => void; pending: () => number } {
  const queue: Array<{ id: number; handler: () => void }> = []
  let nextId = 1
  return {
    timers: {
      setTimeout(handler: () => void): unknown {
        const id = nextId
        nextId += 1
        queue.push({ id, handler })
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
  }
}

/** The entry point's wiring, reproduced around the real watcher. */
function wireGlue(input: { timers: WatchTimers }): {
  ctx: { mcp: { reload: ReturnType<typeof mock> } }
  dispose: () => Promise<void>
  setReady: (value: boolean) => void
} {
  const ctx = { mcp: { reload: mock(async () => {}) } }
  const disposals: Array<() => void | Promise<void>> = []
  let disposed = false
  let ready = false

  const watch: VoiceRuntimeWatch = watchVoiceRuntime({
    dataDir: makeRoot(), // never probed: isReady is injected below
    log: () => {},
    isReady: () => ready,
    onReady: async () => {
      await ctx.mcp.reload()
    },
    timers: input.timers,
    intervalMs: 5,
    timeoutMs: 60_000,
  })
  // index.ts, verbatim semantics: a watcher created after disposal is stopped
  // at once; otherwise it is stopped by the disposal itself.
  if (disposed) watch.stop()
  else disposals.push(async () => {
    watch.stop()
  })

  return {
    ctx,
    dispose: async () => {
      disposed = true
      await Promise.all(disposals.map((dispose) => dispose()))
    },
    setReady: (value: boolean) => {
      ready = value
    },
  }
}

describe("watcher → ctx.mcp.reload() glue", () => {
  test("turning ready reloads EXACTLY once", async () => {
    const { timers, tick } = makeTimers()
    const glue = wireGlue({ timers })
    tick() // still installing
    expect(glue.ctx.mcp.reload).toHaveBeenCalledTimes(0)
    glue.setReady(true)
    tick()
    tick() // the watcher stopped after firing: a second tick can never land
    await Promise.resolve()
    await Promise.resolve()
    expect(glue.ctx.mcp.reload).toHaveBeenCalledTimes(1)
  })

  test("after dispose there are ZERO reloads — even when the runtime turns ready", async () => {
    const { timers, tick, pending } = makeTimers()
    const glue = wireGlue({ timers })
    await glue.dispose()
    expect(pending()).toBe(0) // the pending tick was cancelled
    glue.setReady(true)
    tick() // nothing scheduled: nothing can fire
    await Promise.resolve()
    expect(glue.ctx.mcp.reload).toHaveBeenCalledTimes(0)
  })

  test("dispose after the reload keeps the count at one", async () => {
    const { timers, tick } = makeTimers()
    const glue = wireGlue({ timers })
    glue.setReady(true)
    tick()
    await Promise.resolve()
    await Promise.resolve()
    expect(glue.ctx.mcp.reload).toHaveBeenCalledTimes(1)
    await glue.dispose()
    tick()
    await Promise.resolve()
    expect(glue.ctx.mcp.reload).toHaveBeenCalledTimes(1)
  })
})
