# SignalAI

**Real-time audio quality monitoring and AI dubbing for live sports commentary.**

Football commentary is streamed live to bookmakers, where a commentator who is
too quiet, distorting, or buried under the crowd costs the customer the match
they paid to follow. Interns used to listen to each channel by ear and message
commentators on WhatsApp when something sounded wrong.

SignalAI does that job on **52 channels at once**, a measurement every second,
against broadcast standards rather than guesswork.

```
docker compose up -d --build
```

Then <http://127.0.0.1:8642>.

---

## What it measures

- **Loudness** — ITU-R BS.1770-4, with the filter state carried across
  windows. Resetting it every second injects a transient into exactly the
  low-level readings dead-air detection depends on.
- **True peak** — 4× oversampled. This feed peaks above the ceiling while no
  individual sample does; a sample-peak meter calls that clean.
- **Voice against crowd** — no separation model. A neural VAD finds the gaps
  between words, the crowd is measured *there*, and the voice is what remains
  once the crowd is subtracted out.
- **Whether anyone is actually there** — a pre-match holding clip is
  technically perfect audio and completely useless. A recording never pauses
  to breathe, and it repeats.

Liveness and audio capture need no credentials of any kind: probing the
stream host agrees with the vendor's own panel on which channels are up.

Every threshold traces to a measurement — a blind listening test, a backtest
over labelled nights, or a distribution over real windows — and each is
written down with its evidence in `config.py`.

**SignalAI Dubbing** is the second half, experimental by design: one
commentator's audio pulled once and fanned out to several live
speech-to-speech sessions, so one English feed can serve Greek, German and
French markets. Nothing it produces is published.

---

## What it tells a commentator

| It says | What fixes it |
|---|---|
| **No audio** | check you are live, check the encoder |
| **Too quiet** / **Too loud** | Mic gain |
| **Distorting** | lower Mic gain now |
| **Peak too hot** | ease off a little |
| **Buried in the crowd** | lower the Crowd slider |
| **No atmosphere** | raise the Crowd slider |
| **Holding clip playing** | switch back to LIVE |
| **Stream stopped** | it says whether it is us or them |

Each names a control the commentator actually has. Claiming a channel starts
monitoring and is what routes alerts to Slack; stopping gives a match report.

---

## Changing a threshold

Edit **`thresholds.json`**, then `docker compose restart`. No rebuild.

Anything not listed keeps the default from `src/commentary_qc/config.py`,
which is where the reasoning lives. A typo is refused at startup rather than
ignored — a silently dropped threshold is an outage nobody can see — so a bad
edit crash-loops the recorder. `docker compose logs --tail 20 recorder` will
say `unknown Thresholds keys`.

---

## Running it

```
docker compose up -d --build     # start, or rebuild after a change
docker compose logs -f recorder  # watch it work
docker compose run --rm backup   # snapshot the databases to ./backup/
docker compose down              # stop
```

`docker compose ps` hides exited containers — use `docker ps -a`.

Without Docker:

```
pip install -r requirements.txt
set FFMPEG_BIN=C:\path\to\ffmpeg.exe   # refuses to start without this
python -m commentary_qc.watch --config thresholds.json --claims data/claims.db
python -m commentary_qc.web
```

Three files, none committed: `channels.json` (the 52 channels, read-only in
the container), `.slack.env`, `.translate.env`. API keys come from the
environment only; the page cannot write one and no API response carries one.

**Set `QC_PASSCODE` before the page leaves localhost** — claiming a channel
starts and stops monitoring, which makes the page a control surface.

---

## Tests

```
python tests/test_engine.py          # the rules and their hysteresis
python tests/test_dsp.py             # loudness, true peak, VAD
python tests/test_no_commentary.py   # the filler rule, against labelled nights
```

Fourteen suites in `tests/`. Every vendor is tested against a fake of itself
on localhost, so none of it needs an API key or spends anything.

---

## Three things that will catch you out

1. **Never put the SQLite databases on a host bind mount.** WAL needs shared
   memory a Windows mount cannot provide: the recorder writes perfectly while
   readers see a stale file and the page reports an outage that is not
   happening. They live on a named volume.
2. **Git Bash rewrites `/data/...`** into a Windows path. Prefix any
   `docker compose exec ... /data/...` with `MSYS_NO_PATHCONV=1`.
3. **A reboot stops it.** Docker Desktop does not start itself — tick
   Settings → General → *start on sign-in*.

---

## Where to go next

| | |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | How it works. Every alert and its threshold, a piece of audio walked through each flow, the debt named honestly, and the to-do list. |
| [HANDOVER.md](HANDOVER.md) | Taking it over: what to read and in what order, what you need access to, and what exists only on the host machine. |
| [ENGINEERING-LOG.md](ENGINEERING-LOG.md) | Why. Every decision and what it cost when it was wrong. Search it rather than read it. |

Built with Python, FastAPI, FFmpeg, SQLite and Docker. Two containers, one
command, no cloud dependency.
