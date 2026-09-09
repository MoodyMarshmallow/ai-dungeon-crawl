from dataclasses import asdict
import json

from wcwidth import wcwidth

from ..contracts import GameObservation


def observation_data(observation: GameObservation) -> dict:
    """Return every terminal row and every column's complete visual style."""
    lines = observation.screen.split("\n")
    width = observation.width or max(
        (sum(max(0, wcwidth(char)) for char in line) for line in lines), default=0)
    height = observation.height or len(lines)
    rows = []
    for row in range(height):
        text, columns = [], 0
        for char in lines[row] if row < len(lines) else "":
            size = max(0, wcwidth(char))
            if columns + size > width:
                break
            text.append(char)
            columns += size
        rows.append("".join(text) + " " * (width - columns))

    defaults = {"fg": "default", "bg": "default", "bold": False,
                "italics": False, "underline": False, "reverse": False,
                "blink": False, "cursor": False}
    attributes = {}
    for style in observation.styles:
        if 0 <= style.row < height:
            attrs = {key: value for key, value in asdict(style).items() if key in defaults}
            for col in range(max(0, style.col), min(width, style.col + style.length)):
                attributes[style.row, col] = attrs

    palette, styles, palette_indexes = [], [], {}
    for row in range(height):
        indexes = []
        for col in range(width):
            attrs = defaults | attributes.get((row, col), {})
            attrs["cursor"] = observation.cursor == (row, col)
            key = tuple(attrs.items())
            if key not in palette_indexes:
                palette_indexes[key] = len(palette)
                palette.append(attrs)
            indexes.append(palette_indexes[key])
        styles.append(indexes)
    return {"id": observation.id, "ended": observation.ended,
            "width": width, "height": height, "rows": rows,
            "palette": palette, "styles": styles}


def format_observation(data: dict) -> str:
    """Serialize the full observation as one compact JSON line."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
