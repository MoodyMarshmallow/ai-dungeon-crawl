# AI Dungeon Crawl

> Very early work in progress. Real DCSS is connected; the integration is experimental.

A harness for an AI agent to play [Dungeon Crawl Stone Soup](https://github.com/crawl/crawl) autonomously through a small game CLI.

## Quick start

- Requires macOS, [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh/), the Codex CLI, and a [DCSS checkout with its build dependencies](https://github.com/crawl/crawl/blob/master/crawl-ref/INSTALL.md).
- Sign in with your Codex subscription, then run from the repository root:

```sh
codex login
uv sync
uv run python scripts/build_dcss.py ../crawl
cd dashboard
bun install
bun run dev
```

- Open [localhost:8765](http://127.0.0.1:8765/) and click **Start**. **Stop** cancels the episode.
- **Settings** lets you choose the model, reasoning strength, and turn limit for the next **Start**. Reasoning strength is sent to Codex/OpenAI; other PydanticAI providers require **Provider default**. Model support for individual strengths varies. Terminal launches accept `--model`, `--reasoning-effort`, and `--max-turns` too.
- A turn is one model-submitted shell script. There is no keypress limit; shell errors and timeouts return feedback so the model can continue. Game exit, the turn limit, and **Stop** end a run.
- Each new run writes `runs/<run-id>/events.jsonl`: timestamped configuration, observations, exposed reasoning summaries, tool calls, full shell output, and completion/errors. Dashboard/model previews remain bounded; the log does not contain credentials or encrypted reasoning. Older runs cannot be recovered retroactively.
- Switch between **Terminal** and **Tiles** to view the same game. Saves stay in `runs/`.
- The agent uses `crawl observe` and `crawl press KEY`. It can search the matching local `crawl_manual.rst` with `rg`, `grep`, or `sed`.
- Keypresses produce no output on success; use `crawl observe` for intermediate inspection. The model automatically receives the final screen after each submission. Documentation lookups use the local manual, not in-game help.
- Observations are full JSON: `rows` preserves every text row and space, `styles[row][column]` indexes `palette`, and each palette entry includes all visual attributes and a `cursor` boolean. Columns are terminal columns (wide symbols span two; combining marks add none); blank-cell styling is retained. `crawl observe --json` is no longer accepted.

## Codebase

- `dashboard/` — TypeScript/Bun dashboard for the game screen, model activity, and shell output.
- `src/ai_dungeon_crawl/` — Python agent loop and isolated shell execution; currently macOS-only. Each submission starts in the same episode workspace; files persist, shell variables and directory changes do not.
- `src/ai_dungeon_crawl/shell/` — macOS filesystem/network sandbox, game CLI, and local manual. Credentials are scrubbed and execution is bounded. Directory changes never persist between submissions. Process-detaching/spawn APIs are blocked, so some utilities may not work. Limits are per process/file, not aggregate memory/disk quotas; this is local-development isolation, not a hardened multi-tenant sandbox.
- `src/ai_dungeon_crawl/agent/` — Model integration and streaming, using a Codex subscription by default; PydanticAI API backends are also available.
- `src/ai_dungeon_crawl/game/` — Real game process and full terminal observations with colors; `--game mock` retains the test game.
- `scripts/build_dcss.py` — Builds a separate WebTiles executable with an opt-in input-readiness patch; existing game binaries are kept.
