import tables from "./unicode-widths.json";

// Unicode 17 tables from wcwidth 0.8.3, also used by the Python terminal.
// See unicode-widths.LICENSE. Regenerate these alongside parser Unicode upgrades.
function contains(ranges: number[][], code: number): boolean {
  let low = 0, high = ranges.length - 1;
  while (low <= high) {
    const middle = (low + high) >>> 1;
    const [first, last] = ranges[middle]!;
    if (code < first!) high = middle - 1;
    else if (code > last!) low = middle + 1;
    else return true;
  }
  return false;
}
export function cellWidth(code: number): 0 | 1 | 2 {
  if (code >= 32 && code < 127) return 1;
  if (code < 32 || (code >= 127 && code < 160) || contains(tables.zero, code)) return 0;
  return contains(tables.wide, code) ? 2 : 1;
}
export function lineCells(line: string) {
  const cells: { text: string; column: number; width: number }[] = [];
  let column = 0;
  for (const text of line) {
    const width = cellWidth(text.codePointAt(0)!);
    if (!width && cells.length) cells[cells.length - 1]!.text += text;
    else { cells.push({ text, column, width }); column += width; }
  }
  return cells;
}

/** xterm 6 provider: use the same character widths as the Python screen. */
export const terminalUnicode = {
  version: tables.version,
  wcwidth: cellWidth,
  charProperties(codepoint: number, preceding: number) {
    const width = cellWidth(codepoint);
    const previousWidth = (preceding >> 1) & 3;
    const join = width === 0 && previousWidth > 0;
    return ((join ? previousWidth : width) << 1) | (join ? 1 : 0);
  },
};
