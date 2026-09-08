import { expect, test } from "bun:test";
import { codeHtml, markdownHtml, pythonAnsi } from "./rendering";
import { shellTranscript } from "./transcript";
import { dashboardOptions } from "./server";

test("dashboard defaults to Codex and rejects the scripted launch option", () => {
  const { config, port } = dashboardOptions([]);
  expect(config.backend).toBe("codex");
  expect(config.model).toBe("gpt-5.6-luna");
  expect(config.reasoning_summary).toBe(true);
  expect(port).toBe(8765);
  expect(() => dashboardOptions(["--policy", "scripted"])).toThrow();
  expect(() => dashboardOptions(["--policy", "pydantic"])).toThrow();
  expect(
    dashboardOptions(["--policy", "pydantic", "--model", "test:model"]).config
      .reasoning_summary,
  ).toBe(false);
});

test("Markdown formats reasoning and fenced Python without executing HTML", () => {
  const html = markdownHtml(
    '**Thinking**\n\n- first\n- second\n\n```python\nawait press("l")\n```',
  );
  expect(html).toContain("<strong>Thinking</strong>");
  expect(html).toContain("<ul>");
  expect(html).toContain('class="token keyword"');
  const unsafe = markdownHtml(
    "<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n![image](https://example.com/pixel)",
  );
  expect(unsafe).not.toContain("<script");
  expect(unsafe).not.toContain('href="javascript:');
  expect(unsafe).not.toContain("<img");
  expect(codeHtml("<img src=x onerror=alert(1)>")).not.toContain("<img");
});

test("partial code highlights safely and terminal ANSI is renderer-owned", () => {
  expect(codeHtml('await press("', "python")).toContain('class="token keyword"');
  const source = 'await press("l")\n# comment';
  const colored = pythonAnsi(source);
  expect(colored).toContain("\x1b[38;2;");
  expect(colored.replace(/\x1b\[[0-9;]*m/g, "")).toBe(source);
  const transcript = shellTranscript(
    [
      {
        id: 0,
        code: source + "\x1b]52;secret\x07",
        output: "\x1b[2Junsafe",
        error: null,
        status: "ok",
        model_requests: 1,
        output_truncated: false,
        keys: [],
      },
    ],
    true,
  );
  expect(transcript.replace(/\x1b\[[0-9;]*m/g, "")).not.toMatch(
    /[\x00-\x08\x0b-\x1f\x7f-\x9f]/,
  );
});
