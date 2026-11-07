# Commentary QC — project context

> Handoff brief for Claude Code. Save this as `CLAUDE.md` in the project root and it
> loads automatically each session. Everything the assistant needs to continue is here.

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
`FFMPEG_BIN` - the shell Claude launches from has no ffmpeg on PATH, and
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
