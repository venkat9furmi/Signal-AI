# Commentary QC implementation plan

Prepared 2026-09-10. This is a design and task list, not a claim that the work is implemented.

## Intended outcome

An MMC football commentator claims a channel, enters their name and fixture, and starts a monitoring session. The service measures the outgoing feed, shows fresh status, and posts sustained actionable problems and recoveries in one Slack thread for that session. Releasing the channel ends the session. Claims scope production monitoring; broad platform scans only help people find a channel.

Keep the existing Python, ffmpeg, Silero, FastAPI and SQLite foundation for a single-instance pilot. Do not require a new model or cloud migration to complete the MVP. Outgoing-stream monitoring is delayed feedback, not an instantaneous microphone meter.

## Evidence already available

- Round 1 has notes on all 24 clips. Its exact AAC and MP3 files were remeasured: saved median levels differed by less than 0.9 LUFS; MP3 differences were at most 0.01 LUFS.
- Round 2 has level responses for clips 01–09; remaining levels and all voice responses are blank.
- Round 3 has responses on all 24 clips. Three say `quite`; treat these as proposed `quiet` interpretations in the scoring report without overwriting the originals.
- Six DSP tests and seven historical filler periods passed in the review. The latter do not validate the full alert timing or generalization to other matches.
- The user has created a Slack channel. App membership, credentials, permissions and delivery remain unverified.

## 1. Score and validate the completed assessment

- [ ] Inspect round 3's builder, answer key and clip provenance: establish whether levels were deliberately varied and which original excerpts were reused.
- [ ] Parse responses while preserving original wording and notes; report interpretations and missing/invalid entries explicitly.
- [ ] Remeasure the exact round 3 files. Separate loudness judgements from distortion or intelligibility complaints.
- [ ] Report label distributions and overlap; propose thresholds only where the data supports them. Do not use extreme-value midpoints as unquestioned calibration.
- [ ] Specify what is being calibrated: programme loudness or estimated speech loudness, integration period, playback conditions and intended audience.
- [ ] If evidence remains insufficient, create only the missing controlled comparisons: the same excerpt at different gains, with no added clipping. Keep test order blind and a stable accepted reference available.
- [ ] Validate candidates on different original recordings/commentators. Variants of one excerpt must stay together when separating calibration and validation data.
- [ ] Save a versioned calibration report and configuration containing evidence, uncertainties, level limits and attack/release times. Have an MMC producer review the candidate operating range before wider rollout.

Done when candidate settings have a traceable basis and independent validation, or unsupported rules are explicitly excluded from notifications. Round 3 completion does not itself prove calibration.

## 2. Make measurements and state trustworthy

- [ ] Reset all pending and active rule timers across transport gaps.
- [ ] Persist a transport fault immediately; do not wait for the next audio window to refresh channel state.
- [ ] Distinguish not monitored, connecting, measuring, measurement unavailable and stopped. Track content state separately from transport state.
- [ ] Use last-audio/last-metric timestamps and an explicit freshness timeout. Stale data never produces a healthy verdict.
- [ ] On browser/API failure, visibly mark data unavailable and clear stale readings.
- [ ] Replace unsupported diagnoses such as “Nothing is reaching listeners” with “Monitor cannot receive audio.” Healthy copy should say no configured fault was detected.
- [ ] Apply the effective per-channel settings consistently to the engine and displayed targets.
- [ ] Fix misleading logging, swallowed configuration typos and source-file detection. Validate nested configuration and rule names before starting workers.

Done when a stalled source, killed worker, stopped service and failed browser request all produce honest status; reconnecting starts fresh timing and cannot reuse pre-gap evidence.

## 3. Make claims control monitoring

- [ ] Give each claim a session ID, owner, channel, commentator, fixture and explicit lifecycle.
- [ ] Have the supervisor reconcile active claims, starting one monitor per claimed channel and stopping it after release. Keep any broad recording mode explicit and separate.
- [ ] Allow claiming a configured channel before audio arrives; show waiting status without implying it is broadcasting.
- [ ] Return a connecting state until real measurements arrive; do not equate claim success with capture success.
- [ ] Prevent silent claim takeover and unauthorized release. For the pilot, use authenticated access and server-side ownership checks.
- [ ] Make claim replacement/release atomic and acknowledgement reflect a committed write. Define recovery of open sessions after a service restart.
- [ ] Add an explicit commentary-expected/break state. Claiming alone does not prove that filler is inappropriate at that moment.
- [ ] Escape user and log text in the UI; scope recent alerts to the current session.

Done when claim → start → measure → release → stop works, duplicate claims are handled clearly, and refresh/restart preserve the correct owner and session.

## 4. Connect Slack reliably

- [ ] Configure a Slack bot with posting permission, invite it to the user's channel, and configure token/channel ID locally outside tracked files. Do not request secrets in chat.
- [ ] Post one root message per monitoring session containing channel, fixture and commentator. Persist its thread ID for recovery after restart.
- [ ] Post confirmed faults and recoveries as replies, with plain wording and relevant duration. Do not send quality alerts while measurements are unavailable.
- [ ] Store pending notifications durably; retry transient/rate-limited failures with backoff, track delivery status and minimize duplicates after restart.
- [ ] Keep capture independent of delivery delays. Bound message frequency per session and summarize repeated flapping.
- [ ] End the session thread cleanly after release. Do not discard queued messages on shutdown or remove thread mappings before final delivery.
- [ ] Obtain explicit authorization for the concrete test message before posting externally; creating the channel is not authorization to send test messages.

Done when an authorized synthetic fault and recovery arrive in the right thread, delivery failure cannot interrupt measurement, and restart does not create uncontrolled duplicate threads/messages.

## 5. Restrict the first release to defensible alerts

- [ ] Enable calibrated sustained loud/quiet checks, silence and monitoring-unavailable notifications for the pilot.
- [ ] Distinguish peak-limit exceedance from evidence of hard clipping; do not label every peak exceedance as audible distortion.
- [ ] Record crowd-balance, missing-atmosphere and filler estimates as experimental initially. Enable notifications only after separate validation and appropriate expected-content context.
- [ ] Define precedence: unavailable measurements override audio conclusions; silence should not simultaneously instruct someone to increase gain; one headline should identify the most actionable current condition.
- [ ] Tune fault duration together with level limits using complete passages, including pauses, shouts, halftime and reconnects.

## 6. Rehearse, pilot and operate

- [ ] Run meaningful integration checks with local deterministic audio and simulated interruptions: claim lifecycle, sustained faults, recovery, stale status, session ownership, escaping, database contention and notification retry.
- [ ] Replay held-out real recordings and log missed faults, unnecessary notifications per match and detection delay. Agree acceptance criteria with MMC before evaluating the pilot.
- [ ] Run a limited real-commentator pilot with human review; preserve clips surrounding alerts and representative non-alert periods.
- [ ] Update dependency/install instructions, pin the tested model artifact and record its checksum, document configuration and provide one repeatable startup procedure.
- [ ] Deploy the pilot on an always-on host with supervised restart, HTTPS/access control for remote use, service health checks, disk limits, log rotation, backups and defined audio retention.
- [ ] Verify restore/restart behavior. Ensure missing model or degraded VAD is visible and does not silently retain unvalidated balance alerts.
- [ ] After the MVP works, revisit vendor access for supported endpoints and event metadata, as previously agreed. Separate microphone/mixer signals are a later integration opportunity.

## Work order and user input

Start with round 3 analysis. Reliability and claim wiring can proceed without waiting for final calibration. Add Slack after session identity and state are dependable; rehearse before a real-match pilot.

User input is needed for local Slack connection setup, explicit permission to send test messages, and an experienced MMC listener/producer's review of candidate targets and pilot acceptance criteria. Existing assessment answers remain intact. No code or thresholds are changed by this plan.
