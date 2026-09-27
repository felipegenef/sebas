/**
 * Unit tests for the identity/voice config store and the documented legacy
 * migration. The legacy files are written into a temp dir and only ever read.
 */
import { afterAll, describe, expect, test } from "bun:test"
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import {
  DEFAULT_IDENTITY,
  DEFAULT_VOICE,
  mergeIdentity,
  mergeVoice,
  normalizeIdentity,
  normalizeLanguage,
  normalizeVoiceConfig,
  readVoiceServerFiles,
  resolveIdentity,
  resolveDataDir,
} from "../src/identity"

const tmpRoots: string[] = []

function makeDataDir(users?: unknown, voice?: unknown, profileName = "config.json"): string {
  const root = mkdtempSync(join(tmpdir(), "sebas-identity-"))
  tmpRoots.push(root)
  mkdirSync(root, { recursive: true })
  if (users !== undefined) writeFileSync(join(root, "users.json"), JSON.stringify(users))
  if (voice !== undefined) writeFileSync(join(root, profileName), JSON.stringify(voice))
  return root
}

afterAll(() => {
  for (const root of tmpRoots) rmSync(root, { recursive: true, force: true })
})

describe("normalizeLanguage", () => {
  test("maps aliases the way the voice server does", () => {
    expect(normalizeLanguage("en")).toBe("en-us")
    expect(normalizeLanguage("EN-US")).toBe("en-us")
    expect(normalizeLanguage(" pt ")).toBe("pt-br")
    expect(normalizeLanguage("pt-br")).toBe("pt-br")
    expect(normalizeLanguage("fr")).toBeNull()
    expect(normalizeLanguage(null)).toBeNull()
    expect(normalizeLanguage(7)).toBeNull()
  })
})

describe("normalizeIdentity", () => {
  test("accepts both camelCase and the voice server's snake_case keys", () => {
    expect(normalizeIdentity({ butlerName: "A", mainUser: "B" })).toEqual({
      butlerName: "A",
      language: null,
      mainUser: "B",
      users: [],
      people: [],
      formOfAddress: null,
    })
    expect(normalizeIdentity({
      butler_name: "A",
      language: "pt-br",
      main_user: "B",
      users: ["B", 3, " "],
      people: ["C"],
    })).toEqual({
      butlerName: "A",
      language: "pt-br",
      mainUser: "B",
      users: ["B"],
      people: ["C"],
      formOfAddress: null,
    })
  })

  test("form_of_address maps to formOfAddress with presence semantics", () => {
    // present strings are kept (trimmed) — including "" (explicit clear)…
    expect(normalizeIdentity({ formOfAddress: "  senhor Alex " }).formOfAddress)
      .toBe("senhor Alex")
    expect(normalizeIdentity({ form_of_address: "chefe" }).formOfAddress).toBe("chefe")
    expect(normalizeIdentity({ formOfAddress: "" }).formOfAddress).toBe("")
    expect(normalizeIdentity({ formOfAddress: "   " }).formOfAddress).toBe("")
    // …while missing or invalid keys stay ABSENT (null → falls through)
    expect(normalizeIdentity({ formOfAddress: 7 }).formOfAddress).toBeNull()
    expect(normalizeIdentity({}).formOfAddress).toBeNull()
    // the snake_case fallback works like the other identity keys
    expect(normalizeIdentity({ formOfAddress: null, form_of_address: "doutor" }).formOfAddress)
      .toBe("doutor")
  })

  test("garbage in → defaults, never throws", () => {
    expect(normalizeIdentity("nonsense")).toEqual(DEFAULT_IDENTITY)
    expect(normalizeIdentity(null)).toEqual(DEFAULT_IDENTITY)
    expect(normalizeIdentity({ users: "not-a-list" })).toEqual(DEFAULT_IDENTITY)
  })
})

describe("normalizeVoiceConfig", () => {
  test("migrates the legacy kokoro_voice key", () => {
    expect(normalizeVoiceConfig({ kokoro_voice: "pm_santa", speed: 1.5, play: false })).toEqual({
      voice: "pm_santa",
      speed: 1.5,
      play: false,
    })
  })

  test("missing or invalid keys are absent, never defaults", () => {
    // absent (undefined) is what lets the merge tell "set to the default
    // value" (wins) from "never set" (falls through) — filling DEFAULT_VOICE
    // here is exactly the bug that let legacy values survive storage writes.
    expect(normalizeVoiceConfig({ voice: 3, speed: "fast", play: "yes" })).toEqual({})
    expect(normalizeVoiceConfig(undefined)).toEqual({})
    // absent keys resolve to the defaults…
    expect(mergeVoice(DEFAULT_VOICE, normalizeVoiceConfig({ voice: 3, speed: "fast" }))).toEqual(
      DEFAULT_VOICE,
    )
    // …while valid keys land on top of them
    expect(mergeVoice(DEFAULT_VOICE, normalizeVoiceConfig({ speed: 2.0 }))).toEqual({
      voice: null,
      speed: 2.0,
      play: true,
    })
  })
})

describe("merge", () => {
  test("patch values win only where present", () => {
    const base = normalizeIdentity({ butler_name: "A", main_user: "B", people: ["C"] })
    const merged = mergeIdentity(base, normalizeIdentity({ mainUser: "D" }))
    expect(merged.butlerName).toBe("A")
    expect(merged.mainUser).toBe("D")
    expect(merged.people).toEqual(["C"])
  })

  test("voice merge runs on key presence, not on value ≠ default", () => {
    const base = mergeVoice(DEFAULT_VOICE, normalizeVoiceConfig({ voice: "pm_santa", speed: 1.5, play: false }))

    // absent keys never override
    expect(mergeVoice(base, {})).toEqual(base)

    // keys set to the DEFAULT values still override — the reported bug was
    // that a stored speed:1.0/play:true could never beat this legacy profile
    expect(mergeVoice(base, normalizeVoiceConfig({ speed: 1.0, play: true }))).toEqual({
      voice: "pm_santa",
      speed: 1.0,
      play: true,
    })

    // and the reverse direction behaves the same way
    const quiet = mergeVoice(DEFAULT_VOICE, normalizeVoiceConfig({ speed: 1.0, play: true }))
    expect(mergeVoice(quiet, normalizeVoiceConfig({ speed: 1.5, play: false }))).toEqual({
      voice: null,
      speed: 1.5,
      play: false,
    })
  })

  test("voice merge keeps base when the patch is all absent", () => {
    const base = mergeVoice(DEFAULT_VOICE, normalizeVoiceConfig({ voice: "pm_santa", speed: 1.5 }))
    expect(mergeVoice(base, normalizeVoiceConfig(undefined))).toEqual(base)
  })
})

describe("formOfAddress merge", () => {
  test("runs on key presence: '' clears, absent falls through", () => {
    const base = normalizeIdentity({ main_user: "B", form_of_address: "chefe" })

    // absent (null) never overrides — the lower layer seeds the field
    expect(mergeIdentity(base, normalizeIdentity({ mainUser: "D" })).formOfAddress).toBe("chefe")

    // a present value wins…
    expect(mergeIdentity(base, normalizeIdentity({ formOfAddress: "senhor" })).formOfAddress)
      .toBe("senhor")

    // …and a present "" is an explicit CLEAR: it must beat the lower layer,
    // otherwise cleared storage would resurrect a legacy form of address
    expect(mergeIdentity(base, normalizeIdentity({ formOfAddress: "" })).formOfAddress).toBe("")
    expect(mergeIdentity(base, normalizeIdentity({ formOfAddress: "  " })).formOfAddress).toBe("")
  })

  test("cleared storage beats a legacy form (the storage-wins case)", () => {
    const resolved = resolveIdentity({
      stored: { formOfAddress: "" },
      options: {},
      legacy: {
        identity: normalizeIdentity({ butler_name: "Legacy", main_user: "L", form_of_address: "senhor" }),
        voice: normalizeVoiceConfig({}),
        found: { users: true, voice: false },
      },
    })
    expect(resolved.source).toBe("storage")
    expect(resolved.identity.formOfAddress).toBe("")
  })

  test("absent storage falls through to the legacy form", () => {
    const resolved = resolveIdentity({
      stored: { butlerName: "Stored" },
      options: {},
      legacy: {
        identity: normalizeIdentity({ butler_name: "Legacy", form_of_address: "senhor" }),
        voice: normalizeVoiceConfig({}),
        found: { users: true, voice: false },
      },
    })
    expect(resolved.identity.formOfAddress).toBe("senhor")
    expect(resolved.identity.butlerName).toBe("Stored")
  })
})

describe("resolveDataDir", () => {
  test("XDG_DATA_HOME wins over the default data home", () => {
    expect(resolveDataDir({ XDG_DATA_HOME: "/data" }, "/home/u")).toBe(join("/data", "sebas"))
    expect(resolveDataDir({}, "/home/u")).toBe(join("/home/u", ".local", "share", "sebas"))
    expect(resolveDataDir({ XDG_DATA_HOME: "  " }, "/home/u")).toBe(
      join("/home/u", ".local", "share", "sebas"),
    )
  })

  test("a pre-1.0 legacy dir is auto-detected and used as-is", () => {
    const base = mkdtempSync(join(tmpdir(), "sebas-identity-"))
    tmpRoots.push(base)
    mkdirSync(join(base, "voz"), { recursive: true })
    expect(resolveDataDir({ XDG_DATA_HOME: base }, "/home/u")).toBe(join(base, "voz"))
    mkdirSync(join(base, "sebas"), { recursive: true })
    expect(resolveDataDir({ XDG_DATA_HOME: base }, "/home/u")).toBe(join(base, "sebas"))
  })
})

describe("readVoiceServerFiles", () => {
  test("reads both voice server's config files read-only", () => {
    const dir = makeDataDir(
      { butler_name: "A", language: "pt", main_user: "B", users: ["B"], people: ["C"], form_of_address: "senhor" },
      { kokoro_voice: "pm_santa", speed: 1.2 },
    )
    const legacy = readVoiceServerFiles(dir)
    expect(legacy.found).toEqual({ users: true, voice: true })
    expect(legacy.identity).toEqual({
      butlerName: "A",
      language: "pt-br",
      mainUser: "B",
      users: ["B"],
      people: ["C"],
      formOfAddress: "senhor",
    })
    expect(legacy.voice.voice).toBe("pm_santa")
    expect(legacy.voice.speed).toBe(1.2)
  })

  test("reads a pre-1.0 voz.json profile when config.json is missing", () => {
    const dir = makeDataDir(undefined, { speed: 1.75 }, "voz.json")
    const files = readVoiceServerFiles(dir)
    expect(files.found).toEqual({ users: false, voice: true })
    expect(files.voice.speed).toBe(1.75)
  })

  test("config.json wins over a leftover pre-1.0 voz.json", () => {
    const dir = makeDataDir(undefined, { speed: 1.25 }, "config.json")
    writeFileSync(join(dir, "voz.json"), JSON.stringify({ speed: 1.75 }))
    expect(readVoiceServerFiles(dir).voice.speed).toBe(1.25)
  })

  test("missing or corrupt files are treated as absent", () => {
    const empty = readVoiceServerFiles(join(tmpdir(), "sebas-does-not-exist"))
    expect(empty.found).toEqual({ users: false, voice: false })
    expect(empty.identity).toEqual(DEFAULT_IDENTITY)

    const root = mkdtempSync(join(tmpdir(), "sebas-identity-"))
    tmpRoots.push(root)
    writeFileSync(join(root, "users.json"), "{ not json")
    const corrupt = readVoiceServerFiles(root)
    expect(corrupt.found).toEqual({ users: false, voice: false })
  })
})

describe("resolveIdentity", () => {
  const legacy = {
    identity: normalizeIdentity({ butler_name: "Legacy", main_user: "L" }),
    voice: normalizeVoiceConfig({ voice: "pm_santa" }),
    found: { users: true, voice: true },
  }

  test("storage wins over options and legacy", () => {
    const resolved = resolveIdentity({
      stored: { butlerName: "Stored", mainUser: "S" },
      storedVoice: { voice: "bm_george" },
      options: { identity: { butlerName: "Option" }, voice: { voice: "pf_dora" } },
      legacy,
    })
    expect(resolved.source).toBe("storage")
    expect(resolved.identity.butlerName).toBe("Stored")
    expect(resolved.voice.voice).toBe("bm_george")
  })

  test("options win over legacy when storage is empty", () => {
    const resolved = resolveIdentity({
      options: { identity: { butlerName: "Option" } },
      legacy,
    })
    expect(resolved.source).toBe("options")
    expect(resolved.identity.butlerName).toBe("Option")
    expect(resolved.identity.mainUser).toBe("L") // untouched by options → legacy
    expect(resolved.voice.voice).toBe("pm_santa")
  })

  test("legacy is used when nothing else is set; otherwise defaults", () => {
    const fromLegacy = resolveIdentity({ options: {}, legacy })
    expect(fromLegacy.source).toBe("legacy")
    expect(fromLegacy.identity.butlerName).toBe("Legacy")

    const fromDefaults = resolveIdentity({
      options: {},
      legacy: { identity: DEFAULT_IDENTITY, voice: DEFAULT_VOICE, found: { users: false, voice: false } },
    })
    expect(fromDefaults.source).toBe("defaults")
    expect(fromDefaults.identity).toEqual(DEFAULT_IDENTITY)
    expect(fromDefaults.voice).toEqual(DEFAULT_VOICE)
  })

  test("stored voice keys at their default values beat legacy (QA case)", () => {
    const resolved = resolveIdentity({
      storedVoice: { speed: 1.0, play: true }, // default VALUES, explicitly stored
      options: {},
      legacy: {
        identity: DEFAULT_IDENTITY,
        voice: normalizeVoiceConfig({ voice: "pm_santa", speed: 1.5, play: false }),
        found: { users: false, voice: true },
      },
    })
    expect(resolved.source).toBe("storage")
    expect(resolved.voice).toEqual({ voice: "pm_santa", speed: 1.0, play: true })
  })

  test("stored non-default voice keys beat legacy in the reverse direction", () => {
    const resolved = resolveIdentity({
      storedVoice: { speed: 1.5, play: false },
      options: {},
      legacy: {
        identity: DEFAULT_IDENTITY,
        voice: normalizeVoiceConfig({ speed: 1.0, play: true }),
        found: { users: false, voice: true },
      },
    })
    expect(resolved.voice).toEqual({ voice: null, speed: 1.5, play: false })
  })

  test("legacy seeds only the keys storage leaves absent", () => {
    const resolved = resolveIdentity({
      storedVoice: { voice: "bm_george" }, // no speed/play keys → legacy seeds them
      options: {},
      legacy: {
        identity: DEFAULT_IDENTITY,
        voice: normalizeVoiceConfig({ speed: 1.5, play: false }),
        found: { users: false, voice: true },
      },
    })
    expect(resolved.source).toBe("storage")
    expect(resolved.voice).toEqual({ voice: "bm_george", speed: 1.5, play: false })
  })

  test("options default-valued voice keys beat legacy too", () => {
    const resolved = resolveIdentity({
      options: { voice: { speed: 1.0, play: true } },
      legacy: {
        identity: DEFAULT_IDENTITY,
        voice: normalizeVoiceConfig({ speed: 1.5, play: false }),
        found: { users: false, voice: true },
      },
    })
    expect(resolved.source).toBe("options")
    expect(resolved.voice).toEqual({ voice: null, speed: 1.0, play: true })
  })

  test("identity merges on key presence: set values win, absent keys seed from legacy", () => {
    const resolved = resolveIdentity({
      stored: { butlerName: "Sebas", users: ["Alex"] },
      options: {},
      legacy: {
        identity: normalizeIdentity({ butler_name: "Legacy", main_user: "L", users: ["L"] }),
        voice: normalizeVoiceConfig({}),
        found: { users: true, voice: false },
      },
    })
    expect(resolved.source).toBe("storage")
    expect(resolved.identity.butlerName).toBe("Sebas")  // set in storage → wins
    expect(resolved.identity.mainUser).toBe("L")        // absent in storage → legacy seeds
    expect(resolved.identity.users).toEqual(["Alex"]) // set list wins over legacy list
  })
})
