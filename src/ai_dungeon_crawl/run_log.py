from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .events import record_events


@contextmanager
def episode_log(directory: Path):
    """Create an exclusive, owner-only JSONL journal outside the model's sandbox.

    Each event is flushed immediately; close also fsyncs. A hard process kill
    can leave a final partial line and no completion event. Never overwrite an
    existing log or serialize raw SDK responses, credentials, or exceptions.
    """
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    sequence = 0
    turn = None
    request = None
    request_count = 0
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        def append(event, data):
            nonlocal sequence, turn, request, request_count
            if event == "turn.started":
                turn, request = data["id"], None
            elif event == "model.started":
                request_count += 1
                request = request_count
            record = {"schema_version": 1, "run_id": directory.name,
                      "sequence": sequence, "timestamp": datetime.now(timezone.utc).isoformat(),
                      "turn_id": turn, "model_request_id": request,
                      "event": event, "data": data}
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
            sequence += 1

        try:
            with record_events(append):
                yield directory / "events.jsonl"
        finally:
            output.flush()
            os.fsync(output.fileno())
