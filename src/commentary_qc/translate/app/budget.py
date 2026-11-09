"""The guard on the free credits.

Camb's free tier is finite and this pipeline is capable of spending it in an
afternoon: 52 channels, a segment every fifteen seconds, running unattended
overnight. The failure mode is not a crash -- it is a silent bill, or worse, a
dead feature on the morning of a real match because a holding clip consumed
the month's allowance at 3 a.m.

So spending is bounded before the fact rather than reported after it. Two
caps, because they fail differently:

* **per day** -- the headline allowance.
* **per channel** -- one stuck channel transcribing a loop cannot starve the
  other 51. Without this the first channel to go wrong takes everything.

A :class:`Budget` also latches shut when the provider itself says no. There is
no point asking a second time, and a log full of refusals hides the one line
that explains why nothing works.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


def _utc_day(now: float) -> int:
    return int(now // 86400)


class Budget:
    """How much audio may still be sent, and to which channel."""

    def __init__(
        self,
        daily_minutes: float,
        per_channel_minutes: float = 0.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.daily_s = max(0.0, daily_minutes * 60.0)
        self.per_channel_s = max(0.0, per_channel_minutes * 60.0)
        self._clock = clock
        self._lock = threading.Lock()
        self._day = _utc_day(clock())
        self._spent = 0.0
        self._by_channel: dict[str, float] = {}
        #: Set when the provider refuses to bill us. Only cleared by hand.
        self.blocked_reason = ""
        #: Counted so the page can say "12 segments skipped" rather than
        #: leaving a gap in the captions that looks like silence on air.
        self.refused = 0

    # -- the one question that matters -----------------------------------
    def allow(self, channel: str, seconds: float) -> tuple[bool, str]:
        """May we spend ``seconds`` of audio on ``channel``?

        Returns the verdict and, when it is no, the sentence to record with
        the skipped segment. A refusal is data, not an exception: the caption
        row says why it is missing.
        """
        with self._lock:
            self._roll(self._clock())
            if self.blocked_reason:
                self.refused += 1
                return False, self.blocked_reason
            if self.daily_s and self._spent + seconds > self.daily_s:
                self.refused += 1
                return False, (
                    f"daily budget reached: {self._spent / 60:.1f} of "
                    f"{self.daily_s / 60:.0f} minutes already sent today"
                )
            used = self._by_channel.get(channel, 0.0)
            if self.per_channel_s and used + seconds > self.per_channel_s:
                self.refused += 1
                return False, (
                    f"this channel's budget is used up: {used / 60:.1f} of "
                    f"{self.per_channel_s / 60:.0f} minutes"
                )
            return True, ""

    def charge(self, channel: str, seconds: float) -> None:
        """Record audio as spent.

        Called whether or not the result was usable: the provider charged for
        the call either way, and a budget that only counts successes is not a
        budget.
        """
        with self._lock:
            self._roll(self._clock())
            self._spent += max(0.0, seconds)
            self._by_channel[channel] = self._by_channel.get(channel, 0.0) + max(0.0, seconds)

    def seed(self, spent_s: float, by_channel: dict[str, float] | None = None) -> None:
        """Adopt today's spending as already recorded in the database.

        The recorder restarts -- a deploy, a reboot, Docker's restart policy --
        and an in-memory counter would start the day again each time, which
        turns a daily cap into a per-restart cap.
        """
        with self._lock:
            self._day = _utc_day(self._clock())
            self._spent = max(0.0, spent_s)
            self._by_channel = dict(by_channel or {})
        if spent_s:
            log.info("translation budget: %.1f minutes already spent today", spent_s / 60)

    def block(self, reason: str) -> None:
        """Stop spending until someone looks at it."""
        with self._lock:
            if self.blocked_reason:
                return
            self.blocked_reason = reason
        log.warning("translation budget closed: %s", reason)

    def unblock(self) -> None:
        with self._lock:
            self.blocked_reason = ""

    def _roll(self, now: float) -> None:
        """Reset at the UTC day boundary.

        UTC rather than local time: the recorder runs in a container whose
        clock is UTC, and a cap that moves with the host's daylight saving is
        a cap nobody can reason about.
        """
        day = _utc_day(now)
        if day != self._day:
            log.info(
                "translation budget rolled over: %.1f minutes spent on day %d",
                self._spent / 60, self._day,
            )
            self._day = day
            self._spent = 0.0
            self._by_channel.clear()

    # -- reporting -------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            self._roll(self._clock())
            cap = self.daily_s
            return {
                "spent_s": round(self._spent, 1),
                "spent_minutes": round(self._spent / 60, 2),
                "daily_minutes": round(cap / 60, 1),
                "remaining_minutes": round(max(0.0, cap - self._spent) / 60, 2) if cap else None,
                "fraction": round(self._spent / cap, 4) if cap else None,
                "per_channel_minutes": round(self.per_channel_s / 60, 1),
                "channels": {
                    k: round(v / 60, 2)
                    for k, v in sorted(self._by_channel.items(), key=lambda kv: -kv[1])[:20]
                },
                "blocked": bool(self.blocked_reason),
                "blocked_reason": self.blocked_reason,
                "refused": self.refused,
            }
