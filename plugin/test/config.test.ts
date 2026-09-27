/**
 * Unit tests for option and path resolution. They build throwaway directory
 * layouts under the system temp dir — no live OpenCode, no real voice MCP, no
 * machine state touched.
 */
import { afterAll, describe, expect, test } from "bun:test"
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { homedir, tmpdir } from "node:os"
import { dirname, join } from "node:path"
import {
  expandHome,
  parseOptions,
  resolveNotifyPath,
  resolveVoiceCommand,
  resolveVoiceDir,
} from "../src/config"

const tmpRoots: string[] = []

function makeRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "sebas-config-"))
  tmpRoots.push(root)
  return root
}

function writeFile(path: string): void {
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, "")
}

afterAll(() => {
  for (const root of tmpRoots) rmSync(root, { recursive: true, force: true })
})

describe("parseOptions", () => {
  test("keeps valid values", () => {
    const { options, warnings } = parseOptions({
      voiceDir: "/tmp/voice",
      voiceCommand: ["bash", "run.sh"],
      notifyPath: "/tmp/cards",
      mcpProtocol: "legacy",
      codemode: true,
      migrateIdentity: false,
      identity: { butlerName: "A" },
      voice: { speed: 1.2 },
    })
    expect(options.voiceDir).toBe("/tmp/voice")
    expect(options.voiceCommand).toEqual(["bash", "run.sh"])
    expect(options.notifyPath).toBe("/tmp/cards")
    expect(options.mcpProtocol).toBe("legacy")
    expect(options.codemode).toBe(true)
    expect(options.migrateIdentity).toBe(false)
    expect(options.identity).toEqual({ butlerName: "A" })
    expect(options.voice).toEqual({ speed: 1.2 })
    expect(warnings).toEqual([])
  })

  test("drops wrong types with warnings, never throws", () => {
    const { options, warnings } = parseOptions({
      voiceDir: 42,
      voiceCommand: [],
      mcpProtocol: "quantum",
      codemode: "yes",
      migrateIdentity: 1,
      identity: ["not", "an", "object"],
    })
    expect(options.voiceDir).toBeUndefined()
    expect(options.voiceCommand).toBeUndefined()
    expect(options.mcpProtocol).toBeUndefined()
    expect(options.codemode).toBeUndefined()
    expect(options.migrateIdentity).toBeUndefined()
    expect(options.identity).toBeUndefined()
    expect(warnings.length).toBe(6)
  })

  test("ignores unknown keys quietly", () => {
    const { options, warnings } = parseOptions({ surprise: true })
    expect(options).toEqual({})
    expect(warnings).toEqual([])
  })
})

describe("expandHome", () => {
  test("expands a leading tilde only", () => {
    expect(expandHome("~/somewhere")).toBe(join(homedir(), "somewhere"))
    expect(expandHome("/absolute/path")).toBe("/absolute/path")
    expect(expandHome("relative/path")).toBe("relative/path")
  })
})

describe("resolveVoiceDir", () => {
  test("option wins over env and layout", () => {
    const root = makeRoot()
    const fromOption = join(root, "opt")
    const fromEnv = join(root, "env")
    writeFile(join(fromOption, "server.py"))
    writeFile(join(fromEnv, "server.py"))

    const result = resolveVoiceDir({
      options: { voiceDir: fromOption },
      env: { SEBAS_MCP_DIR: fromEnv },
      pluginRoot: root,
    })
    expect(result.status).toBe("ok")
    if (result.status === "ok") expect(result.value).toBe(fromOption)
  })

  test("env wins over layout when no option is set", () => {
    const root = makeRoot()
    const fromEnv = join(root, "env")
    writeFile(join(fromEnv, "server.py"))

    const result = resolveVoiceDir({ options: {}, env: { SEBAS_MCP_DIR: fromEnv }, pluginRoot: root })
    expect(result.status).toBe("ok")
    if (result.status === "ok") expect(result.value).toBe(fromEnv)
  })

  test("relative layout: vendored plugin/mcp/voice and sibling repo/mcp/voice", () => {
    const vendoredParent = makeRoot()
    const vendored = join(vendoredParent, "plugin")
    writeFile(join(vendored, "mcp", "voice", "server.py"))
    const vendoredResult = resolveVoiceDir({ options: {}, env: {}, pluginRoot: vendored })
    expect(vendoredResult.status).toBe("ok")
    if (vendoredResult.status === "ok") {
      expect(vendoredResult.value).toBe(join(vendored, "mcp", "voice"))
    }

    const repoParent = makeRoot()
    const repo = join(repoParent, "repo")
    const pluginRoot = join(repo, "plugin")
    mkdirSync(pluginRoot, { recursive: true })
    writeFile(join(repo, "mcp", "voice", "server.py"))
    const repoResult = resolveVoiceDir({ options: {}, env: {}, pluginRoot })
    expect(repoResult.status).toBe("ok")
    if (repoResult.status === "ok") {
      expect(repoResult.value).toBe(join(repo, "mcp", "voice"))
    }
  })

  test("bad explicit option falls through to env with a warning", () => {
    const root = makeRoot()
    const fromEnv = join(root, "env")
    writeFile(join(fromEnv, "server.py"))

    const result = resolveVoiceDir({
      options: { voiceDir: join(root, "nowhere") },
      env: { SEBAS_MCP_DIR: fromEnv },
      pluginRoot: root,
    })
    expect(result.status).toBe("ok")
    expect(result.warnings.length).toBe(1)
    if (result.status === "ok") expect(result.value).toBe(fromEnv)
  })

  test("nothing found → skipped with guidance, no paths in messages", () => {
    const result = resolveVoiceDir({ options: {}, env: {}, pluginRoot: makeRoot() })
    expect(result.status).toBe("skipped")
    if (result.status === "skipped") {
      expect(result.problem.length).toBeGreaterThan(0)
      expect(result.nextStep).toContain("voiceDir")
      expect(result.nextStep).toContain("SEBAS_MCP_DIR")
    }
  })
})

describe("resolveVoiceCommand", () => {
  test("explicit command wins and runs inside voiceDir", () => {
    const launch = resolveVoiceCommand({
      options: { voiceCommand: ["python", "server.py"] },
      voiceDir: "/somewhere/voice",
      platform: "linux",
    })
    expect(launch.command).toEqual(["python", "server.py"])
    expect(launch.cwd).toBe("/somewhere/voice")
  })

  test("POSIX default launches run.sh", () => {
    const launch = resolveVoiceCommand({ options: {}, voiceDir: "/somewhere/voice", platform: "linux" })
    expect(launch.command).toEqual(["bash", "run.sh"])
    expect(launch.cwd).toBe("/somewhere/voice")
  })

  test("Windows default falls back to python when the setup never ran", () => {
    const root = makeRoot()
    const launch = resolveVoiceCommand({
      options: {},
      voiceDir: "C:\\somewhere\\voice",
      platform: "win32",
      env: { XDG_DATA_HOME: join(root, "data") },
      home: join(root, "home"),
    })
    expect(launch.command).toEqual(["python", "server.py"])
    expect(launch.cwd).toBe("C:\\somewhere\\voice")
  })

  test("Windows launches the venv interpreter once the setup has run", () => {
    const root = makeRoot()
    const dataHome = join(root, "data")
    writeFile(join(dataHome, "sebas", "venv", "Scripts", "python.exe"))
    const launch = resolveVoiceCommand({
      options: {},
      voiceDir: "C:\\somewhere\\voice",
      platform: "win32",
      env: { XDG_DATA_HOME: dataHome },
      home: join(root, "home"),
    })
    expect(launch.command).toEqual([
      join(dataHome, "sebas", "venv", "Scripts", "python.exe"),
      "server.py",
    ])
    expect(launch.cwd).toBe("C:\\somewhere\\voice")
  })

  test("Windows finds a venv in a pre-1.0 legacy data dir too", () => {
    const root = makeRoot()
    const dataHome = join(root, "data")
    writeFile(join(dataHome, "voz", "venv", "Scripts", "python.exe"))
    const launch = resolveVoiceCommand({
      options: {},
      voiceDir: "C:\\somewhere\\voice",
      platform: "win32",
      env: { XDG_DATA_HOME: dataHome },
      home: join(root, "home"),
    })
    expect(launch.command[0]).toBe(join(dataHome, "voz", "venv", "Scripts", "python.exe"))
  })

  test("explicit command still wins over the Windows venv", () => {
    const root = makeRoot()
    const dataHome = join(root, "data")
    writeFile(join(dataHome, "sebas", "venv", "Scripts", "python.exe"))
    const launch = resolveVoiceCommand({
      options: { voiceCommand: ["python", "server.py"] },
      voiceDir: "C:\\somewhere\\voice",
      platform: "win32",
      env: { XDG_DATA_HOME: dataHome },
      home: join(root, "home"),
    })
    expect(launch.command).toEqual(["python", "server.py"])
  })
})

describe("resolveNotifyPath", () => {
  const notifyRoot = (root: string): string => {
    writeFile(join(root, "notify", "__init__.py"))
    return root
  }

  test("option wins over env and layout", () => {
    const root = makeRoot()
    const fromOption = notifyRoot(join(root, "opt"))
    const fromEnv = notifyRoot(join(root, "env"))

    const result = resolveNotifyPath({
      options: { notifyPath: fromOption },
      env: { SEBAS_NOTIFY_PATH: fromEnv },
      pluginRoot: join(root, "plugin"),
    })
    expect(result.status).toBe("ok")
    if (result.status === "ok") expect(result.value).toBe(fromOption)
  })

  test("layout finds this repository (notify/ next to plugin/) and the vendored layout", () => {
    const repo = makeRoot()
    const pluginRoot = join(repo, "plugin")
    mkdirSync(pluginRoot, { recursive: true })
    notifyRoot(repo)
    const repoResult = resolveNotifyPath({ options: {}, env: {}, pluginRoot })
    expect(repoResult.status).toBe("ok")
    if (repoResult.status === "ok") expect(repoResult.value).toBe(repo)

    const vendoredRoot = notifyRoot(join(makeRoot(), "plugin"))
    const vendoredResult = resolveNotifyPath({ options: {}, env: {}, pluginRoot: vendoredRoot })
    expect(vendoredResult.status).toBe("ok")
    if (vendoredResult.status === "ok") expect(vendoredResult.value).toBe(vendoredRoot)
  })

  test("a notify/ directory without __init__.py is not accepted", () => {
    const root = makeRoot()
    const broken = join(root, "broken")
    writeFile(join(broken, "notify", "elsewhere.txt"))
    const result = resolveNotifyPath({ options: { notifyPath: broken }, env: {}, pluginRoot: root })
    expect(result.status).toBe("skipped")
    expect(result.warnings.length).toBe(1)
    if (result.status === "skipped") expect(result.nextStep).toContain("SEBAS_NOTIFY_PATH")
  })
})
