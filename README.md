https://github.com/user-attachments/assets/6745aaa0-eb3e-42b6-b2e2-3ff13e0d3367

# AI Dungeon Crawl

> Very early work in progress. Real DCSS is connected; the integration is experimental.

A harness for an AI agent to play [Dungeon Crawl Stone Soup](https://github.com/crawl/crawl) autonomously through a small game CLI.

## Quick start

- Requires macOS, [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh/), the Codex CLI, and a [DCSS checkout with its build dependencies](https://github.com/crawl/crawl/blob/master/crawl-ref/INSTALL.md).
- DCSS is pinned in `dcss.lock.json`: [our patched revision `4ec9cb2f09`](https://github.com/MoodyMarshmallow/crawl/commit/4ec9cb2f0938e71cd773be1c1454aba751056017), based on upstream `0941e263bb` (0.35-a0), plus the bundled harness patches. The build helper accepts either exact revision and applies bundled patches as needed; it never switches your checkout. Local source edits are still permitted.
- Sign in with your Codex subscription, then run from the repository root:

```sh
codex login
uv sync
uv run python scripts/build_dcss.py ../crawl
cd dashboard
bun install
bun run dev
```

- Open [localhost:8765](http://127.0.0.1:8765/) and click **Start**. **Stop** cancels the session.
- **Settings** lets you choose the model, reasoning strength, action-turn limit, review-turn limit, and episode limit for the next **Start**; saved changes apply at the next start. Defaults are 3 review turns and 1 episode. Reasoning strength is sent to Codex/OpenAI; other PydanticAI providers require **Provider default**. Model support for individual strengths varies. Terminal launches accept the corresponding options too.
- A session alternates an Action agent playing a game and, after a death only, a Review agent reviewing that episode before the next game. It stops on a win, non-death game exit, error, action-turn limit, episode limit, or manual cancellation; a review-turn limit ends only the current review phase.
- Each session starts with fresh Action and Review contexts. Its first action log is at `runs/<run-id>/`; later episodes use `runs/<run-id>/episode-NNN/action/`, and their reviews use `runs/<run-id>/episode-NNN/review/`. Each phase writes `model.jsonl` and `game.jsonl`, with `tiles.jsonl` for visual data. Review receives read-only snapshots of exactly the action phase's `model.jsonl` and `game.jsonl` (no tiles), plus `crawl_manual.rst`. Shared `artifacts/snapshot/` holds reusable `tools/`, `skills/`, and `prompts/agent_editable/action/` from the same Start session. `crawl observe` reads only the current action `game.jsonl`.
- Review can edit `prompts/agent_editable/action/system_prompt.md`, `initial_prompt.md`, and `execute_shell.md`. These bounded UTF-8 text files publish together with tools and skills after a completed review; the next Action episode loads them once. Package templates are grouped by purpose under `agent/prompt_templates/action_agent/` and `review_agent/`. Fixed schema descriptions and constraints remain in `agent/tools/execute_shell.py` and are included in the read-only episode reference. At runtime, `prompts/read_only/` is protected by the sandbox; only `prompts/agent_editable/action/` can be edited and published. Every new Start uses the packaged originals in `src/ai_dungeon_crawl/agent/prompt_templates/action_agent/`. Review sees read-only original copies at `prompts/read_only/action_originals/` and the completed episode's exact prompt text, fixed tool schema/field descriptions, and configuration at `prompts/read_only/action-prompts.json`. The same snapshot is saved as `action-prompts.json` and an `action.prompts` event in the action log. Tool names, argument validation, and sandbox limits remain code-owned. Zero review turns leave the base prompts unchanged.
- Action template files include YAML front matter with `purpose` and `usage` notes for the reviewer. Only the Markdown body is sent to the Action model or recorded in its effective prompt reference; full files retain their metadata through review and publication. The supported header format is exactly those two single-line string fields (plain text or JSON-style double quotes), bounded to 4 KiB within the 64 KiB file limit. Plain body-only files remain supported; malformed headers and empty bodies are rejected.
- Each Action episode and Review phase keeps its full conversation in memory: the initial user prompt appears once, followed by assistant messages, tool calls, and their actual shell results. Codex disables provider truncation and stops on context-limit errors; other providers follow their configured truncation behavior. Provider reasoning metadata is retained for continuation but never written to activity logs. Shell output remains bounded per submission.
- Streamed model text and reasoning are aggregated into one `model.message` record per indexed block, and each tool call into one `model.tool_call` record. These records include `index`, `kind`, `text`, and (for tools) `name`; `completed` is `true` after a normal model finish and `false` when the partial block is flushed by a new request, turn, episode finish, or log close. Live dashboard deltas remain available during streaming.
- `game.score` records Crawl's native score at input updates (`score`, `game_turn`, `final`). During play it excludes win bonuses; the final native score includes them on a win. Before play or outside an active game, score is `null`. Score telemetry is not automatically sent to the Action agent; the Review agent can read it from the game log.
- Log schema 2 uses `timestamp` for the latest known DCSS elapsed ticks (aut, 10 per standard turn; `null` before game time is available) and `real_timestamp` for ISO UTC wall-clock time. `game.score.game_time` supplies the native tick count; action counts remain separate. Existing schema-1 logs are unchanged.
- Switch between **Terminal** and **Tiles** to view the same game. Saves stay in `runs/`.
- The agent uses `crawl observe` and `crawl press KEY`. It can search the matching local `crawl_manual.rst` with `rg`, `grep`, or `sed`.
- Keypresses produce no output on success; the model must call `crawl observe` to see screens. The dashboard's **Final result** shows the screen after each submission without sending it to the model. Documentation lookups use the local manual, not in-game help.
- `crawl observe` reads the latest logged screen; `-n 5` reads the last five. `--since 100 --until 200` filters inclusive DCSS ticks (10 per standard turn), never real time. History returns chronological JSON lines, defaulting to the latest 20 matches, up to 100 with `-n`. Screens include game `timestamp` and unique log `sequence`; repeated ticks are retained and no matches prints nothing.
- Observations are full JSON: `rows` preserves every text row and space, `styles[row][column]` indexes `palette`, and each palette entry includes all visual attributes and a `cursor` boolean. Columns are terminal columns (wide symbols span two; combining marks add none); blank-cell styling is retained. `crawl observe --json` is no longer accepted.

## Codebase

- `dashboard/` — TypeScript/Bun dashboard for the game screen, model activity, and shell output.
- `src/ai_dungeon_crawl/` — Python agent loop and isolated shell execution; currently macOS-only. Each submission starts in the same episode workspace; files persist, shell variables and directory changes do not.
- `src/ai_dungeon_crawl/shell/` — macOS filesystem/network sandbox, game CLI, and local manual. Credentials are scrubbed and execution is bounded. Directory changes never persist between submissions. Process-detaching/spawn APIs are blocked, so some utilities may not work. Limits are per process/file, not aggregate memory/disk quotas; this is local-development isolation, not a hardened multi-tenant sandbox.
- `src/ai_dungeon_crawl/agent/` — Model integration and streaming, using a Codex subscription by default; PydanticAI API backends are also available.
- `src/ai_dungeon_crawl/game/` — Real DCSS process and full terminal observations with colors.
- `scripts/build_dcss.py` — Builds a separate WebTiles executable with an opt-in input-readiness patch; existing game binaries are kept.
