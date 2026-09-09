import pyte

from ..contracts import GameObservation, ScreenStyle


class TerminalScreen:
    def __init__(self, width: int, height: int) -> None:
        self.screen = pyte.Screen(width, height)
        self.stream = pyte.ByteStream(self.screen)

    def feed(self, data: bytes) -> None:
        self.stream.feed(data)

    def observation(self, id: int, *, ended: bool = False) -> GameObservation:
        screen = self.screen
        styles = []
        for row in range(screen.lines):
            col = 0
            while col < screen.columns:
                cell = screen.buffer[row][col]
                attrs = (cell.fg, cell.bg, cell.bold, cell.italics,
                         cell.underscore, cell.reverse, cell.blink)
                end = col + 1
                while end < screen.columns:
                    other = screen.buffer[row][end]
                    if (other.fg, other.bg, other.bold, other.italics,
                        other.underscore, other.reverse, other.blink) != attrs:
                        break
                    end += 1
                if attrs != ("default", "default", False, False, False, False, False):
                    styles.append(ScreenStyle(row, col, end - col, *attrs))
                col = end
        # VT's pending autowrap cursor remains visually on the last cell;
        # pyte records the pending state as x == columns.
        cursor = (None if screen.cursor.hidden else
                  (screen.cursor.y, min(screen.cursor.x, screen.columns - 1)))
        return GameObservation(id=id, screen="\n".join(screen.display), ended=ended,
                               width=screen.columns, height=screen.lines,
                               styles=tuple(styles), cursor=cursor)


class BoundaryDecoder:
    """Remove an authenticated OSC delimiter even when reads split its bytes.

    on_boundary runs synchronously so each snapshot precedes later output.
    """

    def __init__(self, marker: bytes, terminal: TerminalScreen, on_boundary) -> None:
        self.marker = marker
        self.terminal = terminal
        self.on_boundary = on_boundary
        self.pending = b""

    def feed(self, data: bytes) -> None:
        self.pending += data
        while True:
            index = self.pending.find(self.marker)
            if index >= 0:
                self.terminal.feed(self.pending[:index])
                self.pending = self.pending[index + len(self.marker):]
                self.on_boundary()
                continue
            # Only retain a suffix that might be the beginning of the marker.
            keep = min(len(self.pending), len(self.marker) - 1)
            while keep and not self.marker.startswith(self.pending[-keep:]):
                keep -= 1
            if keep:
                self.terminal.feed(self.pending[:-keep])
                self.pending = self.pending[-keep:]
            else:
                self.terminal.feed(self.pending)
                self.pending = b""
            return

    def finish(self) -> None:
        self.terminal.feed(self.pending)
        self.pending = b""
