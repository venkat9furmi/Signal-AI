# Commentary QC

Automated quality checking for live football commentary at MMC Sport.

Commentators around Europe send live commentary to bookmakers through a
streaming platform. Until now a person listened to each feed by ear and
messaged the commentator when something sounded wrong. This listens instead —
to every channel at once, continuously — and tells the commentator what to fix
in words they can act on mid-sentence.

It hears what the bookmaker hears: the same public audio stream, about ten
seconds behind the microphone.

## What it detects

Nine faults, each with a wait so a passing blip is never reported:

| Fault | Fires when | Who fixes it |
|---|---|---|
| Silence | below −50 LUFS for 12 s | mic is off or muted |
| Holding clip | the audio repeats itself (≥0.60 correlation, 15–90 s cycle) | switch back to LIVE |
| Distorting | 3+ samples at full scale | lower Mic gain |
| No signal | audio stops arriving | ops — says whether one channel or many dropped |
| Level low | minute loudness below −40, clears above −37 | raise Mic gain |
| Level high | minute loudness above −17, clears below −19 | lower Mic gain |
| Peaks too hot | true peak above −1 dBTP | ease Mic gain down |
| Crowd over you | voice under 6 dB above the crowd | lower the Crowd slider |
| Atmosphere thin | voice over 20 dB above the crowd | raise the Crowd slider |

Thresholds come from a blind listening test checked against AES TD1008 and
EBU R128. The reasoning behind every number is in the
[rulebook](https://claude.ai/artifact/CAZMhdoPe1XWWLjP8J1cHi), and the knobs
themselves are in `src/commentary_qc/config.py`.

## Running it

```bash
docker compose up -d --build     # start (or rebuild and restart)
docker compose logs -f recorder  # watch it work
docker compose ps                # NOTE: hides exited containers; use `docker ps -a`
docker compose down              # stop
```

Then open <http://127.0.0.1:8642>.

Two services from one image: **recorder** (pulls audio, measures, alerts) and
**web** (the commentator page). ffmpeg lives inside the image. Measurements,
claims and logs live in the Docker volume `commentary-qc-data`, never on a host
bind mount — SQLite in WAL mode needs shared memory that a Windows bind mount
cannot provide, and getting that wrong makes readers silently see stale data.

Snapshots, safe to run while recording:

```bash
docker compose run --rm backup   # writes ./backup/live-<timestamp>.db
```

## Configuration

`.slack.env` in the project root, never committed:

```
SLACK_BOT_TOKEN=xoxb-...     # Slack app with chat:write
SLACK_CHANNEL_ID=C...        # the channel alerts go to
QC_PASSCODE=...              # optional; required before exposing the page
```

`.translate.env`, also never committed, for the translation layer &mdash; see
`.translate.env.example`. With no such file the pipeline starts `off`.

Without the Slack values the system runs normally and alerts appear only on the
page. Without `QC_PASSCODE` the page has no login, which is fine on localhost
and not fine anywhere else — claiming a channel starts and stops monitoring.

`channels.json` lists MMC's 52 football channels and can override any threshold
per channel. Unknown setting names are rejected at startup rather than ignored.

## How a commentator uses it

1. Open the page, pick the match (live and upcoming fixtures come from a public
   feed covering 17 competitions) and the channel they are on.
2. Claiming starts monitoring that channel within seconds and opens a Slack
   thread carrying their name and fixture.
3. While on air they see one instruction at a time, a loudness meter and a
   ten-minute trend; faults also arrive in Slack.
4. Releasing closes the thread and shows a match report: time on target, time
   too quiet or loud, worst peak, and every fault with its duration.

**Claims are what scope the system.** MMC's channels cannot be told from other
providers' by name, number or language, so a claimed channel is an MMC channel
by definition — and Slack only ever speaks about claimed channels.

## Translation

The audio is already being pulled, decoded and split into speech and gaps for
the quality rules, so turning it into text costs one more read of a buffer that
exists anyway. Open <http://127.0.0.1:8642/translate>.

Three modes, and the middle one is the point:

| Mode | What runs | Who sees it | What it costs |
|---|---|---|---|
| `off` | nothing &mdash; no tap is installed | &mdash; | nothing |
| `shadow` | the whole pipeline, on real matches | nobody | the provider's rate, or nothing with the offline model |
| `prod` | the same pipeline | commentators | the provider's rate |

Shadow mode exists because "it works" and "it is good enough" are different
questions. What is measured in shadow is exactly what goes live, so promoting
it is a switch rather than a rewrite &mdash; and with the `mock` provider the
segmenting, latency and volume can be measured on all 52 channels for nothing.

Switching the mode or the model from the page takes effect within seconds, on
channels already on air, without restarting the recorder.

**Guards**, because the failure mode here is a quiet bill rather than a crash:

- A hard cap on audio sent per UTC day, and per channel so one stuck feed
  cannot eat the allowance. The cap is seeded from the database at startup, so
  a restart does not hand back a fresh day.
- `PAYMENT_REQUIRED` from the provider stops the pipeline and says so once,
  instead of filling the log with refusals.
- Audio that is mostly not speech is never sent. A holding clip, a crowd bed
  and a dead channel all look alike to a transcriber: billable and empty.
- A bounded queue. If the provider cannot keep up, the **oldest** segment is
  dropped &mdash; the newest audio is the only audio worth captioning &mdash;
  and the drop is stored as a row rather than left as a gap.
- Monitoring always wins. The capture threads only append to a buffer; nothing
  in the QC path ever waits on a provider.

Configuration is `.translate.env` (see `.translate.env.example`). The API key
comes from the environment and nowhere else, so nothing the page can write
changes which account is billed.

```
src/commentary_qc/translate/
  models.py      segments, transcripts, captions, failures - no vendor words
  settings.py    every knob, validated at startup
  ports.py       the two capabilities a provider must offer
  mappers/       vendor JSON <-> our types: pure, no HTTP, unit-tested
  providers/     one adapter per model, chosen from a registry
  segmenter.py   cuts at pauses, reusing the QC engine's own VAD
  budget.py      the guard on the credits
  control.py     the runtime switch: page writes, recorder reads
  pipeline.py    the worker and its bounded queue
  store.py       captions, successes and failures alike
  api.py         the HTTP surface
```

## How it works

```
discovery.py   which channels are broadcasting (probes the CDN, no login)
audio_source.py  ffmpeg → 48 kHz mono PCM, reconnects on its own
metrics.py     per 1.024 s window: loudness (BS.1770-4), true peak,
               Silero VAD, voice/crowd separation, repeat score
engine.py      the rules, hysteresis, escalation, clip masking
supervisor.py  one thread per live channel; claims start and stop monitors
store.py       SQLite, single writer, 7-day retention on measurements
notify.py      Slack: one thread per broadcast, claimed channels only
fixtures.py    match times from a public scoreboard feed
report.py      the post-match summary
web.py         the page and its API
```

## Tests

```bash
python tests/test_dsp.py            # loudness against a reference meter
python tests/test_engine.py         # rules, hysteresis, escalation, clip veto
python tests/test_slack_delivery.py # Slack delivery against a fake Slack
python tests/test_no_commentary.py  # filler detection vs labelled recordings
python tests/test_translate.py      # translation: mappers, budget, modes, pipeline
python tools/replay_alerts.py <db>  # replay a night through today's rules
```

The last two need the recorded databases from the September calibration work,
which are not in the repository.

## Operating notes

- `uptime-guard.ps1` (Windows) starts Docker Desktop and the stack if they are
  missing and reports to Slack when monitoring is down. Docker's restart policy
  handles container crashes; nothing but this handles Docker itself not running.
- Measurements older than 7 days are pruned automatically; alerts are kept.
- The page is deliberately plain about uncertainty: if the recorder stops, it
  says so rather than showing the last reading as if it were current.
