# Evidence samples

Rendered by Rime `coda` / `astra` / `en`, pcm_s16le@24000Hz mono, via wss://users-ws.rime.ai/ws3. Regenerate everything with `make evidence && make pronunciation && make samples`.

The `*-heard.wav` files are not the agent's full response: they are the audio that had actually reached the speaker when the caller interrupted, reconstructed from the client's playback position. Each one should stop mid-sentence at exactly the word listed under **Heard**.

| File | Scenario | Audio (s) | Heard | Cut off before | Alignment | Stop (ms) |
|---|---|---|---|---|---|---|
| `A_interrupt_mid_speech-heard.wav` | A_interrupt_mid_speech | 2.78 | "Let me check Tuesday afternoon. On Tuesday" | "afternoon I have 1 PM and 4 PM. Which works for you?" | word_timestamps | 16.5 |
| `B_interrupt_during_tool-heard.wav` | B_interrupt_during_tool | 0.74 | "Let me check" | "Tuesday afternoon." | word_timestamps | 16.2 |
| `C_status_during_tool-heard.wav` | C_status_during_tool | 0.73 | "Let me check" | "Tuesday afternoon." | word_timestamps | 15.7 |
| `E_empty_interrupt_resume-heard.wav` | E_empty_interrupt_resume | 2.2 | "Let me check Wednesday morning. Wednesday morning" | "is full. Want me to try another day?" | word_timestamps | 15.5 |
| `F_interrupt_uncommitted_mutation-heard.wav` | F_interrupt_uncommitted_mutation | 0.08 | "" | "Booking Tuesday at 1 PM for you." | word_timestamps | 16.1 |
| `G_interrupt_after_commit_unheard-heard.wav` | G_interrupt_after_commit_unheard | 5.28 | "Booking Tuesday at 1 PM for you. Done. You're booked Tuesday at 1 PM. Your" | "confirmation code is F D, Q Z 2 J." | word_timestamps | 15.6 |

## Pronunciation variants

See [PRONUNCIATION.md](PRONUNCIATION.md) for the text of each variant and the tokens Rime spoke.

| File | Audio (s) |
|---|---|
| `pron-code-v1.wav` | 4.56 |
| `pron-code-v2.wav` | 4.96 |
| `pron-code-v3.wav` | 7.6 |
| `pron-interrupt_ack-v1.wav` | 4.08 |
| `pron-interrupt_ack-v2.wav` | 3.92 |
| `pron-leadin-v1.wav` | 1.92 |
| `pron-leadin-v2.wav` | 2.08 |
| `pron-time-v1.wav` | 5.28 |
| `pron-time-v2.wav` | 4.0 |
| `pron-unheard_note-v1.wav` | 6.96 |
| `pron-unheard_note-v2.wav` | 4.32 |
