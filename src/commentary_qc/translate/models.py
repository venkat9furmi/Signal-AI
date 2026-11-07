"""The vocabulary of the translation layer.

These types are what the pipeline, the store and the page all speak. None of
them mentions a vendor: a provider adapter maps its own JSON onto these, which
is what lets Camb AI be swapped for anything else without touching the pipeline
or the page.

Everything here is frozen. A segment of audio travels between threads, and a
caption is read by the web process while the recorder writes more of them;
immutable values mean neither can be surprised by the other.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np


# -- what goes in ---------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AudioSegment:
    """One phrase of commentary, cut at a natural pause.

    Not a fixed block: a provider charges for audio and transcribes whole
    sentences far better than halves of two, so the segmenter waits for a gap
    in the speech before closing one. ``t0``/``t1`` are seconds since the
    monitor connected, which is what the QC metrics also use, so a caption can
    be lined up against the loudness trace that was measured at the same moment.
    """

    channel: str
    session: str
    seq: int
    t0: float
    t1: float
    wall: float
    samples: np.ndarray
    rate: int
    language: str = ""
    #: Mean speech ratio of the windows this segment spans. Audio with no
    #: speech in it is not worth sending to anybody.
    speech_ratio: float = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.t1 - self.t0)

    def describe(self) -> str:
        return f"{self.channel}#{self.seq} {self.duration_s:.1f}s @{self.t0:.0f}s"


# -- what comes back ------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Utterance:
    """One timed line of speech. Times are seconds within the segment."""

    text: str
    start_s: float | None = None
    end_s: float | None = None
    speaker: str = ""


@dataclass(frozen=True, slots=True)
class Transcript:
    """What a provider heard."""

    utterances: tuple[Utterance, ...] = ()
    language: str = ""
    provider: str = ""
    model: str = ""
    latency_s: float = 0.0
    #: Whatever the provider returned verbatim, kept for debugging a bad
    #: mapping without having to reproduce the call.
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def text(self) -> str:
        return " ".join(u.text.strip() for u in self.utterances if u.text.strip())

    def __bool__(self) -> bool:
        return bool(self.text)


@dataclass(frozen=True, slots=True)
class Translation:
    """The same lines in another language, in the order they were sent."""

    texts: tuple[str, ...] = ()
    source_language: str = ""
    target_language: str = ""
    provider: str = ""
    model: str = ""
    latency_s: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def text(self) -> str:
        return " ".join(t.strip() for t in self.texts if t.strip())


# -- what gets stored and shown -------------------------------------------
@dataclass(frozen=True, slots=True)
class Caption:
    """One finished unit of work: a phrase, heard and translated.

    This is the row in the database and the card on the page. It records the
    failures too -- a caption with ``status="error"`` is how the page can say
    "the provider refused" instead of simply showing nothing, which looks
    identical to silence on the channel.
    """

    channel: str
    session: str
    seq: int
    wall: float
    t0: float
    t1: float
    mode: str
    status: str = "ok"
    provider: str = ""
    model: str = ""
    source_language: str = ""
    source_text: str = ""
    target_language: str = ""
    target_text: str = ""
    latency_s: float = 0.0
    #: Seconds of audio this cost. Charged whether or not the result was
    #: usable, because the provider charged for it either way.
    cost_s: float = 0.0
    error: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, self.t1 - self.t0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "session": self.session,
            "seq": self.seq,
            "wall": self.wall,
            "t0": round(self.t0, 3),
            "t1": round(self.t1, 3),
            "duration_s": round(self.duration_s, 3),
            "mode": self.mode,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "source_language": self.source_language,
            "source_text": self.source_text,
            "target_language": self.target_language,
            "target_text": self.target_text,
            "latency_s": round(self.latency_s, 3),
            "cost_s": round(self.cost_s, 3),
            "error": self.error,
        }

    @classmethod
    def failed(
        cls,
        segment: AudioSegment,
        mode: str,
        error: str,
        *,
        provider: str = "",
        status: str = "error",
        cost_s: float = 0.0,
        latency_s: float = 0.0,
    ) -> "Caption":
        return cls(
            channel=segment.channel,
            session=segment.session,
            seq=segment.seq,
            wall=segment.wall or time.time(),
            t0=segment.t0,
            t1=segment.t1,
            mode=mode,
            status=status,
            provider=provider,
            source_language=segment.language,
            latency_s=latency_s,
            cost_s=cost_s,
            error=error[:500],
        )


# -- failures -------------------------------------------------------------
class TranslateError(Exception):
    """Base class, so the pipeline can catch everything this layer raises."""


class NotConfigured(TranslateError):
    """No API key, or a language the provider does not offer."""


class RateLimited(TranslateError):
    """Too many requests. Carries the provider's own retry hint when it gives one."""

    def __init__(self, message: str, retry_after: float = 5.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OutOfCredit(TranslateError):
    """The account cannot pay for this call.

    Separated from every other failure on purpose: it is the one error where
    retrying is pointless and continuing is wasteful, so the pipeline stops
    itself and says why rather than filling the log with refusals.
    """


class ProviderFailed(TranslateError):
    """The provider answered, but not with a usable result."""


class JobTimeout(TranslateError):
    """An asynchronous job never reached a terminal state in time."""
