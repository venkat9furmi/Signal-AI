# Architecture and flow

A handover document. It explains what this system is made of, how a piece of
audio travels through it, and why things are arranged the way they are. Read
it top to bottom once; after that use the maps.

Nothing here assumes you know the project. It does assume you can read Python.

---

## 1. What the system does

MMC Sport employs commentators who talk over live football. Their voice goes
out to bookmakers like bet365. Two separate jobs run on that audio:

- **QC Commentary** — listen to every channel and say when something would
  sound wrong to a listener. Too quiet, distorting, buried under the crowd,
  dead air. Alerts reach the commentator, so they can fix it mid-match.
- **MMC AI Dubbing** — take a channel and translate it out loud into another
  language, while the match is happening. Still in testing; nothing it
  produces is published.

The important structural fact: **the audio is pulled once and read twice.**

---

## 2. The big picture

```
       the vendor's CDN                    (we only ever READ from here)
   hls.mediaondemand.net
   /commentaries/<channel>/output/playlist.m3u8
              │
              │  plain HLS. No login, no token, no cookie.
              ▼
      ┌───────────────┐
      │    ffmpeg     │   one process per channel
      │  → PCM mono   │   48 kHz float32, reconnects on its own
      └───────┬───────┘
              │
      ┌───────┴────────────────────────────┐
      │                                    │
      ▼                                    ▼
┌──────────────┐                   ┌──────────────────┐
│ QC ENGINE    │                   │ TRANSLATION      │
│              │                   │                  │
│ measure      │                   │ stream to a model│
│ → judge      │                   │ → get voice back │
│ → alert      │                   │ → store it       │
└──────┬───────┘                   └────────┬─────────┘
       │                                    │
       ▼                                    ▼
  Slack + page                        the page only
  (the commentator)                   (nobody else yet)
```

Two operating system processes, from one Docker image:

| Process | Entry point | What it owns |
|---|---|---|
| **recorder** | `watch.py` | pulling audio, measuring, alerting, Slack |
| **web** | `web.py` | the pages and their API, claims, translation runs |

They do not talk to each other directly. They share state through SQLite
files, and **each file has exactly one writer**. That rule is why there are
three files rather than one.

---

## 3. The files on disk

```
live.db        written by the recorder      read by the web
               every measurement, every alert, channel state

claims.db      written by the web           read by the recorder
               who has claimed which channel, and the translate control row

translate.db   written by the web           read by nobody else
               translation runs, their audio, bench results and scores
```

Why one writer each: SQLite in WAL mode lets many readers and one writer. Two
processes both writing the same file will eventually block each other at a
bad moment. Splitting by owner means neither ever waits on the other.

> **Rule, learned the hard way:** these files must live on a **named Docker
> volume**, never a Windows bind mount. WAL mode needs shared memory that a
> Windows mount cannot provide. The symptom is nasty: the recorder writes
> perfectly while readers see a stale file, so the page reports an outage that
> is not happening. We lost a day to it.

---

## 4. The QC engine

Plain modules, no layering. It is small, it has been live since September, and
it works — so it was deliberately left alone when the translation side was
restructured. Do not reorganise it without a reason.

```
discovery.py     Which channels are broadcasting?
                 Probes the CDN and checks the CONTENT, not the status code --
                 the CDN answers 200 with an empty playlist for paths that
                 exist but carry nothing.

audio_source.py  ffmpeg → 48 kHz mono float32, in ~100 ms blocks.
                 Supervises itself: reconnects, detects a stalled pipe,
                 and yields a "gap" marker rather than silence when the
                 stream is absent. Absent and silent are different faults.

vad.py           Is someone speaking in this chunk? (Silero, ONNX)
loudness.py      How loud is it? (ITU-R BS.1770-4, with filter state kept
                 between windows, plus 4× oversampled true peak)

metrics.py       Turns blocks into one MetricsWindow every 1.024 s:
                 loudness now / short / over a minute, true peak, how many
                 samples are pinned at full scale, how much of the window was
                 speech, the voice level, the crowd level, the gap between
                 them, and a "does this audio repeat itself" score.

engine.py        The rules. Each one reads a MetricsWindow and says yes, no,
                 or "no evidence". Then hysteresis turns that into alerts.

supervisor.py    One thread per channel, restarted if it dies. Also decides
                 whether an outage is ours or theirs by counting how many
                 channels dropped at once.

store.py         SQLite. One writer thread.
notify.py        Slack. One thread per broadcast, so a match is one
                 conversation rather than forty notifications.
scanner.py       Polls liveness for all 52 channels in parallel (~2 s).
fixtures.py      Match times from a public feed, so a holding clip can be
                 judged against the match clock.
report.py        The post-match summary a producer reads.
```

### How a rule actually becomes an alert

This is the part worth understanding, because it is where the noise control
lives.

```
every 1.024 s a MetricsWindow arrives
          │
          ▼
  is a holding clip playing?  ──── yes ──→  freeze level, peak and balance
  (three separate signals)                   rules, and clear any that are up.
          │ no                               Dead air and stream loss still
          ▼                                  fire: those matter regardless.
  does the rule hold?
    yes → add 1.024 s to its "held" timer, reset its "clear" timer
    no  → the opposite
    no evidence → hold BOTH timers and the current state
          │
          ▼
  held long enough (attack)?  ──→ FIRE
  already firing and 6 dB worse? ──→ ESCALATE
  clear long enough (release)? ──→ CLEAR
```

Three things stop it being noisy, and all three were added because it *was*
noisy:

1. **Thermostats.** "Too quiet" fires below −40 LUFS and only clears above
   −37. A channel sitting exactly on the line produces one alert instead of a
   stream. Ch.102 went from 18 alerts a night to 5.
2. **Nothing is blamed on a recording.** Before kick-off and at half-time a
   holding clip plays and nobody is on air to fix it.
3. **A repeated identical fault message is read as a recording.** Live speech
   never peaks at exactly the same sample count twice, so three identical
   messages inside seven minutes latch the clip masking. This came from a real
   incident: Ch.31 reported "17 samples at full scale, +2.5 dBTP" seven times
   over six minutes, and it was the holding clip being blamed on a commentator
   who was not there.

### "No evidence" is a third answer, not a false

A rule can return `None`. Example: "is the commentator buried under the
crowd?" cannot be answered during a silence — there is no voice to measure.
Returning `False` there would quietly clear a real fault. Returning `None`
freezes the timers until there is something to judge. If you add a rule, think
about whether it has a `None` case.

---

## 5. The translation side

This one *is* layered, because it has to survive the model changing. It
already has: Camb AI first, then Gemini, and a third will come.

### The layers

```
  ┌─────────────────────────────────────────────┐
  │  web/        FastAPI routes. Routing and    │
  │              serialisation only.            │
  ├─────────────────────────────────────────────┤
  │  app/        The use cases. Modes, live     │
  │              sessions, the budget, the      │
  │              bench. Knows no vendor's name. │
  ├──────────────────────┬──────────────────────┤
  │  adapters/           │  infra/              │
  │  one folder per      │  ffmpeg, SQLite,     │
  │  vendor, each behind │  WAV bytes.          │
  │  a core port         │                      │
  ├──────────────────────┴──────────────────────┤
  │  core/       The domain. Types and ports.   │
  │              No I/O. No vendor. No          │
  │              framework. Never changes when  │
  │              a model does.                  │
  └─────────────────────────────────────────────┘

        dependencies point INWARDS only
        (web → app → core, adapters → core, infra → core)
```

`settings.py` sits outside the layers on purpose: every layer reads
configuration, and putting it inside one would force the others to depend on
that layer for a reason unrelated to why the layer exists.

**This is enforced, not just documented.** `tests/test_architecture.py` reads
every import in the package and fails if one points the wrong way. Run it
before and after any restructure. It is the difference between a layout and a
rule — and it caught two real violations the moment it was written.

### What lives where

```
translate/
  settings.py          every knob, validated at startup

  core/                ─── the domain ───
    models.py          AudioSegment, Transcript, Translation, SpokenAudio,
                       TranslationRun, LiveEvent, AudioSpec, AudioBlock,
                       and the error types
    ports.py           the capabilities an adapter must satisfy:
                       Transcriber, Translator, Speaker, LiveSession,
                       LiveTranslator, AudioSource, AudioFeed
    languages.py       our language tags, and how they resolve
    channels.py        what a channel name is allowed to be

  infra/               ─── technical, no decisions ───
    audio.py           float32 ↔ PCM16 ↔ WAV bytes
    audio_source.py    FFmpegAudioSource: discovery + an ffmpeg pull
    runs.py            translate.db: runs, their audio, benches, scores
    captions.py        the continuous pipeline's caption store

  adapters/            ─── one folder per vendor ───
    __init__.py        THE REGISTRY. The only file that knows the names.
    gemini/
      protocol.py      the wire format, as pure functions
      provider.py      the socket, the session, the provider
    camb/
      protocol.py      their realtime wire format
      provider.py      their REST chain: transcribe → translate → speak
      realtime.py      their live session
      mapper.py, tts.py   their JSON ↔ our types
    mock/              the offline stand-in

  app/                 ─── the use cases ───
    modes/             shadow (built) and standard (declared, empty)
    live.py            LiveRun: one streaming translation, on a thread
    grab.py            a few seconds of a channel, on demand
    bench.py           one clip, several models, compared
    budget.py          the guard on the credits
    segmenter.py       cuts the feed at pauses, reusing the QC VAD
    pipeline.py        the continuous per-match worker
    control.py         the runtime on/off switch

  web/
    api.py             the HTTP surface, namespaced by mode
```

### The two ideas you need to hold

**A port is a shape, an adapter is a vendor.** `core/ports.py` says "a live
session must have `send`, `drain`, `wait_ready`, `close`, and describe its own
audio format". `adapters/gemini/provider.py` is one thing that has that shape.
`app/live.py` only ever talks to the shape. That is why adding a model is one
folder and one line.

**A mode is an execution wiring, not a feature flag.** The endpoints are
namespaced by it:

```
POST /api/translate/shadow/live      → runs, keeps the audio, publishes nothing
POST /api/translate/standard/live    → 501, and says what building it needs
```

Calling an endpoint *is* choosing the wiring: which provider gets built, what
it may spend, where the result is kept, who sees it. `standard` refuses rather
than quietly falling back to `shadow`, because asking for production and
silently getting the test wiring is the one failure invisible from outside.

---

## 6. The flows

### Flow A — a channel gets measured, a commentator gets told

```
 1. scanner.py asks the CDN which channels are broadcasting.
 2. supervisor.py starts a thread for each one that is live and in scope.
 3. That thread opens ffmpeg and reads ~100 ms blocks forever.
 4. metrics.py accumulates blocks until it has 1.024 s, then emits one
    MetricsWindow: loudness, peak, speech ratio, voice vs crowd, repeat score.
 5. engine.py pushes that window through every rule, with hysteresis.
 6. Any FIRE / ESCALATE / CLEAR goes to:
       → store.py   (live.db, so the page can show it)
       → notify.py  (Slack, but ONLY if a commentator claimed the channel)
 7. The page polls /api/channels every 5 s and redraws.
```

Step 6's condition matters: an idle platform stays silent. Slack only hears
about channels somebody has claimed.

### Flow B — a commentator claims a channel

```
 1. They open /qc, find their channel, pick their match, press claim.
 2. web.py writes a row into claims.db.            ← the web process writes
 3. The recorder reads open claims every 3 s.      ← the recorder reads
 4. Within seconds a monitor starts for that channel, even if it was not in
    channels.json at all.
 5. Their name now appears on any Slack alert for that channel.
 6. Claims older than 6 hours expire by themselves.
 7. When they stop, they get a match report: time inside the target level,
    worst peak, every alert and how long it lasted.
```

This is also how the system learns which match is on which channel. Nothing
else knows that — the fixture feed gives times, not channels.

### Flow C — a live translation

```
 1. Someone opens /translate, picks a channel and a target language.
 2. POST /api/translate/shadow/live
 3. The channel name is validated (it becomes part of a URL).
 4. The mode builds a Wiring: which provider, what budget, what retention.
 5. The budget is checked BEFORE anything opens. A live session is billed by
    the clock, not per call, so there is no request boundary to check at.
 6. LiveRun starts on its own thread and:
       a. asks the AudioSource to resolve the channel → a URL
       b. opens a session with the provider
       c. asks the session what audio format it wants
            Gemini: 16 kHz in, 24 kHz out
            Camb:   24 kHz both ways
       d. opens ffmpeg at THAT rate
       e. loop: read a block → convert to PCM16 → send → collect whatever
          came back → charge the budget → check the caps
 7. The page polls every 1.5 s for captions and progress, and can play the
    translated audio WHILE the session is still running.
 8. When it ends, the run is filed in translate.db with both sides of the
    audio, so it can be listened to again later.
```

Why a thread and not asyncio: everything else in the project is threaded, and
`websockets.sync` lets `send` and `recv` happen on different threads. A reader
thread drains the far end into a queue so the audio loop never blocks on the
network — a loop that paused to read would stop feeding the session, and the
server answers that by hanging up.

### Flow D — one recorded clip, three calls

Used for Camb, which has a REST path. Gemini has no clip path at all and says
so rather than half-working.

```
 grab 15 s  →  POST /transcribe  →  POST /translate  →  POST /tts-stream
 (ffmpeg)      (what was said)      (in the target)     (spoken back)
```

Slower than real time, but each step's output is visible, which is what you
want when one of them is wrong.

### Flow E — comparing two models

```
 1. POST /api/translate/shadow/bench with a channel, some models, some
    languages.
 2. The cost is calculated and shown BEFORE anything is sent:
    3 models × 2 languages × 15 s = 90 s of billable audio.
 3. The clip is captured ONCE.
 4. The identical samples go to every model, in parallel.
 5. Results are shown blind: A, B, C, in shuffled order, with the vendor,
    model and voice all hidden.
 6. People score them 1–5. One score per person per entry.
 7. Someone presses reveal, and a scoreboard ranks the models by what the
    team actually said.
```

Identical audio rather than simultaneous audio: two runs on two different
fifteen seconds of a match compare the match, not the models.

---

## 7. Rules that are not obvious

Each of these exists because something went wrong without it.

| Rule | Why |
|---|---|
| **Never hardcode the stream UUID.** | The HLS path used to contain one that rotates per broadcast. Discovery finds a UUID-free path instead. |
| **Validate by content, not status code.** | The CDN answers `200` with an empty playlist for a channel that is not broadcasting. |
| **One writer per SQLite file.** | Two processes writing one file block each other at the worst moment. |
| **Databases on a named volume, never a Windows bind mount.** | WAL needs shared memory a Windows mount cannot give. Readers go stale and the page reports a false outage. |
| **`time.perf_counter()` for durations, not `monotonic`.** | `monotonic` ticks at ~15.6 ms on this host, so a fast call measures as exactly 0.0. |
| **No semicolon inside a SQL comment.** | The schema is applied one statement at a time, split on the semicolon. |
| **Send no User-Agent to the fixture feed.** | It answers 403 to browser-like ones and 200 to Python's default. |
| **Set `FFMPEG_BIN` outside Docker.** | The shell has no ffmpeg on PATH, and the recorder refuses to start without it rather than failing quietly every 30 s. |
| **Silero VAD needs 64 samples of carried context.** | Feed it a bare 512 and it silently returns ~0.002 for everything, including clean speech. No error. |
| **Keys come from the environment only.** | The API neither accepts nor returns one, so no request can change which account is billed. |
| **Redact every exception that might contain a URL.** | The Gemini key rides in the query string, and the WebSocket library echoes the URI in its errors — straight into a stored row and onto the page. |

---

## 8. Running and testing

```
docker compose up -d --build     # start
docker compose logs -f recorder  # watch
docker compose down              # stop
docker compose run --rm backup   # snapshot the databases
```

The page is on 8642: `/` chooses, `/qc` is the dashboard, `/translate` is
dubbing.

```
python tests/test_architecture.py   # the dependency rule, enforced
python tests/test_security.py       # keys, channel names, header filenames
python tests/test_engine.py         # the rules and their hysteresis
python tests/test_dsp.py            # loudness, true peak, VAD
python tests/test_translate.py      # mappers, modes, budget, the clip path
python tests/test_live_translate.py # live sessions vs a fake server
python tests/test_gemini.py         # the Gemini adapter vs a fake server
python tests/test_bench.py          # comparing models on one clip
python tests/test_slack_delivery.py # delivery vs a fake Slack
python tests/test_no_commentary.py  # the filler rule vs labelled nights
```

**Every vendor is tested against a fake of itself on localhost** — the
handshake, the retries, the refusals, running out of credit. None of it needs
a key and none of it spends anything. If you add an adapter, add its fake;
that is the house style and it is the reason this codebase can be changed
confidently.

---

## 9. What to improve

Ordered by how much they matter, with an honest note on each.

### Would make the biggest difference

1. **Get a real match through Gemini and listen to it.**
   Everything is built and tested, but no human has yet heard this system
   translate real football commentary. That is the one question no amount of
   code answers, and it decides whether the dubbing half is a product or an
   experiment. Needs `GEMINI_API_KEY`.

2. **Find out what loudness target bet365 actually expects.**
   The whole loud/quiet band rests on an assumption of about −18 LUFS,
   cross-checked against broadcast standards and one listening test by one
   person on a laptop. If the real figure is different, every threshold moves.
   This is a question for the client, not a code change, and it is the single
   biggest calibration gap in the project.

3. **A host that is always on.**
   Monitoring currently depends on a laptop being awake and logged in. It was
   down 11 hours once after a reboot because Docker Desktop does not start
   itself. A small always-on machine or a cloud VM removes a whole class of
   problem.

### Worth doing soon

4. **Rotate the credentials that were pasted into a chat** — the Slack bot
   token, the `xapp-` token, the MOD password. Nothing in the code can
   substitute for this.

5. **A real commentator test.** The thresholds come from recordings and one
   listening test. Watching somebody use it mid-match will find things no
   test will.

6. **Point A capture.** Today we read the CDN, which is about 10 seconds
   behind. That is fine for a listener, and useless for a commentator checking
   their own level. Capturing the microphone in the browser would give instant
   feedback. Blocked on understanding how the voice reaches the stream.

7. **Alert dedup across channels.** If every channel goes quiet at once it is
   almost certainly us. The engine already attributes outages this way; the
   same idea has not been applied to quality alerts.

### Technical debt, named honestly

8. **`web.py` is 663 lines and does too much** — routes, wiring, snapshots,
   serialisation. It is the composition root, which is correct, but the
   route handlers should move out of it. The translation side already shows
   the shape this would take.

9. **The QC engine is not layered.** Deliberately: it is live and it works.
   But `supervisor.py` is 570 lines and `engine.py` is 488, and a port for
   "something that delivers metrics windows" would make the engine testable
   without ffmpeg the way the translation side now is.

10. **The bench's blind scoring is more than anyone asked for.** It works and
    it is tested, but with two adapters the fan-out is the valuable part and
    the scoreboard may never be used. Delete it if a month passes untouched.

11. **No end-to-end browser test.** The pages are checked by parsing their
    JavaScript and calling every endpoint they use. That catches a lot, and it
    would not catch a button wired to nothing. Playwright is installed but has
    no browser binary.

### Known unknowns

12. **Does the UUID-free discovery path survive the vendor tightening SSO?**
    It has survived one panel migration. Nobody has promised it will survive
    the next.

13. **The unexplained "disturbance"** one listener heard in clip 13 of the
    first listening test. Never reproduced, never explained.

---

## 10. To-do list

Copy this into whatever you track work in.

**Blocked on somebody else**

- [ ] Get `GEMINI_API_KEY` from the company, put it in `.translate.env`
- [ ] Ask bet365 (via MMC) what loudness target they expect
- [ ] Rotate the Slack bot token, the `xapp-` token and the MOD password
- [ ] Ask the vendor for a stable whitelisted stream endpoint
- [ ] Get a written answer on both model vendors' terms — the audience is a
      bookmaker's customers, and both restrict what their output may be used
      for

**Ready to do**

- [ ] Run a real match through Gemini and listen to it end to end
- [ ] Tick Docker Desktop → Settings → General → start on sign-in
- [ ] Set `QC_PASSCODE` before the page leaves localhost
- [ ] Sit with a commentator during a match and watch them use it
- [ ] Split the route handlers out of `web.py`

**When there is a reason**

- [ ] Point A: capture the microphone in the browser for instant feedback
- [ ] Build `standard` mode — and read `app/modes/standard.py` first, it
      lists exactly what that means
- [ ] Broadcast the translated voice back into a channel (same file; it is a
      bigger job than it sounds — a publishing point per language, a jitter
      buffer, and a policy for when one language takes longer to say than
      another)
- [ ] A port for metrics windows, so the QC engine is testable without ffmpeg
- [ ] A browser test, if the pages grow enough to need one
- [ ] Delete the bench's blind scoring if nobody has used it in a month

---

## Where to look first

If you are picking this up cold:

1. Run the tests. They pass, and they are the fastest description of what the
   system promises.
2. Read `engine.py`. It is the heart of the QC half and it is readable in one
   sitting.
3. Read `core/ports.py`, then `adapters/mock/`. That is the whole translation
   architecture in two short files.
4. Read `CLAUDE.md` for the history — every decision, why it was made, and
   what it cost when it was made wrong.
