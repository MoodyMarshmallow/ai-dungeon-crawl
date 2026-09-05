# AI Dungeon Crawl

A Python harness for language models playing Dungeon Crawl Stone Soup (DCSS).
This repository contains a working Python REPL and agent loop with a **mock game**,
a scripted policy, and real Codex and PydanticAI model policies. The DCSS game
connection is not implemented yet.

```sh
uv run ai-dungeon-crawl
```

Run from this repository on macOS. The demo performs three game actions across
two model turns. Its second script reuses a helper and variable defined by the
first script. The default scripted policy needs no credentials or model calls.
`uv` installs the project's Python 3.12 runtime and dependencies as needed.

## Model backends

For subscription-backed testing, install the official Codex CLI and sign in with
ChatGPT using `codex login`, then run:

```sh
uv run ai-dungeon-crawl --policy codex
```

This uses Codex's managed OAuth credentials, not an API key. Subscription usage
limits still apply. The adapter forces ChatGPT authentication, excludes API-key
environment variables, and never falls back to API billing. It uses ephemeral
requests with user configuration ignored, shell/search/apps/subagents disabled,
and a neutral read-only working directory. Your Codex configuration is not changed.
Tested with Codex CLI 0.144.5; older versions may lack required flags and fail closed.
An optional `--model` selects an available Codex model; otherwise Codex chooses its default.

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

Both adapters implement `Policy.request_turn`. PydanticAI validates an
`execute_python` output-tool submission; Codex returns the equivalent structured
JSON. Neither executes the script: only the runner's sandboxed REPL does that.
Provider credentials are not passed to the REPL.

The default context includes the complete current observation and up to four
recent whole turn records, within 32,000 characters. Omitted turns are reported;
an oversized current observation is rejected rather than silently truncated.
There is no hidden conversation history. PydanticAI allows two logical model
requests (one output-repair attempt) within 120 seconds; Codex has a 180-second
deadline. Provider transport retries are separate from logical request counts.
These limits are configurable on the policy constructors. CLI episode limits
are configurable with `--max-steps` and `--max-turns`.

## Main interface

```python
result = await EpisodeRunner(game, policy).run(max_steps=100, max_turns=50)
```

`EpisodeRunner` owns the model-turn loop, enforces limits on every game input,
records transitions, and closes both the game and REPL on completion or failure.
Supply a fresh game session and REPL for each episode.

There are two internal interfaces, expressed as Python protocols. Implementations
can satisfy them without inheriting a framework class:

| Interface | Responsibility | Adapter |
| --- | --- | --- |
| `GameSession.start/step/close` | Own the isolated process, reconstruct the screen, send input, detect readiness, clean up | DCSS console process in a pseudo-terminal |
| `Policy.request_turn` | Select context, request one Python script, report model-request count when known | `CodexPolicy`, `PydanticPolicy`, or `ScriptedPolicy` |

The runner dispatches the returned script to `PythonRepl.execute_python`.
The REPL sends key requests back to the runner; it never receives the actual
game process or the model client. There is no extra runner protocol or factory.

## The model's REPL

One model turn submits one script. That script can perform zero or many game
actions. The model submits `execute_python(code)` (or equivalent structured JSON
for Codex), represented as `ModelTurn(code, model_requests)`.
The scripted policy exercises that same path without a provider SDK.

Scripts have two game helpers:

```python
obs = observe()          # latest screen; does not send input
obs = await press("l")   # one key, wait for readiness, return new Observation
print(obs.screen)
```

Normal Python loops, functions and conditionals work, including top-level
`await`. Variables and helpers persist for the episode. `observe()` and `press()`
are refreshed before each script; observations expose `id`, `screen`, and `ended`.
Use `print()` for output; expression values are not implicitly displayed.
Background async tasks do not persist between executions.

The next `request_turn(observation, history)` receives `TurnRecord` history,
including code, output, errors, and per-key steps. Every execution result also
includes the latest observation, even if the script did not print it.
Scripts can inspect intermediate screens; the model receives feedback only
after the script finishes. Advanced game-specific stop predicates are deferred.

## The data crossing those interfaces

- `Observation`: a complete screen, a session-local observation ID, and whether
  the process ended. Whitespace matters. A prompt or menu is an observation too.
- `Action`: one printable character or named key such as `ENTER` or `CTRL+S`.
  The game adapter translates it to bytes. A valid key can still be an illegal
  move; the next observation shows what happened.
- `Step`: the before observation, action and after observation.
- `ModelTurn`: submitted Python code and model-request count, including any
  output-repair attempts. A scripted policy reports zero; Codex reports `None`
  because CLI events do not reliably expose internal model-request counts.
- `ExecutionResult`: latest observation, bounded output, error, and execution status.
- `TurnRecord`: a model turn, its execution result, and its completed steps.
  Its `steps` collection groups actions under the model turn that produced them.
- `EpisodeResult`: stop reason, final observation, all steps and all turn records.

Model-request counts are recorded once per model turn, not repeated per key.
Turn IDs are zero-based within an episode. See `CONTEXT.md` for terminology.

See `src/ai_dungeon_crawl/contracts.py` for the contracts and `episode.py` for
the short orchestration implementation. `demo.py` supplies both mock adapters;
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
   keys become execution feedback for the next model turn. Completed steps,
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
Policy tests use simulated model responses, an offline OpenAI client transport,
and a stub Codex executable; they make no paid model calls. A live Codex smoke
test has also completed the mock game through the real REPL. The PydanticAI path
has not yet been tested against a paid live endpoint.

```sh
uv run python -m unittest discover -s tests
```
