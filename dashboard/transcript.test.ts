import { expect, test } from "bun:test";
import { shellTranscript, activityEntries } from "./transcript";
import type { Submission } from "./types";
const submission: Submission = {
  id: 0,
  code: "x = 1\nprint(x)",
  model_requests: 1,
  status: "ok",
  output: "1\n",
  error: null,
  output_truncated: false,
  keys: [],
};
test("shell transcript uses prompts, code and output without turn labels", () => {
  expect(shellTranscript([])).toBe("$ ");
  expect(shellTranscript([submission])).toBe("$ x = 1\n> print(x)\n1\n$ ");
  expect(shellTranscript([{ ...submission, status: "running" }])).toEndWith(
    "1\n",
  );
});
test("execution errors and truncation are not hidden", () => {
  expect(
    shellTranscript([
      { ...submission, error: "ValueError: invalid", output_truncated: true },
    ]),
  ).toContain("ValueError: invalid\n[Output truncated]");
});
test("activity interleaves model calls and differently styled returns", () => {
  const entries = activityEntries({
    models: [
      {
        id: 0,
        turn: 0,
        status: "accepted",
        tokens: null,
        parts: {
          0: {
            kind: "tool",
            name: "execute_shell",
            text: JSON.stringify({ code: submission.code }),
            truncated: false,
          },
        },
      },
    ],
    submissions: [submission],
  });
  expect(entries.map((entry) => entry.text)).toEqual([submission.code, "1\n"]);
  expect(entries.map((entry) => entry.className)).toEqual([
    "model-part tool",
    "result",
  ]);
  expect(entries.map((entry) => entry.turn)).toEqual([0, 0]);
});
test("activity keeps retries in the same turn and orders returns with their turn", () => {
  const model = (id: number, turn: number) => ({
    id,
    turn,
    status: "accepted",
    tokens: null,
    parts: {
      0: { kind: "text" as const, name: "", text: `Response ${id}`, truncated: false },
    },
  });
  const entries = activityEntries({
    models: [model(2, 1), model(0, 0), model(1, 0)],
    submissions: [{ ...submission, id: 1 }, submission],
  });
  expect(entries.map(({ id, turn }) => [id, turn])).toEqual([
    ["model:0:0", 0],
    ["model:1:0", 0],
    ["result:0", 0],
    ["model:2:0", 1],
    ["result:1", 1],
  ]);
});
test("historical Python tool calls retain Python highlighting", () => {
  const entries = activityEntries({
    models: [
      {
        id: 0,
        turn: 0,
        status: "accepted",
        tokens: null,
        parts: {
          0: {
            kind: "tool",
            name: "execute_python",
            text: JSON.stringify({ code: "await press(\"l\")" }),
            truncated: false,
          },
        },
      },
    ],
    submissions: [],
  });
  expect(entries[0]?.format).toBe("python");
});
test("dashboard has no headings and controls stay inside sidebar", async () => {
  const html = await Bun.file(new URL("./index.html", import.meta.url)).text();
  expect(html).not.toMatch(/<(header|h[1-6]|nav)\b/);
  expect(
    html.slice(html.indexOf("<aside"), html.indexOf("</aside>")),
  ).toContain('id="start"');
});
test("shell pane exposes accessible next-run settings without a keypress limit", async () => {
  const html = await Bun.file(new URL("./index.html", import.meta.url)).text();
  const pane = html.slice(html.indexOf('<section class="shell-pane"'), html.indexOf('</section>', html.indexOf('<section class="shell-pane"')));
  expect(pane).toContain('id="shell-tab" role="tab" aria-selected="true" aria-controls="shell-view"');
  expect(pane).toContain('id="settings-tab" role="tab" aria-selected="false" aria-controls="settings-view"');
  expect(pane).toContain('id="settings-view" role="tabpanel" aria-labelledby="settings-tab" aria-hidden="true" inert');
  expect(pane).toContain('name="model" type="text"');
  expect(pane).toContain('name="reasoning_effort"');
  expect(pane).toContain('name="max_turns" type="number" min="0"');
  expect(pane).not.toContain('max_steps');
  expect(pane).not.toContain('type="submit"');
});
