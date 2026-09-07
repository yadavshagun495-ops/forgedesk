"""Evaluation harness: drives a full Session in-process with a real-time simulated client.

The session code path is identical to production: mic PCM goes through the same VAD, the
same STT event queue, the same fence/ledger/tool runner and the same SpeechController.
Only the edges are simulated (scripted user, in-process playback clock) and, optionally,
the TTS engine (FakeTTS for logic-only runs; Rime for the judged evidence).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from forgedesk.audio import silence, tone, write_wav
from forgedesk.config import Settings
from forgedesk.llm.rules import RuleAgent
from forgedesk.session import Session
from forgedesk.stt import BaseSTT
from forgedesk.telemetry import Telemetry
from forgedesk.tools import BookingStore
from forgedesk.transport import SimulatedClient
from forgedesk.tts.fake import FakeTTS

FIXED_TODAY = dt.date(2026, 9, 7)  # a Monday; keeps slot ids and weekday maths deterministic


class Driver:
    """Scripted user + observation helpers for one scenario run."""

    def __init__(self, session: Session, client: SimulatedClient, stt: BaseSTT, tel: Telemetry) -> None:
        self.session = session
        self.client = client
        self.stt = stt
        self.tel = tel
        self.marks: dict[str, float] = {}

    # -- observation --------------------------------------------------------------------
    async def wait_for(self, kind: str, timeout: float = 15.0, since_idx: int = 0, **match: Any) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for ev in self.tel.events[since_idx:]:
                if ev["kind"] == kind and all(ev.get(k) == v for k, v in match.items()):
                    return ev
            await asyncio.sleep(0.01)
        raise TimeoutError(f"no {kind} {match} within {timeout}s (last events: {[e['kind'] for e in self.tel.events[-8:]]})")

    async def wait_idle(self, timeout: float = 20.0) -> None:
        """Wait until the agent finished speaking and is listening again."""
        deadline = time.monotonic() + timeout
        await asyncio.sleep(0.05)
        while time.monotonic() < deadline:
            if not self.session.agent_active and self.session.state == "listening":
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("agent did not go idle")

    async def wait_first_audio(self, epoch: int, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if epoch in self.client.first_audio_times and time.monotonic() >= self.client.first_audio_times[epoch]:
                return
            await asyncio.sleep(0.005)
        raise TimeoutError(f"no audio played for epoch {epoch}")

    @property
    def epoch(self) -> int:
        return self.session.fence.epoch

    # -- user actions -------------------------------------------------------------------
    async def user_says(self, text: str) -> None:
        """A finished user turn (what STT delivers as final + utterance end)."""
        self.marks["user_says"] = self.tel.now_ms()
        await self.stt.push_transcript(text, final=True)

    async def barge_in(self, text: str | None, speech_ms: int = 450) -> float:
        """Talk over the agent: real mic-like audio (so the VAD fires), then the transcript.

        Returns the telemetry time when the user started speaking.
        """
        t_start = self.tel.now_ms()
        self.marks["barge_in_start"] = t_start
        pcm = tone(speech_ms, 16000, freq=180.0, amp=0.35)
        feed = asyncio.ensure_future(self.client.user_speaks_audio(pcm))
        if text is not None:
            # a recogniser typically produces text a little after speech begins
            await asyncio.sleep(min(0.25, speech_ms / 1000.0))
            await self.stt.push_transcript(text, final=False)
        await feed
        if text is not None:
            await asyncio.sleep(0.15)
            await self.stt.push_transcript(text, final=True)
        else:
            await self.client.user_speaks_audio(silence(700, 16000))  # let the VAD see silence -> speech_end
        return t_start


@dataclass
class Scenario:
    name: str
    description: str
    script: Callable[[Driver], Awaitable[None]]
    checks: Callable[["RunResult"], dict[str, bool]]
    tool_delay_ms: int = 0
    claim: str = ""


@dataclass
class RunResult:
    scenario: str
    run_idx: int
    tts: str
    events: list[dict]
    client: dict
    store: dict
    speak_texts: dict[int, list[str]]
    ledger: list[dict]
    marks: dict[str, float]
    metrics: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, bool] = field(default_factory=dict)
    heard_clip: str | None = None

    def find(self, kind: str, **match: Any) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind and all(e.get(k) == v for k, v in match.items())]

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def make_settings(tool_delay_ms: int, tts_kind: str) -> Settings:
    s = Settings()
    s.tool_delay_ms = tool_delay_ms
    s.tts_provider = tts_kind
    s.stt_provider = "scripted"
    s.llm_provider = "rules"
    s.filler_after_ms = 1200
    s.barge_in_min_ms = 180
    return s


def build_tts(kind: str, settings: Settings):
    if kind == "fake":
        return FakeTTS(sample_rate=settings.rime.sample_rate)
    if kind == "rime":
        if settings.rime.transport == "http":
            from forgedesk.tts.rime_http import RimeHttpTTS

            return RimeHttpTTS(settings.rime)
        from forgedesk.tts.rime_ws import RimeWs3TTS

        return RimeWs3TTS(settings.rime)
    raise ValueError(kind)


async def run_scenario(scn: Scenario, tts_kind: str, run_idx: int, out_dir: str, tts=None) -> RunResult:
    settings = make_settings(scn.tool_delay_ms, tts_kind)
    run_id = f"{scn.name}-{run_idx:02d}"
    os.makedirs(os.path.join(out_dir, "runs"), exist_ok=True)
    tel = Telemetry(run_id, path=os.path.join(out_dir, "runs", f"{run_id}.jsonl"), session_id=run_id)
    own_tts = tts is None
    tts = tts or build_tts(tts_kind, settings)
    client = SimulatedClient(sample_rate_out=tts.sample_rate)
    stt = BaseSTT()
    llm = RuleAgent(model_id=settings.rime.model_id)
    store = BookingStore(today=FIXED_TODAY)
    session = Session(settings, client, tts, stt, llm, tel, store=store, today=FIXED_TODAY)
    driver = Driver(session, client, stt, tel)
    task = asyncio.ensure_future(session.run())
    error: str | None = None
    try:
        await asyncio.wait_for(scn.script(driver), timeout=90)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        tel.emit("scenario_error", error=error)
    finally:
        session.stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        if own_tts:
            await tts.aclose()
        tel.close()
    result = RunResult(
        scenario=scn.name,
        run_idx=run_idx,
        tts=tts.name,
        events=list(tel.events),
        client=client.snapshot(),
        store=store.snapshot(),
        speak_texts=dict(client.speak_texts),
        ledger=[e.__dict__ for e in session.ledger.entries],
        marks=dict(driver.marks),
    )
    result.metrics = compute_metrics(result)
    if error:
        result.checks = {"script_completed": False}
        result.metrics["error"] = error
    else:
        result.checks = {"script_completed": True, **scn.checks(result)}
    # save what the user actually heard for the interrupted epoch (evidence clip)
    stops = result.find("interrupt_stopped")
    if stops and tts_kind == "rime":
        ep = stops[0]["epoch"]
        pcm = client.heard_pcm(ep)
        if pcm:
            os.makedirs(os.path.join(out_dir, "clips"), exist_ok=True)
            path = os.path.join(out_dir, "clips", f"{run_id}-heard-e{ep}.wav")
            write_wav(path, pcm, tts.sample_rate)
            result.heard_clip = path
    return result


def compute_metrics(r: RunResult) -> dict[str, Any]:
    m: dict[str, Any] = {}
    stops = r.find("interrupt_stopped")
    if stops:
        s = stops[0]
        m["stop_ms"] = s["stop_ms"]
        m["flush_ack_ms"] = s["flush_ack_ms"]
        m["heard"] = s["heard"]
        m["unheard"] = s["unheard"]
        m["alignment"] = s["method"]
        barge = r.find("barge_in", epoch=s["epoch"])
        if barge and "barge_in_start" in r.marks:
            m["detect_ms"] = round(barge[0]["t_ms"] - r.marks["barge_in_start"], 1)
            m["user_speech_to_stop_ms"] = round(s["t_ms"] - r.marks["barge_in_start"], 1)
    completes = r.find("response_complete")
    played = [e for e in r.find("audio_first_played") if e.get("ttfa_ms") is not None and not str(e.get("user_text", "")).startswith("<")]
    ttfa = [e["ttfa_ms"] for e in played]
    if ttfa:
        m["ttfa_ms"] = ttfa
        m["ttfa_cold"] = [e["ttfa_ms"] for e in played if e.get("cold")]
    fb = [e["ms"] for e in r.find("tts_first_byte")]
    if fb:
        m["tts_first_byte_ms"] = fb
    m["stale_results_fenced"] = len(r.find("tool_stale_result_fenced"))
    m["tools_orphaned"] = len(r.find("tool_orphaned"))
    m["tools_cancelled"] = len(r.find("tool_cancelled"))
    m["tools_reconciled"] = len(r.find("tool_result_reconciled"))
    m["tools_discarded"] = len(r.find("tool_discarded"))
    m["late_frames_dropped"] = sum(v["late_frames_dropped"] for v in r.client.values())
    m["fillers"] = len(r.find("filler_spoken"))
    m["provider"] = completes[-1]["provider"] if completes else r.tts
    return m
