"""Scenarios = the acceptance tests. Each one is a scripted conversation plus pass criteria."""

from __future__ import annotations

import asyncio
import re

from .harness import Driver, RunResult, Scenario

SR = 24000  # only used to convert seconds -> samples for playhead targets; harness reads the real rate


def _sr(d: Driver) -> int:
    return d.client.sample_rate_out


async def wait_playhead(d: Driver, epoch: int, target_samples: int, timeout: float = 15.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if d.client.played(epoch) >= target_samples:
            return
        await asyncio.sleep(0.005)
    raise TimeoutError(f"playhead of epoch {epoch} never reached {target_samples}")


def spoken(r: RunResult, epochs) -> str:
    return " ".join(t for e in epochs for t in r.speak_texts.get(e, []))


def spoken_after(r: RunResult, epoch: int) -> str:
    return spoken(r, [e for e in r.speak_texts if e > epoch])


def is_word_prefix(prefix: str, full: str) -> bool:
    p, f = prefix.split(), full.split()
    return p == f[: len(p)]


CODE_SPOKEN = re.compile(r"\b[A-Z] [A-Z], [A-Z2-9] [A-Z2-9] [A-Z2-9] [A-Z2-9]\b|spell\([A-Z2-9]{6}\)")


# --------------------------------------------------------------------------- A
async def script_interrupt_mid_speech(d: Driver) -> None:
    idx = len(d.tel.events)
    await d.user_says("Hi, I need an oil change on Tuesday afternoon.")
    ev = await d.wait_for("tts_first_byte", seg=1, since_idx=idx)  # the slot offer sentence
    ep = ev["epoch"]
    seg1 = d.session.speech.maps[ep].segments[1]
    await wait_playhead(d, ep, seg1.offset_samples + int(0.6 * _sr(d)))  # 600 ms into the offer
    idx2 = len(d.tel.events)
    await d.barge_in("Actually, make it Thursday afternoon.")
    await d.wait_for("response_complete", since_idx=idx2, timeout=25)


def checks_interrupt_mid_speech(r: RunResult) -> dict[str, bool]:
    stops = r.find("interrupt_stopped")
    if not stops:
        return {"interrupted": False}
    s = stops[0]
    intended = next((e["intended"] for e in r.ledger if e["epoch"] == s["epoch"]), "")
    after = spoken_after(r, s["epoch"])
    return {
        "interrupted": True,
        "audio_stopped_under_300ms": s["stop_ms"] < 300,
        "heard_is_prefix_of_intended": bool(s["heard"]) and is_word_prefix(s["heard"], intended),
        "some_text_unheard": bool(s["unheard"]),
        "word_level_alignment": s["method"] == "word_timestamps",
        "new_request_answered": "Thursday" in after,
        "stale_offer_not_repeated": "On Tuesday" not in after,
        "no_stale_tool_results_spoken": r.metrics["stale_results_fenced"] == 0 or "On Tuesday" not in after,
    }


# --------------------------------------------------------------------------- B
async def script_interrupt_during_tool(d: Driver) -> None:
    idx = len(d.tel.events)
    await d.user_says("Book me Tuesday afternoon for an oil change.")
    await d.wait_for("tool_start", name="check_availability", since_idx=idx)
    await asyncio.sleep(1.0)  # lookup still running (3 s injected delay)
    idx2 = len(d.tel.events)
    await d.barge_in("Actually, Thursday afternoon instead.")
    await d.wait_for("response_complete", since_idx=idx2, timeout=25)
    await asyncio.sleep(2.5)  # let the orphaned Tuesday lookup finish so we can prove it was fenced


def checks_interrupt_during_tool(r: RunResult) -> dict[str, bool]:
    stops = r.find("interrupt_stopped")
    if not stops:
        return {"interrupted": False}
    ep = stops[0]["epoch"]
    after = spoken_after(r, ep)
    tuesday_lookup = [e for e in r.find("tool_start", name="check_availability") if str(e["args"].get("day", "")).lower() == "tuesday"]
    return {
        "interrupted": True,
        "audio_stopped_under_300ms": stops[0]["stop_ms"] < 300,
        "old_lookup_orphaned_not_killed": r.metrics["tools_orphaned"] >= 1,
        "old_lookup_discarded_or_fenced": (r.metrics["tools_discarded"] + r.metrics["stale_results_fenced"] + r.metrics["tools_cancelled"]) >= 1,
        "stale_tuesday_result_never_spoken": "On Tuesday" not in spoken(r, r.speak_texts.keys()),
        "new_request_answered": "Thursday" in after,
        "exactly_one_tuesday_lookup": len(tuesday_lookup) == 1,
    }


# --------------------------------------------------------------------------- C
async def script_status_during_tool(d: Driver) -> None:
    idx = len(d.tel.events)
    await d.user_says("Tuesday afternoon for a brake inspection please.")
    await d.wait_for("tool_start", name="check_availability", since_idx=idx)
    await asyncio.sleep(1.2)
    idx2 = len(d.tel.events)
    await d.barge_in("Hello? Are you still there?")
    await d.wait_for("response_complete", since_idx=idx2, timeout=25)


def checks_status_during_tool(r: RunResult) -> dict[str, bool]:
    stops = r.find("interrupt_stopped")
    if not stops:
        return {"interrupted": False}
    ep = stops[0]["epoch"]
    after = spoken_after(r, ep)
    return {
        "interrupted": True,
        "lookup_not_restarted": len(r.find("tool_start", name="check_availability")) == 1,
        "result_reconciled_into_new_epoch": r.metrics["tools_reconciled"] == 1,
        "status_acknowledged": "Still checking" in after,
        "result_spoken_once_after_reconcile": after.count("On Tuesday") == 1 and "On Tuesday" not in spoken(r, [ep]),
    }


# --------------------------------------------------------------------------- D
async def script_baseline(d: Driver) -> None:
    idx = len(d.tel.events)
    await d.user_says("Hi, I'd like to book an oil change on Tuesday afternoon.")
    await d.wait_for("response_complete", since_idx=idx, timeout=25)
    idx2 = len(d.tel.events)
    await d.user_says("The first one works. My name is Priya.")
    await d.wait_for("response_complete", since_idx=idx2, timeout=25)


def checks_baseline(r: RunResult) -> dict[str, bool]:
    appts = [a for a in r.store["appointments"] if not a["cancelled"]]
    all_text = spoken(r, r.speak_texts.keys())
    return {
        "booking_committed": len(appts) == 1,
        "confirmation_code_spoken_letter_by_letter": bool(CODE_SPOKEN.search(all_text)),
        "two_turns_completed": len(r.find("response_complete")) == 2,
        "ttfa_measured": len(r.metrics.get("ttfa_ms", [])) == 2,
        "not_interrupted": not r.find("interrupt_stopped"),
    }


# --------------------------------------------------------------------------- E
async def script_empty_interrupt_resume(d: Driver) -> None:
    idx = len(d.tel.events)
    await d.user_says("Wednesday morning for a tire rotation.")
    ev = await d.wait_for("tts_first_byte", seg=1, since_idx=idx)
    ep = ev["epoch"]
    seg1 = d.session.speech.maps[ep].segments[1]
    await wait_playhead(d, ep, seg1.offset_samples + int(0.5 * _sr(d)))
    idx2 = len(d.tel.events)
    await d.barge_in(None, speech_ms=350)  # a cough: energy but no words
    await d.wait_for("resume_unheard", since_idx=idx2, timeout=10)
    await d.wait_for("response_complete", since_idx=idx2, timeout=25)


def checks_empty_interrupt_resume(r: RunResult) -> dict[str, bool]:
    stops = r.find("interrupt_stopped")
    res = r.find("resume_unheard")
    if not stops or not res:
        return {"interrupted": bool(stops), "resumed": bool(res)}
    after = spoken_after(r, stops[0]["epoch"])
    resume = res[0].get("resume_text", res[0]["unheard"])
    heard_words = stops[0]["heard"].split()
    return {
        "interrupted": True,
        "resumed": True,
        "resume_covers_all_unheard_text": resume.endswith(stops[0]["unheard"]) and resume in after,
        "resume_restarts_at_sentence_boundary": resume[:1].isupper(),
        "fully_heard_sentences_not_repeated": not (len(heard_words) > 3 and " ".join(heard_words[:4]) in after),
    }


# --------------------------------------------------------------------------- F
async def script_interrupt_uncommitted_mutation(d: Driver) -> None:
    idx = len(d.tel.events)
    await d.user_says("Tuesday afternoon, general service.")
    await d.wait_for("response_complete", since_idx=idx, timeout=30)
    idx2 = len(d.tel.events)
    await d.user_says("The first one please.")
    await d.wait_for("tool_start", name="book_appointment", since_idx=idx2)
    await asyncio.sleep(0.8)  # booking still in flight (2.5 s injected delay)
    idx3 = len(d.tel.events)
    await d.barge_in("Wait, actually make it Thursday afternoon.")
    await d.wait_for("response_complete", since_idx=idx3, timeout=30)


def checks_interrupt_uncommitted_mutation(r: RunResult) -> dict[str, bool]:
    stops = r.find("interrupt_stopped")
    if not stops:
        return {"interrupted": False}
    after = spoken_after(r, stops[0]["epoch"])
    return {
        "interrupted": True,
        "uncommitted_booking_cancelled": r.metrics["tools_cancelled"] >= 1,
        "nothing_committed": len(r.store["commits"]) == 0,
        "no_false_confirmation_spoken": "booked" not in spoken(r, r.speak_texts.keys()),
        "new_request_answered": "Thursday" in after,
    }


# --------------------------------------------------------------------------- G
async def script_interrupt_after_commit_unheard(d: Driver) -> None:
    idx = len(d.tel.events)
    await d.user_says("Tuesday afternoon, oil change, my name is Priya.")
    await d.wait_for("response_complete", since_idx=idx, timeout=25)
    idx2 = len(d.tel.events)
    await d.user_says("The first one.")
    ev = await d.wait_for("tts_first_byte", seg=2, since_idx=idx2)  # "Your confirmation code is ..."
    ep = ev["epoch"]
    seg2 = d.session.speech.maps[ep].segments[2]
    await wait_playhead(d, ep, seg2.offset_samples + int(0.3 * _sr(d)))
    idx3 = len(d.tel.events)
    await d.barge_in("Actually, can you move it to Thursday afternoon?")
    await d.wait_for("response_complete", since_idx=idx3, timeout=25)
    idx4 = len(d.tel.events)
    await d.user_says("The first one.")
    await d.wait_for("response_complete", since_idx=idx4, timeout=25)


def checks_interrupt_after_commit_unheard(r: RunResult) -> dict[str, bool]:
    stops = r.find("interrupt_stopped")
    if not stops:
        return {"interrupted": False}
    ep = stops[0]["epoch"]
    entry = next((e for e in r.ledger if e["epoch"] == ep), None)
    after = spoken_after(r, ep)
    appts = [a for a in r.store["appointments"] if not a["cancelled"]]
    ops = [c["op"] for c in r.store["commits"]]
    return {
        "interrupted": True,
        "unheard_confirmation_detected": bool(entry and entry["unreported_actions"]),
        "unheard_booking_surfaced_first": after.startswith("Quick note"),
        "moved_not_duplicated": len(appts) == 1 and appts[0]["day"] == "Thursday" and ops == ["book", "reschedule"],
        "same_code_kept": len(appts) == 1 and appts[0]["code"] == r.store["commits"][0]["code"],
    }


SCENARIOS: list[Scenario] = [
    Scenario(
        "A_interrupt_mid_speech",
        "User talks over the slot offer and changes the day. Audio must stop promptly; the ledger must know which words were heard; the new answer must be about Thursday.",
        script_interrupt_mid_speech,
        checks_interrupt_mid_speech,
        tool_delay_ms=0,
        claim="Queued Rime audio stops promptly and the next response is grounded in what was heard.",
    ),
    Scenario(
        "B_interrupt_during_tool",
        "User changes the request while a 3 s lookup is in flight. The stale lookup must never be spoken as current.",
        script_interrupt_during_tool,
        checks_interrupt_during_tool,
        tool_delay_ms=3000,
        claim="Delayed tool results cannot re-enter the conversation after an interruption.",
    ),
    Scenario(
        "C_status_during_tool",
        "User asks 'are you still there?' during a 3 s lookup. The lookup must continue and its result must be reconciled, not restarted or lost.",
        script_status_during_tool,
        checks_status_during_tool,
        tool_delay_ms=3000,
        claim="The voice session stays responsive during tool work without losing context.",
    ),
    Scenario(
        "D_baseline_no_interrupt",
        "Normal end-to-end booking, two turns. Measures time-to-first-audio and confirms the code is spoken letter by letter.",
        script_baseline,
        checks_baseline,
        tool_delay_ms=0,
        claim="Normal path works end to end with Rime as the only speaker.",
    ),
    Scenario(
        "E_empty_interrupt_resume",
        "A cough interrupts the agent mid-sentence with no words. The agent resumes from the first unheard word.",
        script_empty_interrupt_resume,
        checks_empty_interrupt_resume,
        tool_delay_ms=0,
        claim="Heard-state is tracked at word level and used for recovery.",
    ),
    Scenario(
        "F_interrupt_uncommitted_mutation",
        "User changes their mind while a 2.5 s booking write is in flight. The write must be cancelled and nothing committed.",
        script_interrupt_uncommitted_mutation,
        checks_interrupt_uncommitted_mutation,
        tool_delay_ms=2500,
        claim="Application state stays consistent with what the user asked for and heard.",
    ),
    Scenario(
        "G_interrupt_after_commit_unheard",
        "Booking commits, user interrupts before hearing the code, then asks to move it. The agent surfaces the unheard booking and reschedules instead of double-booking.",
        script_interrupt_after_commit_unheard,
        checks_interrupt_after_commit_unheard,
        tool_delay_ms=0,
        claim="Committed-but-unheard actions are reconciled, never silently lost or duplicated.",
    ),
]

BY_NAME = {s.name: s for s in SCENARIOS}
