"""Validated, immutable settings for agents and their action/review session."""
from dataclasses import asdict, dataclass, field
import math


REASONING_EFFORTS = ("default", "none", "minimal", "low", "medium", "high", "xhigh")


@dataclass(frozen=True)
class AgentConfig:
    """One agent's provider settings; max_requests includes output repair attempts."""
    backend: str = "codex"
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "default"
    reasoning_summary: bool = True
    timeout_seconds: float = 120
    max_requests: int = 2

    def __post_init__(self):
        if self.backend not in ("codex", "pydantic"):
            raise ValueError(f"Unknown policy backend: {self.backend}")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("An explicit model is required")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("Invalid reasoning effort")
        if self.backend == "pydantic":
            provider, separator, name = self.model.partition(":")
            if not separator or not provider.strip() or not name.strip():
                raise ValueError("The Pydantic backend requires an explicit provider:model")
            if self.reasoning_effort != "default" and provider not in ("openai", "openai-chat", "openai-responses"):
                raise ValueError("Reasoning strength requires a Codex or OpenAI model; use default for other providers")
        if type(self.reasoning_summary) is not bool:
            raise ValueError("Reasoning summary must be a boolean")
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("Model timeout must be finite and positive")
        if type(self.max_requests) is not int or self.max_requests < 1:
            raise ValueError("Model request limit must be a positive integer")


@dataclass(frozen=True)
class SessionConfig:
    """Fresh action/review agents and turn budgets across one or more episodes."""
    action_agent: AgentConfig = field(default_factory=AgentConfig)
    review_agent: AgentConfig = field(default_factory=AgentConfig)
    action_turn_limit: int = 3
    review_turn_limit: int = 3
    episode_limit: int = 1

    def __post_init__(self):
        if not isinstance(self.action_agent, AgentConfig) or not isinstance(self.review_agent, AgentConfig):
            raise ValueError("Action and review agents must be AgentConfig instances")
        if any(type(n) is not int or n < 0 for n in (self.action_turn_limit, self.review_turn_limit)):
            raise ValueError("Turn limits must be nonnegative integers")
        if type(self.episode_limit) is not int or self.episode_limit < 1:
            raise ValueError("Episode limit must be a positive integer")

    def event_data(self):
        """Include established dashboard fields alongside both complete agent settings."""
        return {**asdict(self), "backend": self.action_agent.backend,
                "model": self.action_agent.model, "max_turns": self.action_turn_limit,
                "reasoning_effort": self.action_agent.reasoning_effort,
                "reasoning_summary": self.action_agent.reasoning_summary, "game": "dcss"}
