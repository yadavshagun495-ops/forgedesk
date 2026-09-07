"""Cancellable, epoch-fenced tool execution with a registry of orphaned work.

Rules enforced here (these are the "continuity during tool work" guarantees):
1. A tool result may only be *spoken* by the epoch that requested it, or explicitly
   reconciled into a later epoch through `await_pending`. Late results from an old epoch are
   fenced: recorded, never spoken as current.
2. Read-only lookups survive a barge-in (orphaned, still running) so "what's the status?"
   or "still Thursday, but morning" can reuse or drop them without losing context.
3. Mutations that have not committed when the user interrupts are cancelled; the store also
   re-checks the fence at commit time, so a stale mutation can never land.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..fence import Fence, FenceToken, StaleEpoch
from ..telemetry import Telemetry
from .booking import MUTATIONS, TOOL_SCHEMAS, BookingError, BookingStore

META_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "await_pending",
            "description": "Wait for a background lookup started before the user interrupted, and get its result.",
            "parameters": {"type": "object", "properties": {"pending_id": {"type": "string"}}, "required": ["pending_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_pending",
            "description": "Discard a background lookup that is no longer relevant.",
            "parameters": {"type": "object", "properties": {"pending_id": {"type": "string"}}, "required": ["pending_id"]},
        },
    },
]


@dataclass
class PendingTool:
    pid: str
    name: str
    args: dict
    epoch: int
    task: asyncio.Task
    started_ms: float
    status: str = "running"  # running | done | failed | cancelled | fenced
    result: Any = None
    error: str | None = None
    consumed_epoch: int | None = None
    reported: bool = False  # the user actually heard the outcome
    reconciling_epoch: int | None = None  # a later epoch is explicitly awaiting this result
    orphaned: bool = False
    finished_ms: float | None = None

    @property
    def is_mutation(self) -> bool:
        return self.name in MUTATIONS

    def summary(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.args.items())
        return f"{self.name}({args})"


class ToolRunner:
    def __init__(self, store: BookingStore, fence: Fence, telemetry: Telemetry, delay_ms: int = 0) -> None:
        self.store = store
        self.fence = fence
        self.tel = telemetry
        self.delay_ms = delay_ms
        self.pending: dict[str, PendingTool] = {}
        self._n = 0
        self.stale_results_fenced = 0
        fence.on_bump(self._on_bump)

    # ------------------------------------------------------------------ schemas
    @staticmethod
    def schemas() -> list[dict]:
        return TOOL_SCHEMAS + META_TOOL_SCHEMAS

    # ------------------------------------------------------------------ execution
    async def _execute(self, name: str, args: dict, token: FenceToken) -> Any:
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000.0)  # simulated slow backend
        fn = getattr(self.store, name)
        if name in MUTATIONS:
            return await fn(**args, token=token)
        return await fn(**args)

    def _on_bump(self, old: int, new: int) -> None:
        for p in self.pending.values():
            if p.status != "running" or p.epoch != old:
                continue
            if p.is_mutation:
                p.task.cancel()
                self.tel.emit("tool_cancelled", pid=p.pid, name=p.name, epoch=p.epoch, reason="interrupted before commit")
            else:
                p.orphaned = True
                self.tel.emit("tool_orphaned", pid=p.pid, name=p.name, epoch=p.epoch)

    def _start(self, name: str, args: dict, token: FenceToken) -> PendingTool:
        self._n += 1
        pid = f"p{self._n}"
        task = asyncio.ensure_future(self._execute(name, args, token))
        p = PendingTool(pid, name, args, token.epoch, task, self.tel.now_ms())
        self.pending[pid] = p
        self.tel.emit("tool_start", pid=pid, name=name, args=args, epoch=token.epoch, delay_ms=self.delay_ms)
        task.add_done_callback(lambda t, p=p: self._finish(p))
        return p

    def _finish(self, p: PendingTool) -> None:
        p.finished_ms = self.tel.now_ms()
        dur = round(p.finished_ms - p.started_ms, 1)
        if p.task.cancelled():
            p.status = "cancelled"
            self.tel.emit("tool_end", pid=p.pid, name=p.name, status="cancelled", ms=dur, epoch=p.epoch)
            return
        exc = p.task.exception()
        if isinstance(exc, StaleEpoch):
            p.status = "fenced"
            p.error = str(exc)
            self.tel.emit("tool_end", pid=p.pid, name=p.name, status="fenced_at_commit", ms=dur, epoch=p.epoch)
            return
        if exc is not None:
            p.status = "failed"
            p.error = str(exc)
            self.tel.emit("tool_end", pid=p.pid, name=p.name, status="failed", error=p.error, ms=dur, epoch=p.epoch)
            return
        p.status = "done"
        p.result = p.task.result()
        stale = not self.fence.is_current(p.epoch) and p.reconciling_epoch != self.fence.epoch
        if stale:
            self.stale_results_fenced += 1
            self.tel.emit(
                "tool_stale_result_fenced", pid=p.pid, name=p.name, epoch=p.epoch,
                current_epoch=self.fence.epoch, ms=dur, note="arrived after interruption; not spoken",
            )
        else:
            self.tel.emit("tool_end", pid=p.pid, name=p.name, status="done", ms=dur, epoch=p.epoch)

    async def _wait(self, p: PendingTool, token: FenceToken) -> Any:
        """Wait for p while token stays current. Does not cancel p on staleness."""
        inval = self.fence.invalidated(token.epoch)
        waiter = asyncio.ensure_future(inval.wait())
        try:
            done, _ = await asyncio.wait({p.task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not token.valid:  # interrupted: the caller is stale regardless of the task's fate
                raise StaleEpoch(f"{p.summary()} abandoned: epoch {token.epoch} interrupted")
            if p.task in done:
                if p.task.cancelled():
                    raise BookingError(f"{p.summary()} was cancelled")
                return p.task.result()
            raise StaleEpoch(f"{p.summary()} abandoned: epoch {token.epoch} interrupted")
        finally:
            if not waiter.done():
                waiter.cancel()

    async def call(self, name: str, args: dict, token: FenceToken) -> dict:
        """Run a tool on behalf of `token`'s epoch; returns a JSON-able result for the LLM."""
        token.check(f"tool call {name}")
        if name == "await_pending":
            return await self._await_pending(args.get("pending_id", ""), token)
        if name == "cancel_pending":
            return self._cancel_pending(args.get("pending_id", ""))
        if not hasattr(self.store, name):
            return {"error": f"unknown tool {name}"}
        p = self._start(name, args, token)
        try:
            result = await self._wait(p, token)
        except BookingError as e:
            return {"error": str(e)}
        p.consumed_epoch = token.epoch
        return result

    # ------------------------------------------------------------------ reconciliation
    async def _await_pending(self, pid: str, token: FenceToken) -> dict:
        p = self.pending.get(pid)
        if not p:
            return {"error": f"no pending work {pid}"}
        if p.status == "cancelled":
            return {"error": f"{p.summary()} was cancelled"}
        p.reconciling_epoch = token.epoch
        try:
            result = await self._wait(p, token)
        except BookingError as e:
            return {"error": str(e)}
        p.consumed_epoch = token.epoch
        self.tel.emit("tool_result_reconciled", pid=pid, name=p.name, from_epoch=p.epoch, into_epoch=token.epoch)
        return {"pending_id": pid, "tool": p.name, "args": p.args, "result": result}

    def _cancel_pending(self, pid: str) -> dict:
        p = self.pending.get(pid)
        if not p:
            return {"error": f"no pending work {pid}"}
        if p.status == "running":
            p.task.cancel()
        p.consumed_epoch = self.fence.epoch  # explicitly discarded by the current epoch
        self.tel.emit("tool_discarded", pid=pid, name=p.name, epoch=p.epoch, by_epoch=self.fence.epoch)
        return {"pending_id": pid, "discarded": True}

    def unconsumed(self) -> list[PendingTool]:
        return [p for p in self.pending.values() if p.consumed_epoch is None and p.status in ("running", "done")]

    def unreported_mutations(self) -> list[PendingTool]:
        return [p for p in self.pending.values() if p.is_mutation and p.status == "done" and not p.reported]

    def context_note(self) -> str:
        """Text the LLM sees about background work the user has not been told about yet."""
        lines = []
        now = self.tel.now_ms()
        for p in self.unreported_mutations():
            lines.append(
                f"- {p.summary()} COMPLETED but the user has not been told. Result: {json.dumps(p.result)}. "
                f"Report or reconcile it (pending_id={p.pid})."
            )
        for p in self.unconsumed():
            if p.status == "running":
                lines.append(
                    f"- {p.summary()} is still running in the background (pending_id={p.pid}, "
                    f"{(now - p.started_ms) / 1000:.1f}s so far). Call await_pending if the user still wants it, "
                    f"or cancel_pending if their request changed."
                )
            elif p.status == "done" and not p.is_mutation:
                lines.append(
                    f"- {p.summary()} finished in the background (pending_id={p.pid}). "
                    f"Call await_pending to use its result only if the user still wants it."
                )
        return "\n".join(lines)

    def unreported_actions(self) -> list[str]:
        return [f"{p.summary()} -> {json.dumps(p.result)}" for p in self.unreported_mutations()]

    def mark_reported(self) -> None:
        for p in self.pending.values():
            if p.status == "done" and p.is_mutation:
                p.reported = True

    def snapshot(self) -> list[dict]:
        return [
            {
                "pid": p.pid, "name": p.name, "args": p.args, "epoch": p.epoch, "status": p.status,
                "orphaned": p.orphaned, "consumed_epoch": p.consumed_epoch, "reported": p.reported,
            }
            for p in self.pending.values()
        ]


def now_ms() -> float:
    return time.monotonic() * 1000.0
