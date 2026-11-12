"""One commentator, several languages, at the same time.

This is the shape the product actually wants. MMC pays one person to call a
match in English; the markets that want it are Greek, German, Italian and
French. Translating the same feed once per language, sequentially, answers a
research question. Translating it into all of them at once is the thing you
would sell.

A group is one channel, one audio pull, and one model session per output
language. The pull is shared through :class:`~.fanout.SharedSource`, so the
legs are reading the same bytes at the same moment -- which matters more than
it sounds, because the point of running them together is comparing them, and
two independent pulls of a live HLS stream can start seconds apart.

Each leg is an ordinary :class:`~.live.LiveRun` and knows nothing about the
others. That is deliberate: a leg that fails takes only its own language
down, and the group reports it as one language missing rather than as a
broken run. A Greek session refused for an unsupported language should not
cost you the German translation that was working.

**Why a group can be refused outright.** Some providers document a concurrency
of one -- Camb's free plan does -- and a second session opened beside the
first is refused by the far end *after* the audio has started flowing and the
meter has started running. Finding that out here, before anything is spent,
is the same reasoning that put the single-session limit in
:class:`~.live.LiveRunner`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from commentary_qc.translate.app.fanout import SharedSource
from commentary_qc.translate.app.live import DONE, FAILED, LiveRun
from commentary_qc.translate.core.identify import (
    FixtureRef,
    identify_language,
    identify_match,
)
from commentary_qc.translate.core.models import NotConfigured, TranslateError

log = logging.getLogger(__name__)

#: Most languages one group may produce at once. Not a technical limit on our
#: side -- the fan-out would feed twenty -- but every leg is a metered session,
#: so the cost of a mistake scales with this number.
DEFAULT_MAX_TARGETS = 4


class TooManyTargets(TranslateError):
    """More output languages were asked for than can be run at once.

    Its own type because, like :class:`~.live.AtCapacity`, it is about
    arithmetic rather than breakage: the page should say which number was
    exceeded and offer to drop a language, not report a fault.
    """


def _provider_session_limit(provider: Any) -> int:
    """How many live sessions this provider will hold open, or 0 for unknown.

    Asked of the adapter rather than assumed here, because the number is a
    fact about one vendor's plan. An adapter that does not know says nothing
    and gets the benefit of the doubt -- refusing on a limit we invented
    would be worse than letting the far end answer.
    """
    try:
        value = int(getattr(provider, "max_live_sessions", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


class LiveGroup:
    """Several live translations of one channel, started and stopped together."""

    def __init__(
        self,
        channel: str,
        wiring: Any,
        targets: list[str],
        *,
        audio: Any,
        source_language: str = "auto",
        live_mode: str = "",
        max_seconds: float = 60.0,
        max_targets: int = DEFAULT_MAX_TARGETS,
        fixtures: Any = None,
        configured_language: str = "",
        on_finish: Any | None = None,
    ) -> None:
        wanted = [t.strip().lower() for t in targets if (t or "").strip()]
        # Order preserved, duplicates dropped: asking for Greek twice would
        # open two metered sessions producing the same audio.
        seen: set[str] = set()
        self.targets = [t for t in wanted if not (t in seen or seen.add(t))]
        if not self.targets:
            raise NotConfigured("say which language or languages to translate into")
        limit = max(1, int(max_targets))
        if len(self.targets) > limit:
            raise TooManyTargets(
                f"{len(self.targets)} languages at once, and the limit is "
                f"{limit}. Each one is a separate metered session, so the "
                f"limit is about cost rather than capability "
                f"(QC_LIVE_TARGETS_MAX raises it)."
            )

        provider = getattr(wiring, "provider", None)
        allowed = _provider_session_limit(provider)
        if allowed and len(self.targets) > allowed:
            name = getattr(provider, "name", "this model")
            raise TooManyTargets(
                f"{name} holds {allowed} live session"
                f"{'' if allowed == 1 else 's'} open at a time, so it cannot "
                f"produce {len(self.targets)} languages at once. Run them one "
                f"after another, or pick a model that allows more."
            )

        self.id = uuid.uuid4().hex[:16]
        self.channel = channel
        self.wiring = wiring
        self.source_language = (source_language or "auto").strip().lower()
        self.configured_language = (configured_language or "").strip().lower()
        self.max_seconds = max(5.0, float(max_seconds))
        self.started = time.time()
        self.ended = 0.0
        self.error = ""
        self.notes: list[str] = []
        self._fixtures = fixtures
        self._on_finish = on_finish
        self._lock = threading.Lock()
        self._filed = False
        #: The last reading from the shared pull. See snapshot().
        self._last_pull: list[dict[str, Any]] = []

        # One shared pull for the whole group. Built here rather than passed
        # in, because its lifetime is the group's: the last leg to stop takes
        # the ffmpeg process down with it.
        #
        # `expect` is the number of languages, and it is what makes the legs
        # comparable: the pull holds off reading until every leg has opened
        # its session and joined, so all of them hear the same opening
        # second. Without it the leg whose session booted fastest gets a
        # head start measured in seconds.
        self.audio = SharedSource(audio, expect=len(self.targets))
        self.legs: list[LiveRun] = [
            LiveRun(
                channel,
                wiring,
                source_language=self.source_language,
                target_language=target,
                live_mode=live_mode,
                audio=self.audio,
                max_seconds=self.max_seconds,
                on_finish=self._leg_finished,
            )
            for target in self.targets
        ]
        for leg in self.legs:
            leg.group = self.id

    # -- lifecycle -------------------------------------------------------
    def budget_needed(self) -> float:
        """Seconds the whole group will spend if every leg runs to its limit.

        Checked before anything starts. A group that begins Greek and German
        and is then refused Italian halfway through has spent real credit to
        produce an incomplete comparison, which is the worst of both.
        """
        return self.max_seconds * len(self.legs)

    def start(self) -> "LiveGroup":
        budget = getattr(self.wiring, "budget", None)
        if budget is not None:
            allowed, why = budget.allow(self.channel, self.budget_needed())
            if not allowed:
                raise TranslateError(
                    f"{len(self.legs)} languages for {self.max_seconds:.0f}s "
                    f"each needs {self.budget_needed():.0f}s of audio: {why}"
                )
        for leg in self.legs:
            leg.start()
        return self

    def stop(self, why: str = "stopped from the page") -> None:
        for leg in self.legs:
            if leg.running:
                leg.stop(why)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        for leg in self.legs:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not leg.wait(left):
                return False
        return True

    @property
    def running(self) -> bool:
        return any(leg.running for leg in self.legs)

    def _leg_finished(self, leg: LiveRun) -> None:
        """Called by each leg's own thread as it winds up.

        The group is finished when its last leg is, and the hook that files
        it must run exactly once however many legs there are -- hence the
        flag rather than a count, which would be wrong if a leg failed before
        it ever started.
        """
        if self.running:
            return
        with self._lock:
            if self._filed:
                return
            self._filed = True
        self.ended = time.time()
        failures = [l for l in self.legs if l.phase == FAILED]
        if len(failures) == len(self.legs):
            self.error = failures[0].error or "every language failed"
        if self._on_finish is not None:
            try:
                self._on_finish(self)
            except Exception:  # noqa: BLE001 - filing it must not undo it
                log.exception("the finish hook for live group %s failed", self.id)

    # -- what was heard --------------------------------------------------
    def heard_text(self) -> str:
        """The source commentary, from whichever leg heard the most of it.

        Every leg hears the same audio, but not every provider emits an input
        transcript for every session, and a leg that failed early heard less.
        Taking the longest is simply taking the best-informed witness.
        """
        best = ""
        for leg in self.legs:
            text = leg.heard_text()
            if len(text) > len(best):
                best = text
        return best

    def _fixture_refs(self) -> list[FixtureRef]:
        """Today's fixtures, as the plain values the domain matches against.

        Tolerant by design: the fixture feed is a public scoreboard with no
        key and it is allowed to be unreachable. No fixtures means no match
        guess, which is a missing feature rather than a failed run.
        """
        source = self._fixtures
        if source is None:
            return []
        try:
            if hasattr(source, "matches"):
                rows = source.matches()
            elif callable(source):
                rows = source()
            else:
                # A plain sequence, which is what a caller holding a fixed
                # list of fixtures passes -- and what a test passes.
                rows = list(source)
        except Exception:  # noqa: BLE001 - a guess must not break a run
            log.debug("could not read fixtures for the match guess")
            return []
        out = []
        for row in rows or []:
            get = row.get if isinstance(row, dict) else lambda k, d="": getattr(row, k, d)
            home, away = get("home", ""), get("away", "")
            if not home or not away:
                continue
            out.append(
                FixtureRef(
                    id=str(get("id", "") or get("match_id", "")),
                    home=str(home),
                    away=str(away),
                    competition=str(get("competition", "") or ""),
                    state=str(get("state", "") or get("phase", "") or ""),
                )
            )
        return out

    def identity(self) -> dict[str, Any]:
        """What the commentary says this channel is: a language and a match.

        Computed on demand from the transcript so far rather than stored. It
        improves as the broadcast goes on -- the team names are usually said
        in the first minute and the language is settled within a sentence --
        and a value that only gets better is not worth caching.
        """
        heard = self.heard_text()
        language = identify_language(heard)
        match = identify_match(heard, self._fixture_refs())
        # "auto" is what we tell a provider when nobody recorded this
        # channel's language. It is not a claim about the channel, so it is
        # not shown as one.
        configured = self.configured_language
        if configured in ("auto", "unknown"):
            configured = ""
        mismatch = bool(configured) and not language.agrees_with(configured)
        out: dict[str, Any] = {
            "heard_words": len(heard.split()),
            "language": language.to_dict(),
            "configured": configured,
            "match": match.to_dict(),
        }
        if mismatch:
            out["warning"] = (
                f"this channel is listed as {configured} but the commentary "
                f"sounds like {language.name}"
            )
        out["language_matches"] = not mismatch
        return out

    # -- reporting -------------------------------------------------------
    def snapshot(self, captions: int = 40) -> dict[str, Any]:
        legs = [leg.snapshot(captions) for leg in self.legs]
        # The shared source forgets a pull once its last listener has gone,
        # which is right -- it holds nothing open -- but it would also mean
        # the evidence that there was only one pull disappears the moment the
        # run ends, exactly when somebody reads the result. So the last
        # reading is kept.
        pull = self.audio.stats()
        if pull:
            # Stored with `running` cleared: it is a last reading, and a
            # finished run reporting a running pull would be a small lie
            # that somebody would eventually try to act on.
            self._last_pull = [dict(p, running=False) for p in pull]
        spoken = sum(l.get("spoken_seconds", 0.0) for l in legs)
        sent = max([l.get("sent_seconds", 0.0) for l in legs] or [0.0])
        elapsed = (self.ended or time.time()) - self.started
        return {
            "id": self.id,
            "channel": self.channel,
            "running": self.running,
            "error": self.error,
            "started": self.started,
            "elapsed_s": round(elapsed, 1),
            "limit_s": self.max_seconds,
            "targets": list(self.targets),
            "source_language": self.source_language,
            # One pull, and this is the proof of it: the sent figure is the
            # audio each leg received, not the sum, because they received the
            # same audio.
            "sent_seconds": round(sent, 1),
            "spoken_seconds": round(spoken, 1),
            "billed_seconds": round(
                sum(l.get("sent_seconds", 0.0) for l in legs), 1
            ),
            "legs": legs,
            "pull": pull or self._last_pull,
            "identity": self.identity(),
            "notes": list(self.notes),
        }

    def summary(self) -> dict[str, Any]:
        """The short form, for a list of past groups."""
        return {
            "id": self.id,
            "channel": self.channel,
            "running": self.running,
            "targets": list(self.targets),
            "elapsed_s": round((self.ended or time.time()) - self.started, 1),
            "done": sum(1 for l in self.legs if l.phase == DONE),
            "failed": sum(1 for l in self.legs if l.phase == FAILED),
            "error": self.error,
        }


__all__ = ["DEFAULT_MAX_TARGETS", "LiveGroup", "TooManyTargets"]
