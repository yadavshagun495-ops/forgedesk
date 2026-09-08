# Demo script (target 4–5 minutes)

Setup before recording: `.env` with `RIME_API_KEY` (+ `DEEPGRAM_API_KEY` recommended), headphones on,
`make preflight-synth` green, `make run`, browser at http://127.0.0.1:8080, Event log panel expanded.
Nothing secret on screen: the UI never shows keys; `GET /config` is safe to show.

| Time | On screen | Say |
|---|---|---|
| 0:00–0:35 | Title card → the ForgeDesk UI | "Forge Auto Care is a car-service shop whose customers call while driving or with their hands under a bonnet. ForgeDesk is its voice front desk. Every word it says is spoken by Rime — badge top right: Rime Coda, speaker astra, English, 24 kHz PCM over the ws3 websocket. There is no screen to fall back on; if speech breaks, the product breaks." |
| 0:35–1:20 | **Normal flow.** Click Start call. Say: "Hi, I need an oil change on Tuesday afternoon." → agent offers times → "The first one works, my name is Priya." → confirmation with code read letter by letter. Point at "End of your turn → first audio" metric. | "Normal path: the agent says a short lead-in, looks up the slow booking system, offers at most three times, books, and reads the code letter by letter — that wording came out of A/B renders we saved. Time to first audio is measured from the end of my turn to the first sample the browser actually played." |
| 1:20–1:50 | Slide or the README diagram | "The hard problem we chose is interruption and recovery while tools are running. Three guarantees: audio stops promptly; the app knows exactly which words I heard — Rime's word-level timestamps make that possible; and a lookup that was already in flight can never be spoken as if it were current." |
| 1:50–3:10 | **Stress case.** Set Tool delay = 3 s. Say: "Book me Thursday afternoon." Agent: "Let me check Thursday afternoon." … while it's waiting, talk over it: "Actually, make it Friday morning." Show: heard/unheard split, stop latency, Background work panel: Thursday lookup `orphaned` → `discarded`, Friday lookup `running` → `done`. Then, during the next 3 s lookup say: "Are you still there?" → "Still checking Friday morning. One moment." → result reconciled and offered once. | "Three-second delay injected into every backend call. I interrupt mid-lookup and change the day. The audio stopped in a few milliseconds; the ledger shows I heard 'Let me check Thursday' and not 'afternoon'. The Thursday lookup was not killed — it was orphaned, then discarded because my request changed; if it finishes now, its result is fenced and never spoken. Second interruption: a status question. The agent doesn't restart the lookup; it waits for the one already running and reconciles the result into the new turn — spoken exactly once." |
| 3:10–3:50 | **Unheard confirmation.** Delay back to 0. Say: "The first one." While it's reading the code, interrupt: "Wait, can you move it to Thursday?" → "Quick note, I did book Friday at 9 AM, code … Let me move it to Thursday." → pick → "Same code." Show store state: one appointment, moved, not duplicated. | "The booking committed before I interrupted, but I never heard the code. Next turn, the agent tells me first, then reschedules instead of double-booking. State stays consistent with what I actually heard." |
| 3:50–4:30 | `evidence/SUMMARY-rime.md` table; play one `evidence/samples/*-heard.wav`; then `evidence/PRONUNCIATION.md` | "Evidence: seven scripted scenarios, twenty runs each, driving the same session code with a real-time simulated client and real Rime. Stop latency, speech-to-stop, time-to-first-audio warm and cold, and the count of late results that were fenced. This clip is what the user heard up to the interruption — it stops mid-word. And the delivery choices aren't opinion: we render each variant with the model and voice fixed and read back Rime's own word timestamps, which is how we know Coda speaks the spaced code as ten separate tokens and treats `spell()` as literal text." |
| 4:30–4:50 | Badge; `GET /config`; `make preflight-synth` output | "Active provider is always visible; fallback to Rime HTTP is disclosed if it ever triggers; no non-Rime speech exists in the judged flow. Preflight validates the exact model, speaker and language against Rime's live catalog and scans for secrets. Repo, README and RIME_EVIDENCE.md have everything to reproduce it." |

## Tips
- Keep the Event log open but scrolled to the top; `interrupt_stopped` lines are the money shot.
- If the room is noisy, raise `BARGE_IN_MIN_MS` to 250 to avoid false barge-ins; mention it as a knob.
- Prefer Deepgram for STT on the recording (better endpointing, mic goes through AEC).
- Show `cold (first synthesis)` on the first turn, `warm` after — proves the labelling rule.
- Time-to-first-audio depends on where you record: from India it is ~400–880 ms, most of it trans-Pacific
  network time. Say so out loud rather than letting it look like application latency.
- Optional 15-second beat if you want to show resilience: with the call running, kill the network for a
  moment. The badge turns amber (`FALLBACK PATH`, Rime HTTP) and speech continues. This happened for real
  during development and is written up in RIME_EVIDENCE.md.
