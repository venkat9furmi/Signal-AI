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


# -- audio that comes back ------------------------------------------------
@dataclass(frozen=True, slots=True)
class SpokenAudio:
    """Translated speech, as bytes we can hand to an ``<audio>`` element.

    The point of the whole feature is that somebody listens to it, so the
    audio is a first-class result rather than a side effect of a text
    translation. ``credits`` is whatever figure the provider reported for
    this one call, kept verbatim as a string: Camb returns it in an
    ``X-Credits-Required`` header and publishes no balance endpoint, so this
    is the only honest number available and inventing a unit for it would be
    worse than passing it through.
    """

    audio: bytes = b""
    media_type: str = "audio/wav"
    voice: str = ""
    model: str = ""
    provider: str = ""
    language: str = ""
    latency_s: float = 0.0
    credits: str = ""

    def __bool__(self) -> bool:
        return bool(self.audio)


@dataclass(frozen=True, slots=True)
class TranslationRun:
    """One deliberate end-to-end attempt: audio in, translated audio out.

    This is what an execution mode produces and what the page shows. It keeps
    both ends of the work -- the audio that was heard and the audio that was
    produced -- because the only way to judge a translation of live commentary
    is to listen to the two one after the other.

    A failure is a run too, with ``status`` saying so. A run that vanished
    because the provider refused looks identical to one that was never
    started, and that ambiguity is what makes a model feel unreliable when it
    is actually just unpaid.
    """

    id: str
    created: float
    mode: str
    #: ``clip`` for the three-call file path, ``live`` for a realtime session.
    #: The two produce the same record and are listed together, but they are
    #: different products with different languages and different billing, so
    #: a run has to say which one it was.
    kind: str = "clip"
    #: The bench this run belongs to, when it was one entrant in a
    #: comparison rather than a run on its own. Empty for a solo run.
    bench: str = ""
    channel: str = ""
    seconds: float = 0.0
    provider: str = ""
    model: str = ""
    voice: str = ""
    source_language: str = ""
    source_text: str = ""
    target_language: str = ""
    target_text: str = ""
    status: str = "ok"
    error: str = ""
    transcribe_s: float = 0.0
    translate_s: float = 0.0
    speak_s: float = 0.0
    total_s: float = 0.0
    credits: str = ""
    source_url: str = ""
    heard_audio: bytes = field(default=b"", compare=False, repr=False)
    spoken_audio: bytes = field(default=b"", compare=False, repr=False)
    heard_type: str = "audio/wav"
    spoken_type: str = "audio/wav"

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        """Everything but the audio.

        The blobs are deliberately excluded: they are fetched by their own URL
        so the browser can stream them into a player and cache them, rather
        than base64 in a JSON body that has to be re-sent on every poll.
        """
        return {
            "id": self.id,
            "created": self.created,
            "mode": self.mode,
            "kind": self.kind,
            "bench": self.bench,
            "channel": self.channel,
            "seconds": round(self.seconds, 2),
            "provider": self.provider,
            "model": self.model,
            "voice": self.voice,
            "source_language": self.source_language,
            "source_text": self.source_text,
            "target_language": self.target_language,
            "target_text": self.target_text,
            "status": self.status,
            "error": self.error,
            "transcribe_s": round(self.transcribe_s, 2),
            "translate_s": round(self.translate_s, 2),
            "speak_s": round(self.speak_s, 2),
            "total_s": round(self.total_s, 2),
            "credits": self.credits,
            "source_url": self.source_url,
            "has_heard": bool(self.heard_audio),
            "has_spoken": bool(self.spoken_audio),
            "heard_type": self.heard_type,
            "spoken_type": self.spoken_type,
        }


class ModeNotBuilt(TranslateError):
    """An execution mode exists in the registry but has no implementation.

    Deliberately an error and not a silent fall back to another mode: asking
    for production and quietly getting the test wiring is the one failure that
    would be impossible to notice from the outside.
    """


class NoAudio(TranslateError):
    """There was nothing worth sending -- silence, or a channel off air."""

# -- a live session's vocabulary ---------------------------------------------
#: What a live session can tell us. Deliberately fewer kinds than any
#: vendor's event list: the caller cares about audio, text, readiness and
#: trouble, and collapsing the rest at the adapter boundary keeps every
#: provider's protocol out of the streaming loop.
#:
#: These live in the domain rather than beside one vendor's wire format,
#: because two adapters now produce them and `ports.py` needs them. A core
#: type imported from an adapter is the dependency rule backwards.
READY = "ready"
STARTING = "starting"
AUDIO = "audio"
HEARD = "heard"
SAID = "said"
FAILED = "failed"
NOTE = "note"


@dataclass(slots=True)
class AudioBlock:
    """One chunk off a feed, or a marker that the feed is not delivering.

    A gap is a value, not an exception: a reconnect mid-session is normal and
    the audio after it is not continuous with what came before, which is
    something a listener judging a translation needs to be told rather than
    have smoothed over.
    """

    samples: Any = None
    wall: float = 0.0
    gap: bool = False
    reason: str = ""

    @property
    def empty(self) -> bool:
        return self.samples is None or getattr(self.samples, "size", 0) == 0


@dataclass(frozen=True, slots=True)
class AudioSpec:
    """The shape of a stream of samples.

    A value object rather than three loose numbers, because the numbers
    differ per vendor and per direction: Camb takes and returns 24 kHz, while
    Gemini takes 16 kHz and returns 24. A single ``rate`` on a session was
    only ever correct by accident.
    """

    rate: int
    width: int = 2
    channels: int = 1

    @property
    def bytes_per_second(self) -> int:
        return self.rate * self.width * self.channels

    def bytes_for(self, seconds: float) -> int:
        """Whole samples worth of ``seconds``. Never a partial sample: half a
        sample plays as a click and shifts everything after it."""
        return int(max(0.0, seconds) * self.rate) * self.width * self.channels

    def seconds_for(self, nbytes: int) -> float:
        return max(0, nbytes) / float(self.bytes_per_second or 1)

    def describe(self) -> str:
        channels = "mono" if self.channels == 1 else f"{self.channels}ch"
        return f"{self.rate} Hz {channels} PCM{self.width * 8}"


@dataclass(slots=True)
class LiveEvent:
    """One thing a live session said."""

    kind: str
    text: str = ""
    audio: bytes = b""
    code: str = ""
    final: bool = False
    fatal: bool = False
    #: The exception this event means, when it means one. Set by the adapter,
    #: because only the adapter knows what its vendor's codes signify -- and
    #: the app should not be reading vendor error codes to find out.
    error: "TranslateError | None" = None
    raw: dict[str, Any] = field(default_factory=dict)
