"""Epoch fencing.

Every agent response belongs to an epoch. A user barge-in or a new user turn bumps the
epoch; anything still carrying an older epoch (LLM tokens, tool results, TTS audio) is
stale and must never reach the speaker or the application state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable


class StaleEpoch(Exception):
    """Raised when work tagged with an old epoch tries to commit or speak."""


@dataclass(frozen=True)
class FenceToken:
    epoch: int
    _fence: "Fence" = field(repr=False, compare=False)

    @property
    def valid(self) -> bool:
        return self._fence.epoch == self.epoch

    def check(self, what: str = "operation") -> None:
        if not self.valid:
            raise StaleEpoch(f"{what} from epoch {self.epoch} is stale (current {self._fence.epoch})")


class Fence:
    def __init__(self) -> None:
        self._epoch = 0
        self._listeners: list[Callable[[int, int], None]] = []
        self._events: dict[int, asyncio.Event] = {}

    @property
    def epoch(self) -> int:
        return self._epoch

    def token(self) -> FenceToken:
        return FenceToken(self._epoch, self)

    def is_current(self, epoch: int) -> bool:
        return epoch == self._epoch

    def bump(self) -> int:
        old = self._epoch
        self._epoch += 1
        ev = self._events.pop(old, None)
        if ev:
            ev.set()
        for cb in list(self._listeners):
            cb(old, self._epoch)
        return self._epoch

    def on_bump(self, cb: Callable[[int, int], None]) -> None:
        self._listeners.append(cb)

    def invalidated(self, epoch: int) -> asyncio.Event:
        """Event that fires when `epoch` stops being current. Useful for racing waits."""
        if epoch != self._epoch:
            ev = asyncio.Event()
            ev.set()
            return ev
        return self._events.setdefault(epoch, asyncio.Event())


async def race_with_fence(coro, token: FenceToken):
    """Await `coro` but abandon it (cancelled) as soon as `token` becomes stale."""
    task = asyncio.ensure_future(coro)
    inval = token._fence.invalidated(token.epoch)
    waiter = asyncio.ensure_future(inval.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        raise StaleEpoch(f"epoch {token.epoch} invalidated while waiting")
    finally:
        if not waiter.done():
            waiter.cancel()
