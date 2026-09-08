"""OpenAI-compatible Chat Completions streaming client (OpenAI, Groq, OpenRouter, Ollama...)."""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from . import LLMDone, LLMEvent, TextDelta, ToolCall


class OpenAICompatLLM:
    def __init__(self, api_key: str, base_url: str, model: str, temperature: float = 0.3, http_client: Any = None) -> None:
        self.name = f"openai-compat:{model}"
        self.model = model
        self.temperature = temperature
        kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        if http_client is not None:  # tests inject a mock transport here
            kwargs["http_client"] = http_client
        self.client = AsyncOpenAI(**kwargs)
        self.last_first_token_ms: float | None = None

    async def stream(
        self, messages: list[dict], tools: list[dict], context: dict[str, Any] | None = None
    ) -> AsyncIterator[LLMEvent]:
        t0 = time.monotonic()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await self.client.chat.completions.create(**kwargs)
        partial: dict[int, dict] = {}
        first = True
        async for chunk in resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                if first:
                    self.last_first_token_ms = (time.monotonic() - t0) * 1000.0
                    first = False
                yield TextDelta(delta.content)
            for tc in delta.tool_calls or []:
                slot = partial.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments
        calls: list[ToolCall] = []
        for idx in sorted(partial):
            slot = partial[idx]
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(slot["id"] or f"call_{idx}", slot["name"], args))
        for c in calls:
            yield c
        yield LLMDone(calls)
