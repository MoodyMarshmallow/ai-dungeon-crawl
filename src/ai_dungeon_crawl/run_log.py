from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .events import record_events
from .observation_history import observation_journal


@contextmanager
def episode_log(directory: Path):
    """Create separate model, game-state, and tile journals outside the sandbox.

    Model content is buffered until its request ends; other events are flushed
    immediately, and close also fsyncs. A hard process kill can lose pending
    model content and leave a final partial line with no completion event. Never overwrite an
    existing log or serialize raw SDK responses, credentials, or exceptions.
    All files share one sequence counter so their events can be merged for review.
    """
    directory.mkdir(parents=True, exist_ok=True)
    sequence = 0
    turn = None
    request = None
    request_count = 0
    game_time = None
    model_parts = {}
    with ExitStack() as stack:
        outputs = {}
        for name in ("model.jsonl", "game.jsonl", "tiles.jsonl", "events.jsonl"):
            if (directory / name).exists():
                raise FileExistsError(directory / name)
        for name in ("model.jsonl", "game.jsonl", "tiles.jsonl"):
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            outputs[name] = stack.enter_context(os.fdopen(fd, "w", encoding="utf-8"))
        def flush_model_parts(completed):
            parts = tuple(model_parts.items())
            # A failed write may already have reached disk; never replay the batch.
            model_parts.clear()
            for index, part in parts:
                write("model.tool_call" if part["kind"] == "tool" else "model.message",
                      {"index": index, **part, "completed": completed})

        def append(event, data):
            if event == "model.part":
                index = data["index"]
                if data.get("replace") or index not in model_parts:
                    model_parts[index] = {"kind": data["kind"], "text": data.get("text", "")}
                    if "name" in data:
                        model_parts[index]["name"] = data["name"]
                else:
                    part = model_parts[index]
                    part["text"] += data.get("text", "")
                    if "name" in data:
                        part["name"] = part.get("name", "") + data["name"]
                return
            if event == "model.finished":
                flush_model_parts(completed=True)
            elif event in {"model.started", "turn.started", "episode.finished"}:
                flush_model_parts(completed=False)
            write(event, data)

        def write(event, data):
            nonlocal sequence, turn, request, request_count, game_time
            if event == "game.score" and data.get("game_time") is not None:
                game_time = data["game_time"]
            if event == "turn.started":
                turn, request = data["id"], None
            elif event == "model.started":
                request_count += 1
                request = request_count
            record = {"schema_version": 2, "run_id": directory.name,
                      "sequence": sequence, "timestamp": game_time,
                      "real_timestamp": datetime.now(timezone.utc).isoformat(),
                      "turn_id": turn, "model_request_id": request,
                      "event": event, "data": data}
            if event == "game.tiles":
                journal = "tiles.jsonl"
            elif event.startswith("game."):
                journal = "game.jsonl"
            else:
                journal = "model.jsonl"
            output = outputs[journal]
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
            sequence += 1

        try:
            with record_events(append), observation_journal(directory / "game.jsonl"):
                yield directory / "model.jsonl"
        finally:
            try:
                flush_model_parts(completed=False)
            finally:
                for output in outputs.values():
                    output.flush()
                    os.fsync(output.fileno())
