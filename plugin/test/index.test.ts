/**
 * Unit tests for the plugin entry point's pure helpers — the JSON projections
 * that seed plugin `storage`. A field missing from a projection is silently
 * dropped from the seeded JSON and lost on the next read, so the projection is
 * pinned here field by field.
 */
import { describe, expect, test } from "bun:test"
import { normalizeIdentity } from "../src/identity"
import { identityJson } from "../src/index"

describe("identityJson", () => {
  test("carries formOfAddress into the seeded storage JSON", () => {
    const json = identityJson(
      normalizeIdentity({ butler_name: "Sebas", form_of_address: "senhor Alex" }),
    )
    expect(json).toEqual({
      butlerName: "Sebas",
      language: null,
      mainUser: null,
      users: [],
      people: [],
      formOfAddress: "senhor Alex",
    })
    // The seeded JSON must survive the read-back parser unchanged: this is the
    // shape the next startup merges, presence semantics included.
    expect(normalizeIdentity(json).formOfAddress).toBe("senhor Alex")
  })

  test("carries no undefined values — storage accepts plain JSON only", () => {
    const json = identityJson(normalizeIdentity({}))
    if (typeof json !== "object" || json === null || Array.isArray(json)) {
      throw new Error("the projection must be a JSON object")
    }
    for (const [key, value] of Object.entries(json)) {
      expect(value).not.toBeUndefined() // a present-but-undefined key is not JSON
      expect(Object.hasOwn(json, key)).toBe(true)
    }
    expect(() => JSON.stringify(json)).not.toThrow()
  })
})
