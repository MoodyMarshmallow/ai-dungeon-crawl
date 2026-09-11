import asyncio
import json
import math

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.models import Model
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import UsageLimits
from pydantic_ai.tools import GenerateToolJsonSchema

from ..contracts import AgentTurn, GameObservation, Policy, AgentTurnRecord, validate_source
from ..events import events_enabled
from .prompts import ActionPrompts, base_prompts, read_only_prompt

REASONING_EFFORTS = ("default", "none", "minimal", "low", "medium", "high", "xhigh")

SYSTEM_PROMPT = base_prompts().system_prompt


SHELL_DESCRIPTION = base_prompts().execute_shell


REVIEW_SYSTEM_PROMPT = read_only_prompt('review_agent/system_prompt')

REVIEW_SHELL_DESCRIPTION = read_only_prompt('review_agent/execute_shell')


class ShellScript(BaseModel):
    """Shell source for the harness to execute, not in this model runtime."""

    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=65536,
                      description="Bash script, at most 64 KiB UTF-8.")
    timeout_ms: int | None = Field(default=None, ge=1, le=180000,
                                  description="Execution timeout in milliseconds; null uses the configured default (normally 5000). Maximum 180000.")

    @field_validator("code")
    @classmethod
    def check_source_size(cls, value: str) -> str:
        validate_source(value)
        return value


def _execution_feedback(execution) -> str:
    """Only explicit shell feedback crosses into the model conversation."""
    return json.dumps({"output": execution.output, "error": execution.error,
                       "status": execution.status,
                       "output_truncated": execution.output_truncated}, ensure_ascii=False)


def _check_limits(timeout_seconds: float):
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Model timeout must be finite and positive")


class PydanticPolicy:
    """One structured-output request cycle, using the selected API provider.

    Owns the full in-memory conversation for exactly one action or review phase.
    No game tools, model fallback, history pruning, or script retries.
    Provider SDK transport retries are distinct from the logical request count.
    """

    def __init__(self, model: str | Model, *, initial_prompt: str = "Play DCSS and survive.",
                 timeout_seconds: float = 120, max_requests: int = 2,
                 reasoning_summary: bool = False, reasoning_effort: str = "default",
                 profile: str = "action", prompts: ActionPrompts | None = None):
        _check_limits(timeout_seconds)
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        self.model, self.initial_prompt = model, initial_prompt
        self.timeout_seconds, self.max_requests = timeout_seconds, max_requests
        self._messages = []
        self._pending_call = None
        self._pending_turn = None
        self._accepted_turns = 0
        self.reasoning_summary = reasoning_summary
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("Invalid reasoning effort")
        self.reasoning_effort = reasoning_effort
        if profile not in ("action", "review"):
            raise ValueError("Unknown policy profile")
        self.profile = profile
        self.prompts = prompts or ActionPrompts(SYSTEM_PROMPT, initial_prompt, SHELL_DESCRIPTION)
        if profile == "action":
            self.initial_prompt = self.prompts.initial_prompt

    async def request_turn(self, observation: GameObservation,
                           history: tuple[AgentTurnRecord, ...]) -> AgentTurn:
        return await self._request_turn(self.model, observation, history)

    def model_settings(self):
        settings = {"max_tokens": 4096, "parallel_tool_calls": False,
                    "openai_truncation": "auto"}
        if self.reasoning_effort != "default":
            settings["openai_reasoning_effort"] = self.reasoning_effort
        if self.reasoning_summary:
            settings["openai_reasoning_summary"] = "detailed"
        return settings

    def prompt_reference(self):
        schema = ShellScript.model_json_schema(schema_generator=GenerateToolJsonSchema)
        fixed_description = schema.pop('description')
        return {"prompts": self.prompts.effective_text(),
                "tool": {"name": "execute_shell", "description": self.prompts.execute_shell + '. ' + fixed_description,
                         "parameters_json_schema": schema},
                "schema_scope": "Provider-independent validated output tool; providers may transform JSON schema.",
                "requested_policy_model_settings": self.model_settings(),
                "model_timeout_seconds": self.timeout_seconds, "max_requests": self.max_requests,
                "fixed_constraints": "Exactly one validated execute_shell output ends each turn. No network or personal-file access. "
                "Shell source <= 65536 UTF-8 bytes; timeout 1-180000 ms. Tool/schema/security remain code-owned. "
                "Initial prompt is sent once; complete native conversation and execution feedback persist within one episode."}

    async def _request_turn(self, model: str | Model, observation: GameObservation,
                            history: tuple[AgentTurnRecord, ...]) -> AgentTurn:
        """Accept only one output-tool submission; execution belongs to the runner."""
        if len(history) != self._accepted_turns:
            raise ValueError("Conversation history is out of sync; use a fresh policy for each phase")
        messages = list(self._messages)
        if self._pending_call is not None:
            if history[-1].turn != self._pending_turn:
                raise ValueError("Execution feedback does not match the pending shell submission")
            messages.append(ModelRequest(parts=[ToolReturnPart(
                "execute_shell", _execution_feedback(history[-1].execution),
                tool_call_id=self._pending_call.tool_call_id,
            )]))
        if events_enabled():
            from .model_stream import ObservedModel
            model = ObservedModel(model)
        # This is an output tool: Pydantic validates data, but does not execute code.
        agent = Agent(
            model, instructions=REVIEW_SYSTEM_PROMPT if self.profile == "review" else self.prompts.system_prompt,
            output_type=ToolOutput(ShellScript, name="execute_shell", description=(
                REVIEW_SHELL_DESCRIPTION if self.profile == "review" else self.prompts.execute_shell)),
            retries=self.max_requests - 1,
            model_settings=self.model_settings(),
        )
        async with asyncio.timeout(self.timeout_seconds):
            async with agent:
                result = await agent.run(self.initial_prompt if not messages else None,
                                         message_history=messages,
                                         usage_limits=UsageLimits(request_limit=self.max_requests))
        transcript = result.all_messages()
        response_index = next(index for index in range(len(transcript) - 1, -1, -1)
                              if isinstance(transcript[index], ModelResponse))
        response = transcript[response_index]
        calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
        if len(calls) != 1 or calls[0].tool_name != "execute_shell":
            raise ValueError("An agent turn must submit exactly one execute_shell tool call")
        turn = AgentTurn(result.output.code, model_requests=result.usage.requests,
                         timeout_ms=result.output.timeout_ms)
        # Pydantic adds an output-tool acknowledgement after the final response.
        # Keep the native response (including opaque provider reasoning) intact,
        # but replace that acknowledgement next turn with actual runner feedback.
        self._messages = transcript[:response_index + 1]
        self._pending_call, self._pending_turn = calls[0], turn
        self._accepted_turns += 1
        return turn


class CodexPolicy(PydanticPolicy):
    """The same output-tool policy over subscription-authenticated Responses.

    No Codex agent subprocess or final-message parser. Authentication is read
    from the CLI's file-backed login; expired credentials require re-login.
    """

    def __init__(self, model: str, *, auth_path=None, http_client=None, **kwargs):
        if not model:
            raise ValueError("The Codex backend requires an explicit model")
        super().__init__(model, **kwargs)
        self.auth_path, self.http_client = auth_path, http_client

    def prompt_reference(self):
        reference = super().prompt_reference()
        reference['backend_overrides'] = {
            'removed_settings': ['max_tokens', 'temperature', 'top_p'],
            'openai_store': False, 'openai_send_reasoning_ids': True,
            'openai_truncation': 'disabled',
        }
        return reference

    async def request_turn(self, observation: GameObservation,
                           history: tuple[AgentTurnRecord, ...]) -> AgentTurn:
        from .codex import codex_model
        async with codex_model(self.model, self.auth_path, self.http_client) as model:
            return await self._request_turn(model, observation, history)


def create_policy(backend: str, *, model: str | None = None,
                  initial_prompt: str = "Play DCSS and survive.", reasoning_summary: bool = False,
                  reasoning_effort: str = "default", profile: str = "action",
                  prompts: ActionPrompts | None = None) -> Policy:
    """Explicit selection; never fall back from subscription access to paid APIs."""
    if backend == "codex":
        if not model:
            raise ValueError("The Codex backend requires an explicit model")
        return CodexPolicy(model, initial_prompt=initial_prompt, reasoning_summary=reasoning_summary,
                           reasoning_effort=reasoning_effort, profile=profile, prompts=prompts)
    if backend == "pydantic":
        if not model:
            raise ValueError("The Pydantic backend requires an explicit provider:model")
        if reasoning_effort != "default" and model.split(":", 1)[0] not in ("openai", "openai-chat", "openai-responses"):
            raise ValueError("Reasoning strength requires a Codex or OpenAI model; use default for other providers")
        return PydanticPolicy(model, initial_prompt=initial_prompt, reasoning_summary=reasoning_summary,
                              reasoning_effort=reasoning_effort, profile=profile, prompts=prompts)
    raise ValueError(f"Unknown policy backend: {backend}")
