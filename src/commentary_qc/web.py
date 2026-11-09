"""The commentator page: see MMC's channels and matches, claim one, watch your audio.

**The dashboard** answers "what is going on right now" at a glance: how many of
MMC's channels carry live commentary, how many are playing a holding clip,
which have faults, which matches are live or coming up, and whether our own
machinery (the recorder, the platform scan, the match feed, Slack) is working.
It refreshes itself every few seconds.

**Claiming** ties a commentator and a match to a channel and starts the
recorder listening to it within seconds. Picking the match from the list is
what lets a holding clip be judged against the match clock: before kick-off and
at half-time it is normal, during play it means the commentary is not going out.

**Watching** is the part a commentator uses live. It reports a verdict and an
instruction, never a measurement: someone mid-sentence cannot act on
"-25.4 LUFS", but they can act on "raise your Mic gain".

The web app owns ``claims.db``; the recorder owns the measurements database and
only reads the claims. One writer per file. Nothing here reads the vendor's
website (user decision, 2026-09-15): liveness comes from the public stream,
match times from a public fixture feed.

Run:  python -m commentary_qc.web --db live.db --port 8642
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from commentary_qc.config import Settings
from commentary_qc.fixtures import FixtureFeed
from commentary_qc.registry import Registry, default_channels
from commentary_qc.report import build as build_report
from commentary_qc.scanner import LiveScanner, describe
from commentary_qc.store import EventStore
from commentary_qc.translate.web import api as translate_api
from commentary_qc.translate.app import control as translate_control
from commentary_qc.translate.app.modes import ModeRegistry
from commentary_qc.translate.infra.runs import RunStore
from commentary_qc.translate.settings import TranslateSettings

log = logging.getLogger(__name__)

#: What the commentator should DO about each fault, in their language, not ours,
#: and naming the controls they actually have: the MOD publishing tools give
#: every commentator a Mic gain slider, a Crowd (noise bed) slider and a
#: "switch to LIVE" button. Ordered by urgency -- the first matching rule is the
#: one shown, because a person on air can act on one instruction, not five.
ADVICE: list[tuple[str, str, str, str]] = [
    # (rule, state, headline, what to do)
    # We cannot tell from here whether listeners hear nothing -- only that our
    # monitor is not receiving audio. Say exactly that.
    ("stream_down", "bad", "NO SIGNAL", "The monitor is not receiving your stream"),
    ("silence", "bad", "NO AUDIO", "Check your microphone is on and not muted"),
    ("filler_playing", "bad", "HOLDING LOOP", "A holding clip is playing - switch back to LIVE"),
    ("clipping", "bad", "DISTORTING", "Lower your Mic gain - the audio is clipping"),
    ("too_quiet", "warn", "LEVEL LOW", "Raise your Mic gain, or move closer to the mic"),
    ("too_loud", "warn", "LEVEL HIGH", "Lower your Mic gain slightly"),
    ("peak_over", "warn", "PEAKS TOO HOT", "Lower your Mic gain a little"),
    ("crowd_dominating", "warn", "CROWD OVER YOU", "Lower the Crowd slider, or speak up"),
    ("voice_dominates", "warn", "ATMOSPHERE THIN", "Raise the Crowd slider a little"),
    ("no_crowd", "warn", "NO ATMOSPHERE", "No crowd behind you - add a Crowd bed if you want one"),
]
HEADLINES = {rule: headline for rule, _, headline, _ in ADVICE}

#: What each channel state is called on the page.
STATE_LABELS = {
    "onair": "Commentary live",
    "dry": "Commentary, no crowd",
    "filler": "Holding loop",
    "silent": "Silent",
    "nosignal": "No signal",
    "starting": "Starting",
    "checking": "Checking",
    "waiting": "Waiting for stream",
    "unwatched": "Not measured",
    "down": "Stopped",
}

#: Seconds without a recorder heartbeat before we stop believing it is alive.
#: It beats every 3 s, so this is several missed beats, not one slow write.
RECORDER_STALE_S = 15.0
#: Seconds without a fresh window before a "live" channel counts as stalled.
METRIC_STALE_S = 20.0
#: Seconds a newly started monitor may take to produce its first reading.
WARMUP_S = 45.0
#: How far ahead the match list looks.
UPCOMING_DAYS = 7


#: Shared passcode for the page. Empty (the default) means no login, which is
#: right while it only listens on localhost. Set QC_PASSCODE before exposing
#: it to a network: anyone who can open the page can claim or release a
#: channel, and claiming is what starts and stops monitoring.
#:
#: One shared secret rather than per-person accounts: there are dozens of
#: commentators, no user database, and the page already asks who you are when
#: you claim. Accounts would be a login system to maintain for no extra safety
#: at this stage.
PASSCODE = os.environ.get("QC_PASSCODE", "").strip()
COOKIE = "qc_auth"
#: How long a commentator stays signed in on their own machine.
COOKIE_DAYS = 30
#: Open without a login, so container health checks and uptime probes do not
#: need the secret.
OPEN_PATHS = {"/healthz", "/login"}


def _cookie_value() -> str:
    """A value only someone holding the passcode can produce."""
    return hmac.new(PASSCODE.encode(), b"commentary-qc", hashlib.sha256).hexdigest()


def signed_in(request: Request) -> bool:
    if not PASSCODE:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _cookie_value())


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MMC Commentary QC</title>
<style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b0c;color:#f7f7f2;
  font:15px/1.5 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
form{background:#151517;border:1px solid #2a2a30;padding:28px;width:min(360px,92vw);
  box-shadow:0 14px 30px -20px #000}
h1{margin:0 0 4px;font:700 22px/1.1 "Barlow Condensed","Arial Narrow",sans-serif;
  letter-spacing:.08em;text-transform:uppercase}
h1 span{color:#ffd400}
p{margin:0 0 18px;color:#b9b9b1;font-size:13.5px}
label{display:block;font-size:12.5px;letter-spacing:.12em;text-transform:uppercase;color:#84847b;margin-bottom:6px}
input{width:100%;box-sizing:border-box;background:#101012;border:1px solid #2a2a30;color:#f7f7f2;
  padding:10px 12px;font-size:16px}
button{width:100%;margin-top:16px;padding:12px;border:0;background:#ffd400;color:#0b0b0c;
  font:700 16px/1 "Barlow Condensed","Arial Narrow",sans-serif;letter-spacing:.12em;
  text-transform:uppercase;cursor:pointer}
.err{margin-top:12px;color:#ff4d3d;font-size:13.5px}
</style></head><body>
<form method="post" action="/login">
  <h1>MMC <span>Commentary QC</span></h1>
  <p>Enter the team passcode to continue.</p>
  <label for="p">Passcode</label>
  <input id="p" name="passcode" type="password" autofocus autocomplete="current-password">
  <button type="submit">Sign in</button>
  __ERROR__
</form></body></html>"""


class ClaimIn(BaseModel):
    channel: str
    commentator: str = ""
    fixture: str = ""
    #: The match picked from the fixture list, if any.
    event_id: str = ""
    #: Take the channel over from whoever holds it. Without this, claiming a
    #: channel someone else has is refused, so nobody is displaced silently.
    force: bool = False


def _slug_sort(slug: str) -> tuple[str, int]:
    family, _, num = slug.rpartition("-")
    try:
        return (family, int(num))
    except ValueError:
        return (family, 0)


def content_state(metric: dict | None, active: list[str], loop_line: float = 0.6,
                  quiet_line: float = -40.0, speech_floor: float = 0.10) -> str:
    """What a measured channel is actually carrying.

    "The stream is up" is not the useful question. Every channel on the platform
    is up for hours either side of its match; what matters is whether it is
    carrying commentary or a holding loop.

    The board is allowed to be faster than the alerts: it calls a holding loop
    as soon as the repeat score says so, without the alert's 3-minute hold. And
    until the repeat test has its ~3 minutes of history it says "checking"
    rather than guessing -- right after a start, a pre-match clip otherwise
    shows as live commentary for several minutes.
    """
    if "stream_down" in active or not metric:
        return "nosignal"
    if "silence" in active or (metric.get("lufs_short") or -99) < -50:
        return "silent"
    # Same guard as the rule: quiet and speechless is a dead-ish input, not a
    # clip, whatever the repeat test says.
    quiet_and_silentish = ((metric.get("lufs_minute") or 0) < quiet_line
                           and (metric.get("speech_ratio_med") or 0) < speech_floor)
    if not quiet_and_silentish and (
        "filler_playing" in active or (metric.get("loop_score") or 0) >= loop_line
    ):
        return "filler"
    if not metric.get("loop_lag_s"):
        return "checking"
    if (metric.get("crowd_lufs") or -99) > -40:
        return "onair"
    return "dry"


def verdict(active: list[str]) -> dict:
    """Turn the firing rules into one thing to say."""
    for rule, state, headline, action in ADVICE:
        if rule in active:
            return {"state": state, "headline": headline, "action": action, "rule": rule}
    return {"state": "good", "headline": "GOOD", "action": "No faults detected", "rule": None}


def in_context(v: dict, match: dict | None, filler_now: bool, claimed: bool = False) -> dict:
    """Judge a holding clip against the match clock.

    A holding clip before kick-off or at half-time is the normal state of a
    channel, not a fault; the same clip while the ball is in play means the
    commentary is not going out, which is the most serious fault there is.
    Only possible when the commentator picked their match from the list.
    """
    is_filler = filler_now or v.get("rule") == "filler_playing"
    if not is_filler:
        return v
    if not match:
        # A commentator who claimed the channel told us they are on air, so a
        # clip there is still a fault even without a match picked.
        if claimed:
            return v
        # Otherwise nothing says commentary is expected: a clip on an unclaimed
        # channel is one waiting for its match. Without this the board reads
        # "14 faults" every morning, which teaches people to ignore it.
        return {"state": "wait", "headline": "HOLDING CLIP",
                "action": "Waiting for a match - claim the channel when you start",
                "rule": "filler_playing"}
    phase = match.get("phase")
    if phase == "upcoming":
        return {"state": "wait", "headline": "PRE-MATCH CLIP",
                "action": "Normal before kick-off - switch to LIVE when you start",
                "rule": "filler_playing"}
    if phase == "halftime":
        return {"state": "wait", "headline": "HALF-TIME CLIP",
                "action": "Normal at half-time - switch back to LIVE for the restart",
                "rule": "filler_playing"}
    if phase in ("finished", "off"):
        return {"state": "wait", "headline": "MATCH OVER",
                "action": "The holding clip is playing after the match", "rule": "filler_playing"}
    return {"state": "bad", "headline": "HOLDING LOOP",
            "action": "The match is live - switch back to LIVE now", "rule": "filler_playing"}


def create_app(
    db: str, channels_file: str | None, settings: Settings, claims_db: str = "claims.db"
) -> FastAPI:
    # Two files, one writer each. The recorder owns the measurements and writes
    # several rows a second; a second process cannot create tables in that file
    # or reliably write to it. Claims are small, separate, and owned here.
    store = EventStore(db, schema=False).start()
    claims = EventStore(claims_db).start()
    if channels_file and Path(channels_file).exists():
        registry = Registry.load(channels_file, settings.source)
    else:
        registry = Registry(default_channels(), settings.source)
    # Discovery is wider than MMC's list, so a commentator working on a channel
    # nobody configured can still find and claim it.
    scanner = LiveScanner(settings.source).start()
    feed = FixtureFeed().start()

    # The translation layer. Its execution modes are built once per process,
    # because a mode holds the thing that has to outlive a request: its
    # spending budget. Runs go in a third database file -- the page produces
    # them, so the page owns the file, which is the one-writer-per-file rule
    # the rest of this system already runs on.
    translate_settings = TranslateSettings.from_env()
    translate_control.ensure(claims_db)
    translate_modes = ModeRegistry(translate_settings)
    # Beside the claims file, so it lands on the same named volume. Never a
    # host bind mount: SQLite in WAL mode needs shared memory a Windows mount
    # cannot provide, which cost this project a day of a page reporting a
    # false outage while the data was in fact being written perfectly.
    translate_runs = RunStore(str(Path(claims_db).parent / "translate.db"))
    th = settings.thresholds
    targets = {
        "quiet": th.too_quiet_lufs, "quiet_clear": th.too_quiet_clear_lufs,
        "loud": th.too_loud_lufs, "loud_clear": th.too_loud_clear_lufs,
        "target": th.target_lufs, "peak": th.true_peak_dbtp,
        "balance_low": th.crowd_dominating_db, "balance_high": th.voice_dominates_db,
    }

    translate_router = translate_api.build_router(
        translate_settings,
        translate_modes,
        translate_runs,
        registry=registry,
        source=settings.source,
        ffmpeg_bin=settings.ffmpeg_bin,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        scanner.stop()
        feed.stop()
        store.close()
        claims.close()
        # Live translation sessions first: each one holds an ffmpeg pull and a
        # metered socket, and a session the process forgot about would keep
        # both open -- and keep being billed -- until the container died.
        stopped = getattr(translate_router, "live_runs", None)
        if stopped is not None:
            count = stopped.stop_all("the server is shutting down")
            if count:
                log.info("stopped %d live translation session(s)", count)
        translate_runs.close()

    app = FastAPI(title="Commentary QC", lifespan=lifespan)
    app.include_router(translate_router)
    page = Path(__file__).parent / "static" / "claim.html"
    translate_page = Path(__file__).parent / "static" / "translate.html"

    @app.middleware("http")
    async def require_passcode(request: Request, call_next):
        if PASSCODE and request.url.path not in OPEN_PATHS and not signed_in(request):
            # A browser gets the login page; the page's own fetch() calls get
            # a 401 so the JavaScript shows "cannot reach the server" instead
            # of quietly rendering a login form as data.
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "not signed in"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Open on purpose: health checks must not need the passcode."""
        return JSONResponse({"ok": True})

    @app.get("/login", response_class=HTMLResponse)
    def login_form() -> str:
        return LOGIN_PAGE.replace("__ERROR__", "")

    @app.post("/login")
    def login(passcode: str = Form("")) -> object:
        if not PASSCODE or hmac.compare_digest(passcode.strip(), PASSCODE):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, _cookie_value(), max_age=COOKIE_DAYS * 86400,
                            httponly=True, samesite="lax")
            return resp
        log.warning("failed sign-in attempt")
        return HTMLResponse(
            LOGIN_PAGE.replace("__ERROR__", '<div class="err">That passcode is not right.</div>'),
            status_code=401,
        )

    def recorder(now: float) -> dict:
        """Is the recorder alive, and what is it doing?"""
        row = store.service("recorder")
        if not row:
            return {"running": False, "seen": False, "monitoring": [], "slack": "unknown"}
        info = row["info"]
        age = now - (row["last_seen"] or 0)
        return {
            "running": age < RECORDER_STALE_S and not info.get("stopped"),
            "seen": True,
            "since": row["started"],
            "last_seen": row["last_seen"],
            "monitoring": info.get("monitoring", []),
            "claimed": info.get("claimed", []),
            "slack": info.get("slack", "unknown"),
        }

    def snapshot() -> dict:
        """Every channel worth showing, with one derived state each."""
        now = time.time()
        rec = recorder(now)
        measuring = set(rec["monitoring"]) if rec["running"] else set()
        state = {r["channel"]: r for r in store.current_state()}
        open_claims = claims.active_claims()
        discovered = scanner.live()

        names = sorted(
            set(discovered) | set(registry.channels) | set(open_claims), key=_slug_sort
        )
        out = []
        for name in names:
            ch = registry.channels.get(name) or discovered.get(name) or describe(name)
            st = state.get(name) or {}
            active = list(st.get("active") or [])
            metric = st.get("last_metric")
            claim = open_claims.get(name)
            if claim:
                claim = dict(claim)
                match = feed.get(claim.get("event_id") or "")
                claim["match"] = match.to_dict() if match else None
            broadcasting = name in discovered
            measured = name in measuring

            if measured:
                stale = now - (st.get("last_seen") or 0) > METRIC_STALE_S
                # HLS takes several seconds to deliver the first audio. Until
                # the first reading, a fresh monitor is starting, not failing.
                warming = not metric and now - (st.get("since") or now) < WARMUP_S
                if warming:
                    active = [a for a in active if a != "stream_down"]
                    sid = "starting"
                else:
                    if (not st.get("live") or stale) and "stream_down" not in active:
                        active = ["stream_down"] + active
                    sid = content_state(metric, active, th.filler_loop_score,
                                        th.too_quiet_lufs, th.filler_quiet_speech_floor)
            elif claim:
                sid = "starting" if broadcasting and rec["running"] else "waiting"
            elif broadcasting:
                sid = "unwatched"
            else:
                sid = "down"

            v = None
            label = STATE_LABELS[sid]
            if measured and sid not in ("starting", "checking"):
                v = in_context(verdict(active), claim and claim.get("match"),
                               sid == "filler", claimed=bool(claim))
                if sid == "filler" and v["state"] == "wait":
                    label = v["headline"].capitalize()
            fault = v["headline"] if v and v["state"] in ("bad", "warn") else None
            out.append({
                "channel": name,
                "label": ch.label,
                "short": ch.short,
                "language": ch.language,
                # One of MMC's channels: configured, or claimed by a commentator.
                "mmc": name in registry.channels or bool(claim),
                "broadcasting": broadcasting,
                "configured": name in registry.channels,
                "measured": measured,
                "state": sid,
                "state_label": label,
                "claim": claim,
                "metric": metric if measured else None,
                "active": active if measured else [],
                "verdict": v,
                "fault": fault,
                "session": st.get("session"),
                "since": st.get("since"),
            })
        return {"now": now, "recorder": rec, "channels": out, "discovered": discovered}

    def match_lists(rows: list[dict], now: float) -> dict:
        claimed = {}
        for c in rows:
            cl = c["claim"]
            if cl and cl.get("event_id"):
                claimed[cl["event_id"]] = {"channel": c["channel"], "short": c["short"],
                                           "commentator": cl.get("commentator") or ""}
        live, upcoming, recent = [], [], []
        for m in feed.matches():
            d = m.to_dict()
            d["claimed"] = claimed.get(m.id)
            if m.phase in ("live", "halftime"):
                live.append(d)
            elif m.phase == "upcoming" and now - 3600 <= m.kickoff <= now + UPCOMING_DAYS * 86400:
                upcoming.append(d)
            elif m.phase == "finished" and m.kickoff >= now - 5 * 3600:
                recent.append(d)
        return {"live": live, "upcoming": upcoming, "recent": recent, "health": feed.health()}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page.read_text(encoding="utf-8")

    @app.get("/translate", response_class=HTMLResponse)
    def translate_view() -> str:
        """Shadow mode: try the model on a channel and listen to the result.

        A separate page rather than another panel on the commentator view. A
        commentator on air needs one instruction at a time; whoever is judging
        whether a model is worth publishing needs to hear both sides of a
        translation. Those are different jobs.
        """
        return translate_page.read_text(encoding="utf-8")

    @app.get("/api/channels")
    def channels() -> JSONResponse:
        """The dashboard: channels, a summary, matches and recent alerts."""
        snap = snapshot()
        rows = snap["channels"]
        mmc = [c for c in rows if c["mmc"]]
        counts = Counter(c["state"] for c in mmc)
        matches = match_lists(rows, snap["now"])
        labels = {c["channel"]: c["short"] for c in mmc}
        feed_rows = []
        for a in store.recent_alerts(40):
            if a["channel"] not in labels:
                continue
            feed_rows.append({
                "wall": a["wall"], "channel": a["channel"], "short": labels[a["channel"]],
                "event": a["event"], "rule": a["rule"], "severity": a["severity"],
                "headline": HEADLINES.get(a["rule"], a["rule"].replace("_", " ").upper()),
                "message": a["message"],
            })
        return JSONResponse({
            "channels": rows,
            "summary": {
                "broadcasting": len(snap["discovered"]),
                "mmc_broadcasting": sum(1 for c in mmc if c["broadcasting"]),
                "others": sum(1 for c in rows if not c["mmc"] and c["broadcasting"]),
                "states": counts,
                "claimed": sum(1 for c in mmc if c["claim"]),
                "measured": sum(1 for c in mmc if c["measured"]),
                "faults": sum(1 for c in mmc if c["fault"]),
                "matches_live": len(matches["live"]),
                "next_match": matches["upcoming"][0] if matches["upcoming"] else None,
                "scan": scanner.health(),
                "recorder": snap["recorder"],
            },
            "matches": matches,
            "feed": feed_rows[:20],
            "targets": targets,
            "now": snap["now"],
        })

    @app.post("/api/claim")
    def claim(body: ClaimIn) -> JSONResponse:
        name = body.channel.strip()
        if name not in registry.channels and name not in scanner.live():
            raise HTTPException(404, f"{name!r} is not broadcasting and not configured")
        held = claims.active_claims().get(name)
        if held and not body.force:
            raise HTTPException(409, {
                "message": f"{held.get('commentator') or 'Someone'} already has this channel",
                "claim": held,
            })
        match = feed.get(body.event_id.strip())
        fixture = body.fixture.strip() or (match.name if match else "")
        claims.claim(name, body.commentator.strip()[:60], fixture[:120],
                     match.id if match else "", match.kickoff if match else None)
        log.info("%s claimed by %s (%s)", name, body.commentator, fixture)
        return JSONResponse({"ok": True})

    @app.post("/api/release")
    def release(body: ClaimIn) -> JSONResponse:
        claims.release(body.channel)
        log.info("%s released", body.channel)
        return JSONResponse({"ok": True})

    @app.get("/api/status/{channel}")
    def status(channel: str) -> JSONResponse:
        """Live state for one channel: the verdict plus the numbers behind it."""
        snap = snapshot()
        row = next((c for c in snap["channels"] if c["channel"] == channel), None)
        if row is None:
            ch = describe(channel)
            row = {"channel": channel, "label": ch.label, "short": ch.short,
                   "language": "", "measured": False, "state": "down", "claim": None,
                   "metric": None, "active": [], "verdict": None, "session": None,
                   "since": None, "broadcasting": False}
        rec = snap["recorder"]

        if row["measured"] and row["verdict"]:
            v = row["verdict"]
        elif row["measured"] and row["state"] == "checking":
            v = {"state": "wait", "headline": "CHECKING",
                 "action": "Listening - the full picture takes about 3 minutes", "rule": None}
        elif row["measured"] or (row["claim"] and row["broadcasting"] and rec["running"]):
            v = {"state": "wait", "headline": "STARTING",
                 "action": "Listening - first reading in a few seconds", "rule": None}
        elif not rec["running"]:
            v = {"state": "unknown", "headline": "NOT MEASURING",
                 "action": "The recorder is not running - tell the producer", "rule": None}
        elif row["claim"]:
            v = {"state": "wait", "headline": "WAITING FOR YOUR STREAM",
                 "action": "Measuring starts as soon as the channel is broadcasting",
                 "rule": None}
        else:
            v = {"state": "unknown", "headline": "NOT BEING MEASURED",
                 "action": "Claim this channel to start monitoring", "rule": None}

        alerts = []
        if row["measured"] and row.get("session"):
            alerts = store.recent_alerts(10, channel, session=row["session"])
        return JSONResponse({
            **row,
            "verdict": v,
            "alerts": alerts,
            "targets": targets,
            "now": snap["now"],
        })

    @app.get("/api/report/{channel}")
    def report(channel: str, minutes: float = 180.0) -> JSONResponse:
        """What a broadcast looked like end to end: the producer's view."""
        now = time.time()
        minutes = max(5.0, min(minutes, 720.0))
        data = build_report(store, channel, now - minutes * 60, now, th)
        ch = registry.channels.get(channel)
        claim = claims.active_claims().get(channel)
        if claim:
            claim = dict(claim)
            match = feed.get(claim.get("event_id") or "")
            claim["match"] = match.to_dict() if match else None
        data["label"] = ch.label if ch else describe(channel).label
        data["claim"] = claim
        data["minutes"] = minutes
        data["now"] = now
        return JSONResponse(data)

    @app.get("/api/history/{channel}")
    def history(channel: str, minutes: float = 10.0) -> JSONResponse:
        """Loudness over the last few minutes, for the trend chart."""
        now = time.time()
        minutes = max(1.0, min(minutes, 120.0))
        return JSONResponse({
            "points": store.history(channel, now - minutes * 60),
            "from": now - minutes * 60, "now": now, "targets": targets,
        })

    return app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="commentary-qc-web")
    p.add_argument("--db", default="commentary_qc.db", help="The recorder's database, read-only.")
    p.add_argument("--channels", default="channels.json")
    p.add_argument("--claims-db", default="claims.db",
                   help="Separate file for claims, so it has a single writer.")
    p.add_argument("--config", help="JSON settings file.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8642)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    import uvicorn

    app = create_app(args.db, args.channels, Settings.load(args.config), args.claims_db)
    log.info("commentator page on http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
