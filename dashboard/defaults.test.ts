import { expect, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadDefaults, saveDefaults, validateDefaults, type Settings } from "./defaults";
import { Dashboard, dashboardOptions, startServer } from "./server";

test("saved defaults survive reload and explicit CLI options win", () => {
  const directory = mkdtempSync(join(tmpdir(), "crawl-defaults-"));
  try {
    const path = join(directory, "defaults.json");
    expect(loadDefaults(path)).toBeUndefined();
    expect(dashboardOptions([]).config.reasoning_effort).toBe("low");
    const settings: Settings = { model: "gpt-5.6-sol", reasoning_effort: "high", max_turns: 20 };
    saveDefaults(path, settings);
    expect(loadDefaults(path)).toEqual(validateDefaults(settings));
    expect(dashboardOptions([], loadDefaults(path)).config).toMatchObject(settings);
    expect(dashboardOptions(["--model", "gpt-5.6-terra", "--reasoning-effort", "low", "--max-turns", "8"], loadDefaults(path)).config)
      .toMatchObject({ model: "gpt-5.6-terra", reasoning_effort: "low", max_turns: 8 });
    for (const invalid of [{ ...settings, reasoning_effort: "default" }, { ...settings, max_turns: -1 },
                          { ...settings, model: "invalid" }, { ...settings, extra: true }])
      expect(() => saveDefaults(path, invalid)).toThrow();
    expect(JSON.parse(readFileSync(path, "utf8"))).toEqual(validateDefaults(settings));
  } finally { rmSync(directory, { recursive: true }); }
});

test("save defaults endpoint uses control safeguards and updates next-run settings without starting", async () => {
  const directory = mkdtempSync(join(tmpdir(), "crawl-defaults-"));
  const path = join(directory, "defaults.json");
  const dashboard = new Dashboard(dashboardOptions([]).config);
  const server = await startServer(dashboard, 0, path);
  const url = `http://127.0.0.1:${server.port}/defaults`;
  const settings: Settings = { model: "gpt-5.6-terra", reasoning_effort: "medium", max_turns: 7 };
  const request = (headers: Record<string, string>, body = settings) => fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body),
  });
  try {
    expect((await request({})).status).toBe(403);
    expect((await request({ "X-Dashboard-Request": "1", Origin: "https://elsewhere.example" })).status).toBe(403);
    expect(loadDefaults(path)).toBeUndefined();
    expect((await request({ "X-Dashboard-Request": "1" })).status).toBe(200);
    expect(loadDefaults(path)).toEqual(validateDefaults(settings));
    expect(dashboard.config.model).toBe("gpt-5.6-terra");
    expect(dashboard.child).toBeNull();
    expect((await request({ "X-Dashboard-Request": "1" }, { ...settings, max_turns: -1 })).status).toBe(400);
    expect(loadDefaults(path)).toEqual(validateDefaults(settings));
  } finally { await server.stop(true); rmSync(directory, { recursive: true }); }
});

test("settings expose named models, explicit Low, and a default-save button", async () => {
  const html = await Bun.file(join(import.meta.dir, "index.html")).text();
  expect(html).toContain('<select id="setting-model"');
  for (const name of ["Luna", "Terra", "Sol"]) expect(html).toContain(`>${name}</option>`);
  expect(html).not.toContain('<option value="default">');
  expect(html).toContain('<option value="low" selected>');
  expect(html).toContain('id="save-defaults"');
});
