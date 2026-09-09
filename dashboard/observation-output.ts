import { lineCells } from "./unicode";

const colors: Record<string, string> = {
  black: "#000000", red: "#cc0000", green: "#4e9a06", brown: "#c4a000",
  blue: "#3465a4", magenta: "#75507b", cyan: "#06989a", white: "#d3d7cf",
  brightblack: "#555753", brightred: "#ef2929", brightgreen: "#8ae234",
  brightbrown: "#fce94f", brightblue: "#729fcf", brightmagenta: "#ad7fa8",
  brightcyan: "#34e2e2", brightwhite: "#eeeeec",
};
const flags = ["bold", "italics", "underline", "reverse", "blink", "cursor"] as const;
type Palette = Record<typeof flags[number], boolean> & { fg: string; bg: string };
interface OutputObservation {
  id: number; ended: boolean; width: number; height: number;
  rows: string[]; palette: Palette[]; styles: number[][];
}
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function keys(value: Record<string, unknown>, names: readonly string[]) {
  return Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
}
function validColor(value: unknown): value is string {
  return typeof value === "string" && (value === "default" || Object.hasOwn(colors, value) || /^[0-9a-f]{6}$/i.test(value));
}
/** Recognize only the complete CLI schema. Invalid or unfinished lines stay raw. */
export function parseOutputObservation(source: string): OutputObservation | null {
  if (!source.trimStart().startsWith("{")) return null;
  try {
    const value: unknown = JSON.parse(source);
    if (!record(value) || !keys(value, ["id", "ended", "width", "height", "rows", "palette", "styles"])) return null;
    const { id, ended, width, height, rows, palette, styles } = value;
    if (!Number.isSafeInteger(id) || (id as number) < 0 || typeof ended !== "boolean" ||
        !Number.isSafeInteger(width) || !Number.isSafeInteger(height) ||
        (width as number) < 1 || (height as number) < 1 || (width as number) * (height as number) > 250000) return null;
    if (!Array.isArray(rows) || rows.length !== height || !Array.isArray(styles) || styles.length !== height ||
        !Array.isArray(palette) || !palette.length || palette.length > (width as number) * (height as number)) return null;
    if (!palette.every(style => record(style) && keys(style, ["fg", "bg", ...flags]) &&
        validColor(style.fg) && validColor(style.bg) && flags.every(flag => typeof style[flag] === "boolean"))) return null;
    if (!rows.every(row => typeof row === "string" && !/[\x00-\x1f\x7f-\x9f]/.test(row) &&
        lineCells(row).reduce((total, cell) => total + cell.width, 0) === width)) return null;
    if (!styles.every(row => Array.isArray(row) && row.length === width &&
        row.every(index => Number.isInteger(index) && index >= 0 && index < palette.length))) return null;
    return value as unknown as OutputObservation;
  } catch { return null; }
}
function escape(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function color(value: string, fallback: string): string {
  return value === "default" ? fallback : colors[value] ?? `#${value}`;
}
function screenHtml(value: OutputObservation): string {
  const rows = value.rows.map((row, index) => lineCells(row).map(cell => {
    const style = value.palette[value.styles[index]![cell.column]!]!;
    let fg = color(style.fg, "#dedbd4"), bg = color(style.bg, "#000000");
    if (style.reverse) [fg, bg] = [bg, fg];
    const cursor = value.styles[index]!.slice(cell.column, cell.column + Math.max(1, cell.width)).some(id => value.palette[id]!.cursor);
    const css = `width:${cell.width}ch;color:${fg};background-color:${bg};font-weight:${style.bold ? 700 : 400};font-style:${style.italics ? "italic" : "normal"};text-decoration:${style.underline ? "underline" : "none"}`;
    return `<span class="observation-cell${cursor ? " observation-cursor" : ""}${style.blink ? " observation-blink" : ""}" style="${css}">${escape(cell.text)}</span>`;
  }).join("")).join("\n");
  return `<pre class="observation-screen">${rows}</pre>`;
}
/** Rendering is confined to the sidebar; the source and shell transcript are untouched. */
export function executionOutputHtml(source: string): string {
  return source.split("\n").map(line => {
    const observation = parseOutputObservation(line);
    return observation ? screenHtml(observation) : escape(line);
  }).join("\n");
}
