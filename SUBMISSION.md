# DataForge 2026 — Rime AI Track — Submission

**Project:** ForgeDesk — a full-duplex voice front desk that survives being interrupted

| | |
|---|---|
| **Demo video (4:11)** | https://youtu.be/wCTvlzVX3Ew — also in this zip at `demo/forgedesk-demo.mp4` (captions: `demo/forgedesk-demo.srt`) |
| **Repository** | https://github.com/yadavshagun495-ops/forgedesk |
| **Track** | Rime AI Track |
| **Speech provider** | Rime — `modelId=coda`, `speaker=astra`, `lang=en`, `pcm_s16le@24000Hz mono`, `wss://users-ws.rime.ai/ws3`, `segment=never` |

---

## The user and the problem

Forge Auto Care is a car-service shop (synthetic data). Its customers call while driving or with their
hands full, so speech is the entire interface — there is no screen to fall back on. Rime speaks every word
the agent says. Remove the speech and there is no product.

## The hard voice problem we chose to prove

**Interruption and recovery + conversation continuity during tool work.** When the caller talks over the
agent — mid-sentence, or while a slow booking-system lookup is in flight — the application must:

1. stop queued audio promptly,
2. know **exactly which words the caller heard** (Rime Coda's word-level timestamps over `/ws3`),
3. never speak a stale tool result as if it were current,
4. cancel or reconcile background work correctly, and
5. answer the new request grounded in what was actually heard.

## Result — measured, not asserted

7 acceptance scenarios × 20 runs against live Rime: **140/140 passed** (2,773 s).

| Metric | Result |
|---|---|
| Barge-in → playback stopped and queued audio dropped | **15.7 ms p50 · 16.5 p95 · 17.1 max** |
| Caller's first mic sample → stopped (incl. a deliberate 180 ms guard) | **≤ 184 ms p95** |
| Stale tool results spoken as current | **0 / 140** |
| Audio frames played after a stop | **0 / 140** |
| Heard/unheard from Rime word timestamps | **every interrupted run** |
| Time-to-first-audio (warm p50) | 650–760 ms — ~600 ms of it is network from India to Rime US-West |

Every individual check passed 20/20, including `nothing_committed`, `moved_not_duplicated`,
`result_reconciled_into_new_epoch` and `same_code_kept`.

## What is in this zip

| Path | What it is |
|---|---|
| `README.md` | Setup, architecture, third-party services, limitations, failure behavior, exact Rime configuration |
| `RIME_EVIDENCE.md` | The hard voice claim, acceptance test defined up front, procedure, results, limitations |
| `demo/forgedesk-demo.mp4` | The demo recording (+ `.srt` captions); `demo/README.md` states exactly what is live vs scripted |
| `evidence/SUMMARY-rime.md` | The judged run: per-scenario and per-check results |
| `evidence/results/` | Item-level results, one JSON per run (140 Rime + 35 offline) |
| `evidence/samples/` | **The audio the caller actually heard** before each interruption, cut at the real playback position |
| `evidence/PRONUNCIATION.md` | Delivery variants with model and voice held constant, judged by Rime's own word timestamps |
| `forgedesk/` | The application (epoch fencing, heard-state ledger, tool orphan/cancel/reconcile, Rime ws3 + HTTP clients) |
| `eval/` | The seven acceptance scenarios and the harness that runs them |
| `scripts/` | Preflight, secret scan, pronunciation renderer, demo recorder |
| `tests/` | 32 tests, no API keys required |
| `.env.example` | Placeholders only |

## Reproduce it

```bash
make setup                 # venv + dependencies
cp .env.example .env       # then set RIME_API_KEY
make preflight-synth       # validates coda/astra/en against Rime's LIVE catalog + one real synthesis
make test                  # 32 tests, no keys needed
make run                   # http://127.0.0.1:8080  (use headphones)
make evidence RUNS=20      # regenerates every number above
make demo                  # re-records the demo video
```

## Honest notes

- Word-level heard-state requires Rime timestamps (Coda, `en`/`es`); otherwise the ledger falls back to a
  proportional estimate and reports `method=proportional`.
- Stop latency is measured to the client's acknowledgement, an upper bound on when playback stopped.
- Time-to-first-audio above is network-bound from India and is reported as measured, not as Rime's
  inference speed. Cold and warm runs are labelled separately.
- Fallback is disclosed: `rime-ws3 → rime-http → unavailable`, always visible in the UI badge and telemetry.
  There is no non-Rime speech in the judged flow.
- Synthetic data only; English only; not telephony.
