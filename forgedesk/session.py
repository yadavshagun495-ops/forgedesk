"""Session orchestrator: the full-duplex loop.

The application keeps accepting user audio while Rime speech is playing and while tools run.
A barge-in (mic energy and/or recogniser text) immediately:
  1. bumps the epoch (fences every in-flight LLM token, tool result and audio frame),
  2. tells the client to stop and drop queued audio, and closes the Rime stream,
  3. records exactly what the user heard (word-level, via Rime timestamps),
  4. orphans read-only lookups / cancels uncommitted mutations,
so the next response is grounded in what was actually heard and requested.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .fence import Fence, FenceToken, StaleEpoch
from .ledger import HeardLedger, LedgerEntry
from .llm import LLM, LLMDone, TextDelta, ToolCall
from .llm.prompt import speakable, system_prompt
from .speech import SpeechController, Utterance
from .stt import BaseSTT, STTEvent
from .telemetry import Telemetry
from .textseg import SentenceChunker
from .tools import BookingStore, ToolRunner
from .transport import Transport
from .tts import TTSEngine
from .vad import EnergyVAD

FILLERS = ["Still checking.", "One moment, the booking system is slow today.", "Almost there."]


@dataclass
class ResponseStats:
    epoch: int
    user_text: str
    t_user_end: float
    t_llm_first_token: float | None = None
    t_tts_first_byte: float | None = None
    t_audio_first_sent: float | None = None
    t_first_audio_played: float | None = None
    tool_calls: list[str] = field(default_factory=list)
    interrupted: bool = False
    complete: bool = False
    cold: bool = False

    def summary(self) -> dict:
        def d(t: float | None) -> float | None:
            return round(t - self.t_user_end, 1) if t is not None else None

        return {
            "epoch": self.epoch,
            "user_text": self.user_text,
            "llm_first_token_ms": d(self.t_llm_first_token),
            "tts_first_byte_ms": d(self.t_tts_first_byte),
            "audio_first_sent_ms": d(self.t_audio_first_sent),
            "ttfa_ms": d(self.t_first_audio_played),
            "tool_calls": self.tool_calls,
            "interrupted": self.interrupted,
            "complete": self.complete,
            "cold": self.cold,
        }


class Session:
    def __init__(
        self,
        settings: Settings,
        transport: Transport,
        tts: TTSEngine,
        stt: BaseSTT,
        llm: LLM,
        telemetry: Telemetry,
        store: BookingStore | None = None,
        today: dt.date | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.stt = stt
        self.llm = llm
        self.tel = telemetry
        self.fence = Fence()
        self.store = store or BookingStore(today=today or dt.date.today())
        self.runner = ToolRunner(self.store, self.fence, telemetry, delay_ms=settings.tool_delay_ms)
        self.speech = SpeechController(tts, transport, telemetry, self.fence, settings.rime if settings.resolved_tts() == "rime" else None)
        self.ledger = HeardLedger()
        self.vad = EnergyVAD(sample_rate=settings.input_sample_rate, min_speech_ms=settings.barge_in_min_ms)
        self.history: list[dict] = []
        self.state = "listening"
        self.active: asyncio.Task | None = None
        self.active_utt: Utterance | None = None
        self.stats: dict[int, ResponseStats] = {}
        self._interim_finals: list[str] = []
        self._turn_lock = asyncio.Lock()
        self._awaiting_text_after_interrupt = False
        self._recovery_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._first_audio_marks: dict[int, float] = {}
        self._today_label = (today or dt.date.today()).strftime("%A, %B %d")

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        await self.stt.start()
        await self.speech.tts.warmup()
        await self._send_config()
        self._tasks = [
            asyncio.ensure_future(self._audio_loop()),
            asyncio.ensure_future(self._stt_loop()),
            asyncio.ensure_future(self._control_loop()),
        ]
        self.tel.emit("session_start", tts=self.speech.tts.describe(), stt=self.stt.describe(), llm=self.llm.name)
        try:
            await self._stop.wait()
        finally:
            for t in self._tasks:
                t.cancel()
            if self.active:
                self.active.cancel()
            await self.stt.stop()
            await self.speech.aclose()
            self.tel.emit("session_end")

    def stop(self) -> None:
        self._stop.set()

    async def _send_config(self) -> None:
        await self.transport.send_json(
            {
                "type": "config",
                "settings": self.settings.public(),
                "tts_engine": self.speech.tts.describe(),
                "stt": self.stt.describe(),
                "llm": self.llm.name,
                "output_sample_rate": self.speech.tts.sample_rate,
                "today": self._today_label,
            }
        )
        await self.transport.send_json({"type": "provider", "tts": self.speech.provider, "detail": self.speech.tts.describe(), "fallback": False})

    async def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.tel.emit("state", state=state, epoch=self.fence.epoch)
            await self.transport.send_json({"type": "state", "state": state, "epoch": self.fence.epoch})

    @property
    def agent_active(self) -> bool:
        return self.active is not None and not self.active.done()

    # ------------------------------------------------------------------ input loops
    async def _audio_loop(self) -> None:
        mode = self.settings.barge_in_mode
        while True:
            pcm = await self.transport.audio_in.get()
            if self.stt.server_side_audio:
                await self.stt.feed(pcm)
            for ev in self.vad.feed(pcm):
                if ev.kind == "speech_start":
                    self.tel.emit("vad_speech_start", agent_active=self.agent_active, state=self.state)
                    if mode in ("vad", "either") and self.agent_active:
                        await self.interrupt("vad")
                elif ev.kind == "speech_end":
                    self.tel.emit("vad_speech_end")
                    if self._awaiting_text_after_interrupt:
                        self._schedule_recovery()

    async def _stt_loop(self) -> None:
        mode = self.settings.barge_in_mode
        while True:
            ev: STTEvent = await self.stt.queue.get()
            if ev.kind == "interim":
                await self.transport.send_json({"type": "transcript", "role": "user", "text": ev.text, "final": False})
                if mode in ("stt", "either") and self.agent_active and ev.text.strip():
                    await self.interrupt("stt_interim")
            elif ev.kind == "final":
                self._interim_finals.append(ev.text)
                await self.transport.send_json({"type": "transcript", "role": "user", "text": " ".join(self._interim_finals), "final": False})
                if mode in ("stt", "either") and self.agent_active and ev.text.strip():
                    await self.interrupt("stt_final")
            elif ev.kind == "utterance_end":
                text = " ".join(self._interim_finals).strip()
                self._interim_finals = []
                if text:
                    await self.end_of_turn(text)
            elif ev.kind == "speech_started":
                if mode in ("stt", "either") and self.agent_active:
                    await self.interrupt("stt_speech_started")
            elif ev.kind == "error":
                self.tel.emit("stt_error", error=ev.text)
                await self.transport.send_json({"type": "event", "kind": "stt_error", "error": ev.text})

    async def _control_loop(self) -> None:
        while True:
            msg = await self.transport.control_in.get()
            typ = msg.get("type")
            if typ == "disconnect":
                self.stop()
                return
            if typ == "interrupt":
                await self.interrupt("button")
            elif typ == "text":
                await self.end_of_turn(str(msg.get("text", "")))
            elif typ == "transcript":
                await self.stt.push_transcript(str(msg.get("text", "")), bool(msg.get("final")))
            elif typ == "first_audio":
                ep = int(msg.get("epoch", -1))
                if ep not in self._first_audio_marks:
                    now = self.tel.now_ms()
                    self._first_audio_marks[ep] = now
                    st = self.stats.get(ep)
                    ttfa = round(now - st.t_user_end, 1) if st else None
                    self.tel.emit("audio_first_played", epoch=ep, ttfa_ms=ttfa, cold=st.cold if st else None,
                                  user_text=st.user_text if st else None)
            elif typ == "set":
                if "tool_delay_ms" in msg:
                    self.runner.delay_ms = max(0, int(msg["tool_delay_ms"]))
                    self.tel.emit("config_change", tool_delay_ms=self.runner.delay_ms)
            elif typ == "hello":
                await self._send_config()

    # ------------------------------------------------------------------ interruption
    async def interrupt(self, reason: str) -> None:
        if not self.agent_active:
            return
        async with self._turn_lock:
            await self._interrupt_locked(reason)

    async def _interrupt_locked(self, reason: str) -> None:
        if not self.agent_active:
            return
        old_epoch = self.fence.epoch
        utt = self.active_utt
        task = self.active
        t0 = self.tel.now_ms()
        self.tel.emit("barge_in", epoch=old_epoch, reason=reason, state=self.state)
        self.fence.bump()  # from here on, everything tagged old_epoch is stale
        if task:
            task.cancel()
        heard = None
        ack_ms = 0.0
        if utt is not None:
            heard, ack_ms = await self.speech.stop(utt)
        stop_ms = self.tel.now_ms() - t0
        st = self.stats.get(old_epoch)
        if st:
            st.interrupted = True
        entry = LedgerEntry(
            epoch=old_epoch,
            intended=utt.map.intended_text() if utt else "",
            heard=heard.heard if heard else "",
            unheard=heard.unheard if heard else "",
            interrupted=True,
            method=heard.method if heard else "nothing",
            unreported_actions=self.runner.unreported_actions(),
            resume_text=heard.resume_text if heard else "",
        )
        self.ledger.record(entry)
        self.history.append({"role": "assistant", "content": entry.heard or "(interrupted before any speech was heard)"})
        self.tel.emit(
            "interrupt_stopped",
            epoch=old_epoch,
            reason=reason,
            stop_ms=round(stop_ms, 1),
            flush_ack_ms=round(ack_ms, 1),
            heard=entry.heard,
            unheard=entry.unheard,
            method=entry.method,
            played_samples=heard.played_samples if heard else 0,
            total_samples=heard.total_samples if heard else 0,
        )
        await self.transport.send_json(
            {"type": "heard", "epoch": old_epoch, "heard": entry.heard, "unheard": entry.unheard, "method": entry.method,
             "stop_ms": round(stop_ms, 1)}
        )
        if task:
            try:
                await task
            except (asyncio.CancelledError, StaleEpoch):
                pass
            except Exception:  # noqa: BLE001
                pass
        if self.active is task:
            self.active = None
        if self.active_utt is utt:
            self.active_utt = None
        self._awaiting_text_after_interrupt = True
        await self._set_state("listening")
        if not self.vad.speaking:  # user already stopped talking while we were stopping the audio
            self._schedule_recovery()

    def _schedule_recovery(self) -> None:
        if self._recovery_task and not self._recovery_task.done():
            return
        self._recovery_task = asyncio.ensure_future(self._recover_if_silent())

    async def _recover_if_silent(self, grace_s: float = 1.4) -> None:
        """User barged in but said nothing usable: resume from the first unheard word."""
        await asyncio.sleep(grace_s)
        if not self._awaiting_text_after_interrupt or self.agent_active:
            return
        last = self.ledger.last()
        self._awaiting_text_after_interrupt = False
        if not last or not last.interrupted or not last.unheard:
            return
        resume = last.resume_text or last.unheard
        self.tel.emit("resume_unheard", epoch=last.epoch, unheard=last.unheard, resume_text=resume)
        await self.say(f"Sorry, as I was saying. {resume}", label="resume")

    # ------------------------------------------------------------------ turns
    async def end_of_turn(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()
        self._awaiting_text_after_interrupt = False
        async with self._turn_lock:
            if self.agent_active:
                await self._interrupt_locked("late_transcript")
            self.tel.emit("user_turn_end", text=text)
            await self.transport.send_json({"type": "transcript", "role": "user", "text": text, "final": True})
            self.history.append({"role": "user", "content": text})
            self.fence.bump()
            token = self.fence.token()
            self.stats[token.epoch] = ResponseStats(token.epoch, text, self.tel.now_ms(), cold=getattr(self.speech.tts, "cold", False))
            self.active = asyncio.ensure_future(self._respond(token))

    async def say(self, text: str, label: str = "say") -> None:
        """Speak a canned utterance as its own response epoch (greeting, recovery, errors)."""
        async with self._turn_lock:
            if self.agent_active:
                return
            self.fence.bump()
            token = self.fence.token()
            self.stats[token.epoch] = ResponseStats(token.epoch, f"<{label}>", self.tel.now_ms())
            self.active = asyncio.ensure_future(self._speak_only(token, text))

    async def _speak_only(self, token: FenceToken, text: str) -> None:
        utt = self.speech.new_utterance(token)
        self.active_utt = utt
        model_id = self.settings.rime.model_id
        try:
            await self._set_state("speaking")
            chunker = SentenceChunker()
            for s in chunker.feed(text):
                utt.add(speakable(s, model_id))
            rest = chunker.flush()
            if rest:
                utt.add(speakable(rest, model_id))
            utt.end()
            heard = await self.speech.wait_played(utt)
            if not utt.cancelled:
                self.history.append({"role": "assistant", "content": text})
                self.ledger.record(LedgerEntry(token.epoch, text, heard.heard, heard.unheard, False, heard.method))
                self._finish_stats(token.epoch, utt)
        except (asyncio.CancelledError, StaleEpoch):
            pass
        finally:
            if self.active_utt is utt:
                self.active_utt = None
            if not utt.cancelled:
                self.active = None
                await self._set_state("listening")

    def _context(self) -> tuple[str, dict[str, Any]]:
        notes = []
        ledger_note = self.ledger.pending_note()
        if ledger_note:
            notes.append(ledger_note)
        pend = self.runner.context_note()
        if pend:
            notes.append("Background work status:\n" + pend)
        unreported = [
            {"pid": p.pid, "name": p.name, "args": p.args, "result": p.result, "code": (p.result or {}).get("code") if isinstance(p.result, dict) else None}
            for p in self.runner.unreported_mutations()
        ]
        pending = [
            {"pid": p.pid, "name": p.name, "args": p.args, "status": p.status, "is_mutation": p.is_mutation}
            for p in self.runner.unconsumed()
        ]
        return "\n".join(notes), {"pending": pending, "unreported": unreported, "ledger": ledger_note}

    async def _respond(self, token: FenceToken) -> None:
        epoch = token.epoch
        st = self.stats[epoch]
        utt = self.speech.new_utterance(token)
        self.active_utt = utt
        model_id = self.settings.rime.model_id
        note, ctx = self._context()
        system = system_prompt(model_id, self._today_label)
        if note:  # keep a single leading system message; some OpenAI-compatible providers reject mid-history system turns
            system += "\n\nContext for this turn:\n" + note
        messages: list[dict] = [{"role": "system", "content": system}]
        messages += self.history
        additions: list[dict] = []
        filler_task: asyncio.Task | None = None
        try:
            await self._set_state("thinking")
            for round_no in range(5):
                chunker = SentenceChunker()
                text_acc = ""
                calls: list[ToolCall] = []
                self.tel.emit("llm_request", epoch=epoch, round=round_no, llm=self.llm.name)
                async for ev in self.llm.stream(messages, self.runner.schemas(), ctx):
                    token.check("llm stream")
                    if isinstance(ev, TextDelta):
                        if st.t_llm_first_token is None:
                            st.t_llm_first_token = self.tel.now_ms()
                            self.tel.emit("llm_first_token", epoch=epoch, ms=round(st.t_llm_first_token - st.t_user_end, 1))
                        text_acc += ev.text
                        for s in chunker.feed(ev.text):
                            utt.add(speakable(s, model_id))
                    elif isinstance(ev, ToolCall):
                        calls.append(ev)
                    elif isinstance(ev, LLMDone):
                        pass
                rest = chunker.flush()
                if rest:
                    utt.add(speakable(rest, model_id))
                if text_acc.strip():
                    await self.transport.send_json({"type": "transcript", "role": "assistant", "text": text_acc.strip(), "epoch": epoch, "final": False})
                assistant_msg: dict = {"role": "assistant", "content": text_acc or None}
                if calls:
                    assistant_msg["tool_calls"] = [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}} for c in calls
                    ]
                messages.append(assistant_msg)
                additions.append(assistant_msg)
                if not calls:
                    break
                await self._set_state("tool")
                filler_task = asyncio.ensure_future(self._filler(utt, token))
                for c in calls:
                    st.tool_calls.append(c.name)
                    await self.transport.send_json({"type": "tool", "status": "running", "name": c.name, "args": c.args, "epoch": epoch})
                    result = await self.runner.call(c.name, c.args, token)
                    await self.transport.send_json({"type": "tool", "status": "done", "name": c.name, "args": c.args, "result": result, "epoch": epoch})
                    tool_msg = {"role": "tool", "tool_call_id": c.id, "content": json.dumps(result)}
                    messages.append(tool_msg)
                    additions.append(tool_msg)
                filler_task.cancel()
                filler_task = None
                _, ctx = self._context()
                await self._set_state("thinking" if utt.idle else "speaking")
            utt.end()
            await self._set_state("speaking")
            heard = await self.speech.wait_played(utt)
            if utt.cancelled or not token.valid:
                return
            self.history.extend(additions)
            self.ledger.record(LedgerEntry(epoch, utt.map.intended_text(), heard.heard, heard.unheard, False, heard.method))
            self.runner.mark_reported()
            self._finish_stats(epoch, utt)
            await self.transport.send_json({"type": "transcript", "role": "assistant", "text": utt.map.intended_text(), "epoch": epoch, "final": True})
        except (asyncio.CancelledError, StaleEpoch):
            return
        except Exception as e:  # noqa: BLE001
            self.tel.emit("response_error", epoch=epoch, error=f"{type(e).__name__}: {e}")
            if token.valid and not utt.cancelled:
                utt.add("Sorry, something went wrong on my end. Could you say that again?")
                utt.end()
                await self.speech.wait_played(utt)
        finally:
            if filler_task:
                filler_task.cancel()
            if self.active_utt is utt:
                self.active_utt = None
            if token.valid and not utt.cancelled:
                self.active = None
                await self._set_state("listening")

    async def _filler(self, utt: Utterance, token: FenceToken) -> None:
        delay = self.settings.filler_after_ms / 1000.0
        i = 0
        try:
            while True:
                await asyncio.sleep(delay if i == 0 else 3.0)
                if not token.valid or utt.cancelled:
                    return
                if utt.idle:
                    text = FILLERS[min(i, len(FILLERS) - 1)]
                    utt.add(text, filler=True)
                    self.tel.emit("filler_spoken", epoch=token.epoch, text=text)
                    i += 1
        except asyncio.CancelledError:
            return

    def _finish_stats(self, epoch: int, utt: Utterance) -> None:
        st = self.stats.get(epoch)
        if not st:
            return
        st.complete = True
        st.t_audio_first_sent = utt.first_audio_sent_ms
        st.t_first_audio_played = self._first_audio_marks.get(epoch)
        fb = self.tel.find("tts_first_byte", epoch=epoch)
        if fb:
            st.t_tts_first_byte = fb[0]["t_ms"]
        self.tel.emit("response_complete", **st.summary(), provider=self.speech.provider, fillers=utt.fillers)
