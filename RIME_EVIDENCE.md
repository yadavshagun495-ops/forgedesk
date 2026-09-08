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

### What the first live run caught (why measuring the user-visible path matters)

The offline harness passed 35/35 while the product was, against the real API, badly broken. The first run
with a Rime key measured **`stop_ms` = 10,001 ms** on every interruption scenario — the user would have kept
hearing the old sentence for ten seconds. Causes, all invisible to a proxy metric:

| Symptom | Cause | Fix |
|---|---|---|
| barge-in → audio stop = 10,001 ms | `cancel()` awaited the WebSocket close handshake; mid-stream the server keeps sending, so it blocked for the full `close_timeout` | detach the socket, close it in the background, continue on a pre-warmed spare — **~15 ms** |
| every synthesis after the first interruption failed with `ConcurrencyError: cannot call recv while another coroutine is already running recv` | the abandoned generator still owned the socket and cleared the engine's busy flag when it was finalised | per-synthesis socket ownership: a generator only releases the engine if it still owns the connection |
| a turn after a ~7 s pause fell back to Rime HTTP (4.2 s to first audio) | Rime drops idle ws3 sockets; both the pooled socket and the equally old spare were dead, so both retries failed | 5 s pings, `close_code`/age checked before reuse, and a dropped connection forces a brand-new socket instead of a stale spare |

After the fixes, the same seven scenarios pass end to end against Rime, and a direct connection test
(cold, warm, 8/15/30/45 s idle gaps, and cancel-mid-stream-then-resynthesize) shows **0 reconnects** with
warm TTFB 400–880 ms throughout. The HTTP fallback that engaged during the failure is itself evidence that
the disclosed fallback path works and stays visible.

### Region choice (network vs model latency)

Six warm ws3 requests per endpoint from the development machine (India), production configuration:

| Endpoint | Cold | Warm TTFB p50 | Min | Max |
|---|---|---|---|---|
| `wss://users-ws.rime.ai` (US West, default) | 519 ms | **578 ms** | 359 ms | 582 ms |
| `wss://users-east-ws.rime.ai` (US East) | 403 ms | 637 ms | 514 ms | 723 ms |

Most of this is trans-Pacific network time — TLS connect alone is ~1.1 s from here, and Rime publishes ~96 ms
P50 model latency for Coda. Time-to-first-audio figures below are therefore **network-bound and specific to
this location**; they are not a claim about Rime's inference speed, and a judge running the same command from
a US region should see materially lower numbers.

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
every variant over ws3 with the production model and voice held constant, saves the clips, and records **the
tokens Rime actually spoke** from its word-level timestamps — so the comparison does not depend on anyone's
ears. Full table: [evidence/PRONUNCIATION.md](evidence/PRONUNCIATION.md).

The decisive one, confirmation codes (Coda, `astra`, identical apart from the text):

| Variant | Text sent | Spoken tokens | Audio |
|---|---|---|---|
| v1 | `Your confirmation code is FD-7Q2K.` | 5 — the code is a single token | 3.92 s |
| **v2 (shipped on Coda)** | `Your confirmation code is F D, 7 Q 2 K.` | **10 — every letter and digit separate** | 5.20 s |
| v3 | `Your confirmation code is spell(FD7Q2K).` | 5 — `spell(FD7Q2K).` stays one token | 6.00 s |

v3 is the form ForgeDesk ships on Mist models; rendering it on Coda shows objectively why the choice must be
model-specific (Coda does not process `spell()`, and the 6.00 s of audio for one token is it reading the
literal text). `forgedesk/llm/prompt.py` picks the right form from `RIME_MODEL_ID`.

## Limitations and unsupported input

- Word-level heard-state requires Rime timestamps (Coda, `en`/`es`). Otherwise the ledger uses a
  proportional estimate and reports `method=proportional`. A word is counted as heard at 60 % played.
- `stop_ms` is measured to the client's acknowledgement (upper bound). On localhost this is a few ms; over a
  real network add one round trip. `detect_ms` includes the deliberate 180 ms barge-in guard against
  coughs and clicks; both are reported separately.
- **Time-to-first-audio here is dominated by trans-Pacific network latency** (see the region table above),
  not by Rime inference or by ForgeDesk. It is reported as measured, from India, warm and cold labelled.
- Rime drops idle ws3 sockets within seconds. Pings and a warm spare hide this, but the first turn after a
  long pause can still pay a reconnect, and if both connections are dead the turn is served by the disclosed
  Rime HTTP fallback (visibly, in the badge and telemetry) instead of failing.
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
