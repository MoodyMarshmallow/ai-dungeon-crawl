import { expect, test } from "bun:test";
import { executionOutputHtml, parseOutputObservation, finalObservationHtml, finalResultHtml } from "./observation-output";
import { shellTranscript } from "./transcript";

const plain = { fg: "default", bg: "default", bold: false, italics: false, underline: false, reverse: false, blink: false, cursor: false };
test("captured final screens render without observe output and escape unsafe content", () => {
  const html = finalObservationHtml({ id: 3, screen: '<界', ended: false, width: 3, height: 1,
    cursor: [0, 1], styles: [{ row: 0, col: 0, length: 3, ...plain, fg: 'red' }] });
  expect(html).toContain('observation-screen');
  expect(html).toContain('&lt;');
  expect(html).toContain('observation-cursor');
  expect(html).toContain('color:#cc0000');
  const unsafe = finalObservationHtml({ id: 1, ended: false, screen: 'x',
    styles: [{ row: 0, col: 0, length: 1, ...plain, fg: '\"><img src=x>' }] });
  expect(unsafe).not.toContain('<img');
});
function fixture() {
  return { id: 4, ended: false, width: 5, height: 1, rows: ["a界é "],
    palette: [plain, { ...plain, fg: "brightred", bg: "123abc", bold: true, italics: true, underline: true, reverse: true, blink: true }, { ...plain, cursor: true }],
    styles: [[0, 1, 2, 2, 0]] };
}
test("sidebar applies palette attributes by terminal columns and outlines wide and combining cursors", () => {
  const html = executionOutputHtml(JSON.stringify(fixture()));
  expect(html).toContain('class="observation-screen"');
  expect(html).toContain('class="observation-cell observation-cursor observation-blink" style="width:2ch;color:#123abc;background-color:#ef2929;font-weight:700;font-style:italic;text-decoration:underline">界</span>');
  expect(html).toContain('class="observation-cell observation-cursor" style="width:1ch');
  expect(html).toContain('>é</span>');
  expect(html).not.toContain('"palette"');
});
test("multiple observations interleaved with plain output preserve malformed and streaming tails", () => {
  const raw = JSON.stringify(fixture());
  const tail = '{"id":4,"rows":[';
  const html = executionOutputHtml(`before <tag>\n${raw}\nmiddle\n${raw}\n${tail}`);
  expect(html.match(/<pre /g)?.length).toBe(2);
  expect(html).toStartWith("before &lt;tag&gt;\n");
  expect(html).toContain("\nmiddle\n");
  expect(html).toEndWith('{&quot;id&quot;:4,&quot;rows&quot;:[');
  expect(executionOutputHtml(raw.slice(0, -1))).not.toContain("<pre");
  expect(executionOutputHtml(`${tail}\n[Output truncated]`)).toEndWith("\n[Output truncated]");
});
test("journal screen metadata preserves rendering while unknown or invalid fields stay raw", () => {
  for (const timestamp of [null, 0, 17]) {
    const raw = JSON.stringify({ ...fixture(), timestamp, sequence: 12 });
    expect(parseOutputObservation(raw)).not.toBeNull();
    expect(executionOutputHtml(raw)).toContain('class="observation-screen"');
  }
  for (const metadata of [{ timestamp: "today", sequence: 1 }, { timestamp: 0, sequence: -1 },
      { timestamp: 0 }, { timestamp: 0, sequence: 1, real_timestamp: "now" }]) {
    expect(parseOutputObservation(JSON.stringify({ ...fixture(), ...metadata }))).toBeNull();
  }
});
test("only strict complete observations render, including valid dimensions and safe palettes", () => {
  const invalid = [
    { ...fixture(), extra: true }, { ...fixture(), width: 6 }, { ...fixture(), height: 2 },
    { ...fixture(), ended: 1 }, { ...fixture(), id: -1 },
    { ...fixture(), rows: ["a\t界é "] }, { ...fixture(), styles: [[0, 1, 0, 0, 8]] },
    { ...fixture(), styles: [[0, 1]] }, { ...fixture(), palette: [{ ...plain, cursor: "true" }] },
    { ...fixture(), palette: [{ ...plain, fg: 'red; background:url(https://evil)' }] },
    { ...fixture(), palette: [{ ...plain, fg: '__proto__' }] },
    { ...fixture(), palette: [{ ...plain, bg: '"><img src=x>' }] },
    { id: 2, screen: "old format" }, { rows: ["arbitrary JSON"] },
  ];
  for (const value of invalid) {
    const raw = JSON.stringify(value);
    expect(parseOutputObservation(raw)).toBeNull();
    expect(executionOutputHtml(raw)).not.toContain("<pre");
    expect(executionOutputHtml(raw)).not.toContain("<img");
  }
});
test("screen text is escaped and the shell continues to show exact raw JSON", () => {
  const value = { ...fixture(), width: 3, rows: ["<&>"], palette: [plain], styles: [[0, 0, 0]] };
  const raw = JSON.stringify(value);
  const html = executionOutputHtml(raw);
  expect(html).toContain("&lt;</span>");
  expect(html).toContain("&amp;</span>");
  expect(html).toContain("&gt;</span>");
  const submission = { id: 0, code: "crawl observe", output: raw + "\n", error: null, status: "ok" as const, model_requests: 1, output_truncated: false, keys: [] };
  expect(shellTranscript([submission])).toBe(`$ crawl observe\n${raw}\n$ `);
  expect(shellTranscript([submission], true)).toContain(raw);
  expect(submission.output).toBe(raw + "\n");
});
test("final result shows one automatic screen while raw observe output and errors stay available", () => {
  const first = JSON.stringify(fixture());
  const second = JSON.stringify({ ...fixture(), id: 5, rows: ["later"] });
  const output = `${first}\n${second}\n`;
  const observation = { id: 6, screen: "final", ended: false };
  const html = finalResultHtml(observation, "error", "boom <tag>", false);
  expect(html.match(/class="observation-screen"/g)?.length).toBe(1);
  expect(html).not.toContain("a界é");
  expect(html).toContain("boom &lt;tag&gt;");
  const submission = { id: 0, code: "crawl observe; crawl observe", output, error: "boom <tag>", status: "error" as const, model_requests: 1, output_truncated: false, keys: [], observation };
  expect(shellTranscript([submission])).toContain(output);
  expect(shellTranscript([submission])).toContain("boom <tag>");
});
