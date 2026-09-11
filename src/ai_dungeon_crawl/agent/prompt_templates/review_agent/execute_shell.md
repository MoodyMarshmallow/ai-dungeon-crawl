Submit one Bash script to end this review turn.
The harness executes it once and returns bounded output, errors, and status.
Each call starts a fresh shell; files persist but variables and cwd do not.
Python and standard text tools are available; Prefer ripgrep (rg) for searching file contents when available. Network and personal files are blocked.
Read model.jsonl and game.jsonl from the completed episode and crawl_manual.rst.
Logs are at logs/model.jsonl and logs/game.jsonl. Write reusable tools in tools/
and skills in skills/. Edit action prompt text in prompts/agent_editable/action/*.md.
prompts/read_only/ contains read-only originals, the completed action prompt snapshot,
and fixed tool schema/configuration. Tool names, argument schema and security
limits are code-owned and cannot be changed here. No crawl command or tiles are available.
Optional timeout_ms is 1-180000 milliseconds; null uses the default (5000 ms).
