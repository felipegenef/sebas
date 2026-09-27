/** Unit tests for the voice MCP server config the plugin registers and for the
 *  wiring of an already user-configured voice server (the merge-never-clobber
 *  contract). */
import { describe, expect, test } from "bun:test"
import type { Mcp } from "@opencode/plugin"
import type { MCPEditor } from "@opencode/plugin/promise/mcp"
import {
  applyVoiceTransform,
  buildVoiceServerConfig,
  NOTIFY_ENV,
  VOICE_MCP_NAME,
  wireVoiceServer,
  type VoiceWireInput,
} from "../src/mcp"

/** The mutable config shape `MCPEditor.update` hands its callback. */
type MutableServerConfig = Parameters<Parameters<MCPEditor["update"]>[1]>[0]

/**
 * In-memory stand-in for the session transform editor. Entries are seeded as
 * raw JSON (any key allowed, like a user's opencode.json) and the call log
 * records which editor methods production code touched — `set` replaces the
 * entry, `update` edits it in place, so the log pins "never clobber".
 */
class FakeEditor {
  private readonly entries = new Map<string, MutableServerConfig>()
  readonly calls: string[] = []

  seed(name: string, config: Record<string, unknown>): void {
    this.entries.set(name, config as MutableServerConfig)
  }

  list(): [string, MutableServerConfig][] {
    return [...this.entries]
  }

  get(name: string): MutableServerConfig | undefined {
    return this.entries.get(name)
  }

  set(name: string, config: Mcp.ServerConfig): void {
    this.calls.push(`set:${name}`)
    this.entries.set(name, config as MutableServerConfig)
  }

  update(name: string, update: (config: MutableServerConfig) => void): void {
    this.calls.push(`update:${name}`)
    const config = this.entries.get(name)
    if (config !== undefined) update(config)
  }

  remove(name: string): void {
    this.calls.push(`remove:${name}`)
    this.entries.delete(name)
  }
}

describe("buildVoiceServerConfig", () => {
  test("registers a local stdio server with the resolved launch recipe", () => {
    const config = buildVoiceServerConfig({
      launch: { command: ["bash", "run.sh"], cwd: "/somewhere/voice" },
      notifyPath: "/somewhere/cards",
      protocol: "legacy",
      codemode: true,
    })
    expect(config.type).toBe("local")
    if (config.type !== "local") throw new Error("unreachable")
    expect(config.command).toEqual(["bash", "run.sh"])
    expect(config.cwd).toBe("/somewhere/voice")
    expect(config.environment).toEqual({ [NOTIFY_ENV]: "/somewhere/cards" })
    expect(config.protocol).toBe("legacy")
    expect(config.codemode).toBe(true)
  })

  test("omits the notify environment when cards are unavailable", () => {
    const config = buildVoiceServerConfig({
      launch: { command: ["python", "server.py"], cwd: "/somewhere/voice" },
    })
    if (config.type !== "local") throw new Error("unreachable")
    expect(config.environment).toBeUndefined()
    expect(config.protocol).toBeUndefined()
    expect(config.codemode).toBeUndefined()
  })

  test("server name and env knob are stable contracts", () => {
    expect(VOICE_MCP_NAME).toBe("voice")
    expect(NOTIFY_ENV).toBe("SEBAS_NOTIFY_PATH")
  })
})

describe("wireVoiceServer", () => {
  test("merges only SEBAS_NOTIFY_PATH into an existing entry; every other key survives byte-identical", () => {
    const entry = {
      type: "local" as const,
      command: ["python", "/opt/voice/server.py", "--strict", "--label=ünïcode"],
      cwd: "/opt/voice",
      environment: { VOICE_HOME: "/data/voice", LANG: "pt_BR.UTF-8" },
      protocol: "legacy" as const,
      codemode: true,
      disabled: false,
      timeout: { startup: 5000 },
      enabled: true, // not a schema key: unknown keys survive untouched too
    }
    const editor = new FakeEditor()
    editor.seed(VOICE_MCP_NAME, entry)

    const outcome = wireVoiceServer(editor, { notifyPath: "/somewhere/cards" })

    expect(outcome).toEqual({ status: "merged" })
    // `update` edits in place — `set` would replace the user's whole entry.
    expect(editor.calls).toEqual([`update:${VOICE_MCP_NAME}`])
    expect(editor.get(VOICE_MCP_NAME)).toEqual({
      ...entry,
      environment: {
        VOICE_HOME: "/data/voice",
        LANG: "pt_BR.UTF-8",
        [NOTIFY_ENV]: "/somewhere/cards",
      },
    })
    const after = editor.get(VOICE_MCP_NAME)
    if (after?.type !== "local") throw new Error("unreachable")
    // The command is byte-identical: no relaunch, no normalization.
    expect(JSON.stringify(after.command)).toBe(JSON.stringify(entry.command))
  })

  test("respects an existing SEBAS_NOTIFY_PATH in the entry's environment (the user wins, even an explicit empty)", () => {
    for (const userValue of ["/user/cards", ""]) {
      const entry = {
        type: "local" as const,
        command: ["bash", "/user/voice/run.sh"],
        environment: { [NOTIFY_ENV]: userValue, KEEP: "1" },
      }
      const editor = new FakeEditor()
      editor.seed(VOICE_MCP_NAME, structuredClone(entry))

      const outcome = wireVoiceServer(editor, { notifyPath: "/somewhere/cards" })

      expect(outcome).toEqual({
        status: "untouched",
        reason: `its environment already sets ${NOTIFY_ENV} (the user's value wins)`,
      })
      expect(editor.calls).toEqual([])
      expect(editor.get(VOICE_MCP_NAME)).toEqual(entry)
    }
  })

  test("leaves the entry untouched when no notify path can be resolved", () => {
    const entry = {
      type: "local" as const,
      command: ["node", "voice.mjs"],
      environment: { KEEP: "yes" },
    }
    const editor = new FakeEditor()
    editor.seed(VOICE_MCP_NAME, structuredClone(entry))

    const outcome = wireVoiceServer(editor, {})

    expect(outcome).toEqual({
      status: "untouched",
      reason: "no notify path could be resolved; spoken-notice fallback",
    })
    expect(editor.calls).toEqual([])
    expect(editor.get(VOICE_MCP_NAME)).toEqual(entry)
  })

  test("registers a fresh entry when none exists — the same payload as always", () => {
    const editor = new FakeEditor()

    const outcome = wireVoiceServer(editor, {
      launch: { command: ["bash", "run.sh"], cwd: "/somewhere/voice" },
      notifyPath: "/somewhere/cards",
      protocol: "legacy",
      codemode: true,
    })

    expect(outcome).toEqual({ status: "registered" })
    expect(editor.calls).toEqual([`set:${VOICE_MCP_NAME}`])
    expect(editor.get(VOICE_MCP_NAME)).toEqual({
      type: "local",
      command: ["bash", "run.sh"],
      cwd: "/somewhere/voice",
      environment: { [NOTIFY_ENV]: "/somewhere/cards" },
      codemode: true,
      protocol: "legacy",
    })
  })

  test("stays out of the way when there is no entry and no launch recipe", () => {
    const editor = new FakeEditor()

    const outcome = wireVoiceServer(editor, { notifyPath: "/somewhere/cards" })

    expect(outcome).toEqual({
      status: "untouched",
      reason: "the voice MCP could not be located to register",
    })
    expect(editor.calls).toEqual([])
  })

  test("leaves a remote voice entry alone — there is no local environment to extend", () => {
    const entry = {
      type: "remote" as const,
      url: "https://voice.example/mcp",
      headers: { Authorization: "Bearer token" },
    }
    const editor = new FakeEditor()
    editor.seed(VOICE_MCP_NAME, structuredClone(entry))

    const outcome = wireVoiceServer(editor, { notifyPath: "/somewhere/cards" })

    expect(outcome).toEqual({
      status: "untouched",
      reason: "a remote server gets no local environment",
    })
    expect(editor.calls).toEqual([])
    expect(editor.get(VOICE_MCP_NAME)).toEqual(entry)
  })
})

describe("buildVoiceServerConfig — only present keys (V2 rejects present-but-undefined)", () => {
  const launch = { command: ["bash", "run.sh"], cwd: "/somewhere/voice" }

  test("an unset option leaves the key ABSENT — never present-but-undefined", () => {
    const config = buildVoiceServerConfig({ launch })
    // The exact regression: with `codemode`/`protocol`/`environment` unset,
    // the old builder emitted `key: undefined`, V2 schema validation rejected
    // the transform, and the whole plugin was silently disabled.
    expect(Object.keys(config).sort()).toEqual(["command", "cwd", "type"])
    for (const [key, value] of Object.entries(config)) {
      expect(value).not.toBeUndefined()
      expect(`${key}=${String(value)}`).not.toContain("undefined") // belt and braces
    }
  })

  test("set options produce present keys — including falsy values", () => {
    const config = buildVoiceServerConfig({
      launch,
      notifyPath: "/somewhere/cards",
      protocol: "auto",
      codemode: false,
    })
    expect(Object.keys(config).sort()).toEqual(["codemode", "command", "cwd", "environment", "protocol", "type"])
    if (config.type !== "local") throw new Error("unreachable")
    expect(config.codemode).toBe(false) // an explicit false is a PRESENT key
    expect(config.environment).toEqual({ [NOTIFY_ENV]: "/somewhere/cards" })
  })

  test("explicit undefined inputs are dropped exactly like absent ones", () => {
    const config = buildVoiceServerConfig({
      launch,
      notifyPath: undefined,
      protocol: undefined,
      codemode: undefined,
    })
    expect(Object.keys(config).sort()).toEqual(["command", "cwd", "type"])
    for (const value of Object.values(config)) expect(value).not.toBeUndefined()
  })
})

describe("applyVoiceTransform — the replayable transform callback", () => {
  const launch = { command: ["bash", "run.sh"], cwd: "/somewhere/voice" }
  const input: VoiceWireInput = {
    launch,
    notifyPath: "/somewhere/cards",
    protocol: "legacy",
    codemode: true,
  }
  // Typed against the editor's own mutable config shape so every key is
  // checked against the real Mcp.ServerConfig union (and never widens).
  const ours: MutableServerConfig = {
    type: "local",
    command: ["bash", "run.sh"],
    cwd: "/somewhere/voice",
    environment: { [NOTIFY_ENV]: "/somewhere/cards" },
    protocol: "legacy",
    codemode: true,
  }
  const userEntry = {
    type: "local" as const,
    command: ["python", "/user/voice/server.py"],
    cwd: "/user/voice",
    environment: { LANG: "pt_BR.UTF-8" },
    disabled: false,
  }

  test("user entry → untouched: not ours means zero editor writes", () => {
    const editor = new FakeEditor()
    editor.seed(VOICE_MCP_NAME, structuredClone(userEntry))

    expect(applyVoiceTransform(editor, input)).toEqual({ branch: "skipped-user-entry" })
    expect(editor.calls).toEqual([])
    expect(editor.get(VOICE_MCP_NAME)).toEqual(userEntry)
  })

  test("no entry → registers, with a payload that carries only present keys", () => {
    const editor = new FakeEditor()

    expect(applyVoiceTransform(editor, input)).toEqual({ branch: "registered" })
    expect(editor.calls).toEqual([`set:${VOICE_MCP_NAME}`])
    expect(editor.get(VOICE_MCP_NAME)).toEqual(ours)
    const registered = editor.get(VOICE_MCP_NAME) as Record<string, unknown>
    for (const value of Object.values(registered)) {
      expect(value).not.toBeUndefined()
    }
  })

  test("replay after the user entry disappears → registers", () => {
    const editor = new FakeEditor()
    editor.seed(VOICE_MCP_NAME, structuredClone(userEntry))
    expect(applyVoiceTransform(editor, input)).toEqual({ branch: "skipped-user-entry" })

    // The next rebuild starts from a fresh registry where the user entry is
    // gone — the callback must re-apply itself (that is "replayable").
    editor.remove(VOICE_MCP_NAME)
    editor.calls.splice(0)

    expect(applyVoiceTransform(editor, input)).toEqual({ branch: "registered" })
    expect(editor.calls).toEqual([`set:${VOICE_MCP_NAME}`])
    expect(editor.get(VOICE_MCP_NAME)).toEqual(ours)
  })

  test("replay with our own entry still present → idempotent refresh", () => {
    const editor = new FakeEditor()
    expect(applyVoiceTransform(editor, input)).toEqual({ branch: "registered" })
    editor.calls.splice(0)

    expect(applyVoiceTransform(editor, input)).toEqual({ branch: "refreshed" })
    expect(editor.calls).toEqual([`set:${VOICE_MCP_NAME}`])
    expect(editor.get(VOICE_MCP_NAME)).toEqual(ours)
  })

  test("an entry differing by one key is the user's — skipped, never clobbered", () => {
    const editor = new FakeEditor()
    editor.seed(VOICE_MCP_NAME, { ...ours, disabled: false })

    expect(applyVoiceTransform(editor, input)).toEqual({ branch: "skipped-user-entry" })
    expect(editor.calls).toEqual([])
  })

  test("ownership ignores key order (same config, reordered keys → ours)", () => {
    const editor = new FakeEditor()
    editor.seed(VOICE_MCP_NAME, {
      protocol: "legacy",
      codemode: true,
      environment: { [NOTIFY_ENV]: "/somewhere/cards" },
      cwd: "/somewhere/voice",
      command: ["bash", "run.sh"],
      type: "local",
    })

    expect(applyVoiceTransform(editor, input)).toEqual({ branch: "refreshed" })
    expect(editor.get(VOICE_MCP_NAME)).toEqual(ours)
  })

  test("no entry and no launch recipe → nothing registered", () => {
    const editor = new FakeEditor()
    expect(applyVoiceTransform(editor, {})).toEqual({ branch: "skipped-no-launch" })
    expect(editor.calls).toEqual([])
    expect(applyVoiceTransform(editor, { notifyPath: "/somewhere/cards" })).toEqual({
      branch: "skipped-no-launch",
    })
    expect(editor.calls).toEqual([])
  })

  test("a throwing editor never takes the plugin down (get)", () => {
    const editor = new FakeEditor()
    Object.defineProperty(editor, "get", { value: () => { throw new Error("registry exploded") } })

    expect(() => applyVoiceTransform(editor, input)).not.toThrow()
    expect(applyVoiceTransform(editor, input)).toEqual({
      branch: "failed",
      reason: "registry exploded",
    })
  })

  test("a throwing editor never takes the plugin down (set)", () => {
    const editor = new FakeEditor()
    Object.defineProperty(editor, "set", { value: () => { throw new Error("set exploded") } })

    expect(() => applyVoiceTransform(editor, input)).not.toThrow()
    expect(applyVoiceTransform(editor, input)).toEqual({
      branch: "failed",
      reason: "set exploded",
    })
  })
})
