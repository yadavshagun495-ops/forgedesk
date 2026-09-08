"""Compose the final demo video: real screen recording + the three Rime voices + captions.

Audio is mixed in numpy so every source lands on an exact sample:
  narrator (bancroft) and caller (godfrey) at the times the driver recorded,
  the agent's real speech (astra) exactly as the browser played it, cut where it was interrupted.

Captions come from Rime's word timestamps, so they are word-accurate rather than estimated, and
are colour-coded per speaker.

    python -m scripts.demo.compose      # -> demo/forgedesk-demo.mp4 + .srt
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np

from forgedesk.audio import read_wav

BUILD = os.path.join("demo", "build")
VOICE = os.path.join("demo", "voice")
CAPTURE = os.path.join("demo", "capture")
OUT_MP4 = os.path.join("demo", "forgedesk-demo.mp4")
OUT_SRT = os.path.join("demo", "forgedesk-demo.srt")
SR = 24000

STYLES = {  # name -> (colour BGR hex for ASS, label)
    "narration": ("&H00FFFFFF", ""),
    "caller": ("&H00F0C48A", "Caller: "),
    "agent": ("&H004BA0F9", "ForgeDesk: "),
}


def load(path: str) -> np.ndarray:
    pcm, sr = read_wav(path)
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if sr != SR:
        n = int(round(x.size * SR / sr))
        x = np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)
    return x


def anchor_offset(cues: list[dict], cap: dict, voice: dict) -> float:
    """Seconds to add to capture time to reach video time.

    Derived from the caller turns: the driver sends the final transcript right after the caller's
    audio finishes, and the server logs that same message with its own clock.
    """
    said = [e for e in cap["events"] if e.get("type") == "transcript" and e.get("role") == "user" and e.get("final")]
    deltas = []
    for cue in [c for c in cues if c["kind"] == "caller"]:
        match = next((e for e in said if e.get("text", "").strip() == cue["text"].strip()), None)
        if match:
            deltas.append(cue["at"] + voice[cue["key"]]["duration_s"] - match["t_ms"] / 1000.0)
    if not deltas:
        return 0.0
    return float(np.median(deltas))


def build_audio(cues: list[dict], voice: dict, cap: dict | None, offset: float, total_s: float) -> np.ndarray:
    out = np.zeros(int((total_s + 2.0) * SR), dtype=np.float32)

    def add(x: np.ndarray, at_s: float, gain: float) -> None:
        i = max(0, int(at_s * SR))
        n = min(x.size, out.size - i)
        if n > 0:
            out[i : i + n] += x[:n] * gain

    for cue in cues:
        if cue["kind"] in ("narration", "caller"):
            add(load(os.path.join(VOICE, f"{cue['key']}.wav")), cue["at"], 1.0 if cue["kind"] == "narration" else 0.95)
    if cap and os.path.exists(os.path.join(CAPTURE, "agent.wav")):
        add(load(os.path.join(CAPTURE, "agent.wav")), offset, 0.95)
    peak = float(np.max(np.abs(out))) or 1.0
    if peak > 0.97:
        out *= 0.97 / peak
    return out


def group_words(words: list[str], starts: list[float], ends: list[float], base: float, max_words: int = 9, max_s: float = 3.2):
    cues = []
    i = 0
    while i < len(words):
        j = i
        while j < len(words) and (j - i) < max_words and (ends[j] - starts[i]) < max_s:
            j += 1
            if j < len(words) and words[j - 1].endswith((".", "?", "!")):
                break
        cues.append((base + starts[i], base + ends[j - 1], " ".join(words[i:j])))
        i = j
    return cues


def caption_cues(cues: list[dict], voice: dict, cap: dict | None, offset: float) -> list[tuple[float, float, str, str]]:
    out: list[tuple[float, float, str, str]] = []
    for cue in cues:
        if cue["kind"] not in ("narration", "caller"):
            continue
        m = voice[cue["key"]]
        if m["words"]:
            for a, b, text in group_words(m["words"], m["starts"], m["ends"], cue["at"]):
                out.append((a, b, text, cue["kind"]))
        else:
            out.append((cue["at"], cue["at"] + m["duration_s"], m["text"], cue["kind"]))
    if cap:
        sr = cap["sample_rate"]
        for ep in cap["timeline"]:
            end_ms = ep["start_ms"] + ep["duration_ms"]
            for k, seg in enumerate(ep["segments"]):
                a = offset + (ep["start_ms"] + seg["at_samples"] * 1000 / sr) / 1000
                nxt = ep["segments"][k + 1]["at_samples"] * 1000 / sr if k + 1 < len(ep["segments"]) else ep["duration_ms"]
                b = offset + (ep["start_ms"] + nxt) / 1000
                b = min(b, offset + end_ms / 1000)
                if b - a > 0.25 and seg["text"].strip():
                    out.append((a, b, seg["text"].strip(), "agent"))
    return sorted(out)


def ts_ass(t: float) -> str:
    h, rem = divmod(max(0.0, t), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ts_srt(t: float) -> str:
    h, rem = divmod(max(0.0, t), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"


def write_captions(cues: list[tuple[float, float, str, str]], w: int, h: int) -> str:
    ass = os.path.join(BUILD, "captions.ass")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
"""
    for name, (colour, _) in STYLES.items():
        head += (
            f"Style: {name},DejaVu Sans,34,{colour},&H00000000,&H96000000,"
            f"{1 if name == 'agent' else 0},3,0,0,2,120,120,46,1\n"
        )
    head += "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    lines = []
    for a, b, text, kind in cues:
        label = STYLES[kind][1]
        safe = text.replace("{", "(").replace("}", ")").replace("\n", " ")
        lines.append(f"Dialogue: 0,{ts_ass(a)},{ts_ass(b)},{kind},,0,0,0,,{label}{safe}")
    with open(ass, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")
    with open(OUT_SRT, "w", encoding="utf-8") as f:
        for i, (a, b, text, kind) in enumerate(cues, 1):
            f.write(f"{i}\n{ts_srt(a)} --> {ts_srt(b)}\n{STYLES[kind][1]}{text}\n\n")
    return ass


def main() -> int:
    tl_path = os.path.join(BUILD, "timeline.json")
    if not os.path.exists(tl_path):
        print("run `python -m scripts.demo.record` first", file=sys.stderr)
        return 2
    tl = json.load(open(tl_path, encoding="utf-8"))
    voice = {m["key"]: m for m in json.load(open(os.path.join(VOICE, "index.json"), encoding="utf-8"))}
    cap_path = os.path.join(CAPTURE, "agent.json")
    cap = json.load(open(cap_path, encoding="utf-8")) if os.path.exists(cap_path) else None
    if cap is None:
        print("! no agent capture found; the product's own speech will be missing", file=sys.stderr)

    video = os.path.join(BUILD, "video.webm")
    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video]).decode().strip())
    offset = anchor_offset(tl["cues"], cap, voice) if cap else 0.0
    print(f"video {dur:.1f}s, capture offset {offset:+.2f}s")

    mix = build_audio(tl["cues"], voice, cap, offset, dur)
    wav = os.path.join(BUILD, "mix.wav")
    import wave

    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(mix, -1, 1) * 32767).astype("<i2").tobytes())

    cues = caption_cues(tl["cues"], voice, cap, offset)
    ass = write_captions(cues, tl["viewport"]["width"], tl["viewport"]["height"])
    print(f"{len(cues)} caption cues")

    cmd = [
        "ffmpeg", "-y", "-i", video, "-i", wav,
        "-vf", f"subtitles={ass}:fontsdir=/usr/share/fonts,fps=30,format=yuv420p",
        # 48 kHz stereo: 24 kHz mono AAC is legal but several players render it silently
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000:resampler=soxr,pan=stereo|c0=c0|c1=c0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-shortest", OUT_MP4,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    size = os.path.getsize(OUT_MP4) / 1e6
    final = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", OUT_MP4]).decode().strip())
    probe = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_name,sample_rate,channels", "-of", "csv=p=0", OUT_MP4]).decode().strip()
    print(f"\n{OUT_MP4}  {final / 60:.2f} min  {size:.1f} MB  audio: {probe}")
    print(f"{OUT_SRT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
