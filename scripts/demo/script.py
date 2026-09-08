"""The demo recording: narration lines and the beats they drive.

Every beat is a real action against the running product. `caller` lines are rendered to audio and
injected through the client's microphone path, so barge-in is detected by the same VAD a live
microphone would trigger. Narration is Rime too (a different speaker), which is disclosed on
screen and in the README.

Actions understood by the driver:
  wait <seconds>            pause
  start                     begin the call
  delay <ms>                set the injected booking-system latency
  say <text>                caller speaks (audio injected) and the transcript is delivered
  bargein <text>            caller speaks over the agent mid-response
  await_idle                wait until the agent has finished speaking
  await_speaking            wait until the agent is speaking
  await_tool                wait until a tool call is in flight
  focus <selector>          highlight a panel
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Beat:
    narration: str = ""
    action: str = ""
    caller: str = ""  # spoken by the caller; rendered with the caller voice
    focus: str = ""  # CSS selector to highlight while this beat plays
    label: str = ""  # on-screen chapter label
    pause_after: float = 0.4
    tags: list[str] = field(default_factory=list)


NARRATOR_VOICE = "bancroft"  # Coda, male, elder — deliberately different from the agent
CALLER_VOICE = "godfrey"  # Coda, male, young adult, casual — the customer
AGENT_VOICE = "astra"  # what the product itself uses

BEATS: list[Beat] = [
    Beat(
        label="The user and the problem",
        narration=(
            "Forge Auto Care's customers call while driving, or with their hands full. "
            "ForgeDesk is its voice front desk. There is no screen to fall back on."
        ),
        action="wait 0.2",
    ),
    Beat(
        narration=(
            "Every word the agent says is spoken by Rime. The badge names the exact configuration: "
            "Coda, speaker astra, English, twenty four kilohertz P C M over the ws3 websocket."
        ),
        action="start",
        focus="#provider",
    ),
    Beat(
        label="Normal flow",
        narration="First, the normal path.",
        action="wait 0.3",
    ),
    Beat(
        caller="Hi, I need an oil change on Tuesday afternoon.",
        action="say",
        pause_after=0.2,
    ),
    Beat(
        narration=(
            "The panel measures the delay from the end of the caller's turn to the first audio sample "
            "the browser actually played."
        ),
        action="await_idle",
        focus=".side .panel:nth-child(2)",
    ),
    Beat(
        caller="The first one works. My name is Priya.",
        action="say",
        pause_after=0.2,
    ),
    Beat(
        narration=(
            "The confirmation code is read letter by letter. We chose that wording by rendering variants "
            "with the model and voice fixed, and reading back Rime's word timestamps."
        ),
        action="await_idle",
    ),
    Beat(
        label="The hard voice problem",
        narration=(
            "Now the hard problem: interruption and recovery while tools run. Audio must stop promptly. "
            "The application must know which words the caller heard. And a lookup already in flight must "
            "never be spoken as if it were current."
        ),
        action="delay 3000",
        focus=".controls .knob",
    ),
    Beat(
        label="Stress case: interrupt during a slow lookup",
        narration="A three second delay is now injected into every booking system call.",
        action="wait 0.2",
    ),
    Beat(
        caller="Book me Thursday afternoon for a brake inspection.",
        action="say",
        pause_after=0.0,
    ),
    Beat(
        action="await_tool",
        pause_after=0.0,
    ),
    Beat(
        caller="Actually, make it Friday morning instead.",
        action="bargein",
        pause_after=0.3,
        tags=["interrupt"],
    ),
    Beat(
        label="Stress case: staying responsive during tool work",
        caller="Hello? Are you still there?",
        action="bargein_after_tool",
        pause_after=0.4,
        tags=["interrupt"],
    ),
    Beat(
        narration=(
            "Two interruptions there. The first changed the day while the Thursday lookup was still "
            "running. Playback stopped in about a millisecond; across the twenty run evidence set the "
            "median is fifteen point seven. The ledger recorded which words reached the speaker, and the "
            "Thursday lookup was orphaned, then cancelled. The second was a status question during the "
            "next lookup. The agent did not restart it and did not lose it: it waited for the one already "
            "running, reconciled the result into the new turn, and spoke it exactly once."
        ),
        action="await_idle",
        focus=".side .panel:nth-child(1)",
    ),
    Beat(
        label="The result the caller never heard",
        narration=(
            "The last case breaks most agents. The booking commits, then the caller interrupts before "
            "the code reaches their ear."
        ),
        action="delay 0",
    ),
    Beat(
        caller="The first one please.",
        action="say",
        pause_after=0.0,
    ),
    Beat(
        action="await_code",
        pause_after=0.0,
    ),
    Beat(
        caller="Wait, can you move it to Thursday afternoon?",
        action="bargein",
        pause_after=0.5,
        tags=["interrupt"],
    ),
    Beat(
        narration=(
            "The booking had committed, but the caller never heard it. The agent leads with that, "
            "then reschedules instead of double booking."
        ),
        action="await_idle",
    ),
    Beat(
        caller="The first one.",
        action="say",
        pause_after=0.2,
    ),
    Beat(
        narration="Same appointment, same code, moved. State matches what the caller heard.",
        action="await_idle",
    ),
    Beat(
        label="Evidence",
        narration=(
            "Measured, not asserted. Seven scenarios, twenty runs each, against live Rime: "
            "one hundred and forty of one hundred and forty passed. Barge-in to playback stopped, "
            "fifteen point seven milliseconds median. Stale results spoken as current: zero."
        ),
        action="show_evidence",
        pause_after=0.3,
    ),
    Beat(
        narration=(
            "Time to first audio is about seven hundred milliseconds here. Six hundred of that is "
            "network time from India to Rime's US West region."
        ),
        action="wait 0.2",
    ),
    Beat(
        narration=(
            "The repository has the tests, the item level results, and the audio the caller actually heard. "
            "One command regenerates all of it. Even this narration is Rime."
        ),
        action="show_outro",
        pause_after=1.0,
    ),
]
