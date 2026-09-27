/**
 * The injected persona text is shipped to every user and every machine, so two
 * properties are locked in here:
 *   1. every rule the project mandates is present;
 *   2. no identity and no machine-specific paths ever leak into it.
 */
import { describe, expect, test } from "bun:test"
import { BUTLER_INSTRUCTIONS, INSTRUCTIONS_MARKER } from "../src/instructions"

describe("butler instructions", () => {
  test("carry every mandated rule", () => {
    const text = BUTLER_INSTRUCTIONS
    // two roles: silent executor vs. cordial butler
    expect(text).toContain("Two roles")
    expect(text).toContain("translation layer")
    // speak-before-reply
    expect(text).toContain("BEFORE writing the text response")
    expect(text).toContain("speak(text, context)")
    // queue formula (card only on collision)
    expect(text).toContain("awaiting_confirmation")
    expect(text).toContain('card: "shown"')
    expect(text).toContain("Listen button")
    expect(text).toContain("confirmed: true")
    // voice off = text only
    expect(text).toContain("text only")
    // restart warning
    expect(text).toContain("restart")
    // portability convention
    expect(text).toContain("machine-specific paths")
    // identity is configuration
    expect(text).toContain("get_user_name")
    expect(text).toContain("never hardcode")
    // form of address: the tool reports it, the text never states it
    expect(text).toContain("form_of_address")
    expect(text).toContain("used verbatim")
  })

  test("encode the collision-only queue rules", () => {
    const text = BUTLER_INSTRUCTIONS
    // R1: never two voices — the daemon is the only speaker, one turn at a time
    expect(text).toContain("NEVER overlap")
    // R2: card ONLY when voice calls collide; the parked message is never auto-played
    expect(text).toContain("card only on collision")
    expect(text).toContain("WHILE another message is still being spoken")
    expect(text).toContain("PARKED")
    expect(text).toContain("never played automatically")
    expect(text).toContain('"listen later if you want"')
    // R3: sequential calls just speak in order
    expect(text).toContain("no card, no notice, no confirmation")
    // R4: user-initiated playback plays in turn behind whatever is playing
    expect(text).toContain("in turn behind whatever is playing")
    // R5: card:"shown" → do NOT ask in chat, do NOT speak again for that message
    expect(text).toContain("do NOT ask the user anything in chat")
    expect(text).toContain("do NOT call `speak` again for that message")
    // R5: card:"unavailable" (or no card) → the old confirmation flow
    expect(text).toContain('card: "unavailable"')
    expect(text).toContain("WAIT for the answer")
    // the pre-R1-R5 formula ("requests are queued…") must not come back
    expect(text).not.toContain("queued and spoken strictly one at a time")
  })

  test("contain no identity values (they live in configuration)", () => {
    const text = BUTLER_INSTRUCTIONS
    for (const name of ["Sebas", "Alex", "Doe", "Sam"]) {
      expect(text).not.toContain(name)
    }
    // the form of address is identity too: the text points at the
    // get_user_name TOOL and never states a vocative value
    for (const vocative of ["senhor", "senhora", "Senhor", "doutor", "chefe"]) {
      expect(text).not.toContain(vocative)
    }
  })

  test("carry the first-interaction rubric (ask once, then save)", () => {
    const text = BUTLER_INSTRUCTIONS
    // ask ONCE, everything in one friendly message
    expect(text).toContain("First interaction")
    expect(text).toContain("ASKS, ONCE")
    expect(text).toContain("single friendly message")
    // the three questions: name, how to be called, language
    expect(text).toContain("how they like to be called")
    expect(text).toContain("English and Portuguese")
    // the answers are saved so the question is never repeated
    expect(text).toContain("set_user_name(name, form_of_address=...)")
    expect(text).toContain("set_language(...)")
    expect(text).toContain("never repeated")
    // never infer gender from a name
    expect(text).toContain("never guess gender")
  })

  test("address the user neutrally until the identity is saved", () => {
    const text = BUTLER_INSTRUCTIONS
    expect(text).toContain("Until the identity is saved")
    expect(text).toContain("NEUTRALLY")
    expect(text).toContain("NO gendered treatment")
  })

  test("never contain a gendered default or example", () => {
    const text = BUTLER_INSTRUCTIONS
    // the Portuguese vocatives the ban above covers (kept explicit here so a
    // future default like 'Senhor {first}' can never sneak back in)
    for (const vocative of ["senhor", "senhora", "Senhor", "doutor", "chefe"]) {
      expect(text).not.toContain(vocative)
    }
    // English gendered honorifics, word-bounded so substrings stay legal
    expect(text).not.toMatch(/\b(sir|madam|madame|mister|mistress|mr|mrs|ms|miss|monsieur|mademoiselle|herr|frau)\b/i)
    // and no greeting template that could carry one
    expect(text).not.toContain("{first}")
  })

  test("contain no machine-specific paths", () => {
    const text = BUTLER_INSTRUCTIONS
    for (const needle of ["/home/", "/Users/", "C:\\", "~/.config", "~/.local", "/tmp/"]) {
      expect(text).not.toContain(needle)
    }
  })

  test("carry a stable idempotency marker", () => {
    expect(INSTRUCTIONS_MARKER).toBe("sebas")
  })
})
