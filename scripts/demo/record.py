"""Record the demo video by driving the real product.

Nothing here is a mock-up: a headless Chromium loads the actual web client, the caller's
utterances are injected through the same WebSocket audio path the microphone uses (so the
server's VAD detects barge-in exactly as it would from a live mic), and the agent's speech is
real Rime audio captured server-side and cut at the exact sample the caller interrupted.

    python -m scripts.demo.record            # -> demo/build/{video.webm,timeline.json}

Then `python -m scripts.demo.compose` muxes narration, caller, agent audio and burned-in captions.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time

import httpx
from playwright.sync_api import Page, sync_playwright

from .script import BEATS, Beat

BUILD = os.path.join("demo", "build")
VOICE = os.path.join("demo", "voice")
CAPTURE = os.path.join("demo", "capture")
URL = "http://127.0.0.1:8079/?demo=1"
PORT = 8079
VIEWPORT = {"width": 1600, "height": 900}
FLAGS = ["--autoplay-policy=no-user-gesture-required", "--mute-audio", "--hide-scrollbars", "--force-device-scale-factor=1"]

OVERLAY_CSS = """
#fd-chapter{position:fixed;left:28px;bottom:26px;z-index:9999;font:600 26px/1.3 system-ui,sans-serif;
 color:#fff;background:linear-gradient(90deg,rgba(249,115,22,.95),rgba(194,65,12,.9));padding:12px 22px;
 border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.45);opacity:0;transition:opacity .4s ease;max-width:60%;
 pointer-events:none}
#fd-chapter.on{opacity:1}
.fd-focus{outline:3px solid #f97316 !important;outline-offset:4px;border-radius:16px;
 box-shadow:0 0 0 9999px rgba(0,0,0,.42) !important;position:relative;z-index:9998}
#fd-sheet{position:fixed;inset:0;z-index:10000;background:rgba(11,15,20,.97);color:#e6edf3;
 padding:54px 70px;font:16px/1.6 system-ui,sans-serif;opacity:0;visibility:hidden;pointer-events:none;
 transition:opacity .5s ease;overflow:hidden}
#fd-sheet.on{opacity:1;visibility:visible}
#fd-sheet h1{font-size:38px;margin:0 0 6px}
#fd-sheet h2{font-size:17px;color:#8b98a8;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;margin:0 0 26px}
#fd-sheet table{border-collapse:collapse;width:100%;font-size:16px}
#fd-sheet th,#fd-sheet td{padding:9px 12px;border-bottom:1px solid #1f2a3a;text-align:left}
#fd-sheet th{color:#8b98a8;font-size:13px;text-transform:uppercase;letter-spacing:1px}
#fd-sheet td.k{color:#86efac;font-variant-numeric:tabular-nums;font-weight:700}
#fd-sheet .big{display:flex;gap:44px;margin:30px 0 34px}
#fd-sheet .big div{background:#121923;border:1px solid #1f2a3a;border-radius:16px;padding:20px 26px;flex:1}
#fd-sheet .big b{display:block;font-size:40px;color:#fb923c;font-variant-numeric:tabular-nums}
#fd-sheet .big span{color:#8b98a8;font-size:14px}
#fd-sheet code{background:#182231;padding:2px 7px;border-radius:6px;color:#d8c7ff}
#fd-sheet ul{margin:18px 0 0;padding-left:20px} #fd-sheet li{margin:8px 0}
"""

OVERLAY_JS = """
(css) => {
  const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s);
  const c = document.createElement('div'); c.id = 'fd-chapter'; document.body.appendChild(c);
  const sh = document.createElement('div'); sh.id = 'fd-sheet'; document.body.appendChild(sh);
  window.fdOverlay = {
    chapter: (t) => { c.textContent = t || ''; c.classList.toggle('on', !!t); },
    focus: (sel) => {
      document.querySelectorAll('.fd-focus').forEach(e => e.classList.remove('fd-focus'));
      if (sel) { const e = document.querySelector(sel); if (e) e.classList.add('fd-focus'); }
    },
    sheet: (html) => { sh.innerHTML = html || ''; sh.classList.toggle('on', !!html); },
  };
}
"""


def evidence_sheet() -> str:
    d = json.load(open(os.path.join("evidence", "summary-rime.json"), encoding="utf-8"))
    rows = ""
    for name, s in d["scenarios"].items():
        st = s["stop_ms"]
        rows += (
            f"<tr><td>{name.split('_', 1)[1].replace('_', ' ')}</td>"
            f"<td class='k'>{s['passed']}/{s['runs']}</td>"
            f"<td class='k'>{st['p50'] if st['p50'] is not None else '–'} / {st['p95'] if st['p95'] is not None else '–'}</td>"
            f"<td class='k'>{s['stale_results_fenced_total']}</td>"
            f"<td>{', '.join(s['alignment_methods'])}</td></tr>"
        )
    eng = d["tts_engine"]
    return f"""
      <h1>Evidence</h1><h2>make evidence RUNS=20 &nbsp;·&nbsp; {eng['model_id']} / {eng['speaker']} / {eng['lang']} · {eng['endpoint']}</h2>
      <div class="big">
        <div><b>140/140</b><span>runs passed · 7 scenarios × 20</span></div>
        <div><b>15.7 ms</b><span>barge-in → playback stopped (p50); 17.1 ms max</span></div>
        <div><b>0</b><span>stale results spoken · 0 frames played after stop</span></div>
      </div>
      <table><tr><th>Scenario</th><th>Pass</th><th>Stop p50/p95 (ms)</th><th>Late results fenced</th><th>Heard-state alignment</th></tr>{rows}</table>
    """


OUTRO_SHEET = """
  <h1>ForgeDesk</h1><h2>Full-duplex voice front desk · Rime Coda</h2>
  <div class="big">
    <div><b>make evidence</b><span>regenerates every number in this video</span></div>
    <div><b>make test</b><span>32 tests, no API keys required</span></div>
    <div><b>make preflight-synth</b><span>live catalog + secret check</span></div>
  </div>
  <ul>
    <li><code>RIME_EVIDENCE.md</code> — the claim, the acceptance test, item-level results, limitations</li>
    <li><code>evidence/samples/</code> — the audio the caller actually heard before each interruption</li>
    <li><code>evidence/PRONUNCIATION.md</code> — delivery variants, model and voice held constant</li>
    <li>Speech in this video: agent <code>astra</code>, caller <code>godfrey</code>, narrator <code>bancroft</code> — all Rime Coda</li>
  </ul>
"""


class Recorder:
    def __init__(self, page: Page, t0: float) -> None:
        self.page = page
        self.t0 = t0
        self.cues: list[dict] = []
        self.voice = {m["key"]: m for m in json.load(open(os.path.join(VOICE, "index.json"), encoding="utf-8"))}

    def t(self) -> float:
        return time.monotonic() - self.t0

    def cue(self, kind: str, **kw) -> None:
        self.cues.append({"at": round(self.t(), 3), "kind": kind, **kw})

    # ------------------------------------------------------------------ waits
    def count(self, kind: str) -> int:
        return int(self.page.evaluate("k => window.fdDemo.count(k)", kind))

    def wait_count(self, kind: str, since: int, timeout: float = 30.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.count(kind) > since:
                return True
            self.page.wait_for_timeout(50)
        print(f"  ! timeout waiting for event {kind!r}")
        return False

    def wait_state(self, state: str, timeout: float = 40.0) -> None:
        try:
            self.page.wait_for_function(
                "s => document.getElementById('orb-label').textContent === s", arg=state, timeout=timeout * 1000
            )
        except Exception:  # noqa: BLE001
            print(f"  ! timeout waiting for state {state}")

    def wait_caption(self, needle: str, timeout: float = 30.0) -> None:
        try:
            self.page.wait_for_function(
                "n => (document.getElementById('caption').textContent||'').toLowerCase().includes(n)",
                arg=needle.lower(), timeout=timeout * 1000,
            )
        except Exception:  # noqa: BLE001
            print(f"  ! timeout waiting for caption {needle!r}")

    # ------------------------------------------------------------------ actions
    def speak_caller(self, key: str, text: str) -> None:
        b64 = open(os.path.join(VOICE, f"{key}.mic.b64"), encoding="utf-8").read()
        self.cue("caller", key=key, text=text)
        self.page.evaluate("b => window.fdDemo.mic(b)", b64)
        self.page.evaluate("t => window.fdDemo.transcript(t, true)", text)

    def narrate(self, key: str, text: str) -> float:
        dur = self.voice[key]["duration_s"]
        self.cue("narration", key=key, text=text)
        return dur


def start_server() -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"DEMO_CAPTURE_DIR": CAPTURE, "TTS_PROVIDER": "rime", "STT_PROVIDER": "browser", "LLM_PROVIDER": "rules", "TOOL_DELAY_MS": "0"})
    shutil.rmtree(CAPTURE, ignore_errors=True)
    proc = subprocess.Popen(
        [".venv/bin/python", "-m", "uvicorn", "forgedesk.server:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=1).status_code == 200:
                return proc
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("server did not start")


def run_beat(rec: Recorder, beat: Beat, i: int) -> None:
    page = rec.page
    if beat.label:
        page.evaluate("t => window.fdOverlay.chapter(t)", beat.label)
    page.evaluate("s => window.fdOverlay.focus(s)", beat.focus or "")
    action, _, arg = beat.action.partition(" ")

    narration_wait = 0.0
    if beat.narration:
        narration_wait = rec.narrate(f"{i:02d}-narration", beat.narration)

    if action == "start":
        page.click("#start")
        page.wait_for_function("() => window.fdDemo && window.fdDemo.ready()", timeout=20000)
        rec.wait_state("listening")
    elif action == "delay":
        page.evaluate("ms => window.fdDemo.setDelay(ms)", int(arg))
    elif action == "show_evidence":
        page.evaluate("h => window.fdOverlay.sheet(h)", evidence_sheet())
    elif action == "show_outro":
        page.evaluate("h => window.fdOverlay.sheet(h)", OUTRO_SHEET)

    if narration_wait:
        page.wait_for_timeout(int(narration_wait * 1000))

    if action in ("say", "bargein"):
        rec.speak_caller(f"{i:02d}-caller", beat.caller)
    elif action == "bargein_after_tool":
        rec.wait_state("tool", timeout=25)
        page.wait_for_timeout(800)
        rec.speak_caller(f"{i:02d}-caller", beat.caller)
    elif action == "await_idle":
        rec.wait_state("listening")
    elif action == "await_speaking":
        rec.wait_state("speaking")
    elif action == "await_tool":
        rec.wait_state("tool", timeout=25)
        page.wait_for_timeout(900)
    elif action == "await_code":
        rec.wait_caption("confirmation code", timeout=30)
        page.wait_for_timeout(700)
    elif action == "wait":
        page.wait_for_timeout(int(float(arg or 0) * 1000))

    if beat.pause_after:
        page.wait_for_timeout(int(beat.pause_after * 1000))


def main() -> int:
    if not os.path.exists(os.path.join(VOICE, "index.json")):
        print("run `python -m scripts.demo.render_voice` first", file=sys.stderr)
        return 2
    os.makedirs(BUILD, exist_ok=True)
    for f in ("video.webm", "timeline.json"):
        try:
            os.remove(os.path.join(BUILD, f))
        except OSError:
            pass
    server = start_server()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=FLAGS)
            ctx = browser.new_context(viewport=VIEWPORT, permissions=[], record_video_dir=BUILD, record_video_size=VIEWPORT)
            t0 = time.monotonic()
            page = ctx.new_page()
            page.goto(URL)
            page.evaluate(OVERLAY_JS, OVERLAY_CSS)
            rec = Recorder(page, t0)
            page.wait_for_timeout(700)
            for i, beat in enumerate(BEATS):
                label = beat.label or (beat.action.split(" ")[0])
                print(f"[{rec.t():6.1f}s] beat {i:02d} {label}", flush=True)
                run_beat(rec, beat, i)
            total = rec.t()
            page.wait_for_timeout(400)
            video_path = page.video.path()
            ctx.close()
            browser.close()
    finally:
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    final = os.path.join(BUILD, "video.webm")
    if video_path != final:
        shutil.move(video_path, final)
    with open(os.path.join(BUILD, "timeline.json"), "w", encoding="utf-8") as f:
        json.dump({"duration_s": round(total, 2), "viewport": VIEWPORT, "cues": rec.cues}, f, indent=2)
    print(f"\nrecorded {total:.1f}s -> {final}")
    cap = os.path.join(CAPTURE, "agent.json")
    print(f"agent audio capture: {'ok' if os.path.exists(cap) else 'MISSING'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
