import { expect, test } from "bun:test";
import { TileJournal, tileEvents, tileAssets, tilePage, tileScript, tileStyle } from "./tiles";
import { screenAnsi } from "./screen";
import { cellWidth, terminalUnicode } from "./unicode";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";

type RendererMessage = { msg: string; [key: string]: any };

/** Execute the shipped browser script, including EventSource and multi dispatch. */
async function viewerHarness() {
  const menus: RendererMessage[] = [];
  const errors: unknown[] = [];
  const delivered: RendererMessage[] = [];
  const status = { hidden: false, textContent: "" };
  let source!: { onmessage: (event: { data: string }) => void; closed: boolean; close(): void };
  const handlers: Record<string, (message: RendererMessage) => void> = {
    menu(message) { if (message.replace) menus.pop(); menus.push(message); },
    close_menu() { menus.pop(); },
    close_all_menus() { menus.length = 0; },
    menu_scroll(message) {
      // The official renderer assumes a menu exists at this assignment.
      menus[menus.length - 1]!.server_first_visible = message.first;
    },
    map(message) { if (message.broken) throw new Error("broken map renderer"); },
  };
  const comm = {
    register_handlers(value: typeof handlers) { Object.assign(handlers, value); },
    handle_message_immediately() { return false; },
    handle_message(message: RendererMessage) { delivered.push(message); handlers[message.msg]?.(message); },
  };
  const element = { setAttribute() {}, appendChild() {}, replaceChildren() {} };
  const jquery = () => ({ trigger() {} });
  const require = Object.assign((_dependencies: string[], callback: Function) => callback(jquery, comm, {}, { inv: {} }, {}, {}, {}), { config() {} });
  runInNewContext(tileScript, {
    require, define() {}, window: {},
    document: { querySelectorAll: () => [], createElement: () => element, getElementById: (id: string) => id === "viewer-status" ? status : element },
    console: { error: (error: unknown) => errors.push(error) },
    EventSource: class {
      onmessage!: (event: { data: string }) => void;
      closed = false;
      constructor() { source = this; }
      close() { this.closed = true; }
    },
  });
  // The browser waits for image decoding before opening its event stream.
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { menus, errors, delivered, status, get closed() { return source.closed; }, send(messages: RendererMessage[]) { source.onmessage({ data: JSON.stringify({ run_id: 1, messages }) }); } };
}

test("viewer survives the captured orphan scroll before an ability menu", async () => {
  const viewer = await viewerHarness();
  viewer.send([{ msg: "menu", tag: "previous" }, { msg: "close_menu" }]);
  viewer.send([{ msg: "menu_scroll", first: 0, last_hovered: 1, force: false }]);
  viewer.send([{ msg: "menu", tag: "ability" }, { msg: "close_menu" }, { msg: "close_all_menus" }, { msg: "map" }]);
  expect(viewer.errors).toEqual([]);
  expect(viewer.closed).toBe(false);
  expect(viewer.delivered.at(-1)?.msg).toBe("map");
  expect(viewer.delivered.filter((message) => message.msg === "menu_scroll")).toEqual([]);
});

test("viewer scroll guard respects nested menus, replacement, close-all, and multi", async () => {
  const viewer = await viewerHarness();
  viewer.send([{ msg: "multi", msgs: [
    { msg: "menu_scroll", first: 99, force: true },
    { msg: "menu", tag: "parent" },
    { msg: "menu", tag: "child" },
    { msg: "menu", tag: "replacement", replace: true },
    { msg: "menu_scroll", first: 4 },
  ] }]);
  expect(viewer.menus.map((menu) => [menu.tag, menu.server_first_visible])).toEqual([["parent", undefined], ["replacement", 4]]);
  viewer.send([{ msg: "close_menu" }, { msg: "menu_scroll", first: 2 }]);
  expect(viewer.menus.map((menu) => [menu.tag, menu.server_first_visible])).toEqual([["parent", 2]]);
  viewer.send([{ msg: "menu", tag: "child" }, { msg: "close_all_menus" }, { msg: "close_menu" }, { msg: "menu_scroll", first: 99 }]);
  // Replacement of an empty stack still opens a menu in the official renderer.
  viewer.send([{ msg: "menu", tag: "new", replace: true }, { msg: "menu_scroll", first: 3, force: true }]);
  expect(viewer.menus.map((menu) => [menu.tag, menu.server_first_visible])).toEqual([["new", 3]]);
  expect(viewer.errors).toEqual([]);
  expect(viewer.closed).toBe(false);
});

test("viewer still reports unrelated renderer failures and closes its stream", async () => {
  const viewer = await viewerHarness();
  viewer.send([{ msg: "map", broken: true }]);
  expect(viewer.errors.map(String)).toEqual(["Error: broken map renderer"]);
  expect(viewer.closed).toBe(true);
  expect(viewer.status.hidden).toBe(false);
  expect(viewer.status.textContent).toBe("Tiles could not render this game update.");
});

test("late tile viewers replay initial state and every ordered delta; reconnect resumes", async () => {
  const journal = new TileJournal();
  journal.reset(2);
  journal.append([{ msg: "map", clear: true, cells: [{ x: 1, y: 2, t: { fg: 9 } }] }]);
  journal.append([{ msg: "map", cells: [{ x: 2, y: 2, t: { fg: 10 } }] }]);
  const reader = tileEvents(journal, new Request("http://localhost/tiles/events"), () => {}).getReader();
  const decode = async () => new TextDecoder().decode((await reader.read()).value);
  expect(await decode()).toContain('"run_id":2');
  expect(await decode()).toContain('"clear":true');
  expect(await decode()).toContain('"fg":10');
  await reader.cancel();
  const reconnect = tileEvents(journal, new Request("http://localhost/tiles/events", { headers: { "Last-Event-ID": "2:1" } }), () => {}).getReader();
  await reconnect.read();
  const update = new TextDecoder().decode((await reconnect.read()).value);
  expect(update).toContain("id: 2:2");
  expect(update).not.toContain('"clear":true');
  expect(update).toContain('"fg":10');
  journal.reset(3);
  const reset = new TextDecoder().decode((await reconnect.read()).value);
  expect(reset).toContain('"run_id":3');
  expect(reset).not.toContain('"fg":10');
  await reconnect.cancel();
});

test("viewer only inventories static renderer assets and has no input bridge", () => {
  const directory = mkdtempSync(resolve(tmpdir(), "crawl-viewer-test-"));
  const previous = process.env.CRAWL_WEB_ASSETS;
  mkdirSync(resolve(directory, "static/scripts/contrib"), { recursive: true });
  writeFileSync(resolve(directory, "static/scripts/contrib/require.js"), "// fixture");
  writeFileSync(resolve(directory, "static/.env"), "secret");
  process.env.CRAWL_WEB_ASSETS = directory;
  const assets = tileAssets("/unused/crawl");
  if (previous === undefined) delete process.env.CRAWL_WEB_ASSETS;
  else process.env.CRAWL_WEB_ASSETS = previous;
  rmSync(directory, { recursive: true });
  expect(assets.files.has("/tiles/static/scripts/contrib/require.js")).toBe(true);
  expect(assets.files.has("/tiles/game/../../../../.env")).toBe(false);
  expect([...assets.files.keys()].every((path) => /\.(js|css|png|gif|woff2?|ttf|svg)$/.test(path))).toBe(true);
  expect(tileScript).toContain("comm.send_message=function(){}");
  expect(tileScript).not.toContain("new WebSocket");
  expect(tileScript).not.toContain("postMessage");
  expect(tileScript).not.toContain('"game/game"],async function');
  expect(tileScript).toContain("window.assert=function(){}");
  expect(tilePage("", false)).toContain("Tiles unavailable");
});

test("tiles iframe loads eagerly and is never initialized by tab selection", async () => {
  const html = await Bun.file(new URL("./index.html", import.meta.url)).text();
  const app = await Bun.file(new URL("./app.ts", import.meta.url)).text();
  const iframe = html.match(/<iframe\b[^>]*id="tiles-frame"[^>]*>/)?.[0];
  expect(iframe).toBeDefined();
  expect(iframe).toContain('src="/tiles/"');
  expect(iframe).not.toContain('loading="lazy"');
  expect(app).not.toContain('$("tiles-frame")');
  expect(app).not.toContain('frame.src = "/tiles/"');
});

test("tiles use fixed-size text and an upstream-rendered read-only inventory", () => {
  expect(tileStyle).toContain("background:#000");
  expect(tileStyle).toContain("font-size:13px!important");
  expect(tileStyle).toContain("overflow-y:auto");
  expect(tileScript).toContain("new cr.DungeonCellRenderer()");
  expect(tileScript).toContain("slot<52");
  expect(tileScript).toContain("canvas.title=label");
  expect(tileScript).toContain("renderInventory();");
  expect(tileScript).toContain("comm.send_message=function(){}");
});

test("style coordinates measure terminal columns through wide and combining characters", () => {
  const style = { row: 0, col: 2, length: 1, fg: "brightred", bg: "default", bold: false, italics: false, underline: false, reverse: false, blink: false };
  expect(screenAnsi({ id: 1, ended: false, screen: "界X     ", styles: [style] }))
    .toBe("界\x1b[0;91;49mX\x1b[0m     ");
  expect(screenAnsi({ id: 2, ended: false, screen: "e\u0301X", styles: [{ ...style, col: 1 }] }))
    .toBe("e\u0301\x1b[0;91;49mX\x1b[0m");
  expect(cellWidth("界".codePointAt(0)!)).toBe(2);
  expect(cellWidth(0x301)).toBe(0);
  expect(cellWidth(0x1f600)).toBe(2);
  expect(terminalUnicode.charProperties(0x301, terminalUnicode.charProperties(0x65, 0))).toBe(3);
});

test("screen visual runs keep colors and text without admitting control injection", () => {
  const ansi = screenAnsi({ id: 1, screen: "@ abc", ended: false, styles: [
    { row: 0, col: 0, length: 1, fg: "red", bg: "default", bold: true, italics: false, underline: false, reverse: false, blink: false },
    { row: 0, col: 2, length: 3, fg: "11aaee", bg: "\x1b]52;attack", bold: false, italics: false, underline: true, reverse: false, blink: false },
  ] });
  expect(ansi).toContain("\x1b[0;31;49;1m@\x1b[0m");
  expect(ansi).toContain("\x1b[0;38;2;17;170;238;49;4mabc\x1b[0m");
  expect(ansi).not.toContain("attack");
  expect(ansi.replace(/\x1b\[[0-9;]*m/g, "")).toBe("@ abc");
});
