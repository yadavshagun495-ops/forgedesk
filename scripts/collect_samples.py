"""Curate a small, committed evidence sample from the generated artifacts.

    python -m scripts.collect_samples

Copies one "what the user actually heard" clip per scenario plus the pronunciation variants into
evidence/samples/ (the only audio the repository commits) and writes an index describing each one,
so judges can hear the interruption behaviour without running anything.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import wave

from forgedesk.config import load_settings

SAMPLES = "samples"


def duration_s(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def main() -> int:
    settings = load_settings()
    ev = settings.evidence_dir
    out = os.path.join(ev, SAMPLES)
    os.makedirs(out, exist_ok=True)
    results_path = os.path.join(ev, "results-rime.jsonl")
    if not os.path.exists(results_path):
        print("no results-rime.jsonl; run `make evidence` first", file=sys.stderr)
        return 2

    by_scenario: dict[str, dict] = {}
    for line in open(results_path, encoding="utf-8"):
        r = json.loads(line)
        by_scenario.setdefault(r["scenario"], r)  # first run of each scenario

    index: list[dict] = []
    for scenario, r in sorted(by_scenario.items()):
        m = r["metrics"]
        matches = sorted(glob.glob(os.path.join(ev, "clips", f"{scenario}-*-heard-e*.wav")))
        if not matches:
            continue
        src = matches[0]
        dst = os.path.join(out, f"{scenario}-heard.wav")
        shutil.copyfile(src, dst)
        index.append(
            {
                "file": os.path.basename(dst),
                "scenario": scenario,
                "what_it_is": "the audio the caller actually heard before the interruption stopped playback",
                "heard": m.get("heard"),
                "unheard": m.get("unheard"),
                "alignment": m.get("alignment"),
                "stop_ms": m.get("stop_ms"),
                "audio_s": round(duration_s(dst), 2),
            }
        )

    for src in sorted(glob.glob(os.path.join(ev, "clips", "pron-*.wav"))):
        dst = os.path.join(out, os.path.basename(src))
        shutil.copyfile(src, dst)
        index.append(
            {
                "file": os.path.basename(dst),
                "scenario": "pronunciation",
                "what_it_is": "delivery variant rendered with the model and voice held constant",
                "audio_s": round(duration_s(dst), 2),
            }
        )

    cfg = settings.rime.public()
    lines = [
        "# Evidence samples",
        "",
        f"Rendered by Rime `{cfg['model_id']}` / `{cfg['speaker']}` / `{cfg['lang']}`, {cfg['audio_format']}, "
        f"via {cfg['endpoint']}. Regenerate everything with `make evidence && make pronunciation && make samples`.",
        "",
        "The `*-heard.wav` files are not the agent's full response: they are the audio that had actually "
        "reached the speaker when the caller interrupted, reconstructed from the client's playback position. "
        "Each one should stop mid-sentence at exactly the word listed under **Heard**.",
        "",
        "| File | Scenario | Audio (s) | Heard | Cut off before | Alignment | Stop (ms) |",
        "|---|---|---|---|---|---|---|",
    ]
    for i in index:
        if i["scenario"] == "pronunciation":
            continue
        lines.append(
            f"| `{i['file']}` | {i['scenario']} | {i['audio_s']} | \"{i['heard']}\" | \"{i['unheard']}\" | "
            f"{i['alignment']} | {i['stop_ms']} |"
        )
    lines += ["", "## Pronunciation variants", "",
              "See [PRONUNCIATION.md](PRONUNCIATION.md) for the text of each variant and the tokens Rime spoke.",
              "", "| File | Audio (s) |", "|---|---|"]
    for i in index:
        if i["scenario"] == "pronunciation":
            lines.append(f"| `{i['file']}` | {i['audio_s']} |")

    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8") as fh:
        json.dump({"config": cfg, "samples": index}, fh, indent=2)
    total_mb = sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out)) / 1e6
    print(f"{len(index)} sample(s) in {out} ({total_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
