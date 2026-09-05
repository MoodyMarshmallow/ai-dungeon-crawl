#!/usr/bin/env python3
"""Offline Codex protocol fixture; never contacts a model or executes its code."""

import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
assert args[0] == "exec"
assert "--ignore-user-config" in args and "--ephemeral" in args
assert args[args.index("--sandbox") + 1] == "read-only"
assert 'forced_login_method="chatgpt"' in args
assert 'model_provider="openai"' in args
assert "features.shell_tool=false" in args
assert "features.multi_agent=false" in args
assert "features.apps=false" in args
assert 'web_search="disabled"' in args
assert "OPENAI_API_KEY" not in os.environ
assert "CODEX_API_KEY" not in os.environ
assert "OPENAI_BASE_URL" not in os.environ
assert "CODEX_ACCESS_TOKEN" not in os.environ

payload = json.loads(sys.stdin.read())
mode = payload["goal"]
if mode == "timeout":
    time.sleep(30)
if mode == "overflow":
    sys.stdout.write("x" * (3 * 1024 * 1024))
    sys.stdout.flush()
    time.sleep(30)
if mode == "failed":
    print("fake-sensitive-diagnostic", file=sys.stderr)
    sys.exit(1)
output = Path(args[args.index("--output-last-message") + 1])
output.write_text(json.dumps({"code": 123 if mode == "invalid" else 'await press("l")'}))
if mode == "tool":
    print(json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}))
if mode != "incomplete":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 8}}))
