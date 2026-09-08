# Evidence summary (rime)

Started 2026-09-08 04:11:10 UTC, 2773.6 s total.

Speech engine: provider=rime, model_id=coda, speaker=astra, lang=en, endpoint=wss://users-ws.rime.ai/ws3, audio_format=pcm_s16le@24000Hz mono, transport=websocket-json (ws3), engine=rime-ws3

| Scenario | Pass | Stop p50/p95 (ms) | Speech->stop p95 (ms) | TTFA warm p50/p95 (ms) | TTFA cold p50 | Late results fenced | Alignment |
|---|---|---|---|---|---|---|---|
| A_interrupt_mid_speech | 20/20 | 15.6 / 16.5 | 183.7 | 682.9 / 763.0 | 550.4 | 0 | word_timestamps |
| B_interrupt_during_tool | 20/20 | 15.9 / 16.5 | 181.8 | 688.7 / 759.9 | - | 0 | word_timestamps |
| C_status_during_tool | 20/20 | 15.7 / 16.0 | 182.1 | 651.8 / 1052.7 | - | 0 | word_timestamps |
| D_baseline_no_interrupt | 20/20 | - / - | - | 756.7 / 917.9 | - | 0 | n/a |
| E_empty_interrupt_resume | 20/20 | 15.6 / 16.4 | 182.0 | 748.9 / 969.4 | - | 0 | word_timestamps |
| F_interrupt_uncommitted_mutation | 20/20 | 15.9 / 16.1 | 181.5 | 680.1 / 958.9 | - | 0 | nothing, word_timestamps |
| G_interrupt_after_commit_unheard | 20/20 | 15.6 / 16.0 | 182.0 | 756.1 / 979.0 | - | 0 | word_timestamps |

Stop = barge-in detected on the server -> client acknowledged that playback stopped and queued audio was dropped. Speech->stop = from the first mic sample of the user's interruption (includes the 180 ms barge-in guard). TTFA = end of user turn -> first audio sample actually played by the client. Late results fenced = tool results that arrived after an interruption and were never spoken as current.

## A_interrupt_mid_speech

User talks over the slot offer and changes the day. Audio must stop promptly; the ledger must know which words were heard; the new answer must be about Thursday.

Claim: Queued Rime audio stops promptly and the next response is grounded in what was heard.

- script_completed: 20/20
- interrupted: 20/20
- audio_stopped_under_300ms: 20/20
- heard_is_prefix_of_intended: 20/20
- some_text_unheard: 20/20
- word_level_alignment: 20/20
- new_request_answered: 20/20
- stale_offer_not_repeated: 20/20
- no_stale_tool_results_spoken: 20/20
- heard-audio clips: 20 (evidence/clips/)

## B_interrupt_during_tool

User changes the request while a 3 s lookup is in flight. The stale lookup must never be spoken as current.

Claim: Delayed tool results cannot re-enter the conversation after an interruption.

- script_completed: 20/20
- interrupted: 20/20
- audio_stopped_under_300ms: 20/20
- old_lookup_orphaned_not_killed: 20/20
- old_lookup_discarded_or_fenced: 20/20
- stale_tuesday_result_never_spoken: 20/20
- new_request_answered: 20/20
- exactly_one_tuesday_lookup: 20/20
- heard-audio clips: 20 (evidence/clips/)

## C_status_during_tool

User asks 'are you still there?' during a 3 s lookup. The lookup must continue and its result must be reconciled, not restarted or lost.

Claim: The voice session stays responsive during tool work without losing context.

- script_completed: 20/20
- interrupted: 20/20
- lookup_not_restarted: 20/20
- result_reconciled_into_new_epoch: 20/20
- status_acknowledged: 20/20
- result_spoken_once_after_reconcile: 20/20
- heard-audio clips: 20 (evidence/clips/)

## D_baseline_no_interrupt

Normal end-to-end booking, two turns. Measures time-to-first-audio and confirms the code is spoken letter by letter.

Claim: Normal path works end to end with Rime as the only speaker.

- script_completed: 20/20
- booking_committed: 20/20
- confirmation_code_spoken_letter_by_letter: 20/20
- two_turns_completed: 20/20
- ttfa_measured: 20/20
- not_interrupted: 20/20

## E_empty_interrupt_resume

A cough interrupts the agent mid-sentence with no words. The agent resumes from the first unheard word.

Claim: Heard-state is tracked at word level and used for recovery.

- script_completed: 20/20
- interrupted: 20/20
- resumed: 20/20
- resume_covers_all_unheard_text: 20/20
- resume_restarts_at_sentence_boundary: 20/20
- fully_heard_sentences_not_repeated: 20/20
- heard-audio clips: 20 (evidence/clips/)

## F_interrupt_uncommitted_mutation

User changes their mind while a 2.5 s booking write is in flight. The write must be cancelled and nothing committed.

Claim: Application state stays consistent with what the user asked for and heard.

- script_completed: 20/20
- interrupted: 20/20
- uncommitted_booking_cancelled: 20/20
- nothing_committed: 20/20
- no_false_confirmation_spoken: 20/20
- new_request_answered: 20/20
- heard-audio clips: 19 (evidence/clips/)

## G_interrupt_after_commit_unheard

Booking commits, user interrupts before hearing the code, then asks to move it. The agent surfaces the unheard booking and reschedules instead of double-booking.

Claim: Committed-but-unheard actions are reconciled, never silently lost or duplicated.

- script_completed: 20/20
- interrupted: 20/20
- unheard_confirmation_detected: 20/20
- unheard_booking_surfaced_first: 20/20
- moved_not_duplicated: 20/20
- same_code_kept: 20/20
- heard-audio clips: 20 (evidence/clips/)
