import { afterEach, describe, expect, test } from "bun:test";
import { Dashboard, dashboardOptions, startServer } from "./server";
import { applyEvent, emptyState } from "./state";
import type { Config } from "./types";

const config: Config = {
  backend: "pydantic",
  model: "invalid-test-provider:model",
  reasoning_effort: "default",
  max_turns: 3,
  game: "mock",
  reasoning_summary: false,
};

test("dashboard launches actual Crawl by default and permits explicit mock development", () => {
  expect(dashboardOptions([]).config.game).toBe("dcss");
  expect(dashboardOptions(["--game", "mock"]).config.game).toBe("mock");
  expect(dashboardOptions(["--crawl-path", "/tmp/crawl-custom"]).config.crawl_path).toBe("/tmp/crawl-custom");
  expect(dashboardOptions(["--manual-path", "/tmp/manual-custom.rst"]).config.manual_path).toBe("/tmp/manual-custom.rst");
  expect(() => dashboardOptions(["--game", "unknown"])).toThrow();
  expect(dashboardOptions(["--reasoning-effort", "high", "--max-turns", "5"]).config.reasoning_effort).toBe("high");
  expect(() => dashboardOptions(["--reasoning-effort", "bogus"])).toThrow();
  expect(() => dashboardOptions(["--max-steps", "5"])).toThrow();
  expect(() => dashboardOptions(["--max-turns", "-1"])).toThrow();
});

describe("display state", () => {
  test("assembles tool previews without making them shell submissions", () => {
    const state = emptyState(config);
    applyEvent(state, { event: "model.started", data: {} });
    applyEvent(state, {
      event: "model.part",
      data: {
        index: 0,
        kind: "tool",
        name: "execute_shell",
        text: '{"code":"pa',
        replace: true,
      },
    });
    expect(state.submissions).toHaveLength(0);
    applyEvent(state, {
      event: "model.part",
      data: { index: 0, kind: "tool", text: 'ss"}', replace: false },
    });
    expect(state.models[0].parts["0"].text).toBe('{"code":"pass"}');
    applyEvent(state, {
      event: "execution.submitted",
      data: { id: 0, code: "pass", model_requests: 1 },
    });
    expect(state.submissions).toHaveLength(1);
    expect(state.models[0].status).toBe("accepted");
  });
  test("caps retained requests and preview length", () => {
    const state = emptyState(config);
    for (let i = 0; i < 40; i++) {
      applyEvent(state, { event: "model.started", data: {} });
      applyEvent(state, {
        event: "model.part",
        data: {
          index: 0,
          kind: "reasoning",
          text: "x".repeat(70000),
          replace: true,
        },
      });
    }
    expect(state.models).toHaveLength(30);
    expect(state.models[0].parts["0"].text).toHaveLength(65536);
    expect(state.models[0].parts["0"].truncated).toBe(true);
  });
  test("retains partial output and marks interrupted execution on stop", () => {
    const state = emptyState(config);
    applyEvent(state, {
      event: "execution.submitted",
      data: { id: 0, code: "pass", model_requests: 0 },
    });
    applyEvent(state, {
      event: "execution.output",
      data: { text: "<script>text, not HTML</script>" },
    });
    applyEvent(state, {
      event: "episode.finished",
      data: { status: "stopped", stop_reason: "cancelled" },
    });
    expect(state.submissions[0].status).toBe("interrupted");
    expect(state.submissions[0].output).toContain("<script>");
    expect(state.finished_at).not.toBeNull();
  });
});

describe("localhost server and Python bridge", () => {
  let server: Awaited<ReturnType<typeof startServer>> | null = null;
  let dashboard: Dashboard;
  afterEach(async () => {
    dashboard?.stop();
    await dashboard?.completion;
    await server?.stop(true);
  });

  async function setup() {
    dashboard = new Dashboard(config);
    server = await startServer(dashboard, 0);
    return `http://127.0.0.1:${server.port}`;
  }
  test("settings validate atomically, update state, and use control safeguards", async () => {
    const base = await setup();
    const settings = { model: "openai:new-model", reasoning_effort: "high", max_turns: 5 };
    const post = (body: unknown, headers = { "X-Dashboard-Request": "1" }) => fetch(base + "/config", {
      method: "POST", headers, body: JSON.stringify(body),
    });
    expect((await post(settings, {} as never)).status).toBe(403);
    expect((await post(settings, { "X-Dashboard-Request": "1", Origin: "https://evil.example" } as never)).status).toBe(403);
    const before = dashboard.revision;
    for (const invalid of [null, [], {}, { ...settings, model: " " }, { ...settings, model: "bad\nmodel" },
      { ...settings, reasoning_effort: "bogus" }, { ...settings, model: "anthropic:example" }, { ...settings, max_turns: -1 },
      { ...settings, max_turns: 1.5 }, { ...settings, max_turns: "5" }, { ...settings, extra: true }]) {
      expect((await post(invalid)).status).toBe(400);
      expect(dashboard.config).toEqual(config);
      expect(dashboard.revision).toBe(before);
    }
    expect((await post(settings)).status).toBe(200);
    expect(dashboard.config).toMatchObject(settings);
    expect(dashboard.state.config).toEqual(dashboard.config);
    expect(dashboard.revision).toBe(before + 1);
    expect(dashboard.child).toBeNull();
    dashboard.state.status = "running";
    expect((await post({ ...settings, max_turns: 10 })).status).toBe(409);
    expect(dashboard.config.max_turns).toBe(5);
    dashboard.state.status = "idle";
  });
  test("rejects cross-site controls and DNS rebinding", async () => {
    const base = await setup();
    expect((await fetch(base + "/run", { method: "POST" })).status).toBe(403);
    expect(
      (
        await fetch(base + "/run", {
          method: "POST",
          headers: {
            "X-Dashboard-Request": "1",
            Origin: "https://evil.example",
          },
        })
      ).status,
    ).toBe(403);
    expect(
      (await fetch(base + "/state", { headers: { Host: "evil.example" } }))
        .status,
    ).toBe(403);
    expect(dashboard.child).toBeNull();
    const page = await fetch(base);
    expect(page.headers.get("Content-Security-Policy")).toContain(
      "frame-ancestors 'none'",
    );
    expect((await fetch(base + "/assets/app.js")).status).toBe(200);
    expect((await fetch(base + "/assets/xterm.css")).status).toBe(200);
    expect((await fetch(base + "/assets/sans-400.woff2")).status).toBe(200);
    expect((await fetch(base + "/assets/mono-400.woff2")).status).toBe(200);
    expect((await fetch(base + "/assets/unknown")).status).toBe(404);
    expect((await fetch(base + "/tiles/?path=/etc/passwd")).status).toBe(200);
    expect((await fetch(base + "/tiles/static/.env")).status).toBe(404);
    const viewer = await fetch(base + "/tiles/");
    expect(viewer.headers.get("Content-Security-Policy")).toContain("frame-ancestors 'self'");
    expect((await fetch(base + "/tiles/input", { method: "POST", headers: { "X-Dashboard-Request": "1" } })).status).toBe(404);
  });
  test("SSE reconnect gets current state without starting a run", async () => {
    const base = await setup();
    for (const screen of ["first", "second"]) {
      dashboard.accept({
        event: "game.observation",
        data: { id: 1, screen, ended: false },
      });
      const abort = new AbortController();
      const response = await fetch(base + "/events", { signal: abort.signal });
      const reader = response.body!.getReader();
      const chunk = await reader.read();
      expect(new TextDecoder().decode(chunk.value)).toContain(screen);
      await reader.cancel();
      abort.abort();
    }
    expect(dashboard.child).toBeNull();
  });
  test.skipIf(process.platform !== "darwin")(
    "reports Python provider setup failure and allows a fresh run without network calls",
    async () => {
      const base = await setup();
      for (let run = 1; run <= 2; run++) {
        const response = await fetch(base + "/run", {
          method: "POST",
          headers: { "X-Dashboard-Request": "1" },
        });
        expect(response.status).toBe(202);
        expect(dashboard.start()).toBe(false);
        await dashboard.completion;
        expect(dashboard.state.status).toBe("error");
        expect(dashboard.state.run_id).toBe(run);
        expect(dashboard.state.actions).toBe(0);
        expect(dashboard.state.submissions).toHaveLength(0);
        expect(dashboard.state.error).toBeTruthy();
      }
    },
    15000,
  );
  test("stopping during startup releases the process and allows restart", async () => {
    await setup();
    dashboard.start();
    dashboard.stop();
    await dashboard.completion;
    expect(dashboard.child).toBeNull();
    expect(dashboard.state.status).toBe("stopped");
  }, 10000);
});
