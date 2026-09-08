# Pronunciation and delivery variants

Model `coda`, speaker `astra`, lang `en`, pcm_s16le@24000Hz mono, via wss://users-ws.rime.ai/ws3. Model and voice are held constant; only the wording or punctuation changes between variants. **Spoken tokens** come from Rime's own word-level timestamps, so they show what was actually said rather than what was requested. Regenerate with `make pronunciation`.

## code

Confirmation codes must be heard letter by letter. v1 is the raw code, v2 is the spaced form ForgeDesk ships on Coda, v3 is the spell() form ForgeDesk ships on Mist models. Rendering v3 on Coda shows why the choice is model-specific: Coda does not process spell().

| Variant | Text sent to Rime | TTFB (ms) | Audio (s) | Spoken tokens (Rime timestamps) | Clip |
|---|---|---|---|---|---|
| v1 | `Your confirmation code is FD-7Q2K.` | 520 | 3.92 | `Your confirmation code is FD-7Q2K.` | clips/pron-code-v1.wav |
| v2 **(shipped)** | `Your confirmation code is F D, 7 Q 2 K.` | 618 | 5.2 | `Your confirmation code is F D, 7 Q 2 K.` | clips/pron-code-v2.wav |
| v3 | `Your confirmation code is spell(FD7Q2K).` | 698 | 6.0 | `Your confirmation code is spell(FD7Q2K).` | clips/pron-code-v3.wav |

## time

Times: digits with AM/PM vs words. ForgeDesk ships the digit form because it stays short and Rime's text normalization reads it correctly.

| Variant | Text sent to Rime | TTFB (ms) | Audio (s) | Spoken tokens (Rime timestamps) | Clip |
|---|---|---|---|---|---|
| v1 **(shipped)** | `On Thursday afternoon I have 1 PM, 3 PM and 4 PM.` | 390 | 4.72 | `On Thursday afternoon I have 1 PM, 3 PM and 4 PM.` | clips/pron-time-v1.wav |
| v2 | `On Thursday afternoon I have one, three and four in the afternoon.` | 612 | 4.4 | `On Thursday afternoon I have one, three and four in the afternoon.` | clips/pron-time-v2.wav |

## leadin

Lead-in before a slow tool call. A short sentence terminated with a period is released by the chunker immediately; the ellipsis variant delays the flush and adds trailing silence before the lookup starts.

| Variant | Text sent to Rime | TTFB (ms) | Audio (s) | Spoken tokens (Rime timestamps) | Clip |
|---|---|---|---|---|---|
| v1 **(shipped)** | `Let me check Thursday afternoon.` | 610 | 1.92 | `Let me check Thursday afternoon.` | clips/pron-leadin-v1.wav |
| v2 | `Let me check Thursday afternoon...` | 388 | 2.4 | `Let me check Thursday afternoon...` | clips/pron-leadin-v2.wav |

## interrupt_ack

Recovery after an empty interruption. The comma pause before resuming sounds more natural than running straight on.

| Variant | Text sent to Rime | TTFB (ms) | Audio (s) | Spoken tokens (Rime timestamps) | Clip |
|---|---|---|---|---|---|
| v1 **(shipped)** | `Sorry, as I was saying. On Thursday afternoon I have 4 PM.` | 638 | 5.12 | `Sorry, as I was saying. On Thursday afternoon I have 4 PM.` | clips/pron-interrupt_ack-v1.wav |
| v2 | `Sorry as I was saying on Thursday afternoon I have 4 PM.` | 609 | 3.52 | `Sorry as I was saying on Thursday afternoon I have 4 PM.` | clips/pron-interrupt_ack-v2.wav |

## unheard_note

Surfacing a booking the caller never heard confirmed. Leading with 'Quick note' signals a correction without sounding alarmed.

| Variant | Text sent to Rime | TTFB (ms) | Audio (s) | Spoken tokens (Rime timestamps) | Clip |
|---|---|---|---|---|---|
| v1 **(shipped)** | `Quick note, I did book Tuesday at 1 PM, code F D, Q Z 2 J.` | 612 | 6.48 | `Quick note, I did book Tuesday at 1 PM, code F D, Q Z 2 J.` | clips/pron-unheard_note-v1.wav |
| v2 | `I booked Tuesday at 1 PM, F D Q Z 2 J.` | 651 | 4.48 | `I booked Tuesday at 1 PM, F D Q Z 2 J.` | clips/pron-unheard_note-v2.wav |

