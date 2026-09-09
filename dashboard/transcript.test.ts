import { expect, test } from "bun:test";
import { shellTranscript, activityEntries, turnSummary } from "./transcript";
import type { ModelRequest } from "./types";
import type { Submission } from "./types";
import { runInNewContext } from "node:vm";

test("new latest turns open and collapse their predecessor without changing older choices", async () => {
  const app = await Bun.file(new URL("./app.ts", import.meta.url)).text();
  const registration = app.slice(app.indexOf("const latestTurn ="), app.indexOf("turns.set(item.turn, group);") + "turns.set(item.turn, group);".length);
  const turns = new Map<number, { open: boolean }>();
  const add = (turn: number) => {
    const group = { open: false };
    runInNewContext(registration, { turns, item: { turn }, group });
    return group;
  };
  const first = add(0);
  expect(first.open).toBe(true);
  const second = add(1);
  expect(first.open).toBe(false);
  expect(second.open).toBe(true);
  first.open = true;
  const third = add(2);
  expect(first.open).toBe(true);
  expect(second.open).toBe(false);
  expect(third.open).toBe(true);
  expect(add(-1).open).toBe(false);
  expect(third.open).toBe(true);
});

test("turn headings have a separate collapsed-only preview", async () => {
  const app = await Bun.file(new URL("./app.ts", import.meta.url)).text();
  const css = await Bun.file(new URL("./style.css", import.meta.url)).text();
  expect(app).toContain('number.className = "turn-number"');
  expect(app).toContain('heading.className = "turn-heading-preview"');
  expect(app).toContain('title.append(number, heading)');
  expect(css).toContain('.activity-turn[open] > .turn-title > .turn-heading-preview { display: none; }');
});

test("turn summaries follow the first reasoning header and tolerate streaming", () => {
  const model: ModelRequest = { id: 0, turn: 1, status: "running", tokens: null, parts: {} };
  const state = { models: [model] };
  expect(turnSummary(state, 1)).toBe("Turn 2");
  model.parts[0] = { kind: "tool", name: "execute_shell", text: "not reasoning", truncated: false };
  model.parts[1] = { kind: "reasoning", name: "", text: "\n**Searching manual for key play commands**\n\nBody", truncated: false };
  model.parts[2] = { kind: "reasoning", name: "", text: "Later thought", truncated: false };
  expect(turnSummary(state, 1)).toBe("Turn 2: Searching manual for key play commands");
  expect(turnSummary(state, 0)).toBe("Turn 1");
  model.parts[1].text = "## Searching";
  expect(turnSummary(state, 1)).toBe("Turn 2: Searching");
  model.parts[1].text = "**";
  expect(turnSummary(state, 1)).toBe("Turn 2: Later thought");
});
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
  expect(pane).toContain('<select id="setting-model" name="model"');
  expect(pane).toContain('name="reasoning_effort"');
  expect(pane).toContain('name="max_turns" type="number" min="0"');
  expect(pane).not.toContain('max_steps');
  expect(pane).not.toContain('type="submit"');
});
