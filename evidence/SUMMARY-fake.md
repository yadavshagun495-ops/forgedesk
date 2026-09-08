# Evidence summary (fake)

Started 2026-09-08 05:02:44 UTC, 460.3 s total.

> **NOT EVIDENCE.** This run used the offline FakeTTS tone generator to exercise the logic. Only `--tts rime` runs count.

Speech engine: provider=fake, engine=fake, model_id=fake-tone, speaker=n/a, lang=n/a, endpoint=in-process, audio_format=pcm_s16le@24000Hz mono, transport=in-process

| Scenario | Pass | Stop p50/p95 (ms) | Speech->stop p95 (ms) | TTFA warm p50/p95 (ms) | TTFA cold p50 | Late results fenced | Alignment |
|---|---|---|---|---|---|---|---|
| A_interrupt_mid_speech | 5/5 | 15.5 / 15.5 | 180.0 | 166.9 / 167.4 | 167.2 | 0 | word_timestamps |
| B_interrupt_during_tool | 5/5 | 15.4 / 15.6 | 178.7 | 166.4 / 166.9 | - | 0 | word_timestamps |
| C_status_during_tool | 5/5 | 15.5 / 15.8 | 179.8 | 166.8 / 167.5 | - | 0 | word_timestamps |
| D_baseline_no_interrupt | 5/5 | - / - | - | 168.8 / 171.5 | - | 0 | n/a |
| E_empty_interrupt_resume | 5/5 | 15.3 / 15.5 | 180.4 | 166.6 / 167.2 | - | 0 | word_timestamps |
| F_interrupt_uncommitted_mutation | 5/5 | 15.6 / 15.7 | 178.6 | 167.3 / 171.1 | - | 0 | word_timestamps |
| G_interrupt_after_commit_unheard | 5/5 | 15.4 / 15.4 | 179.6 | 171.0 / 184.8 | - | 0 | word_timestamps |

Stop = barge-in detected on the server -> client acknowledged that playback stopped and queued audio was dropped. Speech->stop = from the first mic sample of the user's interruption (includes the 180 ms barge-in guard). TTFA = end of user turn -> first audio sample actually played by the client. Late results fenced = tool results that arrived after an interruption and were never spoken as current.

## A_interrupt_mid_speech

User talks over the slot offer and changes the day. Audio must stop promptly; the ledger must know which words were heard; the new answer must be about Thursday.

Claim: Queued Rime audio stops promptly and the next response is grounded in what was heard.

- script_completed: 5/5
- interrupted: 5/5
- audio_stopped_under_300ms: 5/5
- heard_is_prefix_of_intended: 5/5
- some_text_unheard: 5/5
- word_level_alignment: 5/5
- new_request_answered: 5/5
- stale_offer_not_repeated: 5/5
- no_stale_tool_results_spoken: 5/5

## B_interrupt_during_tool

User changes the request while a 3 s lookup is in flight. The stale lookup must never be spoken as current.

Claim: Delayed tool results cannot re-enter the conversation after an interruption.

- script_completed: 5/5
- interrupted: 5/5
- audio_stopped_under_300ms: 5/5
- old_lookup_orphaned_not_killed: 5/5
- old_lookup_discarded_or_fenced: 5/5
- stale_tuesday_result_never_spoken: 5/5
- new_request_answered: 5/5
- exactly_one_tuesday_lookup: 5/5

## C_status_during_tool

User asks 'are you still there?' during a 3 s lookup. The lookup must continue and its result must be reconciled, not restarted or lost.

Claim: The voice session stays responsive during tool work without losing context.

- script_completed: 5/5
- interrupted: 5/5
- lookup_not_restarted: 5/5
- result_reconciled_into_new_epoch: 5/5
- status_acknowledged: 5/5
- result_spoken_once_after_reconcile: 5/5

## D_baseline_no_interrupt

Normal end-to-end booking, two turns. Measures time-to-first-audio and confirms the code is spoken letter by letter.

Claim: Normal path works end to end with Rime as the only speaker.

- script_completed: 5/5
- booking_committed: 5/5
- confirmation_code_spoken_letter_by_letter: 5/5
- two_turns_completed: 5/5
- ttfa_measured: 5/5
- not_interrupted: 5/5

## E_empty_interrupt_resume

A cough interrupts the agent mid-sentence with no words. The agent resumes from the first unheard word.

Claim: Heard-state is tracked at word level and used for recovery.

- script_completed: 5/5
- interrupted: 5/5
- resumed: 5/5
- resume_covers_all_unheard_text: 5/5
- resume_restarts_at_sentence_boundary: 5/5
- fully_heard_sentences_not_repeated: 5/5

## F_interrupt_uncommitted_mutation

User changes their mind while a 2.5 s booking write is in flight. The write must be cancelled and nothing committed.

Claim: Application state stays consistent with what the user asked for and heard.

- script_completed: 5/5
- interrupted: 5/5
- uncommitted_booking_cancelled: 5/5
- nothing_committed: 5/5
- no_false_confirmation_spoken: 5/5
- new_request_answered: 5/5

## G_interrupt_after_commit_unheard

Booking commits, user interrupts before hearing the code, then asks to move it. The agent surfaces the unheard booking and reschedules instead of double-booking.

Claim: Committed-but-unheard actions are reconciled, never silently lost or duplicated.

- script_completed: 5/5
- interrupted: 5/5
- unheard_confirmation_detected: 5/5
- unheard_booking_surfaced_first: 5/5
- moved_not_duplicated: 5/5
- same_code_kept: 5/5
