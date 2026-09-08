import type { Observation, ScreenStyle } from "./types";
import { terminalText } from "./rendering";
import { lineCells } from "./unicode";

const palette = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"];
function color(value: string, background: boolean): string {
  const index = palette.indexOf(value);
  if (index >= 0) return String((background ? 40 : 30) + index);
  const bright = palette.indexOf(value.replace(/^bright/, ""));
  if (value.startsWith("bright") && bright >= 0) return String((background ? 100 : 90) + bright);
  if (/^[0-9a-f]{6}$/i.test(value)) {
    return `${background ? 48 : 38};2;${[0, 2, 4].map((offset) => parseInt(value.slice(offset, offset + 2), 16)).join(";")}`;
  }
  return background ? "49" : "39";
}
function attributes(style: ScreenStyle): string {
  const codes = ["0", color(style.fg, false), color(style.bg, true)];
  for (const [enabled, code] of [[style.bold, 1], [style.italics, 3], [style.underline, 4], [style.blink, 5], [style.reverse, 7]]) {
    if (enabled) codes.push(String(code));
  }
  return `\x1b[${codes.join(";")}m`;
}

/** Only terminal visual attributes become ANSI; all source text is sanitized. */
export function screenAnsi(observation: Observation): string {
  const lines = terminalText(observation.screen).split("\n");
  const runs = new Map<number, ScreenStyle[]>();
  for (const style of observation.styles ?? []) {
    if (!Number.isInteger(style.row) || !Number.isInteger(style.col) || !Number.isInteger(style.length) || style.row < 0 || style.col < 0 || style.length < 1) continue;
    const row = runs.get(style.row) ?? [];
    row.push(style);
    runs.set(style.row, row);
  }
  return lines.map((line, index) => {
    const cells = lineCells(line);
    const styles = (runs.get(index) ?? []).sort((a, b) => a.col - b.col);
    let result = "", active = "", run = 0;
    for (const cell of cells) {
      while (run < styles.length && styles[run]!.col + styles[run]!.length <= cell.column) run++;
      const style = styles[run];
      const next = style && cell.column >= style.col ? attributes(style) : "";
      if (next !== active) result += next || "\x1b[0m";
      result += cell.text;
      active = next;
    }
    return result + (active ? "\x1b[0m" : "");
  }).join("\n");
}
