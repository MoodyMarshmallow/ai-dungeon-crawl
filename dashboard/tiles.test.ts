import { expect, test } from "bun:test";
import { TileJournal, tileEvents, tileAssets, tilePage, tileScript, tileStyle } from "./tiles";
import { screenAnsi } from "./screen";
import { cellWidth, terminalUnicode } from "./unicode";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

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
