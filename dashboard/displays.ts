import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { terminalText } from "./rendering";
import { shellTranscript } from "./transcript";
import type { Observation, Submission } from "./types";
import { screenAnsi } from "./screen";
import { lineCells, terminalUnicode } from "./unicode";
export { terminalText } from "./rendering";

export class TerminalDisplay {
  private terminal: Terminal;
  private fit = new FitAddon();
  private resize: ResizeObserver;
  private value = "";
  private frame = 0;
  private observation: Observation | null = null;

  constructor(
    private host: HTMLElement,
    private game = false,
  ) {
    this.terminal = new Terminal({
      allowProposedApi: true,
      disableStdin: true,
      cursorBlink: false,
      convertEol: true,
      scrollback: game ? 0 : 10000,
      fontFamily: '"Plex Mono", monospace',
      fontSize: game ? 24 : 12,
      lineHeight: 1.35,
      screenReaderMode: true,
      theme: {
        background: game ? "#000000" : "#181818",
        ...(game ? { black: "#000000" } : {}),
        foreground: "#dedbd4",
        cursor: game ? "#dedbd4" : "#181818",
        selectionBackground: "#465568",
      },
    });
    this.terminal.unicode.register(terminalUnicode);
    this.terminal.unicode.activeVersion = terminalUnicode.version;
    this.terminal.loadAddon(this.fit);
    this.terminal.open(host);
    this.terminal.attachCustomKeyEventHandler(() => false);
    this.resize = new ResizeObserver(() => this.schedule());
    this.resize.observe(game ? host.parentElement! : host);
    void document.fonts.ready.then(() => this.schedule());
  }

  setText(value: string) {
    this.update(terminalText(value));
  }

  setObservation(observation: Observation | null) {
    const previous = this.observation;
    this.observation = observation;
    this.update(observation ? screenAnsi(observation) : "");
    if (previous?.width !== observation?.width || previous?.height !== observation?.height ||
        previous?.cursor?.[0] !== observation?.cursor?.[0] || previous?.cursor?.[1] !== observation?.cursor?.[1]) this.schedule();
  }

  setSubmissions(submissions: Submission[]) {
    this.update(shellTranscript(submissions, true));
  }

  private update(value: string) {
    if (this.value === value) return;
    this.value = value;
    this.schedule();
  }

  private schedule() {
    cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame(() => this.paint());
  }

  private paint() {
    if (!this.host.clientWidth || !this.host.clientHeight) return;
    const content = this.value;
    const buffer = this.terminal.buffer.active;
    const follow = buffer.viewportY >= buffer.baseY;
    const viewport = buffer.viewportY;
    if (this.game) {
      const lines = (this.observation?.screen ?? content).split("\n");
      const cols = this.observation?.width || Math.max(2, ...lines.map((line) => lineCells(line).reduce((total, cell) => total + cell.width, 0)));
      const rows = this.observation?.height || Math.max(1, lines.length);
      const parent = this.host.parentElement!;
      const size = Math.min(
        44,
        Math.max(
          10,
          Math.floor(
            Math.min(
              (parent.clientWidth - 70) / (cols * 0.61),
              (parent.clientHeight - 55) / (rows * 1.35),
            ),
          ),
        ),
      );
      this.terminal.options.fontSize = size;
      this.terminal.resize(cols, rows);
      this.host.style.width = `${Math.ceil(cols * size * 0.61) + 3}px`;
      this.host.style.height = `${Math.ceil(rows * size * 1.35) + 3}px`;
    } else this.fit.fit();
    // Full snapshots prevent duplicate output after reconnecting; preserve manual scrollback.
    this.terminal.write(
      "\x1b[?25l\x1b[0m\x1b[3J\x1b[2J\x1b[H" + content.replace(/\n/g, "\r\n") + this.cursorControl(),
      () => {
        if (!this.game && !follow) this.terminal.scrollToLine(viewport);
      },
    );
  }

  private cursorControl() {
    const cursor = this.observation?.cursor;
    return this.game && cursor && cursor.every(Number.isInteger)
      ? `\x1b[${cursor[0] + 1};${cursor[1] + 1}H\x1b[?25h`
      : "\x1b[?25l";
  }

  dispose() {
    cancelAnimationFrame(this.frame);
    this.resize.disconnect();
    this.terminal.dispose();
  }
}
