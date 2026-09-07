# RIME_EVIDENCE.md — the hard voice claim and how we prove it

## Claim

**ForgeDesk is full-duplex in the sense the brief defines.** While Rime is speaking, or while a slow
booking-system tool is running, a caller can talk over the agent and:

1. queued Rime audio stops promptly (server detects the barge-in → client acknowledges playback stopped and
   queued audio dropped, measured end to end);
2. the application knows **exactly which words the caller heard**, using Rime Coda's word-level timestamps
   over `/ws3`, and grounds the next response in that;
3. tool results from before the interruption are **never spoken as current** — they are fenced, and only
   re-enter the conversation through an explicit reconciliation (`await_pending`) in the new turn;
4. background work is kept, cancelled or reconciled correctly: uncommitted writes are cancelled, committed but
   unheard writes are surfaced first, read-only lookups survive a "are you still there?";
5. the final spoken response reflects what the caller actually heard and requested.

Speech provider for every judged run: **Rime**, `modelId=coda`, `speaker=astra`, `lang=en`,
`wss://users-ws.rime.ai/ws3` (`segment=never`, `audioFormat=pcm`, `samplingRate=24000`). Fallback path
(Rime HTTP `audio/L16`, same model/speaker) is disclosed in the provider badge and telemetry if it triggers.

## Acceptance test (defined before the demo)

Seven scripted scenarios in [eval/scenarios.py](eval/scenarios.py). Each drives the real `Session` code —
same VAD, fence, ledger, tool runner, SpeechController and Rime client as production — with a simulated
client that has a **real-time playback clock** (audio "plays" at 24 kHz from the moment the first frame
arrives), injects real mic PCM so the server VAD fires as it would live, and acknowledges `flush` like the
browser worklet does. Interruptions are injected at fixed offsets into the audio timeline, so runs are
repeatable. The rule agent is used so behaviour is deterministic; the fencing/ledger code is identical with an
LLM.

| # | Scenario | Tool delay | Pass criteria (all must hold) |
|---|---|---|---|
| A | Interrupt mid-speech, change the day | 0 | stop < 300 ms; heard text is a word prefix of intended; some text unheard; alignment = `word_timestamps`; new answer is about Thursday; old Tuesday offer not repeated |
| B | Interrupt during a 3 s lookup, change the day | 3000 | stop < 300 ms; old lookup orphaned (not killed) then discarded/fenced; the Tuesday result is never spoken anywhere; exactly one Tuesday lookup; Thursday answered |
| C | "Are you still there?" during a 3 s lookup | 3000 | lookup **not** restarted (exactly one); result reconciled into the new epoch; status acknowledged; Tuesday result spoken exactly once and only after reconciliation |
| D | Baseline booking, two turns, no interruption | 0 | booking committed; confirmation code spoken letter by letter; TTFA measured for both turns |
| E | Wordless interruption (cough) mid-sentence | 0 | agent resumes from the interrupted sentence; resume text covers all unheard text; fully heard sentences not repeated |
| F | Interrupt while a 2.5 s booking write is in flight | 2500 | write cancelled; **nothing committed**; no false "booked" confirmation ever spoken; Thursday answered |
| G | Interrupt after booking committed but before the code was heard, then "move it to Thursday" | 0 | unheard confirmation detected; surfaced first ("Quick note…"); rescheduled not double-booked (`commits == [book, reschedule]`); same code kept |

Metrics recorded per run (JSONL, `evidence/runs/`): `stop_ms` (barge-in detected → client flush ack),
`flush_ack_ms`, `detect_ms` (first mic sample of the interruption → detection; includes the 180 ms barge-in
guard), `user_speech_to_stop_ms`, `ttfa_ms` (end of user turn → first sample actually played, cold vs warm
labelled), `tts_first_byte_ms` (Rime), counts of orphaned / cancelled / reconciled / discarded tools and of
late results fenced, client-side late frames dropped, heard/unheard text and alignment method. With Rime, the
WAV of what the user heard for the interrupted epoch is saved to `evidence/clips/`.

## Procedure (repeatable)

```bash
make setup && $EDITOR .env          # RIME_API_KEY
make preflight-synth                # validates coda/astra/en against the live catalog; one real ws3 + HTTP synthesis
make evidence RUNS=20               # -> evidence/SUMMARY-rime.md, summary-rime.json, results-rime.jsonl, runs/*.jsonl, clips/*.wav
make pronunciation                  # -> evidence/PRONUNCIATION.md + clips (model+voice constant, wording varies)
make test                           # unit + scenario + websocket tests (offline)
```

Single scenario: `python -m eval.run --tts rime --runs 5 --only C_status_during_tool`.
Offline logic check (tone generator, **not evidence**): `make evidence-offline`.

## Results

### Offline logic run (FakeTTS) — proves the machinery, NOT a Rime measurement

Committed as [evidence/SUMMARY-fake.md](evidence/SUMMARY-fake.md). 7 scenarios × 5 runs, 35/35 passed.
With the in-process client the stop latency is ~15 ms (the simulated acknowledgement delay), alignment is
`word_timestamps` on every interrupted run, zero stale results were ever spoken, and every scenario's
state/ledger checks held. These numbers only show the logic is correct; they say nothing about Rime.

### Judged run (Rime Coda, ws3) — fill in from `evidence/SUMMARY-rime.md`

Run `make evidence RUNS=20` with a Rime key on the demo machine and paste the summary table here. Report
stop p50/p95, speech→stop p95, TTFA warm p50/p95 and cold p50 (labelled separately), late results fenced,
and pass counts per scenario. The generated `evidence/SUMMARY-rime.md` also records the exact engine
description (model, speaker, lang, endpoint, format, transport) captured at run time.

| Scenario | Pass | Stop p50/p95 (ms) | Speech→stop p95 (ms) | TTFA warm p50/p95 (ms) | TTFA cold p50 | Late results fenced | Alignment |
|---|---|---|---|---|---|---|---|
| _to be filled from evidence/SUMMARY-rime.md_ | | | | | | | |

### Live browser check

The same interruption path was exercised through the real WebSocket + AudioWorklet client (typed turn, 3 s
tool delay, manual barge-in during the lead-in, then "Are you still there?"): heard =
`Let me check Thursday`, unheard = `afternoon. Still checking.`, alignment `word_timestamps`, stop 4 ms
(flush ack 3.7 ms on localhost), the orphaned lookup reconciled into the new epoch and its result spoken
exactly once. The recorded demo shows this with a microphone.

## Pronunciation / delivery variants

`eval/fixtures/pronunciation.json` holds five fixtures (confirmation code, times, lead-in punctuation,
interruption acknowledgement, unheard-booking note), each with 2–3 wordings. `make pronunciation` renders
every variant with the production model and voice held constant, saves the clips and a table with TTFB and
duration; the listening notes justify the wording ForgeDesk ships (spaced letters with a comma pause for
codes on Coda, `spell()` on Mist; digit times; period rather than ellipsis before a tool call).

## Limitations and unsupported input

- Word-level heard-state requires Rime timestamps (Coda, `en`/`es`). Otherwise the ledger uses a
  proportional estimate and reports `method=proportional`. A word is counted as heard at 60 % played.
- `stop_ms` is measured to the client's acknowledgement (upper bound). On localhost this is a few ms; over a
  real network add one round trip. `detect_ms` includes the deliberate 180 ms barge-in guard against
  coughs and clicks; both are reported separately.
- The simulated client models playback as continuous from first frame; it does not model audio device
  buffering (typically 10–40 ms extra on real hardware).
- Cold vs warm: the first synthesis of a process is labelled cold (socket pre-warming hides most of it);
  numbers are reported separately and never mixed.
- Wordless interruptions shorter than 180 ms are ignored by design; wordless interruptions longer than that
  stop the agent and trigger the resume-from-unheard behaviour after 1.4 s of silence.
- Speaker-echo can trigger false barge-ins without echo cancellation; the demo uses headphones and the
  browser's AEC. Chrome's Web Speech API captures audio outside our AEC path — Deepgram is preferred on
  speakers.
- Scenarios use the deterministic rule agent. LLM wording varies, but every fencing and ledger guarantee is
  enforced outside the LLM (fence, tool runner, store commit check, player worklet).
- English only in the judged flow; synthetic data only.
