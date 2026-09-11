import { expect, test } from "bun:test";
import { Dashboard, dashboardOptions } from "./server";
import { validateDefaults } from "./defaults";
import { activityEntries } from "./transcript";

test("phase color marks the full turn card rather than its counter", async () => {
  const app = await Bun.file(new URL("./app.ts", import.meta.url)).text();
  const css = await Bun.file(new URL("./style.css", import.meta.url)).text();
  expect(app).toContain('group.dataset.mode = mode');
  expect(css).toContain('.activity-turn { border-left: 3px solid #b26d23; padding-left: 12px; }');
  expect(css).toContain('.activity-turn[data-mode="review"] { border-left-color: #8534d6; }');
  expect(css).not.toContain('.turn-number { border-left:');
  expect(css).not.toMatch(/border-radius:\s*[1-9]/);
});

test("phase transitions tag requests, retain history, freeze review screen and reset the next game", () => {
  const dashboard = new Dashboard(dashboardOptions([]).config);
  const screen = { id: 1, screen: "final game", ended: true };
  dashboard.accept({ event: "game.observation", data: screen });
  dashboard.accept({ event: "game.tiles", data: { messages: [{ msg: "map" }] } });
  dashboard.accept({ event: "turn.started", data: { id: 0 } });
  dashboard.accept({ event: "model.started", data: {} });
  dashboard.accept({ event: "model.part", data: { index: 0, kind: "reasoning", text: "Action", replace: true } });
  dashboard.accept({ event: "mode.changed", data: { mode: "review", episode: 1 } });
  expect(dashboard.state.observation).toEqual(screen);
  expect(dashboard.tiles.batches).toHaveLength(1);
  dashboard.accept({ event: "turn.started", data: { id: 1 } });
  dashboard.accept({ event: "model.started", data: {} });
  dashboard.accept({ event: "execution.submitted", data: { id: 1, code: "ls", model_requests: 1 } });
  dashboard.accept({ event: "execution.finished", data: { id: 1, output: "notes", error: null, status: "ok", output_truncated: false, observation: screen } });
  expect(dashboard.state.models.map(model => [model.turn, model.mode, model.episode])).toEqual([[0, "action", 1], [1, "review", 1]]);
  expect(activityEntries(dashboard.state).at(-1)).toMatchObject({ mode: "review", episode: 1, text: "notes" });
  expect(dashboard.state.submissions[0]?.observation).toBeUndefined();
  const epoch = dashboard.tiles.run;
  dashboard.accept({ event: "mode.changed", data: { mode: "action", episode: 2 } });
  expect(dashboard.state.observation).toBeNull();
  expect(dashboard.tiles.run).toBeGreaterThan(epoch);
  expect(dashboard.tiles.batches).toHaveLength(0);
  expect(dashboard.state.models).toHaveLength(2);
});

test("new defaults and CLI limits round trip and legacy action limits migrate", () => {
  const settings = { model: "gpt-5.6-luna", reasoning_effort: "low" as const, action_turn_limit: 4, review_turn_limit: 2, episode_limit: 3 };
  expect(validateDefaults(settings)).toEqual(settings);
  expect(dashboardOptions([], settings).config).toMatchObject(settings);
  expect(dashboardOptions(["--action-turn-limit", "8", "--review-turn-limit", "0", "--episode-limit", "2"], settings).config)
    .toMatchObject({ action_turn_limit: 8, review_turn_limit: 0, episode_limit: 2 });
  expect(validateDefaults({ model: settings.model, reasoning_effort: "low", max_turns: 7 }))
    .toEqual({ ...settings, action_turn_limit: 7, review_turn_limit: 3, episode_limit: 1 });
  for (const invalid of [{ ...settings, episode_limit: 0 }, { ...settings, review_turn_limit: -1 }, { ...settings, action_turn_limit: 1.5 }])
    expect(() => validateDefaults(invalid)).toThrow();
});
