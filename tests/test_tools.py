"""Tool runner: fencing, orphaning, cancellation and reconciliation semantics."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from forgedesk.fence import Fence, StaleEpoch
from forgedesk.telemetry import Telemetry
from forgedesk.tools import BookingStore, ToolRunner

TODAY = dt.date(2026, 9, 7)


def make(delay_ms=0):
    fence = Fence()
    tel = Telemetry("t")
    store = BookingStore(today=TODAY)
    runner = ToolRunner(store, fence, tel, delay_ms=delay_ms)
    return fence, tel, store, runner


async def test_read_tool_returns_and_is_consumed():
    fence, tel, store, runner = make()
    res = await runner.call("check_availability", {"day": "Tuesday", "period": "afternoon"}, fence.token())
    assert res["day"] == "Tuesday" and "slots" in res
    assert runner.pending["p1"].consumed_epoch == 0


async def test_interrupt_orphans_read_tool_and_fences_its_result():
    fence, tel, store, runner = make(delay_ms=300)
    token = fence.token()
    call = asyncio.ensure_future(runner.call("check_availability", {"day": "Tuesday"}, token))
    await asyncio.sleep(0.05)
    fence.bump()  # user barged in
    with pytest.raises(StaleEpoch):
        await call
    p = runner.pending["p1"]
    assert p.orphaned and p.status == "running"
    await asyncio.sleep(0.4)
    assert p.status == "done" and p.consumed_epoch is None
    assert runner.stale_results_fenced == 1
    assert tel.find("tool_stale_result_fenced")


async def test_orphaned_result_can_be_reconciled_by_new_epoch():
    fence, tel, store, runner = make(delay_ms=200)
    call = asyncio.ensure_future(runner.call("check_availability", {"day": "Tuesday"}, fence.token()))
    await asyncio.sleep(0.05)
    fence.bump()
    with pytest.raises(StaleEpoch):
        await call
    res = await runner.call("await_pending", {"pending_id": "p1"}, fence.token())
    assert res["tool"] == "check_availability" and res["result"]["day"] == "Tuesday"
    assert runner.pending["p1"].consumed_epoch == 1
    assert tel.find("tool_result_reconciled")


async def test_interrupt_cancels_uncommitted_mutation():
    fence, tel, store, runner = make(delay_ms=300)
    slot = next(s for s in store.slots.values() if s.available)
    call = asyncio.ensure_future(
        runner.call("book_appointment", {"slot_id": slot.slot_id, "customer_name": "A", "vehicle": "B", "service": "oil change"}, fence.token())
    )
    await asyncio.sleep(0.05)
    fence.bump()
    with pytest.raises(StaleEpoch):
        await call
    await asyncio.sleep(0.05)
    assert runner.pending["p1"].status == "cancelled"
    assert store.commits == [] and slot.available
    assert tel.find("tool_cancelled")


async def test_store_refuses_stale_commit_even_if_task_survives():
    fence, tel, store, runner = make()
    token = fence.token()
    slot = next(s for s in store.slots.values() if s.available)
    fence.bump()
    with pytest.raises(StaleEpoch):
        await store.book_appointment(slot.slot_id, "A", "B", "oil change", token=token)
    assert store.commits == []


async def test_completed_mutation_unreported_until_marked():
    fence, tel, store, runner = make()
    slot = next(s for s in store.slots.values() if s.available)
    res = await runner.call("book_appointment", {"slot_id": slot.slot_id, "customer_name": "A", "vehicle": "B", "service": "oil change"}, fence.token())
    assert res["code"].startswith("FD-")
    assert runner.unreported_actions() and "COMPLETED" in runner.context_note()
    runner.mark_reported()
    assert runner.unreported_actions() == []


async def test_cancel_pending_discards_running_lookup():
    fence, tel, store, runner = make(delay_ms=500)
    call = asyncio.ensure_future(runner.call("check_availability", {"day": "Tuesday"}, fence.token()))
    await asyncio.sleep(0.02)
    fence.bump()
    with pytest.raises(StaleEpoch):
        await call
    res = await runner.call("cancel_pending", {"pending_id": "p1"}, fence.token())
    assert res["discarded"]
    await asyncio.sleep(0.02)
    assert runner.pending["p1"].status == "cancelled"
