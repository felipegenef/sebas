/**
 * Tests for the diagnostics log file: every `[sebas]` line lands in
 * the Sebas data dir's plugin.log, the file is trimmed to its last lines, and
 * no filesystem failure ever propagates into the plugin. Throwaway temp dirs
 * only — nothing machine-specific is touched.
 */
import { afterAll, describe, expect, test } from "bun:test"
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import {
  createDiagnostics,
  DIAGNOSTICS_FILE,
  DIAGNOSTICS_MAX_LINES,
  resolveDiagnosticsPath,
} from "../src/diagnostics"

const tmpRoots: string[] = []

function makeRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "sebas-diag-"))
  tmpRoots.push(root)
  return root
}

afterAll(() => {
  for (const root of tmpRoots) rmSync(root, { recursive: true, force: true })
})

function readLines(path: string): string[] {
  return readFileSync(path, "utf-8").split("\n").filter((line) => line !== "")
}

describe("createDiagnostics", () => {
  test("appends [sebas] lines to plugin.log and echoes them", () => {
    const filePath = join(makeRoot(), DIAGNOSTICS_FILE)
    const echoed: string[] = []
    const diagnostics = createDiagnostics({
      filePath,
      echo: (line) => echoed.push(line),
    })

    diagnostics.log("info", "voice MCP located via layout")
    diagnostics.log("warn", "notify path unavailable")

    const expected = [
      "[sebas] voice MCP located via layout",
      "[sebas] notify path unavailable",
    ]
    expect(readLines(filePath)).toEqual(expected)
    expect(echoed).toEqual(expected)
  })

  test("trims the file to its last DIAGNOSTICS_MAX_LINES lines", () => {
    const filePath = join(makeRoot(), DIAGNOSTICS_FILE)
    const diagnostics = createDiagnostics({ filePath, echo: () => {} })

    for (let i = 1; i <= DIAGNOSTICS_MAX_LINES + 50; i += 1) {
      diagnostics.log("info", `line ${i}`)
    }

    const lines = readLines(filePath)
    expect(lines).toHaveLength(DIAGNOSTICS_MAX_LINES)
    expect(lines[0]).toBe("[sebas] line 51")
    expect(lines[lines.length - 1]).toBe(`[sebas] line ${DIAGNOSTICS_MAX_LINES + 50}`)
  })

  test("honours a custom maxLines", () => {
    const filePath = join(makeRoot(), DIAGNOSTICS_FILE)
    const diagnostics = createDiagnostics({ filePath, maxLines: 3, echo: () => {} })

    for (const name of ["a", "b", "c", "d", "e"]) diagnostics.log("info", name)

    expect(readLines(filePath)).toEqual([
      "[sebas] c",
      "[sebas] d",
      "[sebas] e",
    ])
  })

  test("never throws when the file cannot be written", () => {
    const root = makeRoot()
    const blocker = join(root, "blocker")
    writeFileSync(blocker, "") // a FILE where the log's directory must be
    const diagnostics = createDiagnostics({
      filePath: join(blocker, DIAGNOSTICS_FILE),
      echo: () => {},
    })

    expect(() => diagnostics.log("info", "hello")).not.toThrow()
    expect(() => diagnostics.log("warn", "hello again")).not.toThrow()
  })

  test("never throws when the console sink throws", () => {
    const filePath = join(makeRoot(), DIAGNOSTICS_FILE)
    const diagnostics = createDiagnostics({
      filePath,
      echo: () => {
        throw new Error("console gone")
      },
    })

    expect(() => diagnostics.log("info", "still recorded")).not.toThrow()
    expect(readLines(filePath)).toEqual(["[sebas] still recorded"])
  })
})

describe("resolveDiagnosticsPath", () => {
  test("lands in the Sebas data dir (XDG_DATA_HOME/sebas or the default data home)", () => {
    expect(resolveDiagnosticsPath({ XDG_DATA_HOME: "/data" }, "/home/u")).toBe(
      join("/data", "sebas", DIAGNOSTICS_FILE),
    )
    expect(resolveDiagnosticsPath({}, "/home/u")).toBe(
      join("/home/u", ".local", "share", "sebas", DIAGNOSTICS_FILE),
    )
    expect(resolveDiagnosticsPath({ XDG_DATA_HOME: "  " }, "/home/u")).toBe(
      join("/home/u", ".local", "share", "sebas", DIAGNOSTICS_FILE),
    )
  })

  test("follows an auto-detected pre-1.0 legacy data dir", () => {
    const base = makeRoot()
    mkdirSync(join(base, "voz"), { recursive: true })
    expect(resolveDiagnosticsPath({ XDG_DATA_HOME: base }, "/home/u")).toBe(
      join(base, "voz", DIAGNOSTICS_FILE),
    )
    mkdirSync(join(base, "sebas"), { recursive: true })
    expect(resolveDiagnosticsPath({ XDG_DATA_HOME: base }, "/home/u")).toBe(
      join(base, "sebas", DIAGNOSTICS_FILE),
    )
  })
})
