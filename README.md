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
`.translate.env.example`. With no such file the /translate page runs on the
free offline stand-in and the continuous pipeline stays `off`.

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

Open <http://127.0.0.1:8642/translate>. Pick a channel that is on air, press
Translate, and listen to what the model produced next to what it heard.

### Modes are execution wirings, not a volume knob

A mode decides which provider is built, what it may spend, where the result is
kept and who is allowed to see it. The endpoints are namespaced by it, so
calling one *is* choosing the wiring rather than passing a flag something
downstream might ignore:

```
POST /api/translate/shadow/run       -> runs
POST /api/translate/standard/run     -> 501, with what building it needs
```

| Mode | Built | What it is for | Who sees the output |
|---|---|---|---|
| `shadow` | yes | Trying a model on real commentary and listening to it | only whoever pressed the button |
| `standard` | no | The same work, published | commentators and the dashboard |

`standard` is declared and deliberately empty &mdash; `modes/standard.py` says
exactly what building it means. It refuses rather than falling back to shadow,
because asking for production and silently getting the test wiring is the one
failure nobody would notice from outside.

### What the audio translation actually is

Three chained calls, not one:

| Step | Endpoint | Result |
|---|---|---|
| Hear it | `POST /transcribe` | the commentary as timed text, in the language spoken |
| Translate it | `POST /translate` | the lines in the target language, in order |
| Speak it | `POST /tts-stream` | the translation as WAV bytes, in one response |

Camb does have a single end-to-end dubbing call, `POST /dub`, and we cannot
use it: it fetches the media from a URL, and our clip is a few seconds held in
memory on a host with no public address. The page says so rather than leaving
it looking like an oversight.

The page asks the *provider* what its steps are, not this file. They differ
per vendor, and a page with either hardcoded would start lying the day the
adapter changed.

### Credit

Camb publishes **no** balance, quota or usage endpoint. The only figure it
gives is an `X-Credits-Required` response header on the call that incurred it.
So the page shows two numbers and says which is which: our own count of
seconds sent, which is what the cap is enforced on, and whatever the provider
last charged. A running balance would have to be invented, and an invented
balance is worse than none.

**Guards**, because the failure mode here is a quiet bill rather than a crash:

- A hard cap on audio sent per UTC day, and per channel. Seeded from the
  database at startup, so a restart does not hand back a fresh allowance.
- Silence is never sent. A dead channel costs a credit to be told there were
  no words in it.
- `PAYMENT_REQUIRED` latches the budget shut and says so once, rather than
  filling the log with refusals.
- A model with no key cannot be selected at all &mdash; the registry refuses
  it before any audio is captured, instead of failing after a 15 s grab.
- Every failure is a stored run with a status and the audio that was sent, so
  "the model refused" and "nothing came back" look different.

Configuration is `.translate.env` (see `.translate.env.example`). The API key
comes from the environment and nowhere else; the API neither accepts nor
returns it, so nothing a request can write changes which account is billed.

### The continuous pipeline

Separate from the page and off by default: `watch.py --translate shadow`
transcribes claimed channels for the length of a match instead of one clip on
demand. Same adapters, same budget shape, its own mode setting.

```
src/commentary_qc/translate/
  models.py      segments, transcripts, captions, runs, failures - no vendor words
  settings.py    every knob, validated at startup
  ports.py       the capabilities a provider must offer, including speech
  modes/         execution wirings: shadow (built), standard (declared)
  mappers/       vendor JSON <-> our types: pure, no HTTP, unit-tested
  providers/     one adapter per model, chosen from a registry
  grab.py        a few seconds of a live channel, on demand
  audio.py       48 kHz float32 -> the 16 kHz mono WAV a provider accepts
  runs.py        shadow runs and their audio, so it can be played back
  budget.py      the guard on the credits
  segmenter.py   cuts at pauses, reusing the QC engine's own VAD
  control.py     the continuous pipeline's runtime switch
  pipeline.py    the continuous worker and its bounded queue
  store.py       captions, successes and failures alike
  api.py         the HTTP surface, namespaced by mode
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
python tests/test_translate.py      # translation: mappers, modes, budget, runs, pipeline
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
