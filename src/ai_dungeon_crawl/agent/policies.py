import asyncio

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import UsageLimits

from ..contracts import AgentTurn, Policy, AgentTurnRecord
from ..config import AgentConfig
from ..events import events_enabled
from .prompts import ActionPrompts, base_prompts, read_only_prompt
from .tools.execute_shell import (EXECUTE_SHELL_NAME, execute_shell_reference,
                                   execute_shell_tool, execution_feedback)

SYSTEM_PROMPT = base_prompts().system_prompt


SHELL_DESCRIPTION = base_prompts().execute_shell


REVIEW_SYSTEM_PROMPT = read_only_prompt('review_agent/system_prompt')

REVIEW_SHELL_DESCRIPTION = read_only_prompt('review_agent/execute_shell')


class PydanticPolicy(Policy):
    """One structured-output request cycle, using the selected API provider.

    Owns the full in-memory conversation for exactly one action or review phase.
    No game tools, model fallback, history pruning, or script retries.
    Provider SDK transport retries are distinct from the logical request count.
    """

    backend = "pydantic"

    def __init__(self, config: AgentConfig, *, model: Model | None = None,
                 initial_prompt: str = "Play DCSS and survive.",
                 profile: str = "action", prompts: ActionPrompts | None = None):
        if config.backend != self.backend:
            raise ValueError(f"{type(self).__name__} requires backend={self.backend!r}")
        self.config = config
        self.model = model if model is not None else config.model
        self.initial_prompt = initial_prompt
        self._messages = []
        self._pending_call = None
        self._pending_turn = None
        self._accepted_turns = 0
        if profile not in ("action", "review"):
            raise ValueError("Unknown policy profile")
        self.profile = profile
        self.prompts = prompts or ActionPrompts(SYSTEM_PROMPT, initial_prompt, SHELL_DESCRIPTION)
        if profile == "action":
            self.initial_prompt = self.prompts.initial_prompt

    async def request_turn(self, history: tuple[AgentTurnRecord, ...]) -> AgentTurn:
        return await self._request_turn(self.model, history)

    def model_settings(self):
        settings = {"max_tokens": 4096, "parallel_tool_calls": False,
                    "openai_truncation": "auto"}
        if self.config.reasoning_effort != "default":
            settings["openai_reasoning_effort"] = self.config.reasoning_effort
        if self.config.reasoning_summary:
            settings["openai_reasoning_summary"] = "detailed"
        return settings

    def prompt_reference(self):
        return {"prompts": self.prompts.effective_text(),
                "tool": execute_shell_reference(self.prompts.execute_shell),
                "schema_scope": "Provider-independent validated output tool; providers may transform JSON schema.",
                "requested_policy_model_settings": self.model_settings(),
                "model_timeout_seconds": self.config.timeout_seconds, "max_requests": self.config.max_requests,
                "fixed_constraints": "Exactly one validated execute_shell output ends each turn. No network or personal-file access. "
                "Shell source <= 65536 UTF-8 bytes; timeout 1-180000 ms. Tool/schema/security remain code-owned. "
                "Initial prompt is sent once; complete native conversation and execution feedback persist within one episode."}

    async def _request_turn(self, model: str | Model,
                            history: tuple[AgentTurnRecord, ...]) -> AgentTurn:
        """Accept only one output-tool submission; execution belongs to the runner."""
        if len(history) != self._accepted_turns:
            raise ValueError("Conversation history is out of sync; use a fresh policy for each phase")
        messages = list(self._messages)
        if self._pending_call is not None:
            if history[-1].turn != self._pending_turn:
                raise ValueError("Execution feedback does not match the pending shell submission")
            messages.append(ModelRequest(parts=[ToolReturnPart(
                EXECUTE_SHELL_NAME, execution_feedback(history[-1].execution),
                tool_call_id=self._pending_call.tool_call_id,
            )]))
        if events_enabled():
            from .model_stream import ObservedModel
            model = ObservedModel(model)
        # This is an output tool: Pydantic validates data, but does not execute code.
        agent = Agent(
            model, instructions=REVIEW_SYSTEM_PROMPT if self.profile == "review" else self.prompts.system_prompt,
            output_type=execute_shell_tool(
                REVIEW_SHELL_DESCRIPTION if self.profile == "review" else self.prompts.execute_shell),
            retries=self.config.max_requests - 1,
            model_settings=self.model_settings(),
        )
        async with asyncio.timeout(self.config.timeout_seconds):
            async with agent:
                result = await agent.run(self.initial_prompt if not messages else None,
                                         message_history=messages,
                                         usage_limits=UsageLimits(request_limit=self.config.max_requests))
        transcript = result.all_messages()
        response_index = next(index for index in range(len(transcript) - 1, -1, -1)
                              if isinstance(transcript[index], ModelResponse))
        response = transcript[response_index]
        calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
        if len(calls) != 1 or calls[0].tool_name != EXECUTE_SHELL_NAME:
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

    backend = "codex"

    def __init__(self, config: AgentConfig, *, auth_path=None, http_client=None, **kwargs):
        super().__init__(config, **kwargs)
        self.auth_path, self.http_client = auth_path, http_client

    def prompt_reference(self):
        reference = super().prompt_reference()
        reference['backend_overrides'] = {
            'removed_settings': ['max_tokens', 'temperature', 'top_p'],
            'openai_store': False, 'openai_send_reasoning_ids': True,
            'openai_truncation': 'disabled',
        }
        return reference

    async def request_turn(self, history: tuple[AgentTurnRecord, ...]) -> AgentTurn:
        from .codex import codex_model
        async with codex_model(self.model, self.auth_path, self.http_client) as model:
            return await self._request_turn(model, history)


def create_policy(config: AgentConfig, *, initial_prompt: str = "Play DCSS and survive.",
                  profile: str = "action",
                  prompts: ActionPrompts | None = None) -> Policy:
    """Explicit selection; never fall back from subscription access to paid APIs."""
    policy_type = CodexPolicy if config.backend == "codex" else PydanticPolicy
    return policy_type(config, initial_prompt=initial_prompt, profile=profile, prompts=prompts)
