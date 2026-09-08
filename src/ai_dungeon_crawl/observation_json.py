from dataclasses import asdict
import json

from wcwidth import wcwidth

from .contracts import GameObservation


def observation_data(observation: GameObservation) -> dict:
    """Preserve the original screen and describe only visible styling.

    Style runs are [row, terminal column, length, palette index]. Styling on
    blank black cells is omitted unless underlined or reversed. No characters,
    rows, or whitespace are trimmed or encoded as coordinate tuples.
    """
    lines = observation.screen.split("\n")
    width = observation.width or max(
        (sum(max(0, wcwidth(char)) for char in line) for line in lines), default=0)
    height = observation.height or len(lines)
    defaults = {"fg": "default", "bg": "default", "bold": False,
                "italics": False, "underline": False, "reverse": False, "blink": False}
    attributes = {}
    for style in observation.styles:
        attrs = {key: value for key, value in asdict(style).items()
                 if key in defaults and value != defaults[key]}
        for col in range(max(0, style.col), min(width, style.col + style.length)):
            attributes[style.row, col] = attrs
    style_runs, palette = [], []
    palette_indexes = {}
    for row in range(height):
        line = lines[row] if row < len(lines) else ""
        cells = []
        col = 0
        for char in line:
            size = max(0, wcwidth(char))
            if not size and cells:
                cells[-1][1] += char
            elif size and col + size <= width:
                cells.append([col, char, size])
                col += size
        while col < width:
            cells.append([col, " ", 1])
            col += 1
        def visible(cell):
            col, char, size = cell
            cell_attrs = [attributes.get((row, x), {}) for x in range(col, col + size)]
            return char != " " or any(
                a.get("bg", "default") not in ("default", "black", "000000") or
                a.get("reverse") or a.get("underline") for a in cell_attrs)

        for col, char, size in cells:
            if not visible((col, char, size)):
                continue
            cell_attrs = [attributes.get((row, x), {}) for x in range(col, col + size)]
            for x, attrs in zip(range(col, col + size), cell_attrs):
                if not attrs:
                    continue
                key = tuple(sorted(attrs.items()))
                if key not in palette_indexes:
                    palette_indexes[key] = len(palette)
                    palette.append(attrs)
                index = palette_indexes[key]
                if (style_runs and style_runs[-1][0] == row and
                        style_runs[-1][1] + style_runs[-1][2] == x and
                        style_runs[-1][3] == index):
                    style_runs[-1][2] += 1
                else:
                    style_runs.append([row, x, 1, index])
    return {"id": observation.id, "ended": observation.ended,
            "width": width, "height": height, "cursor": observation.cursor,
            "screen": observation.screen, "style_palette": palette, "style_runs": style_runs}


def format_observation(data: dict) -> str:
    """Omit blank text rows, retaining their original coordinate mapping."""
    metadata = {key: value for key, value in data.items() if key != "screen"}
    lines = data["screen"].split("\n")
    rows = [row for row, line in enumerate(lines) if line.strip()]
    if len(rows) != len(lines):
        metadata["screen_rows"] = rows
    screen = "\n".join(lines[row] for row in rows)
    return screen + "\n\n" + json.dumps(metadata, ensure_ascii=False)
