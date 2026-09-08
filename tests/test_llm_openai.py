"""The OpenAI-compatible LLM path, exercised against a mocked streaming endpoint.

No API key is involved: an httpx MockTransport replays the exact SSE frame shape that OpenAI,
Groq and OpenRouter emit, so the streaming/tool-call parsing is verified rather than assumed.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from forgedesk.llm import LLMDone, TextDelta, ToolCall
from forgedesk.llm.openai_compat import OpenAICompatLLM


def sse(chunks: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def delta(**d) -> dict:
    return {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": [{"index": 0, "delta": d, "finish_reason": None}]}


def make_llm(chunks: list[dict], captured: dict | None = None) -> OpenAICompatLLM:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.update(json.loads(request.content))
        return httpx.Response(200, content=sse(chunks), headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatLLM("sk-test", "https://mock.invalid/v1", "gpt-4o-mini", http_client=client)


async def collect(llm, messages=None, tools=None):
    out = []
    async for ev in llm.stream(messages or [{"role": "user", "content": "hi"}], tools or []):
        out.append(ev)
    return out


async def test_text_only_stream():
    llm = make_llm([delta(role="assistant"), delta(content="Let me check "), delta(content="Thursday afternoon.")])
    events = await collect(llm)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Let me check Thursday afternoon."
    assert isinstance(events[-1], LLMDone) and events[-1].tool_calls == []
    assert llm.last_first_token_ms is not None


async def test_tool_call_arguments_are_reassembled():
    llm = make_llm(
        [
            delta(content="Sure. "),
            delta(tool_calls=[{"index": 0, "id": "call_1", "type": "function", "function": {"name": "check_ava", "arguments": '{"day"'}}]),
            delta(tool_calls=[{"index": 0, "function": {"name": "ilability", "arguments": ': "Thursday", "period": "afternoon"}'}}]),
        ]
    )
    events = await collect(llm)
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "check_availability"  # name can arrive split across chunks
    assert calls[0].args == {"day": "Thursday", "period": "afternoon"}
    assert events[-1].tool_calls == calls


async def test_parallel_tool_calls_keep_their_order():
    llm = make_llm(
        [
            delta(tool_calls=[{"index": 1, "id": "b", "function": {"name": "cancel_pending", "arguments": '{"pending_id": "p2"}'}}]),
            delta(tool_calls=[{"index": 0, "id": "a", "function": {"name": "await_pending", "arguments": '{"pending_id": "p1"}'}}]),
        ]
    )
    calls = [e for e in await collect(llm) if isinstance(e, ToolCall)]
    assert [c.name for c in calls] == ["await_pending", "cancel_pending"]


async def test_malformed_tool_arguments_do_not_crash_the_turn():
    llm = make_llm([delta(tool_calls=[{"index": 0, "id": "a", "function": {"name": "book_appointment", "arguments": "{not json"}}])])
    calls = [e for e in await collect(llm) if isinstance(e, ToolCall)]
    assert calls[0].args == {}


async def test_tools_and_history_are_sent_as_given():
    captured: dict = {}
    llm = make_llm([delta(content="ok")], captured)
    tools = [{"type": "function", "function": {"name": "check_availability", "parameters": {}}}]
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Tuesday please"}]
    await collect(llm, messages, tools)
    assert captured["messages"] == messages
    assert captured["tools"] == tools and captured["tool_choice"] == "auto"
    assert captured["stream"] is True and captured["model"] == "gpt-4o-mini"


async def test_empty_choices_chunk_is_ignored():
    """Some providers send a usage-only chunk with an empty choices list."""
    llm = make_llm([{"choices": []}, delta(content="fine")])
    events = await collect(llm)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "fine"


async def test_session_sends_a_valid_tool_calling_history():
    """The session must send back an assistant message with tool_calls, then a matching tool result."""
    import datetime as dt

    from forgedesk.session import Session
    from forgedesk.stt import BaseSTT
    from forgedesk.telemetry import Telemetry
    from forgedesk.tools import BookingStore
    from forgedesk.transport import SimulatedClient
    from forgedesk.tts.fake import FakeTTS
    from eval.harness import make_settings

    requests: list[dict] = []
    rounds = [
        [delta(content="Let me check Thursday afternoon. "),
         delta(tool_calls=[{"index": 0, "id": "call_1", "type": "function",
                            "function": {"name": "check_availability", "arguments": '{"day": "Thursday", "period": "afternoon"}'}}])],
        [delta(content="I have 4 PM. Does that work?")],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        chunks = rounds[min(len(requests) - 1, len(rounds) - 1)]
        return httpx.Response(200, content=sse(chunks), headers={"content-type": "text/event-stream"})

    llm = OpenAICompatLLM("sk-test", "https://mock.invalid/v1", "gpt-4o-mini",
                          http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    settings = make_settings(0, "fake")
    tts = FakeTTS(sample_rate=settings.rime.sample_rate, realtime_factor=0.0)
    client = SimulatedClient(sample_rate_out=tts.sample_rate)
    tel = Telemetry("llm-session")
    session = Session(settings, client, tts, BaseSTT(), llm, tel,
                      store=BookingStore(today=dt.date(2026, 9, 7)), today=dt.date(2026, 9, 7))
    task = asyncio.ensure_future(session.run())
    try:
        await session.end_of_turn("I need an oil change Thursday afternoon.")
        await asyncio.wait_for(session.active, timeout=20)
    finally:
        session.stop()
        await tts.aclose()
        task.cancel()

    assert len(requests) == 2, "the tool result must trigger a second completion"
    second = requests[1]["messages"]
    assistant = next(m for m in second if m["role"] == "assistant" and m.get("tool_calls"))
    assert assistant["tool_calls"][0]["function"]["name"] == "check_availability"
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == assistant["tool_calls"][0]["id"]
    assert json.loads(tool_msg["content"])["day"] == "Thursday"
    assert [m["role"] for m in second][:2] == ["system", "user"]
    spoken = " ".join(t for texts in client.speak_texts.values() for t in texts)
    assert "Let me check Thursday afternoon." in spoken and "Does that work?" in spoken
