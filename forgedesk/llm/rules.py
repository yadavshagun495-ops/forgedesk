"""Deterministic rule-based agent for the booking domain.

Used when no LLM key is configured and by the evaluation harness (deterministic behaviour is
what makes the interruption tests repeatable). Implements the same contract as the LLM:
it reads the message history + structured context and emits text and/or tool calls.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncIterator

from . import LLMDone, LLMEvent, TextDelta, ToolCall
from .prompt import speakable_code

DAY_RE = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\b(today|tomorrow)\b", re.I)
PERIOD_RE = re.compile(r"\b(morning|afternoon|evening|noon)\b", re.I)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|o'?clock)\b", re.I)
STATUS_RE = re.compile(r"still there|status|any luck|how'?s it going|are you there|update|did you find|you there|hello\?", re.I)
REPEAT_RE = re.compile(r"\b(repeat|again|say that|what were|one more time|didn'?t catch)\b", re.I)
CANCEL_RE = re.compile(r"\bcancel\b", re.I)
MOVE_RE = re.compile(r"\b(move|reschedule|change it|switch)\b", re.I)
YES_RE = re.compile(r"^\W*(yes|yeah|yep|sure|ok|okay|that works|book it|go ahead|perfect|sounds good|do it|please)\b", re.I)
ORDINAL_RE = re.compile(r"\b(first|second|third|last|earliest|latest|earlier one|later one)\b", re.I)
CODE_RE = re.compile(r"\bF\s?D[- ]?\s?([A-Z2-9](?:\s?[A-Z2-9]){3})\b", re.I)
NAME_RE = re.compile(r"\b(?:my name is|this is|it'?s|i am|i'm)\s+([A-Z][a-z]+)\b")
VEHICLE_RE = re.compile(r"\b(honda|toyota|ford|hyundai|kia|bmw|audi|tesla|maruti|tata|mahindra|suzuki|nissan|chevy|chevrolet|jeep|mazda|subaru|volkswagen|vw)\b[\w\s-]{0,12}", re.I)
SERVICE_WORDS = ["oil change", "tire rotation", "brake", "battery", "inspection", "service", "alignment"]

DAY_NAMES = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


def _day(text: str) -> str | None:
    m = DAY_RE.search(text)
    if not m:
        return None
    if m.group(2):
        return m.group(2).lower()
    return DAY_NAMES[m.group(1).lower()]


def _period(text: str) -> str:
    m = PERIOD_RE.search(text)
    if not m:
        return "any"
    p = m.group(1).lower()
    return "afternoon" if p == "noon" else p


def _service(text: str) -> str:
    low = text.lower()
    for w in SERVICE_WORDS:
        if w in low:
            return "brake inspection" if w == "brake" else ("battery check" if w == "battery" else w if w != "service" else "general service")
    return "general service"


def _hour(text: str) -> int | None:
    m = TIME_RE.search(text)
    if not m:
        return None
    h = int(m.group(1))
    suffix = m.group(3).lower().replace(".", "")
    if suffix.startswith("p") and h < 12:
        h += 12
    if suffix.startswith("a") and h == 12:
        h = 0
    if suffix.startswith("o") and h <= 7:  # "4 o'clock" at a garage means afternoon
        h += 12
    return h


def _join(times: list[str]) -> str:
    if len(times) <= 1:
        return "".join(times)
    return ", ".join(times[:-1]) + " and " + times[-1]


class RuleAgent:
    name = "rules-v1"

    def __init__(self, model_id: str = "coda", token_delay_ms: float = 4.0) -> None:
        self.model_id = model_id
        self.token_delay_ms = token_delay_ms
        self.offered: list[dict] = []
        self.offered_day = ""
        self.offered_period = "any"
        self.last_code: str | None = None
        self.reschedule_code: str | None = None
        self.customer_name = "the caller"
        self.vehicle = "vehicle on file"
        self.service = "general service"
        self._n = 0
        self.last_first_token_ms = 0.0

    # ------------------------------------------------------------------ public
    async def stream(
        self, messages: list[dict], tools: list[dict], context: dict[str, Any] | None = None
    ) -> AsyncIterator[LLMEvent]:
        context = context or {}
        user_text, tool_results = self._split(messages)
        self._absorb_profile(user_text)
        if tool_results:
            text, calls = self._after_tools(tool_results, user_text)
        else:
            text, calls = self._plan(user_text, context)
        async for ev in self._emit(text, calls):
            yield ev

    # ------------------------------------------------------------------ message parsing
    @staticmethod
    def _split(messages: list[dict]) -> tuple[str, list[tuple[str, dict]]]:
        last_user_idx = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        user_text = messages[last_user_idx].get("content", "") if last_user_idx >= 0 else ""
        names: dict[str, str] = {}
        results: list[tuple[str, dict]] = []
        for m in messages[last_user_idx + 1 :]:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    names[tc["id"]] = tc["function"]["name"]
            elif m.get("role") == "tool":
                import json

                try:
                    payload = json.loads(m.get("content") or "{}")
                except json.JSONDecodeError:
                    payload = {"error": m.get("content")}
                results.append((names.get(m.get("tool_call_id", ""), m.get("name", "")), payload))
        return user_text or "", results

    def _absorb_profile(self, text: str) -> None:
        m = NAME_RE.search(text)
        if m:
            self.customer_name = m.group(1)
        v = VEHICLE_RE.search(text)
        if v:
            self.vehicle = v.group(0).strip()
        if any(w in text.lower() for w in SERVICE_WORDS):
            self.service = _service(text)

    # ------------------------------------------------------------------ planning
    def _call(self, name: str, **args: Any) -> ToolCall:
        self._n += 1
        return ToolCall(f"rc{self._n}", name, args)

    def _plan(self, text: str, ctx: dict) -> tuple[str, list[ToolCall]]:
        pending = ctx.get("pending", [])
        running = [p for p in pending if p["status"] == "running"]
        unreported = ctx.get("unreported", [])
        low = text.strip().lower()
        preface = ""
        if unreported:
            u = unreported[0]
            preface = self._unreported_sentence(u) + " "
            if u.get("code"):
                self.last_code = u["code"]
        code_m = CODE_RE.search(text)
        code = ("FD-" + re.sub(r"[\s-]", "", code_m.group(1)).upper()) if code_m else None

        if not low:
            return preface + "Hi, you've reached Forge Auto Care. Which day works for your service?", []

        if STATUS_RE.search(low) and not _day(low):
            if running:
                p = running[0]
                return (
                    preface + f"Still checking {self._describe(p['args'])}. One moment.",
                    [self._call("await_pending", pending_id=p["pid"])],
                )
            if self.offered:
                return preface + "I'm here. " + self._offer_sentence(), []
            return preface + "I'm here. Which day would you like?", []

        if REPEAT_RE.search(low) and self.offered:
            return preface + self._offer_sentence(), []

        if CANCEL_RE.search(low) and (code or self.last_code) and not _day(low):
            target = code or self.last_code
            return preface + "Cancelling that now.", [self._call("cancel_appointment", code=target)]

        day = _day(low)
        if day:
            period = _period(low)
            self.reschedule_code = None
            if unreported and unreported[0].get("code"):
                self.reschedule_code = unreported[0]["code"]
            elif MOVE_RE.search(low) and (code or self.last_code):
                self.reschedule_code = code or self.last_code
            same = [p for p in running if p["name"] == "check_availability" and self._same_query(p["args"], day, period)]
            if same:
                p = same[0]
                return preface + "Still on it, one moment.", [self._call("await_pending", pending_id=p["pid"])]
            calls = [self._call("cancel_pending", pending_id=p["pid"]) for p in running]
            lead = "Sure, " if calls else ""
            calls.append(self._call("check_availability", day=day, period=period))
            verb = "move it to" if self.reschedule_code else "check"
            sentence = f"{lead}let me {verb} {self._describe({'day': day, 'period': period})}."
            return preface + sentence[0].upper() + sentence[1:], calls

        hour = _hour(low)
        if self.offered and (hour is not None or ORDINAL_RE.search(low) or YES_RE.search(low)):
            slot = self._pick(low, hour)
            if slot is None:
                return preface + "I don't have that time. " + self._offer_sentence(), []
            if self.reschedule_code:
                return (
                    preface + f"Moving you to {slot['day']} at {slot['time']}.",
                    [self._call("reschedule_appointment", code=self.reschedule_code, slot_id=slot["slot_id"])],
                )
            return (
                preface + f"Booking {slot['day']} at {slot['time']} for you.",
                [
                    self._call(
                        "book_appointment",
                        slot_id=slot["slot_id"],
                        customer_name=self.customer_name,
                        vehicle=self.vehicle,
                        service=self.service,
                    )
                ],
            )

        if running:
            p = running[0]
            return preface + f"I'm still checking {self._describe(p['args'])}. Want me to keep going?", []
        return preface + "I can book, move, or cancel a service visit. Which day works for you?", []

    # ------------------------------------------------------------------ after tools
    def _after_tools(self, results: list[tuple[str, dict]], user_text: str) -> tuple[str, list[ToolCall]]:
        sentences: list[str] = []
        for name, payload in results:
            if name == "await_pending" and "result" in payload:
                sentences.append("Got it.")
                name, payload = payload.get("tool", ""), payload["result"]
            if name in ("cancel_pending",):
                continue
            if "error" in payload:
                sentences.append(f"Sorry, {payload['error']}. Would another day work?")
                continue
            if name == "check_availability":
                self.offered = payload.get("slots", [])
                self.offered_day = payload.get("day", "")
                self.offered_period = payload.get("period", "any")
                if not self.offered:
                    sentences.append(f"{self.offered_day} {'' if self.offered_period == 'any' else self.offered_period} is full. Want me to try another day?".replace("  ", " "))
                else:
                    sentences.append(self._offer_sentence())
            elif name == "book_appointment":
                self.last_code = payload["code"]
                self.offered = []
                sentences.append(
                    f"Done. You're booked {payload['day']} at {payload['time']}. "
                    f"Your confirmation code is {speakable_code(payload['code'], self.model_id)}. Anything else?"
                )
            elif name == "reschedule_appointment":
                self.last_code = payload["code"]
                self.reschedule_code = None
                self.offered = []
                sentences.append(
                    f"Done. You're now on {payload['day']} at {payload['time']}. "
                    f"Same code, {speakable_code(payload['code'], self.model_id)}. Anything else?"
                )
            elif name == "cancel_appointment":
                self.offered = []
                sentences.append(f"Cancelled your {payload['day']} {payload['time']} visit. Anything else?")
            elif name == "lookup_appointment":
                sentences.append(f"That's {payload['day']} at {payload['time']} for a {payload['service']}.")
        return " ".join(s for s in sentences if s).strip() or "Okay.", []

    # ------------------------------------------------------------------ helpers
    def _offer_sentence(self) -> str:
        when = self.offered_day + ("" if self.offered_period == "any" else f" {self.offered_period}")
        times = [s["time"] for s in self.offered]
        return f"On {when} I have {_join(times)}. Which works for you?"

    def _pick(self, low: str, hour: int | None) -> dict | None:
        if hour is not None:
            for s in self.offered:
                if s["slot_id"].endswith(f"T{hour:02d}"):
                    return s
            return None
        m = ORDINAL_RE.search(low)
        word = m.group(1) if m else "first"
        idx = {"first": 0, "earliest": 0, "second": 1, "third": 2, "last": -1, "latest": -1, "later one": -1, "earlier one": 0}.get(word, 0)
        try:
            return self.offered[idx]
        except IndexError:
            return self.offered[0]

    @staticmethod
    def _same_query(args: dict, day: str, period: str) -> bool:
        return str(args.get("day", "")).lower() == day.lower() and str(args.get("period", "any")).lower() == period.lower()

    @staticmethod
    def _describe(args: dict) -> str:
        day = str(args.get("day", "")).capitalize()
        period = str(args.get("period", "any"))
        return day if period == "any" else f"{day} {period}"

    def _unreported_sentence(self, u: dict) -> str:
        name = u.get("name", "")
        r = u.get("result", {}) or {}
        if name == "book_appointment":
            return f"Quick note, I did book {r.get('day')} at {r.get('time')}, code {speakable_code(r.get('code', ''), self.model_id)}."
        if name == "reschedule_appointment":
            return f"Quick note, your visit was moved to {r.get('day')} at {r.get('time')}."
        if name == "cancel_appointment":
            return f"Quick note, your {r.get('day')} visit is cancelled."
        return ""

    async def _emit(self, text: str, calls: list[ToolCall]) -> AsyncIterator[LLMEvent]:
        self.last_first_token_ms = 0.0
        words = text.split(" ")
        for i in range(0, len(words), 3):
            piece = " ".join(words[i : i + 3])
            if i + 3 < len(words):
                piece += " "
            yield TextDelta(piece)
            if self.token_delay_ms:
                await asyncio.sleep(self.token_delay_ms / 1000.0)
        for c in calls:
            yield c
        yield LLMDone(calls)
