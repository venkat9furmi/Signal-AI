"""Match dates, kick-off times and live state for the competitions MMC covers.

The vendor's schedule is off limits (user decision, 2026-09-15), so fixtures come
from ESPN's public football scoreboard: no key, no login. Checked against MMC's
own schedule for 15-20 Sep 2026: 35 of 39 broadcasts found, every kick-off time
identical. Its one gap is the Greek Cup, which it does not carry.

What it cannot say is which *channel* carries a match -- only the vendor knows
that. The commentator supplies it by picking their match when they claim.

The endpoint is public but unofficial and can change without notice, so every
failure degrades to "match times unavailable", never to a broken page.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

#: (ESPN league code, name shown on the page): the competitions on MMC's
#: schedule, plus the other European ones its language channels cover.
#: Domestic cups are in here because MMC's channels carry them: on 16 Sep the
#: English channels were on four Carabao Cup ties. Codes that do not resolve
#: (Portuguese and Greek cups, cricket) simply have no feed -- those matches are
#: typed in by hand on the claim page.
LEAGUES: tuple[tuple[str, str], ...] = (
    ("uefa.champions", "Champions League"),
    ("uefa.europa", "Europa League"),
    ("uefa.europa.conf", "Conference League"),
    ("eng.1", "Premier League"),
    ("eng.2", "Championship"),
    ("eng.league_cup", "League Cup"),
    ("eng.fa", "FA Cup"),
    ("ita.1", "Serie A"),
    ("ita.coppa_italia", "Coppa Italia"),
    ("por.1", "Primeira Liga"),
    ("gre.1", "Greek Super League"),
    ("ger.1", "Bundesliga"),
    ("ger.dfb_pokal", "German Cup"),
    ("fra.1", "Ligue 1"),
    ("fra.coupe_de_france", "Coupe de France"),
    ("esp.1", "La Liga"),
    ("esp.copa_del_rey", "Copa del Rey"),
)
URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={day}"
_OFF = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_ABANDONED", "STATUS_SUSPENDED"}


@dataclass(slots=True)
class Match:
    id: str
    league: str
    competition: str
    #: Kick-off, epoch seconds (UTC).
    kickoff: float
    home: str
    away: str
    #: upcoming | live | halftime | finished | off (postponed, cancelled...)
    phase: str
    #: Match clock while live ("67'"), otherwise ESPN's short status.
    detail: str
    home_score: str | None = None
    away_score: str | None = None

    @property
    def name(self) -> str:
        return f"{self.home} v {self.away}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["name"] = self.name
        return d


def _phase(status: dict) -> str:
    kind = status.get("type") or {}
    if kind.get("name") in _OFF:
        return "off"
    if kind.get("name") == "STATUS_HALFTIME":
        return "halftime"
    state = kind.get("state")
    if state == "in":
        return "live"
    if state == "post":
        return "finished"
    return "upcoming"


def parse(code: str, competition: str, event: dict) -> Match | None:
    """One ESPN event -> Match, or None if it is not shaped as expected."""
    try:
        comp = event["competitions"][0]
        side = {t["homeAway"]: t for t in comp["competitors"]}
        status = event.get("status") or comp.get("status") or {}
        phase = _phase(status)
        kind = status.get("type") or {}
        detail = kind.get("shortDetail") or kind.get("detail") or ""
        if phase == "live" and status.get("displayClock"):
            detail = status["displayClock"]
        scored = phase in ("live", "halftime", "finished")
        return Match(
            id=str(event["id"]),
            league=code,
            competition=competition,
            kickoff=datetime.fromisoformat(event["date"].replace("Z", "+00:00")).timestamp(),
            home=side["home"]["team"]["displayName"],
            away=side["away"]["team"]["displayName"],
            phase=phase,
            detail=detail,
            home_score=side["home"].get("score") if scored else None,
            away_score=side["away"].get("score") if scored else None,
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return None


class FixtureFeed:
    """Keeps a cached list of recent, live and upcoming matches.

    Today's scoreboards are refreshed every minute (live minute, half-time,
    score); the full week ahead every half hour. About 20 small requests a
    minute, in a background thread.
    """

    def __init__(
        self,
        leagues: tuple[tuple[str, str], ...] = LEAGUES,
        days_ahead: int = 7,
        live_every_s: float = 60.0,
        full_every_s: float = 1800.0,
    ) -> None:
        self.leagues = leagues
        self.days_ahead = days_ahead
        self.live_every_s = live_every_s
        self.full_every_s = full_every_s
        self._matches: dict[str, Match] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._checked = 0.0
        self._errors = 0
        self._requests = 0

    def start(self) -> "FixtureFeed":
        threading.Thread(target=self._run, name="fixtures", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # -- reads ------------------------------------------------------------
    def matches(self) -> list[Match]:
        with self._lock:
            return sorted(self._matches.values(), key=lambda m: (m.kickoff, m.competition))

    def get(self, match_id: str) -> Match | None:
        if not match_id:
            return None
        with self._lock:
            return self._matches.get(match_id)

    def health(self) -> dict:
        with self._lock:
            ok = self._checked > 0 and self._errors < max(1, self._requests // 2)
            return {"ok": ok, "checked_at": self._checked, "errors": self._errors,
                    "requests": self._requests}

    # -- refresh ----------------------------------------------------------
    def _fetch(self, job: tuple[str, str, str]) -> list[Match] | None:
        code, competition, day = job
        # No custom User-Agent: the feed answers 403 to one, and urllib's own
        # default is accepted. It also rate-limits, so a refusal is logged and
        # left alone rather than retried in a tight loop.
        req = urllib.request.Request(URL.format(code=code, day=day))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                events = json.load(resp).get("events") or []
        except Exception as exc:  # noqa: BLE001 - any failure is "no data"
            log.debug("fixtures %s %s: %s", code, day, exc)
            return None
        return [m for m in (parse(code, competition, e) for e in events) if m]

    def refresh(self, days: list[str]) -> None:
        jobs = [(code, comp, day) for code, comp in self.leagues for day in days]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self._fetch, jobs))
        got = {m.id: m for batch in results if batch for m in batch}
        errors = sum(1 for r in results if r is None)
        cutoff = time.time() - 2 * 86400
        with self._lock:
            self._matches.update(got)
            self._matches = {k: m for k, m in self._matches.items() if m.kickoff >= cutoff}
            self._checked = time.time()
            self._errors = errors
            self._requests = len(jobs)
        if errors:
            log.warning("fixtures: %d of %d requests failed", errors, len(jobs))

    def _run(self) -> None:
        last_full = float("-inf")
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            if time.monotonic() - last_full >= self.full_every_s:
                span = range(-1, self.days_ahead + 1)
                last_full = time.monotonic()
            else:
                span = range(-1, 1)
            days = [(now + timedelta(days=d)).strftime("%Y%m%d") for d in span]
            try:
                self.refresh(days)
            except Exception:  # noqa: BLE001
                log.exception("fixture refresh failed")
            self._stop.wait(self.live_every_s)
