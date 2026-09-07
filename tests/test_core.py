"""Unit tests for the fence, ledger, chunker and VAD — the building blocks of the hard problem."""

from __future__ import annotations

import asyncio

import pytest

from forgedesk.audio import silence, tone
from forgedesk.fence import Fence, StaleEpoch, race_with_fence
from forgedesk.ledger import PlayoutMap
from forgedesk.textseg import SentenceChunker
from forgedesk.vad import EnergyVAD


# ----------------------------------------------------------------------------- fence
def test_token_goes_stale_on_bump():
    f = Fence()
    t = f.token()
    assert t.valid
    f.bump()
    assert not t.valid
    with pytest.raises(StaleEpoch):
        t.check("x")


async def test_race_with_fence_abandons_on_bump():
    f = Fence()
    t = f.token()

    async def slow():
        await asyncio.sleep(5)
        return "late"

    async def bump_soon():
        await asyncio.sleep(0.02)
        f.bump()

    asyncio.ensure_future(bump_soon())
    with pytest.raises(StaleEpoch):
        await race_with_fence(slow(), t)


async def test_race_with_fence_returns_when_fast():
    f = Fence()

    async def fast():
        return 42

    assert await race_with_fence(fast(), f.token()) == 42


# ----------------------------------------------------------------------------- ledger
def _map_with_two_sentences(sr=24000):
    pm = PlayoutMap(epoch=1, sample_rate=sr)
    s0 = pm.add_segment("Let me check Tuesday afternoon.")
    pm.add_audio(s0, sr * 2)  # 2 s
    pm.set_timestamps(s0, ["Let", "me", "check", "Tuesday", "afternoon."], [0, 0.3, 0.6, 1.0, 1.5], [0.3, 0.6, 1.0, 1.5, 2.0])
    s0.complete = True
    s1 = pm.add_segment("On Tuesday I have 1 PM and 4 PM.")
    pm.add_audio(s1, sr * 3)
    pm.set_timestamps(
        s1,
        ["On", "Tuesday", "I", "have", "1", "PM", "and", "4", "PM."],
        [0, 0.3, 0.7, 0.9, 1.3, 1.6, 2.0, 2.3, 2.6],
        [0.3, 0.7, 0.9, 1.3, 1.6, 2.0, 2.3, 2.6, 3.0],
    )
    s1.complete = True
    pm.finished = True
    return pm


def test_heard_word_level_split():
    pm = _map_with_two_sentences()
    sr = pm.sample_rate
    h = pm.heard(int(sr * (2 + 0.95)))  # 950 ms into sentence 2: "On Tuesday I" heard (I ends at .9), "have" not yet 60%
    assert h.method == "word_timestamps"
    assert h.heard == "Let me check Tuesday afternoon. On Tuesday I"
    assert h.unheard == "have 1 PM and 4 PM."
    assert h.resume_text == "On Tuesday I have 1 PM and 4 PM."
    assert not h.fully_heard


def test_heard_nothing_and_everything():
    pm = _map_with_two_sentences()
    none = pm.heard(0)
    assert none.heard == "" and none.unheard == pm.intended_text()
    everything = pm.heard(pm.total_samples)
    assert everything.fully_heard and everything.unheard == "" and everything.method == "complete"


def test_heard_proportional_fallback_without_timestamps():
    pm = PlayoutMap(epoch=1, sample_rate=1000)
    s = pm.add_segment("one two three four")
    pm.add_audio(s, 1000)
    s.complete = True
    h = pm.heard(500)
    assert h.method == "proportional"
    assert h.heard == "one two" and h.unheard == "three four"


# ----------------------------------------------------------------------------- chunker
def test_chunker_releases_sentences_and_protects_decimals():
    c = SentenceChunker(first_min_chars=5)
    out = c.feed("Sure thing. Your slot is at 3.30 PM. Dr. Patel will see you")
    assert out == ["Sure thing.", "Your slot is at 3.30 PM."]
    assert c.flush() == "Dr. Patel will see you"


def test_chunker_streams_token_by_token():
    c = SentenceChunker(first_min_chars=10)
    got = []
    for tok in ["Let ", "me ", "check ", "Thursday.", " On ", "Thursday ", "I ", "have ", "4 PM.", " Which works?"]:
        got += c.feed(tok)
    got += [c.flush()] if c.flush() else []
    assert got[:2] == ["Let me check Thursday.", "On Thursday I have 4 PM."]


# ----------------------------------------------------------------------------- VAD
def test_vad_requires_min_speech_then_detects_end():
    vad = EnergyVAD(min_speech_ms=180, min_silence_ms=300)
    ev = vad.feed(tone(100, 16000, amp=0.3))
    assert ev == []  # 100 ms is a blip, not a barge-in
    ev = vad.feed(tone(200, 16000, amp=0.3))
    assert [e.kind for e in ev] == ["speech_start"]
    ev = vad.feed(silence(400, 16000))
    assert [e.kind for e in ev] == ["speech_end"]


def test_vad_ignores_quiet_noise():
    vad = EnergyVAD(min_speech_ms=180)
    assert vad.feed(tone(1000, 16000, amp=0.005)) == []
