# SignalAI

**Real-time audio quality monitoring and AI dubbing for live sports commentary.**

Football commentary is streamed live to bookmakers, where a commentator who is
too quiet, distorting, or buried under the crowd costs the customer the match
they paid to follow. Until now, interns listened to each channel by ear and
messaged commentators on WhatsApp when something sounded wrong.

SignalAI does that job on **52 channels at once**, a measurement every 1.024
seconds, against broadcast standards rather than guesswork.

### What it measures, and why each one is harder than it looks

- **Loudness** — ITU-R BS.1770-4 K-weighting, with the filter state carried
  across windows. The obvious implementation resets the filters every second,
  which injects a transient at each boundary and corrupts exactly the
  low-level readings dead-air detection depends on.
- **True peak** — 4× oversampled per BS.1770-4 Annex 2. This feed peaks above
  the ceiling while no individual sample ever does; a sample-peak meter
  reports it as clean and a downstream encoder clips it.
- **Voice against crowd** — no source separation model. A neural VAD (Silero)
  finds the gaps between words, the crowd is measured *there*, and the voice
  is what remains once the crowd power is subtracted from the speech. Every
  level stays in LUFS, so voice, crowd and programme loudness are directly
  comparable.
- **Is anyone actually there** — a pre-match holding clip is technically
  perfect audio and completely useless. It is caught two ways: a recording
  never pauses to breathe, and a recording repeats. The second compares three
  minutes of loudness against itself at a 15–90 s lag; filler scores
  0.77–1.00, live commentary 0.2–0.5, with no overlap.

Commentators get one instruction at a time, naming a control they actually
have. Producers get a post-match report. **Every threshold traces to a
measurement** — a blind listening test, a backtest over labelled nights, or a
distribution over real windows — and each one is written down with the
evidence beside it in `config.py`.

Noise control is treated as a first-class problem, because an alerting system
nobody trusts is worse than none: thermostats, clip masking, a repeated
identical fault read as a recording, and hold times derived from measured
outage durations rather than picked.

### SignalAI Dubbing

The second half, and experimental by design. One commentator's audio is pulled
once and fanned out to several live speech-to-speech sessions at the same
time, so one English commentator can serve Greek, German and French markets
from a single feed. Ports and adapters throughout — the model is one file and
one registry line, and nothing outside `adapters/` names a vendor.

Nothing it produces is published. It exists so somebody can listen and decide
whether a model is good enough to put in front of anyone.

### The shape of it

Two things run on the same football commentary, and **the audio is pulled once
and both read it**. Python, FastAPI, FFmpeg, SQLite, Docker — two containers,
one command, no cloud dependency.

```
docker compose up -d --build
```

Then open <http://127.0.0.1:8642> and pick one.

**New here?** Read [ARCHITECTURE.md](ARCHITECTURE.md) — it explains how the
whole thing is put together, walks a piece of audio through every flow, names
the technical debt honestly, and ends with the to-do list. This file is the
shorter operator's guide.

---

## QC Commentary

Interns used to sit and listen, then message a commentator on WhatsApp when
something sounded off. This does that job, on all 52 channels at once.

### What it tells you

| It says | It means | What fixes it |
|---|---|---|
| **No audio** | Nothing is coming through at all | check you are live, check the encoder |
| **Too quiet** | Below the level bookmakers expect | raise Mic gain |
| **Too loud** | Above it | lower Mic gain |
| **Distorting** | The signal is squared off — genuine clipping | lower Mic gain now |
| **Peak too hot** | Close to the limit but not clipping yet | ease off a little |
| **Buried in the crowd** | Your voice is under the atmosphere | lower the Crowd slider |
| **No atmosphere** | No crowd bed at all | raise the Crowd slider |
| **Holding clip playing** | A recording is on air, not a person | switch back to LIVE |
| **Stream stopped** | We lost the feed, for 45 s or more | it says whether it is us or them |

The wording matters: each one names a control the commentator actually has in
the MOD publishing tools, rather than describing the physics.

### How a commentator uses it

1. Open the page and find your channel.
2. Claim it, and pick your match from the list.
3. It starts measuring within a couple of seconds.
4. You get one instruction at a time, a level meter, and a ten-minute trend.
5. When you stop, you get a match report: how long you sat inside the target
   level, your worst peak, and every alert with how long it lasted.

Claiming is also what tells us which match is on which channel — nothing else
knows that.

### Why the alerts are not noisy

Five layers, because an alerting system nobody trusts is worse than none:

- **Thermostats.** "Too quiet" fires below −40 and only clears above −37, so a
  channel sitting exactly on the line produces one alert instead of a stream
  of them. Ch.102 went from 18 alerts in a night to 5.
- **Nothing is blamed on a recording.** Before kick-off and at half-time a
  holding clip plays, and nobody is on air to fix it. While one is playing,
  the level, peak and balance rules are frozen. Dead air and stream loss still
  fire, because those matter whatever is playing.
- **A repeated identical fault is read as a recording.** This caught us out on
  7 November: Ch.31 reported "17 samples at full scale, +2.5 dBTP" seven times
  over six minutes, and it was the holding clip's distortion being blamed on a
  commentator who was not there. Real speech never peaks at exactly the same
  sample count twice, so three identical messages now pause those rules and
  say why.
- **A stream that stops has to stay stopped.** `stream_down` used to fire on
  the first missed segment. Measured over 2,010 of them to 12 November: the
  median outage lasted **9 seconds**, 53% were over inside fifteen, and 556
  were our own container failing to resolve the vendor's hostname — not a
  broadcast fault at all. It now waits 45 seconds, which removes 62% of them
  and keeps the quarter that lasted minutes. A gap that short no longer
  clears your other faults either, so one fault stops reading as two.
- **"Getting worse" means the same thing on every scale.** Dead air no longer
  escalates: a channel drifting from −50 to −56 LUFS is already dead, and it
  was tripping the 6 dB step on a quarter of its alerts. The balance rules get
  their own 12 dB step, because voice-over-crowd spans 1.4 to 33.4 dB across
  real commentary where loudness spans a few — one number for both meant
  `crowd_dominating` escalated on 72% of its fires.

---

## MMC AI Dubbing

English commentary in, Greek or German voice out, over one socket, while the
match is still going.

This is **in testing**. Nothing it produces is published, and no commentator
sees it. It exists so you can listen and decide whether a model is good
enough to put in front of anyone.

### Five steps on one page

The page at `/translate` is a sequence, and the numbers mean it:

1. **The source.** Channels that are on air, live ones first, each with what
   the quality engine currently hears on it &mdash; commentary, the vendor's
   holding clip, silence, or a fault, with its level. A bad source makes a
   good model look bad, and this is how you avoid judging a model on audio no
   model could do much with.
2. **The model.** One that cannot be picked says why in a line.
3. **Out loud in.** One or more output languages. Languages the chosen model
   does not carry are struck through rather than quietly missing.
4. **Listen.** It translates while the match is going. Each language gets the
   commentary and its own translated voice as playable audio, and the
   transcript follows whichever you are playing.
5. **Broadcast it back.** Present, locked, and it lists what building it needs.

### One commentator, several languages

MMC pays one person to call a match in English; the markets that want it are
Greek, German, Italian and French. So a run takes several output languages at
once: **one ffmpeg pull, one model session per language.**

That the pull is shared is not just thrift. Three independent pulls of a live
HLS stream start at whatever segment the CDN is serving at that instant, so
the Greek output could be eight seconds ahead of the German one and nobody
comparing them would know why. The pull also waits for every leg to open its
session before it reads the first block, so all of them hear the same opening
second.

The page shows both numbers: seconds pulled once, and seconds charged. Three
languages for thirty seconds is thirty seconds of audio and ninety seconds of
billing.

Each leg is independent. A Greek session refused for an unsupported language
does not cost you the German translation that was working. A provider that
documents a concurrency of one &mdash; Camb's free plan does &mdash; refuses a
multi-language group up front, before anything is spent, rather than letting
the far end refuse the second session once the meter is running.

### Two things it works out from the words

Both fall out of transcription at no extra cost, and both answer a question
the quality engine cannot, because it measures levels rather than words.

- **Which language is actually on the channel.** `channels.json` carries a
  language per channel, but that column is hand-maintained on the vendor's
  side: it says what the channel is *for*. A commentator who signs into the
  wrong channel produces perfectly good audio in the wrong language, every
  level rule passes, and nothing notices. Greek is settled by its alphabet;
  the rest are scored on function words, because "che" against "que" and
  "non" against "no" are what actually separate them. A channel with no
  language recorded &mdash; the racing ones &mdash; can never be a mismatch.
- **Which match is being played.** Nothing else in the system can work this
  out: the commentator picks their match when they claim a channel, and that
  claim is also what turns Slack delivery on, so forgetting to claim means a
  channel measured in silence. The commentator says both team names out loud
  in the first minute. A transcript naming one team has found a *club*; one
  naming both has found a *fixture*, and only the second is worth acting on,
  so they are reported differently.

Both report a confidence and a runner-up rather than a bare answer.

### Two paths, and the difference is the point

|  | What it does | What it answers |
|---|---|---|
| **Live** | Streams a channel to a model and plays the translated voice back as it arrives | can this keep up with a match |
| **Recorded clip** | Records a few seconds, then hears, translates and speaks it in three calls | does this model understand football commentary |

### The models

Two adapters, so you can compare rather than hope:

| | Live | Clips | Greek | German | Cost |
|---|---|---|---|---|---|
| **Gemini** | yes | no | **yes** | yes | free on Google's free tier |
| **Camb AI** | yes, 14 languages | yes | live: **no**, clips: yes | yes | 6 minutes free, once |
| **Offline stand-in** | yes | yes | yes | yes | free, and deliberately sounds nothing like speech |

Gemini is the one that does what MMC needs: `gemini-3.5-live-translate-preview`,
79 languages including Greek, audio straight to translated audio. Camb's live
path carries 14 languages and neither Greek nor Italian.

Adding a third is one file in `adapters/` and one line in the registry.
Nothing in the app or the page learns its name.

### What it costs you to find out

Gemini's free tier charges nothing for this model. The same page says
free-tier input and output are "used to improve our products", so MMC's
commentary becomes training data until somebody agrees otherwise. Worth
knowing before a real match goes through it.

There are caps either way: a daily limit on audio sent, a per-channel limit,
a hard ceiling on one session, and a limit on how many languages one run may
produce, all seeded from the database so a restart does not hand back a fresh
allowance. A group is checked against the budget for *every* leg before the
first one opens, because half a comparison is worth less than none.

### What is not built

**Broadcasting the translated voice back into a channel.** We only ever pull
audio; nothing here can put audio anywhere. The vendor's publishing path is
publish-only and would need a point of our own, one per language, plus a
jitter buffer to turn bursts of speech into an unbroken stream, plus a
decision about what happens when another language takes longer to say than
the English did. `app/modes/standard.py` writes the whole list down.

---

## Running it

Docker, two containers from one image:

```
docker compose up -d --build     # start, or rebuild after a change
docker compose logs -f recorder  # watch it work
docker compose down              # stop
```

The page is on 8642. `docker compose ps` hides containers that exited — use
`docker ps -a` when something is missing.

Snapshots of the database:

```
docker compose run --rm backup   # writes ./backup/live-<stamp>.db
```

### Without Docker

```
pip install -r requirements.txt
set FFMPEG_BIN=C:\path\to\ffmpeg.exe      # it will refuse to start without this
python -m commentary_qc.watch --claims data/claims.db
python -m commentary_qc.web
```

---

## Changing a threshold

Edit **`thresholds.json`** in the project root, then:

```
docker compose restart
```

That is the whole loop. No rebuild.

Anything you do not list keeps the default from
`src/commentary_qc/config.py`, which is also where the comment explaining
**where each number came from** lives. Read that before changing one — every
default has a measurement behind it. The file's own `_example` block lists
every name at its current default; copy the ones you want into `"thresholds"`.

```json
{
  "thresholds": {
    "too_quiet_lufs": -38.0
  }
}
```

**A typo stops the recorder rather than being ignored.** A silently-dropped
threshold is an outage nobody can see, so the loader refuses unknown names and
contradictions (a clear level on the wrong side of its fire level) at startup.
Because the container restarts on failure it will then crash-loop, so if the
recorder will not come up after an edit:

```
docker compose logs --tail 20 recorder     # look for 'unknown Thresholds keys'
```

Fix the file and `docker compose restart`. Both services read it, because the
page draws the same lines on its meters that the recorder judges against.


## Configuration

Three files, none of them committed:

| File | Holds |
|---|---|
| `channels.json` | MMC's 52 football channels. Read-only in the container. |
| `thresholds.json` | Threshold overrides. Read-only in the container; edit on the host and restart. |
| `.slack.env` | Slack credentials, and `QC_PASSCODE` if you want sign-in. |
| `.translate.env` | Model API keys. See `.translate.env.example`. |

API keys come from the environment and nowhere else. The page cannot write
one, and the API neither accepts nor returns one, so no request can change
which account gets billed.

**Set `QC_PASSCODE` before putting the page on any network.** Claiming a
channel starts and stops monitoring, which makes the page a control surface
and not just a view. While it only listens on localhost, leaving it unset is
fine.

---

## How it works

```
discovery.py      which channels are broadcasting (probes the CDN, no login)
audio_source.py   ffmpeg -> 48 kHz mono PCM, reconnects on its own
metrics.py        per 1.024 s: loudness (BS.1770-4), true peak, Silero VAD,
                  voice/crowd separation, and a repeat score
engine.py         the rules, with attack and release so nothing flaps
supervisor.py     one thread per channel, restarted when it dies
store.py          SQLite, one writer
notify.py         Slack, one thread per broadcast
web.py            the page and its API
```

Liveness needs no login: probing the stream host agrees with the vendor's own
panel on which channels are up, and audio analysis has never needed
credentials of any kind.

### The pages

`static/ui.css` holds the tokens and the primitives every page shares -- a 4px
spacing scale, a type scale, three border weights, and semantic colours kept
separate from the brand accent. It is served from its own route rather than a
mounted directory, so nothing else in `static/` is published by accident.

Three rules it follows, because the first version of the dubbing page broke
all three and looked generated as a result: separation is a **border**, not a
third background grey; everything sits on the **4px grid**; and **chrome
recedes** -- headings are body size and differ by weight, so the biggest,
brightest thing on screen is a channel name or a loudness figure. The accent
marks what is selected and what is live, and nothing else.

### The translation side

Laid out in layers that depend inwards only, which is checked by a test:

```
translate/
  core/      the domain. No I/O, no vendor, no framework.
  infra/     ffmpeg, SQLite, WAV bytes.
  adapters/  one folder per model, each behind a core port.
  app/       the use cases: modes, sessions, budget, the bench.
  web/       routing only.
```

`tests/test_architecture.py` reads every import and fails if one points the
wrong way. It is how "swapping the model is one file" stays true instead of
being something that used to be true.

`tests/test_wiring.py` covers the layer that one cannot: the composition
root, where those layers are joined. It exists because of a bug on
9 November that every other test missed — a route handler named `audio`
silently replaced the `audio` dependency the handlers read, so all three
paths that need audio failed with "'function' object has no attribute
'resolve'". Each layer was tested and the joint between them was not.

---

## Tests

```
python tests/test_architecture.py   # the dependency rule, enforced
python tests/test_wiring.py         # the composition root actually wired up
python tests/test_security.py       # keys, channel names, header filenames
python tests/test_thresholds_file.py # thresholds.json loads, and its example is true
python tests/test_engine.py         # the rules and their hysteresis
python tests/test_dsp.py            # loudness, true peak, VAD
python tests/test_translate.py      # mappers, modes, budget, the clip path
python tests/test_live_translate.py # live sessions, against a fake server
python tests/test_gemini.py         # the Gemini adapter, against a fake server
python tests/test_multilanguage.py  # the shared pull, and groups of languages
python tests/test_identify.py       # which language, and which match
python tests/test_bench.py          # comparing models on one clip
python tests/test_slack_delivery.py # delivery, against a fake Slack
python tests/test_no_commentary.py  # the filler rule, against labelled nights
```

Every vendor is tested against a fake of itself on localhost — the handshake,
the retries, the refusals, running out of credit. None of it needs a key and
none of it spends anything.

---

## Security

Keys come from the environment only. The page cannot write one, no API
response carries one, and `.translate.env` and `.slack.env` are ignored by
both git and Docker. `tests/test_security.py` checks all of that, plus two
things a review on 9 November turned up:

- **The Gemini key travels in a URL**, because that socket documents no header
  form — and the WebSocket library echoes the URI inside its own error
  messages. A `GEMINI_LIVE_URL` mistyped as `http://` produced an error
  containing the whole key, which would have been stored in a run's error
  column and shown on the page. Every exception is redacted now.
- **A channel name reaches a URL builder and then ffmpeg.** `../../x`,
  `x

y` and a 300-character name were all accepted and started a run.
  A channel is now validated where it becomes a URL, so every caller is
  covered rather than just the endpoint that remembered to check.

Two things are still yours to do:

- **Rotate the Slack bot token, the `xapp-` token and the MOD password.** All
  three were pasted into a chat. No code change substitutes for that.
- **Set `QC_PASSCODE` before the page leaves localhost.** Claiming a channel
  starts and stops monitoring, so an open page is a control surface.

## Operating notes

- **Never put the SQLite databases on a Windows bind mount.** WAL mode needs
  shared memory a Windows mount cannot provide: the recorder writes perfectly
  while readers see a stale file, and the page reports an outage that is not
  happening. They live on a named volume. Only `channels.json` is bind-mounted.
- **`FFMPEG_BIN` must be set** outside Docker. The recorder refuses to start
  without it rather than failing quietly every thirty seconds.
- **Smart App Control** occasionally blocks one of scipy's DLLs on import.
  Retrying works.
- **Never run the host scripts beside the containers.** A stale watchdog once
  revived them alongside, producing two recorders and a stray database.
- The match feed sends **no User-Agent** — it answers 403 to browser-like ones.
- Measurements older than `--prune-days` (7) are dropped at startup and every
  six hours. Alerts are never pruned.
