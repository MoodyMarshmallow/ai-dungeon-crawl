import { expect, test } from "bun:test";
import { replTranscript, activityEntries } from "./transcript";
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
test("REPL is prompts, code and output without turn labels", () => {
  expect(replTranscript([])).toBe(">>> ");
  expect(replTranscript([submission])).toBe(">>> x = 1\n... print(x)\n1\n>>> ");
  expect(replTranscript([{ ...submission, status: "running" }])).toEndWith(
    "1\n",
  );
});
test("execution errors and truncation are not hidden", () => {
  expect(
    replTranscript([
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
            name: "execute_python",
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
});
test("dashboard has no headings and controls stay inside sidebar", async () => {
  const html = await Bun.file(new URL("./index.html", import.meta.url)).text();
  expect(html).not.toMatch(/<(header|h[1-6]|nav)\b/);
  expect(
    html.slice(html.indexOf("<aside"), html.indexOf("</aside>")),
  ).toContain('id="start"');
});
