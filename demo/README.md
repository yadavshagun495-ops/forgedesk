# The demo video

`forgedesk-demo.mp4` (4:11) with `forgedesk-demo.srt`. Regenerate with `make demo`.

## What you are watching

**The real product, not a recreation.** A headless Chromium loads the actual web client at
`http://127.0.0.1:8079/?demo=1`, which opens a normal WebSocket session against the normal server.
Every agent utterance is live Rime Coda audio, every metric on screen is live telemetry, every tool
state (`ORPHANED`, `CANCELLED`, `RECONCILED`) is the real `ToolRunner` reporting itself. The three
interruptions in the video are genuine barge-ins detected by the server-side VAD.

## What is scripted, and how

So the recording is reproducible rather than dependent on someone talking at the right moment:

| Element | How it is produced | Same code path as a live user? |
|---|---|---|
| Caller's voice | Rendered by Rime (`godfrey`), streamed into the page and sent over the WebSocket in 20 ms frames by `window.fdDemo.mic()` | **Yes** — identical bytes to the microphone worklet, so the server VAD detects barge-in the same way |
| Caller's transcript | Sent by the driver as a `transcript` control message | **Yes** — the same message the browser recogniser sends |
| Agent's voice | Live Rime Coda (`astra`) during the recording | Yes |
| Narration | Rendered by Rime (`bancroft`) and mixed in afterwards | n/a — commentary, not part of the product |
| Captions | Built from Rime's word timestamps for the narration and caller, and from segment offsets in the capture for the agent | n/a |

Three Rime voices are used deliberately so the narrator, the caller and the product are never
confused with one another.

## The audio you hear from the agent is what the caller heard

`forgedesk/capture.py` records every outgoing audio frame and the client's reported playback
position. An interrupted response is written out **cut at the exact sample the browser reached**, so
when the caller talks over the agent mid-word, the mix cuts mid-word too. Nothing is faked in post.

## Pipeline

```bash
python -m scripts.demo.render_voice   # Rime -> demo/voice/*.wav + word timestamps
python -m scripts.demo.record         # drives the real app, records demo/build/video.webm
python -m scripts.demo.compose        # mixes audio, burns captions -> demo/forgedesk-demo.mp4
```

`demo/build/`, `demo/voice/` and `demo/capture/` are intermediates and are not committed; the
finished video and subtitles are.

## Chapters

| Time | Beat |
|---|---|
| 0:00 | The user and the problem; the active Rime configuration |
| 0:33 | Normal flow — book a slot, confirmation code read letter by letter |
| 1:14 | The hard voice problem, and a 3 s delay injected into every booking-system call |
| 1:35 | Caller interrupts mid-lookup and changes the day |
| 1:41 | Caller asks "are you still there?" while the next lookup runs |
| 2:22 | Booking commits, caller interrupts before hearing the code, agent reconciles |
| 3:17 | Evidence: 140/140 runs, stop latency, fenced results |
| 3:55 | How to reproduce everything |
