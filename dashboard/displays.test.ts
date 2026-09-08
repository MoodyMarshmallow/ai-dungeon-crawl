import { expect, test } from "bun:test";
import { terminalText } from "./displays";

test("terminal views preserve plain-text layout", () => {
  expect(terminalText("  #@.#\r\n\tfoo\n")).toBe("  #@.#\n\tfoo\n");
});

test("model output cannot inject terminal controls or clipboard sequences", () => {
  const text = terminalText("\x1b]52;c;secret\x07\x1b[2Jscreen\x9dtest");
  expect(text).not.toMatch(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/);
  expect(text).toContain("screen");
});
