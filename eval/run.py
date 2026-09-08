"""CLI: run the acceptance scenarios N times and write evidence.

    python -m eval.run --tts fake --runs 3            # offline logic check (NOT evidence)
    python -m eval.run --tts rime --runs 20           # judged evidence (needs RIME_API_KEY)
    python -m eval.run --only A_interrupt_mid_speech --runs 5

Runs are resumable: each completed run is written to <out>/results/ immediately and skipped on a
later invocation, so a long evidence job can be interrupted and continued. `--summarize` rebuilds
the summary from whatever is already on disk without running anything.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time

from forgedesk.config import Settings

from .harness import RunResult, build_tts, make_settings, run_scenario
from .scenarios import BY_NAME, SCENARIOS


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    k = (len(vs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vs) - 1)
    return round(vs[lo] + (vs[hi] - vs[lo]) * (k - lo), 1)


def result_path(out_dir: str, tts: str, scenario: str, idx: int) -> str:
    return os.path.join(out_dir, "results", f"{scenario}-{tts}-{idx:02d}.json")


def save_result(out_dir: str, r: RunResult) -> None:
    path = result_path(out_dir, r.tts_kind, r.scenario, r.run_idx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "scenario": r.scenario, "run": r.run_idx, "tts": r.tts, "tts_kind": r.tts_kind,
                "checks": r.checks, "metrics": r.metrics, "heard_clip": r.heard_clip,
                "spoken": {str(k): v for k, v in r.speak_texts.items()},
                "ledger": r.ledger, "store": r.store, "client": r.client,
            },
            f, ensure_ascii=False,
        )


def load_results(out_dir: str, tts: str) -> list[dict]:
    paths = sorted(glob.glob(os.path.join(out_dir, "results", f"*-{tts}-*.json")))
    return [json.load(open(p, encoding="utf-8")) for p in paths]


def summarize(results: list[RunResult], tts_kind: str, tts_desc: dict, started: str, elapsed_s: float) -> dict:
    by: dict[str, list[RunResult]] = {}
    for r in results:
        by.setdefault(r["scenario"], []).append(r)
    out: dict = {"tts": tts_kind, "tts_engine": tts_desc, "started_utc": started, "elapsed_s": round(elapsed_s, 1), "scenarios": {}}
    for name in sorted(by):
        rs = by[name]
        checks: dict[str, int] = {}
        for r in rs:
            for k, v in r["checks"].items():
                checks[k] = checks.get(k, 0) + (1 if v else 0)
        met = [r["metrics"] for r in rs]
        stop = [m["stop_ms"] for m in met if "stop_ms" in m]
        ack = [m["flush_ack_ms"] for m in met if "flush_ack_ms" in m]
        detect = [m["detect_ms"] for m in met if "detect_ms" in m]
        u2s = [m["user_speech_to_stop_ms"] for m in met if "user_speech_to_stop_ms" in m]
        ttfa = [x for m in met for x in m.get("ttfa_ms", [])]
        ttfa_cold = [x for m in met for x in m.get("ttfa_cold", [])]
        ttfa_warm = [x for m in met for x in m.get("ttfa_ms", []) if x not in m.get("ttfa_cold", [])]
        fb = [x for m in met for x in m.get("tts_first_byte_ms", [])]
        out["scenarios"][name] = {
            "description": BY_NAME[name].description,
            "claim": BY_NAME[name].claim,
            "runs": len(rs),
            "passed": sum(1 for r in rs if all(r["checks"].values())),
            "checks": {k: f"{v}/{len(rs)}" for k, v in checks.items()},
            "stop_ms": {"p50": pct(stop, 0.5), "p95": pct(stop, 0.95), "max": max(stop) if stop else None},
            "flush_ack_ms": {"p50": pct(ack, 0.5), "p95": pct(ack, 0.95)},
            "vad_detect_ms": {"p50": pct(detect, 0.5), "p95": pct(detect, 0.95)},
            "user_speech_to_audio_stop_ms": {"p50": pct(u2s, 0.5), "p95": pct(u2s, 0.95)},
            "ttfa_ms_all": {"p50": pct(ttfa, 0.5), "p95": pct(ttfa, 0.95), "n": len(ttfa)},
            "ttfa_ms_cold": {"p50": pct(ttfa_cold, 0.5), "n": len(ttfa_cold)},
            "ttfa_ms_warm": {"p50": pct(ttfa_warm, 0.5), "p95": pct(ttfa_warm, 0.95), "n": len(ttfa_warm)},
            "tts_first_byte_ms": {"p50": pct(fb, 0.5), "p95": pct(fb, 0.95), "n": len(fb)},
            "stale_results_fenced_total": sum(m.get("stale_results_fenced", 0) for m in met),
            "late_frames_dropped_total": sum(m.get("late_frames_dropped", 0) for m in met),
            "alignment_methods": sorted({m.get("alignment", "n/a") for m in met}),
            "errors": [m["error"] for m in met if "error" in m],
            "clips": [r["heard_clip"] for r in rs if r.get("heard_clip")],
        }
    return out


def render_markdown(summary: dict) -> str:
    tts = summary["tts"]
    warn = "" if tts == "rime" else "\n> **NOT EVIDENCE.** This run used the offline FakeTTS tone generator to exercise the logic. Only `--tts rime` runs count.\n"
    lines = [f"# Evidence summary ({tts})", "", f"Started {summary['started_utc']} UTC, {summary['elapsed_s']} s total.", warn]
    eng = summary.get("tts_engine", {})
    if eng:
        lines.append("Speech engine: " + ", ".join(f"{k}={v}" for k, v in eng.items() if k in ("provider", "engine", "model_id", "speaker", "lang", "endpoint", "audio_format", "transport")))
        lines.append("")
    lines.append("| Scenario | Pass | Stop p50/p95 (ms) | Speech->stop p95 (ms) | TTFA warm p50/p95 (ms) | TTFA cold p50 | Late results fenced | Alignment |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for name, s in summary["scenarios"].items():
        st = s["stop_ms"]
        u2s = s["user_speech_to_audio_stop_ms"]
        tw, tc = s["ttfa_ms_warm"], s["ttfa_ms_cold"]
        lines.append(
            f"| {name} | {s['passed']}/{s['runs']} | {st['p50'] if st['p50'] is not None else '-'} / {st['p95'] if st['p95'] is not None else '-'} | "
            f"{u2s['p95'] if u2s['p95'] is not None else '-'} | {tw['p50'] if tw['p50'] is not None else '-'} / {tw['p95'] if tw['p95'] is not None else '-'} | "
            f"{tc['p50'] if tc['p50'] is not None else '-'} | {s['stale_results_fenced_total']} | {', '.join(s['alignment_methods'])} |"
        )
    lines.append("")
    lines.append("Stop = barge-in detected on the server -> client acknowledged that playback stopped and queued audio was dropped. "
                 "Speech->stop = from the first mic sample of the user's interruption (includes the 180 ms barge-in guard). "
                 "TTFA = end of user turn -> first audio sample actually played by the client. "
                 "Late results fenced = tool results that arrived after an interruption and were never spoken as current.")
    lines.append("")
    for name, s in summary["scenarios"].items():
        lines.append(f"## {name}")
        lines.append("")
        lines.append(s["description"])
        lines.append("")
        lines.append(f"Claim: {s['claim']}")
        lines.append("")
        for k, v in s["checks"].items():
            lines.append(f"- {k}: {v}")
        if s["errors"]:
            lines.append(f"- errors: {s['errors']}")
        if s["clips"]:
            lines.append(f"- heard-audio clips: {len(s['clips'])} (evidence/clips/)")
        lines.append("")
    return "\n".join(lines)


def write_summary(out_dir: str, tts_kind: str, tts_desc: dict, started: str, elapsed_s: float) -> tuple[int, int]:
    results = load_results(out_dir, tts_kind)
    summary = summarize(results, tts_kind, tts_desc, started, elapsed_s)
    with open(os.path.join(out_dir, f"summary-{tts_kind}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(out_dir, f"SUMMARY-{tts_kind}.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(summary))
    with open(os.path.join(out_dir, f"results-{tts_kind}.jsonl"), "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return sum(1 for r in results if all(r["checks"].values())), len(results)


async def main_async(args: argparse.Namespace) -> int:
    names = [n for n in (args.only.split(",") if args.only else BY_NAME) if n]
    scenarios = [BY_NAME[n] for n in names]
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    t0 = time.monotonic()
    settings = make_settings(0, args.tts)
    # one engine for the whole run so connection warmth is realistic (first call = cold)
    shared_tts = build_tts(args.tts, settings)
    tts_desc = shared_tts.describe()
    todo = [(scn, i) for scn in scenarios for i in range(args.runs)
            if args.force or not os.path.exists(result_path(out_dir, args.tts, scn.name, i))]
    skipped = len(scenarios) * args.runs - len(todo)
    if skipped:
        print(f"resuming: {skipped} run(s) already on disk, {len(todo)} to go", flush=True)
    try:
        for n, (scn, i) in enumerate(todo, 1):
            r = await run_scenario(scn, args.tts, i, out_dir, tts=shared_tts)
            save_result(out_dir, r)
            flag = "PASS" if r.passed else "FAIL"
            extra = f" stop={r.metrics['stop_ms']}ms" if "stop_ms" in r.metrics else ""
            if r.metrics.get("ttfa_ms"):
                extra += f" ttfa={r.metrics['ttfa_ms']}"
            print(f"[{flag}] ({n}/{len(todo)}) {scn.name} #{i}{extra}", flush=True)
            if not r.passed:
                print("       failed:", [k for k, v in r.checks.items() if not v], r.metrics.get("error", ""), flush=True)
            write_summary(out_dir, args.tts, tts_desc, started, time.monotonic() - t0)
    finally:
        await shared_tts.aclose()
    passed, total = write_summary(out_dir, args.tts, tts_desc, started, time.monotonic() - t0)
    print(f"\n{passed}/{total} runs passed. Summary: {out_dir}/SUMMARY-{args.tts}.md")
    return 0 if passed == total else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tts", choices=["fake", "rime"], default="fake")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", default="", help="comma-separated scenario names")
    ap.add_argument("--out", default=os.environ.get("EVIDENCE_DIR", "evidence"))
    ap.add_argument("--force", action="store_true", help="re-run even if a result file already exists")
    ap.add_argument("--summarize", action="store_true", help="rebuild the summary from results on disk, run nothing")
    args = ap.parse_args()
    if args.summarize:
        passed, total = write_summary(args.out, args.tts, {}, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), 0.0)
        print(f"{passed}/{total} runs passed. Summary: {args.out}/SUMMARY-{args.tts}.md")
        sys.exit(0 if passed == total else 1)
    if args.tts == "rime" and not Settings().rime.has_key:
        print("RIME_API_KEY is not set; use --tts fake for a logic-only run.", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
