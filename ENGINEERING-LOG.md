# Engineering log — SignalAI

> The project diary: every decision, why it was made, and what it cost when it
> was made wrong. Written as the work happened, newest sections last.
>
> Not a document to read front to back. **Search it** when you want to know why
> something is the way it is — the answer is almost always in here, with a date
> and the measurement behind it.

## What this is

An AI quality-check engine for **live football/cricket commentary audio** at MMC Sport
(a Munich sports-media agency). Commentary is streamed live to bookmakers (bet365). We
listen to each feed and raise alerts when the audio isn't broadcast-quality. Previously
interns monitored this by ear and pinged commentators on WhatsApp; we're replacing that
with automated detection.

**Detection targets:** dead air / no audio · too loud · too quiet · crowd dominating
(voice buried) · no crowd / atmosphere missing · clipping.

No labelled data exists, so the engine is **signal-processing + a tunable rule engine**
(runs day one); ML comes later once we've logged enough data.

## Background (why some decisions are what they are)

The platform vendor (mediaondemand / Matt Job) has separately built their own alerting
backend + a broadcaster-facing panel (`control2.mediaondemand.net`), described as
"finished but noisy." We are building our **own** engine — independent QC / better
noise control / our own metering. Relevant technical consequence: Matt is actively
**securing the broadcast** (SSO, killing shared logins), so the currently-open stream
endpoints may get locked down. Keep the source URL + headers **configurable**, and we
should get a stable whitelisted endpoint from him before he tightens things.

## Where we are right now

Inspected the player's network traffic. Findings:

- Playback is **HLS** via Flowplayer + hls.js. The player loads `chunklist.m3u8` +
  a stream of `media-*.aac` segments. No WebRTC, no DRM.
- **Effectively unauthenticated** — the request has no cookie, no Authorization header,
  no token in the query string. Only `Referer`/`Origin` headers (browser defaults, not
  needed for a server-side pull). This is the "too open" security gap the vendor flagged.
- The control panel also exposes the **ingest RTMP** side:
  `rtmp://origin.mediaondemand.net/commentaries/<channel>/live` — AAC, 44.1 kHz, >96 kbps.
  Channel names are **stable**; this is a second possible tap.
- **The catch:** the HLS path contains a UUID that almost certainly rotates per
  broadcast restart, so it can't be hardcoded — see "Open problem: discovery" below.

### Example URLs (UUID may already be dead — grab a fresh one from a channel showing status "OK")
```
HLS chunklist:
  https://hls.mediaondemand.net/commentaries/football-01/output/support/975d4a94-e7df-4aeb-91ff-80fefcebf432/chunklist.m3u8
HLS segment:
  .../975d4a94-.../media-ua95jw4hv_23429.aac
RTMP ingest (stable channel name):
  rtmp://origin.mediaondemand.net/commentaries/football-01/live
```
Channels seen: Cricket Ch.1/2, Football Ch.1/2/3, each with a status (OK / Stopped) and language.

## Architecture decision

**Approach A — direct stream pull** wins. Because it's plain HLS/RTMP, capture workers are
tiny `ffmpeg` pulls, not headless-browser containers. The heavy "browser + virtual audio
sink" fallback is **off the table**. Full MVP shape (build toward this, not all at once):

- **Ingest:** one lightweight capture worker per stream (ffmpeg → PCM), supervised with
  auto-reconnect; a stream registry (which channels, source, language, per-stream thresholds).
- **Processing:** the analyzer (VAD-gated voice/crowd + LUFS + true-peak) → decision engine
  (thresholds + hysteresis).
- **Delivery/data:** Pub/Sub event bus → alert delivery (dedup/rate-limit = 2nd noise layer),
  BigQuery (every metric window), GCS (±10 s clip per alert for review/labelling).
- **Control/ops:** FastAPI + Postgres control plane to retune thresholds live; monitoring
  on worker health + alert-rate-per-stream.

Stack: Python, FastAPI, GCP (GKE/Cloud Run), BigQuery, Pub/Sub, GCS.

## Existing code (already written, tested)

```
commentary-qc/
├── src/
│   ├── audio_source.py   ffmpeg → 16 kHz mono PCM, yields 30 ms frames. Works for
│   │                     file, HLS, RTMP, Icecast, or a loopback device (same code).
│   ├── metrics.py        WindowAnalyzer: per 1 s window — webrtcvad splits speech vs
│   │                     gaps; crowd level = gaps; voice level = speech power minus
│   │                     crowd power; + LUFS (pyloudnorm) + true-peak → MetricsWindow.
│   ├── engine.py         Thresholds (all knobs in one dataclass) + AlertEngine: rules
│   │                     + hysteresis (attack/release) → FIRE / CLEAR events as JSON.
│   └── run.py            Wires source → analyzer → engine, prints one JSON alert/line.
├── requirements.txt      numpy, pyloudnorm, webrtcvad  (+ system ffmpeg)
└── README.md
```
Run: `python src/run.py <file-or-stream-url>`  → JSON alerts on stdout.
Engine logic and the full ffmpeg→analyze→alert loop are both tested and pass.

## Session findings — 2026-09-04 (all probe tasks done, engine running on live audio)

Measured against live `football-01`. This supersedes the open questions below.

- **MONO.** 44.1 kHz AAC, 1 channel, ~130 kbps. So **no mid/side path** — voice/crowd
  separation is VAD-gated, as originally implemented.
- **The pull works with no headers.** Plain `curl`/`ffmpeg` get HTTP 200; no Referer,
  Origin, cookie or token needed. Still configurable via `--header`.
- **RTMP is not pullable.** `rtmp://origin.mediaondemand.net/commentaries/football-01/live`
  answers `Server error: Failed to play live; stream not found` — the server is reachable
  but the path is publish-only. Discovery option 1 is dead.
- **Discovery is SOLVED — the UUID is not required at all.** These serve the same live
  stream, advancing in step with the UUID path:
  - `https://hls.mediaondemand.net/commentaries/<channel>/output/playlist.m3u8`  ← master, preferred
  - `https://hls.mediaondemand.net/commentaries/<channel>/output/support/chunklist.m3u8`
  - `https://hls.mediaondemand.net/commentaries/<channel>/output/chunklist.m3u8`

  `discovery.py` probes these in order and validates by **content, not status code** — the
  CDN answers 200 with a bare `#EXTM3U` for paths that exist but carry no stream.
- **Only `football-01` was live.** `football-02/03`, `cricket-01/02` all 404 (panel status
  "Stopped"), so the channel-name convention for the others is **unconfirmed**.

### CORRECTION (later same day): football-01 was FILLER, not commentary

The 36.7 s cycle is the **holding loop played before commentary starts** — confirmed by MMC.
Every measurement below taken from `football-01` describes that holding audio, not live
commentary. Re-measured across all live channels:

| channel | format | LUFS | peak dBTP | clip run | content |
|---|---|---|---|---|---|
| football-01 | 44.1k mono | −12.7 | +0.4 | 8 | **filler** |
| greyhounds-01 | 44.1k mono | −14.4 | −0.6 | 5 | **filler** |
| horses-01 | 48k stereo | −30.4 | −10.2 | 0 | live commentary |
| horses-02 | 48k stereo | −28.3 | −9.2 | 0 | live commentary |
| horses-03 | 48k stereo | −27.0 | −8.2 | 0 | live commentary |

- **Channels are NOT uniform**: two encoder profiles, 44.1 kHz mono and 48 kHz stereo.
- **The 48 kHz "stereo" is effectively dual mono** — L/R correlation 0.99945, side channel
  35.6 dB below mid, zero lag. The side signal is codec noise. **Mid/side separation is
  therefore useless on every channel**; VAD-gating is the approach throughout, as built.
- **Filler is loud and clipping; live commentary is quiet and clean.** The earlier
  "the feed clips persistently" finding belongs to the *holding audio*, which is a real
  defect in their filler asset but not a live-commentary fault.
- **Live commentary runs ~5-7 dB BELOW the −23 EBU target.** The real-world problem on this
  platform looks like *too quiet*, not too loud — the opposite of the first read.
- **Caveat — the comparison is confounded.** Both loud channels are filler AND 44.1 mono;
  all three quiet channels are live AND 48 k stereo. Loudness cannot be attributed to
  encoder profile vs. filler-vs-live until a 44.1 kHz channel is seen carrying a real match.
- **Discovery generalizes**: the UUID-free path resolved all five live channels. Channel
  families seen live: `football-`, `greyhounds-`, `horses-` (01..03).
- **Auto-reconnect is now proven in production**: a real DNS failure hit mid-run on
  horses-02; the supervisor fired `stream_down`, reconnected and cleared, losing ~3 s.

**Product implication:** alerting on filler is pointless — it is a pre-recorded asset nobody
is going to fix mid-match. The engine should detect the holding loop and suppress
quality alerts (or mark the channel "not yet live"). Filler has a clean signature:
periodic multi-second silences on a fixed cycle. Not yet implemented.

### Quality findings measured on football-01 — NOW KNOWN TO BE FILLER AUDIO

- **The feed clips, persistently.** True peak median **+0.4 dBTP**, max **+1.2 dBTP**, with
  runs of up to **9 consecutive samples at full scale** — genuine hard clipping in the
  source, not just inter-sample overs. EBU R128 permits −1 dBTP. Worth raising with the vendor.
- **It runs hot:** ~**−12 LUFS** median, ~11 dB above the −23 EBU R128 broadcast target.
- **Dry mix:** crowd bed sits at ~−40 LUFS, commentator ~27 dB above it. Little atmosphere.
- **The feed has a structural 8.8 s silence every 36.7 s**, repeating exactly (4x in 150 s).
  Content differs each cycle (waveform correlation 0.015 at that lag), so it is an automated
  playout cycle, not a loop. **A 5 s dead-air attack fires on every one of these (~98 false
  alerts/hour)**; the default is now 12 s. Confirm the intended cadence with the vendor.

With that calibration: **150 s of live audio → 1 alert** (the genuine clipping), no false positives.

### Gotcha worth remembering

**Silero v5 needs 64 samples of carried left context prepended to each 512-sample chunk
(576 total).** Feeding a bare 512 does not error — it silently returns ~0.002 for
everything, including clean speech. This cost real debugging time; `vad.py` documents it.

## Tasks for this session (in order) — DONE, see findings above

1. **`ffprobe` the chunklist** → report codec, sample rate, and **channels (MONO or STEREO)**.
   This is the single most important number — it decides the separation approach (below).
2. **Prove the pull:** `ffmpeg -i <chunklist> -t 30 -c copy /tmp/grab.aac`. If it 403s, retry
   adding `-headers $'Referer: https://control.mediaondemand.net/\r\nOrigin: https://control.mediaondemand.net'`.
3. **Test the stable RTMP tap:** `ffmpeg -i "rtmp://origin.mediaondemand.net/commentaries/football-01/live" -t 20 -c copy /tmp/rtmp.aac`.
   May be publish-only / IP-locked to the encoder — failing is fine, we just need to know.
4. **Run the engine on live audio:** `python src/run.py <chunklist-url>` → confirm it emits
   sensible alerts on a real match. That's the MVP working end-to-end on production audio.
5. Start on **discovery** (see below).

## Open problem: discovery (the rotating UUID)

The HLS chunklist URL isn't stable, so we need to resolve the current URL at runtime.
Options, best first:
1. **Stable RTMP** — if task 3 works, `rtmp://.../commentaries/<channel>/live` sidesteps
   the whole problem.
2. **Master playlist** — hls.js loads a master `playlist.m3u8` *before* `chunklist.m3u8`;
   it may sit at a stable path that points to the current chunklist. Find it in Network.
3. **Control-panel API** — the "Play"/"Go To" button fires an XHR returning the current
   stream URL. Filter Network to Fetch/XHR, click Play, find the JSON. Then call that
   endpoint from the worker to get the live URL per channel.

## Decisions still pending

Answered 2026-09-04: mono (VAD-gating stands) · no headers needed · RTMP publish-only ·
UUID not needed at all. Still open:

- **What loudness target does bet365 actually expect?** The whole loud/quiet band depends
  on it and is currently guessed at −16 LUFS ±. This is the single biggest calibration gap.
- **Is the 8.8 s / 36.7 s silence cycle intended?** It sets the dead-air attack time.
- **Channel naming for the other feeds** — unverifiable until they are live.
- Does the UUID-free path survive a broadcast restart, and Matt's SSO work?

## Working constraints

- **Never hardcode the UUID** — treat the stream URL as discovered per broadcast.
- Keep **source URL + headers configurable** (Matt is securing the platform; endpoints may change).
- **Rules-based first**, no ML yet — but log every metric window so we can train a classifier later.
- Loudness in **LUFS** (broadcast standard), relative balance in energy dB.

## Session 2 - 2026-09-07: multi-channel monitoring built

- **~120 channels on the platform, ~11 live at once.** Probing the CDN agrees
  *exactly* with the panel's OK/Stopped status, so liveness needs no credentials.
  Channel slug = panel name lowercased: "Football Ch.13" -> `football-13`.
  **Except racing**: "Irish Horse Racing" -> `horses-03`, "Greyhound Racing" ->
  `greyhounds-01` - no number in the display name, so that mapping must be
  written down, not derived.
- **Not all channels are MMC's.** Scope lives in `channels.json` and MUST be
  narrowed to MMC's own commentators before running for real.
- Built: `registry.py` (scope + liveness), `supervisor.py` (one thread per live
  channel), `store.py` (SQLite, single source of truth), `notify.py` (Slack,
  one thread per broadcast), `watch.py` (entry point).
- Verified live on all 11 channels: 90 s run, 11 real alerts, 925 metric rows,
  0 spurious alerts on shutdown, ~200 KB of database.
- **Crowd rules do not apply to studio racing commentary** - with no crowd bed,
  `no_crowd`/`crowd_dominating` fire permanently and end up measuring room tone
  between words. Disabled per channel in `channels.json`.
- Two capture points identified but only one built. Point B (CDN pull, ~10 s
  behind, what bet365 actually receives) is done. **Point A** - capturing the
  commentator's microphone locally in the browser for instant feedback - is not,
  and is the right answer for live self-monitoring since the CDN delay is
  unavoidable. Blocked on: how does the commentator's voice reach the stream?

## Roadmap (after MVP)

Silero VAD (better than webrtcvad) → real separation (DeepFilterNet live / Demucs offline) →
multi-stream runner → Pub/Sub bus → BigQuery metrics + GCS clips → FastAPI/Postgres control
plane for live threshold tuning → dashboard / broadcaster delivery with alert dedup.

## Session 3 - 2026-09-15/16: claims drive monitoring, matches, MMC scope

**Scope is settled.** `channels.json` holds MMC's 39 football channels
(21-29 Italian, 31-39 Portuguese, 41-44 German, 51-58 + 100-101 Greek,
102-109 French), confirmed by the user. Previous list kept as
`channels.old.json`. Other providers' channels still appear on the page behind
an "Other providers" filter, never in the counts.

**Claiming now starts monitoring.** `watch.py --claims claims.db`: the
supervisor reads open claims every 3 s and starts/stops a monitor within
seconds, on any channel, alongside the configured list. Claims older than 6 h
expire. The web app owns claims.db, the recorder only reads it.

**Match times come from a public fixture feed**, not the vendor
(`fixtures.py`, ESPN scoreboard, no key). 35 of 39 of MMC's scheduled
broadcasts matched, every kick-off identical; no Greek Cup. **Send no
User-Agent** - it answers 403 to browser-like ones. It cannot say which channel
carries a match: the commentator picks their match when claiming, and that is
what lets a holding clip be judged against the match clock (normal before
kick-off and at half-time, a fault during play).

**Do not use the vendor's new website** (control2 Playlist Controller, SSO) or
its data feed - user decision 2026-09-15. Background on what it exposes, and on
how commentary reaches the stream (WebRTC in the browser or the MOD Broadcast
Windows app, both with a Mic gain slider and an optional synthetic **crowd
bed**), is in memory: `vendor-panel-data`.

**Engine changes**
- Thermostat gap: too_quiet fires below -40 and clears above -37; too_loud
  fires above -17, clears below -19. Ch.102's 18 alerts on 9 Sep became 5.
- `filler_playing` now also fires on a **repeat test**: the loudness envelope of
  the last ~3 min correlating with itself at 15-90 s lag (`metrics.loop_score`).
  Filler 0.77-1.00, live commentary 0.2-0.5. Catches crowd-bearing loops the old
  three-signal rule missed. Backtest: 21/21 labelled periods correct.
- `clipping` (flat-topped samples) split from `peak_over` (true peak above
  -1 dBTP). A -0.8 dBTP over no longer reads "DISTORTING".
- While a holding clip plays, level/peak/balance rules are frozen and cleared:
  nobody is on air to fix a recorded file. Dead air and stream loss still fire.
- Discovery tells "server unreachable" (ProbeFailed) from "not live", so an
  outage on our side no longer looks like every channel stopping.

**The page** (`web.py` + `static/claim.html`, port 8642) is now a dashboard:
scoreboard tiles (commentary live / holding loop / silent / faults / claimed /
matches live), channel board with filters, matches panel (live + upcoming),
claim form with match picker, alert feed, and a per-commentator live view with
a loudness meter and 10-minute trend. Health pills show recorder, stream scan,
match feed and Slack. Advice names the controls commentators actually have
(Mic gain, Crowd slider, switch back to LIVE).

**Operational gotchas** (also in memory `running-the-recorder`): set
`FFMPEG_BIN` - a bare shell does not have ffmpeg on PATH, and
watch.py now refuses to start without it; Smart App Control occasionally blocks
scipy's `_qmc_cy` DLL on import (retry); liveness polling of 39 channels runs in
parallel (~2 s), because 21 s sequential stalled the heartbeat.

## Session 4 - 2026-09-17: matches front and centre, producer report, yellow/black

**The page is now yellow on black** (`static/claim.html`, single theme by
choice - booth and wall screen, not a browser preference), and **matches are a
main full-width section**, not a sidebar: cards with Live now / Today /
Tomorrow / Next 7 days tabs, competition filter, live minute and score, "on
Ch.NN" when a channel has claimed the match, and "Use this match" which feeds
the claim form.

**New in the engine**
- **Escalation**: a firing fault that gets `escalate_step_db` (6 dB) worse
  raises an `ESCALATE` alert, message prefixed "Getting worse - ".
  `_ESCALATION_SCALE` in engine.py defines "worse" per rule.
- **Outage attribution**: `OutageTracker` (supervisor.py) counts how many
  channels lost audio within 45 s. The stream_down message now says "only this
  channel - most likely the encoder or the vendor" or "N channels at once -
  most likely our connection or the stream server".
- **Level rules wait for the clip verdict.** For the first ~3 minutes of a
  connection the repeat test cannot tell commentary from a clip, so
  level/peak/balance rules are frozen; otherwise every restart opened with
  "too loud" and "distorting" on channels playing the vendor's clip.

**Post-match report** (`report.py`, `/api/report/<channel>?minutes=N`, "Match
report" button on the live view, also shown when a commentator stops
monitoring): measured vs commentary time, holding-clip share, median level
against the -18 LUFS streaming target, share within 2 LU of it, time too
quiet/loud, worst peak, distortion, voice-over-crowd, and every alert with its
duration and whether it ever cleared. Clips and silence are excluded from all
commentator figures; the clip's own worst peak is reported separately as
`clip_peak_max` (MMC's football clip clips at +2.7 dBTP).

**Fixture feed widened to 17 competitions** including the domestic cups: on
16 Sep the English channels were carrying four Carabao Cup ties that the old
10-competition list missed. Portuguese/Greek cups and cricket have no public
feed - those fixtures are typed in on the claim page.

**Open, and it matters**: MMC's scope is wider than the 39 channels in
channels.json. The "ics" studio login shows Football Ch.1-6 (English) and
Cricket Ch.1-2 carrying real matches; our list came from the "venkat" account.
Get the full list from a login that sees everything.

## Scope settled - 2026-09-18

`channels.json` holds **52 football channels**: English 1-10, Italian 21-29,
Portuguese 31-39, German 41-44, Greek 51-59 + 100-101, French 102-111. Read
once from the vendor's Playlist Controller with the user's own login, after
which the session and browser profile were deleted; the previous list is
`channels.prev.json`. English channels were added because the studio ("ics")
login shows them carrying MMC matches, and **cricket is deliberately excluded -
"we only do football"**. Ch.107 is kept even though the panel omits it.

**The new Playlist Controller and the audio host are the same platform.** All
41 channel ids from the new site resolved on the open stream host, and its
liveness agreed with the panel on 41 of 41, including which channels were on a
holding clip versus live. So the login is never needed at runtime - only for
this one list - and audio analysis has never required credentials of any kind.

**Disk**: the recorder prunes measurements older than `--prune-days` (default 7)
at startup and every 6 h; alerts are never pruned. The user wants the whole
setup to stay free: local SQLite, a no-key fixture feed, Slack's free plan.

## Runs in Docker - 2026-10-18

```
docker compose up -d --build     # start / rebuild
docker compose logs -f recorder  # watch it
docker compose ps                # hides exited containers - use `docker ps -a`
docker compose down              # stop
```

Two services from one image: **recorder** and **web** (page on 8642, binding
0.0.0.0 inside the container). Databases and logs on the `./data` bind mount;
**ffmpeg lives in the image**, which ends the "ffmpeg not on PATH" failure for
good. Slack credentials come from `.slack.env` with `required: false`, so a
missing file leaves Slack in dry-run rather than stopping the recorder.

`restart: unless-stopped` replaces the PowerShell watchdog and the scheduled
task Windows would not let us register (not an admin account). Proven with a
throwaway crashing container: 6 restarts in 35 s. Two traps when testing this:
`docker kill` counts as a manual stop and does NOT trigger the policy, and
`kill -9 1` inside a container does nothing because the kernel shields PID 1
within its own namespace.

**The host-process scripts are retired** (`start-commentary-qc.ps1`,
`watchdog.ps1`, Startup entry renamed `.disabled`). Never run both: a stale
watchdog revived the host processes beside the containers on 2026-10-18,
producing two recorders and a stray `live.db` in the project root (kept as
`*.stray-20261018`). The real database moved to `data/live.db` with its
history intact - 811k measurements, 7,252 alerts back to 15 Sep.

**Unchanged by the vendor's new panel**: commentators moving from
control.mediaondemand.net to control2 does not affect us. Both panels drive the
same publishing points; audio still comes from
`hls.mediaondemand.net/commentaries/<channel>/output/playlist.m3u8`, and
control2's own player path (`output/support/plc/playlist.m3u8`) returns the
same stream at the same bitrate (re-verified 2026-10-18).

## Slack live, and the bind-mount trap - 2026-10-20

**Slack is connected** (workspace "mmc sport", bot `commentary_qc`). Credentials
live only in `.slack.env` (gitignored, dockerignored), reach the recorder via
compose `env_file`, and are read by watch.py from the environment. Delivery is
**claimed-only**, so an idle platform stays silent. The whole path was proven
against a local fake Slack first (`tests/test_slack_delivery.py`, 9 checks:
threading per broadcast, escalation delivery, info-level suppression,
throttling retried rather than dropped).

**The trap, and it cost an outage of visibility (not of data):** SQLite in WAL
mode needs real shared memory between writer and readers. A **Windows bind
mount cannot provide it**. With `./data:/data`, the recorder wrote into the WAL
perfectly - 1.56M measurements, nothing lost - while the web container read a
stale file and the page reported "recorder stopped, measuring 0 of 47". A clean
`docker compose stop` checkpointed the WAL and every "missing" row appeared.

**Fix: a named volume** (`commentary-qc-data`, declared `external: true`),
which lives in the Linux VM's filesystem. Only `channels.json` stays a bind
mount, read-only. Since the data is no longer a Windows folder, snapshots come
from a helper service using SQLite's online backup API:

```
docker compose run --rm backup     # writes ./backup/live-<stamp>.db
```

**Rule for this project: never put the SQLite databases on a host bind mount.**

## Sign-in on the page - 2026-11-07

The page now supports a **shared passcode**, off by default. Set
`QC_PASSCODE` in `.slack.env` (both services read that file) and the page
requires it; leave it empty and nothing changes, which is right while it only
listens on localhost. **Set it before exposing the page to any network** -
claiming a channel starts and stops monitoring, so an open page is a control
surface, not just a view.

One shared secret rather than per-person accounts: dozens of commentators, no
user database, and the page already asks who you are when you claim. The
cookie is an HMAC of the passcode, lasts 30 days, and is `httponly`.

`/healthz` is deliberately open so the container health check needs no secret
(the compose healthcheck now uses it instead of `/api/channels`). API calls
answer 401 rather than redirecting, so the page's own JavaScript reports "cannot
reach the server" instead of rendering a login form as data.

Verified 2026-11-07 against a container with the passcode set: signed out
`/`→303, `/api/*`→401, `/healthz`→200; wrong passcode→401; right
passcode→303 + cookie; signed in `/`→200 and `/api/*`→200; forged cookie→401.
`python-multipart` is required for the sign-in form and is now in
requirements.txt - without it FastAPI raises at startup and the page never
boots.

## Session 5 - 2026-11-07: live Slack test, and the translation layer

**Slack proven on real faults, not filler.** Earlier tests only ever caught the
vendor's holding clip. This time three live channels were claimed while they
were actually carrying matches - Ch.41 (German), Ch.31 (Portuguese), Ch.100
(Greek) - and **38 alerts were delivered with zero Slack errors**: 19 FIRE,
1 ESCALATE, 18 CLEAR, across `clipping`, `peak_over`, `too_loud`, `too_quiet`,
`crowd_dominating` and `filler_playing`. Ch.31's "commentator buried in crowd"
escalated from -12.3 dB to -30.0 dB and the ESCALATE fired correctly.

**Open noise problem found by that test.** Ch.31 fired `clipping` seven times
with an *identical* message each time - "17 samples at full scale, true peak
+2.5 dBTP", about every 30 s - starting 14:22:53, while `filler_playing` on the
same channel only fired at 14:28:38. The identical numbers are the signature of
MMC's own holding clip (recorded in Session 4 as clipping at +2.7 dBTP). So for
roughly six minutes the clip's distortion was being reported as the
commentator's fault. `_CLIP_MASKED` does freeze peak rules while a clip plays,
but the repeat test needs ~3 minutes of history and the "undecided" freeze only
covers the first ~3 minutes of a *connection* - it does not cover a channel
that was live and then switched to a clip. Worth fixing: a repeated identical
clipping message is itself evidence of a recording.

### Translation (new, `src/commentary_qc/translate/`)

Transcribe and translate live commentary, as a passenger on the QC engine - the
audio is already pulled, decoded and split into speech and gaps for the quality
rules, so this is one more read of a buffer that exists anyway.

**Ports and adapters**, because the model will change and the vendor might:
`models` (vocabulary) - `settings` - `ports` (two capabilities) - `mappers/`
(vendor JSON <-> our types, pure and unit-tested) - `providers/` (one adapter
per model, from a registry) - `segmenter` - `budget` - `control` - `pipeline` -
`store` - `api`. Nothing in the pipeline, the API or the page knows a
provider's name.

**Three modes**, and the gap between the middle two is the point:
- `off` - no tap is installed, no audio buffered, no credit spendable. Default.
- `shadow` - the whole pipeline on real matches, every result stored, **nothing
  published**. With the `mock` provider this is free, so segmenting, latency and
  volume can be measured across all 52 channels for nothing.
- `prod` - the same pipeline, published. Promoting is a switch, not a rewrite.

**Switching is live and crosses processes.** The page writes a
`translate_control` row into `claims.db` (which the web app owns) and the
recorder reads it - the same single-writer mechanism claims already use. A
dedicated poll thread in the pipeline picks it up within ~3 s and bumps a
`generation` counter that running monitors compare against, so turning
translation on reaches channels **already on air**. Both halves of that were
bugs found in testing: without the counter the mode only applied to monitors
that started later, and without the poll thread nothing read the control row
until a tap was requested - a circle that never closed.

**Camb AI** (`providers/camb.py`), base `https://client.camb.ai/apis`, header
`x-api-key`, `urllib` not `requests` - no new dependency. All three endpoints
are create/poll/fetch: `POST /transcribe` (multipart, `media_file`) ->
`GET /transcribe/{task}` -> `GET /transcription-result/{run}`, giving
`{"transcript":[{"start","end","text","speaker"}]}` in seconds; same shape for
`/translate` -> `/translation-result/{run}` -> `{"texts":[...]}`. Statuses
`SUCCESS|PENDING|TIMEOUT|ERROR|PAYMENT_REQUIRED`. **Languages are locale tags
(`en-us`), the numeric ids are deprecated, and the docs never print the tags** -
so `/source-languages` is fetched and `LanguageMap` resolves ours against it,
falling back within a language (`pt-pt` -> `pt-br`) but never across one.
Upload limit 20 MB.

**Guards, because the failure here is a quiet bill rather than a crash.**
Daily and per-channel caps on audio sent, seeded from the database at startup
so a restart does not hand back a fresh day; `PAYMENT_REQUIRED` latches the
budget shut and says so once; audio below `min_speech_ratio` is never sent (a
holding clip and a dead channel both look billable and empty to a transcriber);
a bounded queue that drops the **oldest** segment, because the newest audio is
the only audio worth captioning. Every failure is a stored row, never a gap.

**Monitoring always wins.** Capture threads only append to a buffer. Measured:
40 s of audio handed over in under 2 s while the provider was deliberately
slower than real time, queue bounded, backlog dropped and recorded.

**Audio for upload**: 48 kHz float32 -> 16 kHz 16-bit WAV in memory (`wave`,
no ffmpeg spawn per segment), scipy-filtered decimation by exactly 3. A 20 s
segment is ~640 KB. Scaled by 32767, not 32768, so a sample at full scale does
not wrap to the most negative value.

**Segmentation reuses the QC VAD.** The analyzer's `speech_ratio` tells the
segmenter where a sentence ended, so finding pauses costs no second pass and no
second VAD. Hard stop at `segment_max_s`, early close at a pause after
`segment_min_s`. Added `WindowAnalyzer.elapsed` so captions share the clock the
measurements use.

**Gotcha, cost a debugging round:** `time.monotonic()` on this Windows host
ticks at ~15.6 ms, so a fast provider call measured as exactly 0.0. Every
*reported duration* now uses `time.perf_counter()`; `monotonic` is kept only
for poll deadlines. Watch for mixing the two in one subtraction.

**Also:** the schema strings are applied one statement at a time, split on the
statement terminator - so a SQL comment must never contain one. Cost two
failed runs.

**Page**: `/translate` (`static/translate.html`), same yellow-on-black. Mode
switch with each mode's consequence spelled out, model cards saying why a model
cannot be picked (`CAMB_API_KEY is not set`), a budget meter, a "send a test
segment" probe (speech-shaped noise, not a tone - a tone transcribes as nothing
and makes a working model look broken), the caption feed side by side, and a
channel table saying *why* a feed is not being translated.

**Tested**: `tests/test_translate.py`, **128 checks**, including the whole
adapter against a **fake Camb AI on localhost** - the create/poll/fetch dance,
retry on 429 and 500, no retry on 402, 401 named as a key problem, job timeout -
so none of it needs a key or spends a credit.

**Proven on live audio**: shadow mode on live Portuguese commentary produced 5
captions from 56 s with contiguous timestamps and no drops; then in the
deployed stack on Ch.41/31/100 at once, with the Greek channel's silent
stretches correctly refused rather than billed (`dropped_quiet`).

**Configuration**: `.translate.env` (gitignored, dockerignored);
`.translate.env.example` is committed. The API key comes from the environment
and nowhere else, so nothing the page can write changes which account is
billed, and a provider left out of `enabled_providers` cannot be turned on from
the page at all.

**Left in `shadow` + `mock`** - free, publishes nothing, and `claimed_only` is
on, so nothing runs until a commentator claims a channel. Switch the provider to
`camb` once a key is in `.translate.env`.

## Session 6 - 2026-11-08: modes are endpoint wirings; the page is four things

**The correction that drove this session.** Session 5 built `shadow` as a
persisted global pipeline mode written into `claims.db`. That was wrong. A mode
is an **execution wiring selected by the endpoint**: which provider is built,
what it may spend, where the result is kept, who sees it. So the paths are
namespaced by it and `prod` was renamed to the user's word, `standard`:

```
POST /api/translate/shadow/run     -> runs
POST /api/translate/standard/run   -> 501 + what building it needs
GET  /api/translate/staging/model  -> 404 naming the modes that exist
```

`modes/base.py` holds `ModeInfo` + `Wiring` + `NotBuiltMode`; `modes/shadow.py`
is the only implementation; `modes/standard.py` is **declared and deliberately
empty**, and its docstring lists exactly what building it means (an audience, a
budget agreed with whoever pays, who triggers a run, a retention decision).
It refuses rather than falling back to shadow - asking for production and
silently getting the test wiring is the one failure invisible from outside.
`Mode.ALIASES` keeps `prod` loading, so a control row written last session
still validates.

**Only shadow is built, by instruction.** The old 7-section page is gone
(`git show 6ffb9b2` has it). The page is now four things: the model and what
its audio translation does, credit, a run button, and two audio players.

**The feature is now audio-to-audio, because you have to be able to hear it.**
`SpokenAudio` and `TranslationRun` are new domain types; `Speaker` is a third
port beside `Transcriber` and `Translator`.

- **Camb's one-call dubbing is unreachable for us.** `POST /dub` takes a
  `video_url` it fetches the media from. Our clip is a few seconds in memory on
  a host with no public address, so there is nothing to give it. The page says
  this under "not available" rather than leaving it looking like an oversight.
- **The reachable path is three calls**: `POST /transcribe` (multipart upload)
  -> `POST /translate` -> `POST /tts-stream`. The last one is the only call in
  the adapter that does not parse JSON: it streams raw audio back, so
  `_call(..., raw=True)` returns `(bytes, headers)`.
- **Camb publishes no balance, quota or usage endpoint.** Verified against
  `llms.txt` and the OpenAPI spec. The only figure it gives is the
  `X-Credits-Required` response header on the call that incurred it, documented
  with no unit - so it is stored and shown as a **string, verbatim**. The page
  shows our own count of seconds (what the cap is enforced on) beside it and
  says which is which. An invented balance would be worse than none.
- `/list-voices` contradicts itself in the docs - an object with a `voices` key
  in the example, a bare array in the schema. The mapper accepts both.
- `mappers/tts.py` is separate from `mappers/camb.py`: everything in that file
  is shaped around the create/poll/fetch `task_id` dance, and `/tts-stream` is
  none of that.

**New infrastructure**
- `grab.py` - its own short ffmpeg pull for one run, not a hook into the
  recorder. The containers are separate, the recorder only watches claimed and
  configured channels, and the appeal of a shadow run is that it works on any
  channel at any time. A 15 s pull is cheap; a cross-process audio handoff is not.
- `runs.py` - a **third** SQLite file, `data/translate.db`, owned by the web
  process. Same one-writer-per-file rule as `live.db` (recorder) and
  `claims.db` (page). The audio lives in the row; `KEEP_AUDIO = 25` strips old
  blobs while keeping the record of what was tried.
- Shadow has its **own** budget (`shadow_daily_minutes`, default 10) separate
  from the continuous pipeline's 60. Verified it survives a rebuild: 40 s spent,
  `docker compose up --build`, still 40 s - seeded from the database.

**A real gap this closed.** `providers.describe()` told the page Camb was
unselectable without a key while `providers.build()` happily returned one, so a
request naming it got as far as capturing 15 s of audio before failing on the
first call. Both now go through one `blocked_reason()`.

**Verified on live audio, 8 Nov**: real runs on `football-104` (French),
`football-100` (Greek), `greyhounds-01`. 12 s of real commentary in, a playable
WAV out of both sides (heard RMS 0.237, spoken RMS 0.141 - audible, not a
silent file passing for a working player). A dead channel answers 409 in 0.7 s.
202 checks in `test_translate.py`; the fake Camb now serves `/tts-stream` with
the credit header, `/list-voices` and `/target-languages`.

**Still the offline `mock` provider** - beeps, one note per word, deliberately
nothing like speech so nobody mistakes the stand-in for a voice that got the
commentary wrong. Put a key in `.translate.env` and pick Camb on the page.
Worth pressing nothing else first: the page's "languages" count comes from
`/source-languages`, so whether Greek and Portuguese are actually offered is
unverified until a real key answers.

**Not touched, still open from Session 5**: the clip-blamed-on-commentator
noise (Ch.31 repeating an identical `clipping` message for ~6 min before
`filler_playing` caught up). It is an `engine.py` sequencing change that affects
live alerting, so it waits for a decision.

## Session 7 - 2026-11-08: live translation

**The point of the feature is live, and the clip path was never it.** Session 6
built transcribe -> translate -> speak as three REST calls, which answers "does
this model understand football commentary" from a recorded clip. It cannot
answer "can it keep up with a match", and that is the question. So live is
built, on Camb's realtime WebSocket, and it is the primary control on the page.

**The endpoint is `wss://realtime.camb.ai/v1/realtime`** - a wholly different
protocol and a different host from the REST adapter, so none of that plumbing
is reused and pointing `camb_base_url` at a fake does not redirect it.

- Auth is the `x-api-key` **request header**. The docs also allow a key in the
  first message; we never do, because it would land in any transcript of the
  session.
- `mode` goes in the query string **and** the session message. Documented
  precedence is session, then query, then the `fast` default, so the two
  agreeing costs nothing and matters most for `slow`, where silently falling
  back to `fast` would mean a language quietly refused.
- Audio **up** is base64 inside JSON (`input_audio_buffer.append`) - the server
  **ignores binary frames from the client**. Costs a third in bandwidth and is
  not negotiable. 256 KiB decoded per append.
- Audio **down**: the docs contradict themselves. The AsyncAPI schema and the
  Python SDK say a raw binary frame with no `type` field; the reference prose
  and the Chatterbox page say a JSON event with a base64 `delta`.
  `parse_event` accepts **both**, because guessing wrong gives a session that
  connects, reports progress and produces no audio.
- `input_audio_buffer.commit` is recognised but **not supported** - the server
  answers with an error and carries on. There is no flush and no end-of-turn.
- Wire format is 24 kHz mono PCM16 both ways (SDK-documented; the protocol
  reference never states it). `FFmpegSource` gained an optional `rate` so the
  live tap asks ffmpeg for 24 kHz directly - better than upsampling a 48 kHz
  decode that has already been resampled once.

**Threads, not asyncio.** `websockets.sync` gives a blocking client whose
`send` and `recv` may be used from different threads, which is the shape
needed: the caller feeds audio at the rate it arrives while a reader thread
drains the far end into a queue. A loop that blocked on `recv` between chunks
would stop feeding the session, and the server answers that with
`client_too_slow`. `websockets>=13` is now in requirements.txt - it was already
present via uvicorn on dev machines but the image installs only what is listed.

### The finding that matters for MMC

**Italian and Greek are not in the realtime language list.** The reference
prints fourteen - ar-ae/eg/sa, de-de, en-gb, en-us, es-es, fr-ca, fr-fr, hi-in,
ja-jp, ko-kr, pt-br, zh-cn - and `it-it` and `el-gr` are absent. That is
**20 of the 52 channels** (Italian 9, Greek 11). English, French, German and
Portuguese (via pt-br) are covered: 32 channels.

The docs say slow mode "supports the full language list" and fast mode fewer,
but **never print either per-mode list**, so which of the three this is remains
unresolved. The code therefore **warns rather than refuses**: an undocumented
language that works is likelier than this list being complete, and a real
session is the only way to settle it. `rt.supported()` returns the warning and
the page shows it above the Start button.

### What was built

- `mappers/realtime.py` - the whole protocol as pure functions, no socket.
  Message shapes, both audio transports, the error-code tables, and
  `error_for()` mapping server codes onto our exceptions so no-credit latches
  the budget, a rate limit is retried and a key problem reads as configuration.
  **An unknown error code is treated as fatal**: the server closes the socket
  on most of them, and streaming into a dead session would report no fault.
- `providers/camb_realtime.py` - the socket, the reader thread, the lifecycle.
- `providers/mock_realtime.py` - a free session shaped like the real one: not
  ready the instant it opens, answers in bursts rather than per chunk, returns
  audio at the wire rate, refuses an oversized append. Chimes, not speech, so
  the stand-in is never mistaken for a model that got the commentary wrong.
- `live.py` - `LiveRun` (one thread: ffmpeg in, session out, captions and audio
  collected) and `LiveRunner` (**one session at a time by default**, because
  Camb's free plan documents API concurrency of 1 and a second session is
  otherwise refused by the far end only after audio has started flowing).
- `ports.py` - `LiveSession` and `LiveTranslator`, kept apart from
  `AudioTranslator` because live and file are separately available, separately
  priced and carry different languages.
- Endpoints: `POST/GET /api/translate/shadow/live`,
  `POST .../live/{id}/stop`, `GET .../live/{id}/audio/{heard|spoken}`.
  `standard` answers 501 as before.
- The page keeps its four sections; section 3 is now two tabs, **Live first**
  and the clip path second.

**The budget is enforced against the clock**, re-checked every 5 s, because a
realtime session is billed in windows rather than per call - there is no request
boundary to check at. `live_seconds` (default 60) is a hard ceiling so a session
somebody opened and walked away from cannot run the four hours the protocol
allows. The web process stops every live session on shutdown; one it forgot
would hold an ffmpeg pull and a metered socket open until the container died.

### Two things only the real endpoint revealed

1. **A refused key can close the session with no reason at all** - the upgrade
   is accepted, then the connection drops with no error event and no close
   frame, so there is nothing to report but close code 1006. The fake server
   could not show this because it was written from the docs, where every
   documented failure sends an `error` event first. Now: a session that closes
   before `session.created` without naming a reason is reported as a key or
   plan problem and says which setting to check. A second attempt *did* get the
   documented `invalid_api_key` event, so both paths happen and both are
   handled.
2. **Audio played back while a session is still running** needs the player
   built once per session. Rebuilding it on each poll restarts playback from
   the beginning every 1.5 s, which makes a live translation impossible to
   actually listen to.

### Verified

- `tests/test_live_translate.py`, **112 checks**, including the real adapter
  against a **fake Camb realtime WebSocket on localhost**: handshake, the
  header, both audio transports, no-credit, a rejected key, an unsupported
  language, a session that never boots, a mid-stream failure, a survivable
  error that must *not* tear the session down, and the reasonless close. No key
  and no credit.
- Live on `football-41` (German) with the mock model: 31 s of real commentary
  at 24 kHz, paced 1:1, heard RMS 0.028 (quiet, as this platform's live
  commentary always is), captions paired, both sides playable WAV, a second
  session refused with the channel that held the slot.
- Over HTTP end to end: 202 on start, translated audio served **while the
  session was still running** (48 KB at 2 s, 480 KB at the end), 409 for a
  second session, 404 for an unknown mode, 501 for `standard`, the finished run
  filed as `kind="live"` without anybody polling for it.
- `translate.db` gained a `kind` column with an additive migration, since
  `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists.
  Audio pruning now also has a **byte budget** (192 MB): the row count was
  enough when every run was a 15 s clip, but a live run is minutes of audio on
  both sides.
- 202 checks in `test_translate.py` still pass.

**Still the `mock` provider.** Everything above is proven except a successful
real session, which needs `CAMB_API_KEY`. Camb's free plan is **6 minutes of
API speech to text in total, once** - so `shadow_daily_minutes` is 5 and
`live_seconds` 60, and the first real session should be spent on a channel
carrying an actual match in a language on the list (German or French), not on a
holding clip.

**Not a code question, and it blocks production**: Camb's terms name gambling
as prohibited content (s.13.3(b)) and restrict distributing Platform output to
third parties (s.13.3(j)(i), s.13.4); our output would reach bet365's
customers. s.10.5 makes uploaded audio model-training data. And live streaming
on their published plans is Expert only, $900/month for 24 minutes and one
stream. See memory `camb-ai-plans-and-terms`.

## Session 8 - 2026-11-09: layers, Gemini, two front doors

Five things asked for, all done: Gemini as a second adapter with Camb kept,
the translate route built around live audio, two buttons as the way in, the
assessment junk gone, and the architecture tightened. Decisions taken on the
way: **live speech-to-speech** (not the chain), restructure **translate only**
(not the proven engine), page stays **localhost with the passcode optional**,
and **fix the clip-blame bug** while in there.

### The translate package is layered now, and a test enforces it

```
translate/
  core/      the domain. No I/O, no vendor, no framework.
  infra/     ffmpeg, sqlite, wav bytes, the audio source.
  adapters/  camb/ gemini/ mock/ - each behind a core port.
  app/       modes, live sessions, grab, bench, budget, pipeline.
  web/       routing only.
```

26 files moved, every import absolutised first so the move was a plain
find-and-replace with no dot arithmetic. The proven QC engine was left alone
deliberately: it is live in Docker with 7k alerts behind it, and moving it for
tidiness would risk a working system for no functional gain.

**`tests/test_architecture.py` reads every import and fails if one points the
wrong way.** That is the difference between a layout and a rule. It found two
real leaks the moment it existed:

- `app/` and `web/` were reading **Camb's** sample rate, mode names and
  language list. A second vendor could not have been added cleanly.
- the offline stand-in **imported Camb** to borrow the same constants.

Gemini proved why that mattered: its input is 16 kHz where Camb's is 24, and
it has no notion of "fast" or "slow" at all. So now a session reports its own
`AudioSpec` **per direction**, a provider reports its own modes and its own
language caveat, and an adapter attaches the exception its error codes mean.
Nothing outside `adapters/` names a vendor.

**`AudioSource` is a port.** `app/live.py` and `app/grab.py` were building
`FFmpegSource` and calling discovery themselves - a subprocess in the middle of
a use case. They take the port now, the composition root hands them
`FFmpegAudioSource`, and a live run can be driven from arrays in milliseconds,
including a mid-run gap, which is awkward to arrange for real.

**Two bugs the move surfaced.** `import realtime as rt` silently resolved to
Camb's *session* instead of its *wire format*, because the move created two
modules whose names collide - nothing raised. And a refused key arrived as a
1008 close while the opening message was still being sent, so it read as "the
session refused the opening message" rather than naming the key; the close
reason says `invalid_api_key` and is now read.

### Gemini live translation

`gemini-3.5-live-translate-preview`, and it is the first adapter that can
actually do what MMC wants: **English in, Greek or German voice out**, 79
languages, audio straight to audio over one socket.

- `wss://generativelanguage.googleapis.com/ws/...BidiGenerateContent?key=...`
  - **the key goes in the URL**, the only documented form for this socket, so
    nothing logs a URL without `protocol.redact`.
- Setup is one `{"setup": {...}}` with `translationConfig` **inside**
  `generationConfig`. Google's own docs contradict themselves here - the SDK
  example puts it at the top level - and raw JSON is what we send, so the
  reference wins.
- Audio up: base64 in `realtimeInput.audio` with the MIME string **exactly**
  `audio/pcm;rate=16000`, 100 ms chunks. Audio down: base64 nested at
  `serverContent.modelTurn.parts[].inlineData.data`, **24 kHz**.
- **No source-language field.** The model detects it, so a channel whose
  configured language is wrong costs nothing here - the opposite of the clip
  chain, where it fails the run.
- `echoTargetLanguage` defaults **on**: with it off, an English channel
  translated to English goes silent, which looks exactly like a broken session.
- A connection lasts ~10 minutes and a session 15, and the server sends
  `goAway` first. Surfaced as a note, so a run that ends early says why.
- **Free of charge on the free tier**, which also states that free-tier input
  and output are used to improve Google's products. MMC's commentary becomes
  training data until somebody agrees otherwise.

The provider **refuses the clip path** rather than half-implementing it:
`transcribe`, `translate` and `speak` raise with the reason, and the page shows
"Recorded clips: not available". Its health check deliberately does not open a
session, because that would spend tokens every time the page loaded.

### The clip-blame bug is fixed

Session 5's open problem. Ch.31 reported **the same message** - "17 samples at
full scale, true peak +2.5 dBTP" - seven times over six minutes while
`filler_playing` lagged behind, so a holding clip's distortion was blamed on a
commentator who was not on air. The old masking missed it because the repeat
test needs ~3 minutes of history and the "undecided" freeze only covers the
first 3 minutes of a *connection*, never a channel that was live and then
switched to a clip.

**The fix uses the thing that was staring at us: a fault message that repeats
with identical numbers is itself evidence of a recording.** Live speech never
peaks at exactly the same sample count twice. Three identical messages inside
seven minutes latch the clip masking and emit one `clip_repeat` note instead.
Three tests: the incident itself (7 false alerts -> at most 2 plus one
explanation), a *changing* fault still reported (so this is not a way to
silence a persistent real fault), and dead air never masked.

### Two front doors

`/` is now a landing with two buttons - **QC Commentary** and **MMC AI
Dubbing** - because they are for different people: a commentator fixing their
own level, and whoever is judging a model. The dashboard moved to `/qc`;
`/translate` is unchanged and retitled. Both cards show live figures from the
real API field names, because a landing that always reads zero says the system
is idle when it is not.

### Cleaned up

The six assessment builders and scorers are gone (the listening tests are
finished) and 98 MP3s with them: `assessment*/` went from **84 MB to 320 KB**.
**The evidence was kept** - `labels.json`, the answer keys and the written
results are the provenance of every threshold, and deleting them would leave
nobody able to answer "why -40?". `clips/` and `tools/replay_alerts.py` kept.

### Written down, not built: broadcasting back into a channel

`app/modes/standard.py` now lists what it would take: a publishing point from
the vendor per output language (theirs is publish-only, and writing into a
commentator's channel is not an option), a jitter buffer to turn bursts of
speech into an unbroken stream, a decision about what happens when another
language takes longer to say than the English did, whether MMC wants a
synthetic voice replacing a commentator at all, and a written answer on both
vendors' terms given the audience is a bookmaker's customers.

### State

**Two adapters, shadow mode, nothing published.** Gemini needs `GEMINI_API_KEY`
in `.translate.env`; until then both real models are correctly unselectable and
the offline stand-in runs. README rewritten in plain language.

Checks: 17 architecture, 202 translate, 121 live, 72 bench, 93 Gemini, plus the
engine, DSP, Slack and backtest suites.

## Session 9 - 2026-11-09: multi-language, identity from the words, page rebuilt

Four things asked for and all four built: **one source into several languages
at once**, **which match is on a channel**, **wrong-language detection**, and
the translate page redesigned around the user's own sequence (source -> model
-> languages -> listen -> broadcast, the last locked as version 2).

### One pull, several languages (`app/fanout.py`, `app/group.py`)

A `LiveGroup` is one channel, one ffmpeg pull and one model session per output
language. The fan-out sits **at the `AudioSource` port**, so nothing above it
changed: a leg is an ordinary `LiveRun` and never learns two others are
reading the same bytes. Every endpoint that serves one run - audio, stop, poll
- works on a leg unchanged.

Sharing the pull is not thrift. Three independent pulls of a live HLS stream
start at whatever segment the CDN is serving, so the Greek output could be
seconds ahead of the German one and nobody comparing them would know why.
Two things make the legs genuinely comparable, and **both were bugs the tests
found**:

- **The pull waits for the group to assemble** (`ASSEMBLE_S`, 8 s, returns the
  instant the expected listeners have joined). Each leg opens its own session
  before it asks for audio and sessions do not boot in step, so the first leg
  to arrive was consuming audio the others never saw: measured 6.0 / 4.6 /
  4.0 s on a three-language run before the fix, identical after.
- **Opening is serialised per channel-and-rate** by a lock per key. Without
  it three legs can each find no pull, each spawn ffmpeg, and two of those
  processes get thrown away *after* taking a second copy of the stream from
  the vendor - and the group can end up split across two pulls. A test forces
  the race with a barrier and a deliberately slow resolve.

Back-pressure is the project's existing rule: a slow listener loses its
**oldest** audio, counted and reported; the reader thread never blocks,
because a realtime session answers a slow client by hanging up.

**Refusals happen before anything is spent**: more languages than
`live_targets_max` (4, `QC_LIVE_TARGETS_MAX`), or more than the provider
declares it will hold open. Camb's adapter now states `max_live_sessions = 1`
from its free plan, so the app refuses a group rather than letting the far end
refuse the second session once the meter is running. The app asks the adapter;
it does not read vendor plans.

A leg failing takes only its own language down - a Greek session refused for
an unsupported language must not cost you the German translation.

**A real bug this surfaced, worth remembering:** `LiveRun.running` was
`not finished`, so a run that was *never started* reported itself as running
forever. The group's budget check happens after the runner has taken the legs
on, so a refusal left the channel - and the single-channel slot - blocked
until the process restarted. `running` now means started and not finished,
and `start_group` rolls back its registration if `start()` raises.

### What the commentary says the channel is (`core/identify.py`)

Pure functions over text, no I/O, 28 checks on commentary written
independently of the word lists (scoring a vocabulary against itself proves
nothing).

- **Language**: Greek by alphabet - not a guess, the script is not shared.
  The rest by *function words chosen for contrast*: "che" against "que",
  "non" against "no"/"nao", "molto" against "muy"/"muito". Football words
  were the tempting choice and the wrong one - "fuorigioco" appears twice a
  match, "che" appears in every sentence. Needs 12 words and a 1.35x margin
  over the runner-up, else it says nothing.
- **Match**: team-name tokens against the fixture feed, with club-type words
  ("fc", "city", "united", "real") dropped - a transcript containing "city"
  tells you nothing when four fixtures are City sides. **Both sides named
  beats more hits on one side**, as a sort key rather than a weighted sum, so
  a derby cannot be decided by whichever club is named more often. One side
  only = 0.45 confidence and says so.
- **`auto` is not a claim.** The racing channels have no language in
  `channels.json`, so treating `auto` as configured reported a wrong-language
  fault on every one of them, every run. Found on live audio.

Wired into both the group snapshot and single-run `live_state`. The fixture
feed and the QC snapshot are passed into `build_router` from `web.py` - both
already in that process, so neither costs a probe.

**`qc_snapshot` is passed as a lambda**, because `snapshot` is defined further
down `create_app` than the `build_router` call: naming it directly binds a
local that does not exist yet, and the container crash-looped on
`UnboundLocalError` until it was deferred.

### The channel picker reads the QC engine

`/{mode}/channels` now returns liveness, the current state (commentary /
holding clip / silent), the level, any fault, who claimed it and the match -
read from the quality side's own snapshot, not probed. Live channels sort
first. This platform's live commentary runs 5-7 dB under target, so
translating a quiet or filler channel means judging a model on audio no model
could do much with, without being told that is what happened. Verified live:
football-07 showed "silent / NO AUDIO" before it could be picked.

### The page (`static/translate.html`, rewritten)

Five numbered steps in one column - numbered because it genuinely is a
sequence. Gone: the coverage matrix, the scoreboard, the bench tab, the mode
chips (the old page is in git). The clip path is no longer a peer of live.

- Step 3 shows **seconds pulled once against seconds charged**, so the cost
  shape of three languages is on screen before the button.
- Step 4 gives each language its own panel with both audio players and a
  **synced transcript**: the highlight follows the *commentary* player,
  because that file's timeline is the run's. The translated side is only the
  moments the model was speaking, concatenated, so its clock is a different
  clock - interpolating across it would look precise and drift. Phrase-level,
  not word-level: neither provider gives per-word timestamps on the live
  socket.
- **Audio elements are built once per leg** and never rebuilt on a poll
  (Session 7's lesson, kept).
- The offline stand-in now warns **in the caption feed**, not only on the
  model card. The card said "it is not listening" and the captions still read
  like a transcript, which is where somebody reads them.

### Verified on live audio, 9 Nov

Three languages on `football-01` and on `horses-01`, two on `horses-03`:
one pull, `listeners: 3`, 0 dropped, every leg sent an **identical** number of
seconds, and the heard WAVs came back **byte-identical in size** (1,487,946 B
each) - the shared pull proven from outside. 28 s pulled -> 84 s charged.
139 of today's fixtures considered. Refusals over HTTP: 5 languages -> 409
naming the setting, no languages -> 400, bad channel name -> 400, Camb
without a key -> 400, a second channel -> 409, `standard` -> 501, unknown
mode -> 404. A dead channel reports every URL it tried.

Checks: 68 multi-language, 28 identify, 17 architecture, 202 translate, 121
live, 101 Gemini, 76 security, 72 bench, 13 wiring, plus engine, DSP, Slack
and backtest.

**The flake, explained - and the assertion was the bug.** The leg-alignment
check failed intermittently, and only when the suite ran beside the others.
Cause: the fake feed yielded blocks as fast as the CPU allowed, so under load
a leg got starved of scheduling, its 24-block queue overflowed and it
correctly dropped its oldest audio. The fan-out was right; the test demanded
the legs always match. Two changes: the fake feed is **paced** at 3 ms a block
(a real one delivers one every 100 ms, which is what makes the queue two
seconds of slack), and the assertion now states the real invariant - **legs
may differ only by audio that was dropped, and a drop must be reported**.
Confirmed by reproducing it under deliberate load and then passing.

**Still the `mock` provider** - no key in `.translate.env`. So the identity
features are proven on text and proven to be wired on live audio, but the
transcript they read is the stand-in's own English vocabulary. Whether Gemini
really offers Greek, and whether language detection works on real ASR output,
needs a key.

## Design pass - 2026-11-11: shared tokens, and why it looked generated

User's verdict on the rebuilt translate page: "looks like AI made general
frontend design", "i see no borders". Both were fair, and the causes were
specific rather than a matter of taste.

### What was actually wrong

- **Chrome was shouting and content was whispering.** Big amber uppercase
  condensed headings; channel names, loudness figures and captions small and
  grey. The largest, brightest thing on screen should be a measurement, not
  the word "THE SOURCE".
- **The accent was everywhere**, so it could no longer mark anything. It now
  marks what is selected, what is live, and the one figure that matters
  (seconds charged).
- **Separation was done with background fills *and* a solid opaque line**
  (`#2e2e38`). Dark interfaces that stack three greys go muddy. Now: one
  surface, and semi-transparent borders at 7% / 13% / 22%.
- **Ad-hoc spacing.** Nothing sat on a grid, which reads as almost-aligned -
  worse than obviously loose.
- **16px base type** on what is an operational tool. 13px now, rows 32px.

### `static/ui.css`

Tokens and primitives, shared by every page, served from a single route
(`/static/ui.css`) rather than a mounted directory - the only file in
`static/` worth serving is this one, and mounting the folder would publish
whatever lands in it next. Read per request so editing it needs no restart.

Primitives: `.panel` (+ `.panel-head` / `.panel-body`), `.chip`, `.btn`,
`.note`, `.field` / `.strip`, `.label`, `.num`. A 4px spacing scale, a type
scale, three border weights, semantic colours kept separate from the accent.
Every figure is `font-variant-numeric: tabular-nums`, because the page is
mostly columns of numbers and proportional digits make a column look bent.

### The translate page, rebuilt on it

- **A rail with a visible spine** joining the five step numbers. The sequence
  is real, so drawing it is information. First attempt used the 7% hairline
  and it was invisible against the ground - the complaint in its purest form,
  a border that was present and doing nothing. It is the 13% line now.
- **The channel list is a real table**: sticky header, fixed columns, LUFS
  right-aligned with the unit in the header rather than repeated down every
  row, and a 3px severity stripe down the left edge so state has a shape and
  not only a hue.
- **The cost strip** gives the page its one large size: languages, audio
  pulled, audio charged. That relationship is the whole point of the shared
  pull.
- **The transcript** got a rule between heard and said. Without it they were
  two columns of short text with 12px of nothing between them, reading as two
  unrelated lists rather than one line in two languages.
- **The last run is adopted on load** whether or not it is still running. A
  refresh that threw away the result you were about to judge is the one
  behaviour nobody would forgive.

Each of those came from looking at a headless-Chrome screenshot of the real
page with real data, not from reasoning about the markup. Defects that only a
render shows: a duplicated sentence (shadow mode's `purpose` already ends with
"nothing is published" and the page appended its own copy), three channels all
truncating to "Horses C...", and "On air, not measured" overflowing a pill.

### The QC dashboard: one line, and it was a cascade collision

`/qc` had no side gutter at all - every section touched x=0. Cause:
`.matches{padding:22px 0 6px}` and `.work{...padding:22px 0 50px}` sit on
elements that also carry `.wrap`, and the `padding` **shorthand** silently
wiped `.wrap`'s `padding: 0 24px`. Both are `padding-block` now, which sets
only the vertical halves. Nothing else on that page was touched: it is the
production tool with 7k alerts behind it, and the fault was structural rather
than stylistic.

The landing page was left as it is - it already has borders, a gutter and the
same colour family. Unifying it onto the tokens is worth doing and was not
done here.

### Reference

Checked against current practice rather than invented: a 13px base on a 4px
grid with ~32px rows; separation by semi-transparent border rather than by a
second background; the brand colour under about 5% of pixels; one sans and one
mono. Sources in the session, chiefly a write-up of Linear's token and density
choices and a SaaS frontend-design guide.
