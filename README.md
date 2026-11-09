# MMC Sport Audio

Two things that run on the same football commentary:

- **QC Commentary** listens to every channel that is on air and says when
  something would sound wrong to a bookmaker's customer.
- **MMC AI Dubbing** takes a channel and translates it out loud, as it
  happens, into another language.

The audio is pulled once and both read it. Open <http://127.0.0.1:8642> and
pick one.

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
| **Stream stopped** | We lost the feed | it says whether it is us or them |

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

Three layers, because an alerting system nobody trusts is worse than none:

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

---

## MMC AI Dubbing

English commentary in, Greek or German voice out, over one socket, while the
match is still going.

This is **in testing**. Nothing it produces is published, and no commentator
sees it. It exists so you can listen and decide whether a model is good
enough to put in front of anyone.

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
and a hard ceiling on one session, all seeded from the database so a restart
does not hand back a fresh allowance.

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

## Configuration

Three files, none of them committed:

| File | Holds |
|---|---|
| `channels.json` | MMC's 52 football channels. Read-only in the container. |
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

---

## Tests

```
python tests/test_architecture.py   # the dependency rule, enforced
python tests/test_security.py       # keys, channel names, header filenames
python tests/test_engine.py         # the rules and their hysteresis
python tests/test_dsp.py            # loudness, true peak, VAD
python tests/test_translate.py      # mappers, modes, budget, the clip path
python tests/test_live_translate.py # live sessions, against a fake server
python tests/test_gemini.py         # the Gemini adapter, against a fake server
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
