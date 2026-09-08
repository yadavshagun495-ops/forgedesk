"""Probe: does headless Chromium run the AudioWorklets and report playback position?

Everything in the demo recording depends on this: without a real playback clock in the
browser there is no heard-state to show. Run with the server already up.
"""

from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8080/"
FLAGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=AudioServiceOutOfProcess",
    "--mute-audio",
]


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=FLAGS)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, permissions=[])
        page = ctx.new_page()
        logs: list[str] = []
        page.on("console", lambda m: logs.append(m.text))
        page.goto(URL)
        page.click("#start")
        page.wait_for_function("document.getElementById('orb-label').textContent === 'listening'", timeout=15000)
        page.fill("#typed-text", "I need an oil change on Tuesday afternoon")
        page.click("#typed button[type=submit]")
        deadline = time.time() + 25
        state = {}
        while time.time() < deadline:
            state = page.evaluate(
                """() => ({
                    ttfa: document.getElementById('m-ttfa').textContent,
                    orb: document.getElementById('orb-label').textContent,
                    log: document.getElementById('log').textContent.slice(0, 4000),
                })"""
            )
            if "audio_first_played" in state["log"]:
                break
            time.sleep(0.25)
        played = page.evaluate("() => (document.getElementById('log').textContent.match(/audio_first_played/g)||[]).length")
        ctx.close()
        browser.close()

    ok_worklet = "audio ready: context" in state["log"]
    ok_playback = played > 0
    print(f"worklets loaded : {ok_worklet}")
    print(f"playback ticking: {ok_playback} (audio_first_played x{played})")
    print(f"ttfa reported   : {state['ttfa']}")
    print(f"orb state       : {state['orb']}")
    if not ok_playback:
        print("\nlog head:\n" + state["log"][:1200])
    return 0 if (ok_worklet and ok_playback) else 1


if __name__ == "__main__":
    sys.exit(main())
