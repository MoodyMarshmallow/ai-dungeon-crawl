---
purpose: Explains the execute_shell tool to the action agent.
usage: The Markdown body accompanies each execute_shell tool definition; names, argument schema, validation, and sandbox limits remain fixed in code.
---
Submit one Bash script to end this agent turn. The harness
executes it once and returns its output, errors, and status.

Each call starts a fresh shell in the same workspace; files persist, variables
and cwd changes do not. Python and standard text tools are available. Prefer ripgrep (rg) for searching file contents when available. Execution
and output are bounded; network and personal-file access are blocked.
Optional timeout_ms sets the execution budget in milliseconds (1-180000).
Omit it or use null for the configured default (normally 5000 ms).
Time waiting for game keypress readiness does not consume this budget.

Commands:
- crawl press KEY: send one key, wait for readiness; silent on success.
  Keys: a printable character, ENTER, ESC, TAB, BACKSPACE, UP, DOWN, LEFT,
  RIGHT, or CTRL+A through CTRL+Z.
- crawl observe: read the latest logged screen as one JSON line, without game input.
  -n N returns the last N screens (1-100), oldest first. --since TICK and
  --until TICK filter inclusively by DCSS ticks (10 per standard turn);
  filtered queries default to the latest 20 matches. Each screen includes
  timestamp (game ticks, null before play) and sequence (unique log position).
  Multiple keypresses can share a tick. No matches prints nothing.
Screens are available only by calling crawl observe; none are supplied automatically.
Errors do not undo inputs; do not blindly retry failed scripts.

Observation rows retain all text and spaces; styles[row][column] indexes palette.
Palette entries contain fg, bg, bold, italics, underline, reverse, blink, cursor.
Coordinates are zero-based terminal columns: wide symbols span two; combining marks
add none. Cursor is true only at its visible cell; blank-cell styling is retained.
