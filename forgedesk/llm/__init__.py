"""LLM adapters: an OpenAI-compatible streaming client and a deterministic rule agent.

The rule agent makes the whole product runnable with zero LLM keys and gives the evaluation
harness deterministic behaviour; the real LLM is a drop-in replacement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, Union


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMDone:
    tool_calls: list[ToolCall] = field(default_factory=list)


LLMEvent = Union[TextDelta, ToolCall, LLMDone]


class LLM(Protocol):
    name: str

    def stream(self, messages: list[dict], tools: list[dict], context: dict[str, Any] | None = None) -> AsyncIterator[LLMEvent]: ...


def build_llm(settings) -> LLM:
    kind = settings.resolved_llm()
    if kind == "openai":
        from .openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(settings.llm_api_key, settings.llm_base_url, settings.llm_model)
    if kind == "rules":
        from .rules import RuleAgent

        return RuleAgent(model_id=settings.rime.model_id)
    raise ValueError(f"unknown LLM provider {kind!r}")
