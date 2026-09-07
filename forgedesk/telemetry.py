"""Structured telemetry: every user-visible timing is recorded as an event.

Events are appended to a JSONL file (evidence/runs/<run_id>.jsonl) and fanned out to
subscribers (the web UI shows them live). All timestamps are monotonic milliseconds
relative to the session start so runs are comparable.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

Subscriber = Callable[[dict], Awaitable[None] | None]


def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Telemetry:
    run_id: str
    path: str | None = None
    session_id: str = "local"
    events: list[dict] = field(default_factory=list)
    _subs: list[Subscriber] = field(default_factory=list)
    _t0: float = field(default_factory=time.monotonic)
    _fh: Any = None

    def __post_init__(self) -> None:
        if self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")

    def now_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def subscribe(self, cb: Subscriber) -> None:
        self._subs.append(cb)

    def emit(self, kind: str, **fields: Any) -> dict:
        ev = {"t_ms": round(self.now_ms(), 2), "kind": kind, "session": self.session_id, **fields}
        self.events.append(ev)
        if self._fh:
            self._fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._fh.flush()
        for cb in self._subs:
            try:
                res = cb(ev)
                if asyncio.iscoroutine(res):
                    asyncio.ensure_future(res)
            except Exception:  # telemetry must never break the voice loop
                pass
        return ev

    def find(self, kind: str, **match: Any) -> list[dict]:
        out = []
        for ev in self.events:
            if ev["kind"] != kind:
                continue
            if all(ev.get(k) == v for k, v in match.items()):
                out.append(ev)
        return out

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{os.getpid() % 10000:04d}"
