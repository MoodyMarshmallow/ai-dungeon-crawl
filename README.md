# AI Dungeon Crawl

A Python harness for language models playing Dungeon Crawl Stone Soup (DCSS).
This repository contains a working Python REPL and agent loop with a **mock game**,
and real Codex and PydanticAI model policies. The DCSS game
connection is not implemented yet.

```sh
uv run ai-dungeon-crawl
```

Run from this repository on macOS. Codex is the default agent and uses your
existing Codex login. The game is a four-cell corridor mock.
`uv` installs the project's Python 3.12 runtime and dependencies as needed.

## Local dashboard

The dashboard is TypeScript, served and bundled by Bun. The game harness and
sandboxed REPL stay in Python; Bun launches one Python process per episode and
consumes its newline-delimited display events. No Python HTTP server is needed.

The interface is intentionally minimal: three resizable panes and start/stop
controls inside the sidebar. [xterm.js](https://xtermjs.org/) renders the game
screen and a scrolling Python transcript of submitted code and output. Model
responses are warm white; execution returns are blue. Fonts and
component assets are served locally. The displays never send terminal input to
the game or REPL. Incoming text controls are stripped before rendering because
current observations are plain-text snapshots, not trusted ANSI streams.

From the repository root:

```sh
uv sync
cd dashboard
bun install
bun run dev
```

Open `http://127.0.0.1:8765`, then click **Start**. Opening or refreshing
the page does not call a model. Codex (`gpt-5.6-luna`) is the default, using your
existing Codex login and streaming reasoning summaries. To override the model:

```sh
bun run dev --model gpt-5.6-luna
```

Or select `--policy pydantic --model 'provider:model'`. `--port`, `--max-steps`,
and `--max-turns` configure the server and episode limits. Configuration is fixed
at server startup; restart the server to change it. No automatic hot reload is
enabled, so saving a file never interrupts a running game.

- Top left: latest game screen, updated after each completed keypress.
- Bottom left: syntax-highlighted Python submissions, live output, and errors.
  This is a read-only execution log, not an interactive Python console or
  a variable inspector. The sandbox namespace persists across submissions.
- Right: live model text, provider-exposed reasoning, and tool arguments. The
  Responses-only `--reasoning-summary` flag requests summaries where supported;
  it does not expose hidden reasoning and is not supported by every model.
  Messages and summaries support Markdown; tool calls have highlighted code boxes.

Streamed code is **preview only**. PydanticAI still validates the complete
`execute_python` call before the runner executes it; text-only, multiple-tool,
and incomplete Codex responses remain rejected. Output repair requests appear
separately in the model activity log. The ordinary command-line runner remains
unchanged when no observer is attached.

The browser reconnects to the latest snapshot without restarting the episode.
The dashboard retains the last 30 requests/submissions, up to 16 parts per model
response and 65,536 characters per preview; this is a bounded in-memory monitor,
not durable recording. Slow browsers receive coalesced snapshots rather than
blocking gameplay. **Stop** cancels the harness and closes the REPL; closing the
browser alone does not stop a run. Ctrl+C shuts down the server and its episode.
The Python bridge also cancels its episode if the Bun parent disappears.

The server binds only to loopback and rejects cross-origin controls and unexpected
Host headers. Markdown disables raw HTML and images; code is escaped before highlighting.
Game and REPL output cannot supply terminal controls. OAuth credentials,
encrypted reasoning, signatures, and raw provider errors are not sent to the
browser. This is a local development tool, not an authenticated remote service.

The game is still the **corridor mock**, not DCSS. Connecting a real `GameSession`
is separate work; the monitoring events already come from the shared runner.

```sh
bun run typecheck
bun test
```

## Model backends

For subscription-backed testing, install the official Codex CLI and sign in with
ChatGPT using `codex login`, then run:

```sh
uv run ai-dungeon-crawl --policy codex --model gpt-5.6-luna
```

This calls the subscription Responses endpoint directly using the CLI's file-backed
ChatGPT OAuth login (`$CODEX_HOME/auth.json`, otherwise `~/.codex/auth.json`).
There is no Codex agent subprocess, MCP bridge, or final-message parser. Both
model-backed policies require an explicit model; choose one available to your account.
Subscription usage limits still apply, and there is no API-key fallback.

Credentials are read in memory, never logged or written by the adapter, and sent
only to the fixed subscription endpoint. The default HTTP client ignores inherited
proxies and does not follow redirects. Missing, expired, or keychain-only logins
fail closed: use a file-backed `codex login` before retrying. Automatic OAuth refresh
is not implemented, to avoid rotating the CLI's shared refresh token. No changes
are made to your Codex configuration. This backend may change independently of the
public Responses API; it is a tested integration, not a compatibility guarantee.

For API-backed testing, set your provider's credentials in the environment and
choose an explicit model (replace the placeholder below):

```sh
uv run ai-dungeon-crawl --policy pydantic --model 'openai:<model-id>'
```

OpenAI and Anthropic provider dependencies are included (`OPENAI_API_KEY` and
`ANTHROPIC_API_KEY`, respectively). API usage is billed separately. In Python,
`PydanticPolicy` also accepts a configured PydanticAI `Model`, allowing custom
providers and OpenAI-compatible endpoints without changing the runner. Other
providers may require additional dependencies and endpoint-specific testing.

Both adapters use the same `Policy.request_turn` implementation and PydanticAI
`execute_python` output tool. Exactly one accepted call ends the agent turn.
Ordinary assistant text, including JSON or code blocks in a final message, is
never executable output. Neither policy executes code: the runner's sandboxed
REPL executes the submission afterward, and supplies feedback to the next agent
turn. No post-tool model continuation is needed. Provider credentials are never
passed to the REPL. Multiple tool submissions in one response are rejected.

The default context includes the complete current observation and up to four
recent whole turn records, within 32,000 characters. Omitted turns are reported;
an oversized current observation is rejected rather than silently truncated.
There is no hidden conversation history. Both adapters allow two logical model
requests (one output-repair attempt) within 120 seconds. Provider transport retries
are separate from logical request counts; the Codex transport disables HTTP retries.
Codex requires streaming and `store=false`; the adapter drains that stream and
rejects incomplete responses. Its endpoint does not support the generic output-token
limit, so the deadline and request limit bound a turn, not a token quota.
These limits are configurable on the policy constructors. CLI episode limits
are configurable with `--max-steps` and `--max-turns`.

## Main interface

```python
result = await EpisodeRunner(game, policy).run(max_steps=100, max_turns=50)
```

`EpisodeRunner` owns the agent-turn loop, enforces limits on every game input,
records transitions, and closes both the game and REPL on completion or failure.
Supply a fresh game session and REPL for each episode.

There are two internal interfaces, expressed as Python protocols. Implementations
can satisfy them without inheriting a framework class:

| Interface | Responsibility | Adapter |
| --- | --- | --- |
| `GameSession.start/step/close` | Own the isolated process, reconstruct the screen, send input, detect readiness, clean up | DCSS console process in a pseudo-terminal |
| `Policy.request_turn` | Select context, request one Python script, report model-request count when known | `CodexPolicy` or `PydanticPolicy` |

The runner dispatches the returned script to `PythonRepl.execute_python`.
The REPL sends key requests back to the runner; it never receives the actual
game process or the model client. There is no extra runner protocol or factory.

## The model's REPL

One agent turn submits one script. That script can perform zero or many game
actions. The model calls the `execute_python(code)` output tool, represented as
`AgentTurn(code, model_requests)`.

Scripts have two game helpers:

```python
obs = observe()          # latest screen; does not send input
obs = await press("l")   # one key, wait for readiness, return new GameObservation
print(obs.screen)
```

Normal Python loops, functions and conditionals work, including top-level
`await`. Variables and helpers persist for the episode. `observe()` and `press()`
are refreshed before each script; observations expose `id`, `screen`, and `ended`.
Use `print()` for output; expression values are not implicitly displayed.
Background async tasks do not persist between executions.

The next `request_turn(observation, history)` receives `AgentTurnRecord` history,
including code, output, errors, and per-key steps. Every execution result also
includes the latest observation, even if the script did not print it.
Scripts can inspect intermediate screens; the model receives feedback only
after the script finishes. Advanced game-specific stop predicates are deferred.

## The data crossing those interfaces

- `GameObservation`: a complete screen, a session-local observation ID, and whether
  the process ended. Whitespace matters. A prompt or menu is an observation too.
- `GameAction`: one printable character or named key such as `ENTER` or `CTRL+S`.
  The game adapter translates it to bytes. A valid key can still be an illegal
  move; the next observation shows what happened.
- `GameStep`: the before observation, action and after observation.
- `AgentTurn`: submitted Python code and model-request count, including any
  output-repair attempts. Both model-backed
  policies report their logical request count. `None` is reserved for adapters
  that cannot expose a reliable count.
- `ExecutionResult`: latest observation, bounded output, error, and execution status.
- `AgentTurnRecord`: an agent turn, its execution result, and its completed steps.
  Its `steps` collection groups actions under the agent turn that produced them.
- `GameEpisodeResult`: stop reason, final observation, all steps and all turn records.

Model-request counts are recorded once per agent turn, not repeated per key.
Turn IDs are zero-based within an episode. See `CONTEXT.md` for terminology.

See `src/ai_dungeon_crawl/contracts.py` for the contracts and `episode.py` for
the short orchestration implementation. `mock_game.py` supplies the game mock;
`policies.py` supplies the model adapters and explicit backend selection.

## Limits and failure behavior

1. **One action at a time.** An action is not a DCSS turn. Inventory, targeting,
   confirmations and movement all use the same observation/action cycle.
2. **Readiness belongs to the game adapter.** It must reconstruct terminal
   updates and return at an input boundary, with a finite timeout. A quiet
   output stream alone is only a readiness heuristic, not proof.
3. **Budgets are enforced outside the script.** `max_steps` counts individual
   keys; `max_turns` also bounds scripts that perform no actions or repeatedly
   fail. No new key is sent after exit or exhaustion, even inside a loop that
   catches exceptions. Scripts are never automatically rerun.
4. **Script errors preserve partial progress.** Syntax/runtime errors and invalid
   keys become execution feedback for the next agent turn. Completed steps,
   output and namespace changes remain; execution is not transactional.
5. **Cleanup is unconditional.** A session closes even after failed startup,
   policy failure, step failure or cancellation. Each real session gets a fresh
   save directory; cleanup does not delete saves or promise to save the game.
6. **Game failures are fatal.** Game/transport exceptions propagate after cleanup,
   without a script retry. A timed-out game input may already have been sent.
   Script execution timeout instead returns `repl_timeout` and kills the worker.
   Results also distinguish `game_exited`, `step_limit`, and `turn_limit`.
   Process exit does not necessarily mean a win or death.

By default each script gets 5 seconds of worker/IPC time, 8,000 output characters,
and at most 64 KiB of source. Worker startup has a separate 10-second deadline.
Time awaiting `GameSession.step()` is excluded from the script budget; the game
adapter must enforce its own finite I/O timeout. Configure the first two limits
by supplying `PythonRepl(timeout_seconds=..., max_output_chars=...)` to the runner.

## Local execution sandbox

`PythonRepl` launches a separate interpreter through macOS `sandbox-exec`, with
a deny-by-default profile. The worker can read its own source, Python runtime
and required system libraries; it cannot read arbitrary user-file contents,
write files, access the network or fork processes. Filesystem metadata reads
are allowed for runtime path resolution. The parent passes a minimal environment
without inherited provider credentials, and Python runs with isolated startup
and site-package initialization disabled.

The worker has no game handle: the parent validates and budgets every requested
key. The IPC protocol is private; scripts should use the supplied helpers and
`print`, not write directly to protocol streams. Malformed IPC ends the episode.

This local adapter currently supports **macOS only** and fails closed if its
sandbox cannot start. It has no unrestricted fallback. Sandbox restrictions are
tested against synthetic data, but this is not a hardened multi-tenant execution
service. There is no strict memory quota; containers/VMs with resource quotas
are future work for running arbitrary remote models at scale.

## Deliberate limits of this mock

The contract permits one script submission per turn, not concurrent tool calls.
Backend selection is explicit; there is no automatic model/provider fallback.

The mock stores plain text screens. Before evaluating real ASCII gameplay,
extend observations to preserve terminal colors/attributes, which can convey
game information. The DCSS adapter also needs verified readiness, bounded I/O,
and process-exit handling. Those behaviors are not simulated by the demo.

This first loop retains completed steps and turns in memory and returns them on
normal stops (including script timeout). It is not a durable recorder: fatal
game, worker, or policy failures raise without returning a partial trajectory.
Add incremental recording before expensive or long model runs, including request
attempts, usage, timing, model configuration, DCSS commit and seed. Add episode-wide
request/cost limits alongside the current per-turn request and action limits. Keep these concrete until a
second implementation requires another interface.

Tests exercise the actual sandboxed worker. In an environment that prohibits
nested sandboxes, run them from a normal local terminal. Unsupported platforms
skip the macOS integration tests and still test contract validation/fail-closed behavior.
Policy tests use simulated model responses and offline OpenAI Chat Completions
and Codex Responses/SSE transports; they make no paid model calls. A live direct
Codex Responses smoke test completed the mock game through the real output tool
and REPL. The API-key path has not yet been tested against a paid live endpoint.

```sh
uv run python -m unittest discover -s tests
```
