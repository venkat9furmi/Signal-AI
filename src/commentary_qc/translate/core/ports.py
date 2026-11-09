"""The contract a provider has to meet.

Protocols rather than base classes: an adapter does not inherit from anything,
it simply has the right methods. That keeps the vendor's SDK -- if we ever use
one -- out of our type hierarchy, and it means a test double is a plain object
rather than a subclass pretending to be a client.

Two capabilities, kept separate because they are separately available: a
provider might transcribe well and translate badly, and the pipeline is allowed
to use one adapter for each.
"""

from __future__ import annotations

from typing import Iterator, Protocol, Sequence, runtime_checkable

from commentary_qc.translate.core.models import (
    AudioBlock,
    AudioSegment,
    AudioSpec,
    LiveEvent,
    SpokenAudio,
    Transcript,
    Translation,
)


@runtime_checkable
class Transcriber(Protocol):
    """Speech in, timed text out."""

    name: str

    def transcribe(self, segment: AudioSegment, language: str) -> Transcript:
        """Turn one segment into text.

        ``language`` is a BCP-47 tag, or ``"auto"`` to let the provider decide.
        Raises a :class:`~.models.TranslateError` subclass on failure -- never
        returns a half-result, because a caption with the wrong words in it is
        worse than no caption.
        """
        ...


@runtime_checkable
class Translator(Protocol):
    """Text in one language in, the same text in another out."""

    name: str

    def translate(
        self, texts: Sequence[str], source: str, target: str
    ) -> Translation:
        """Translate a batch, preserving order.

        Order matters: the caller pairs the results back up with the lines it
        sent by position, so a provider that reorders or drops one breaks the
        pairing. An adapter that cannot guarantee order must raise.
        """
        ...


@runtime_checkable
class Provider(Transcriber, Translator, Protocol):
    """Both capabilities plus the housekeeping the pipeline needs."""

    def languages(self) -> dict[str, str]:
        """Tags the provider accepts, mapped to a human name.

        Fetched rather than assumed. Camb's docs deprecate numeric language
        ids in favour of locale tags but do not list the tags, so guessing
        ``it-it`` for Italian is exactly the kind of assumption that fails
        silently on a Saturday.
        """
        ...

    def healthcheck(self) -> dict:
        """Can we reach it, and does the key work? Never raises."""
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class Speaker(Protocol):
    """Text in, speech out.

    The third capability, and the one that makes the result listenable. Kept
    separate from translation for the same reason the other two are separate:
    a provider may translate well and have no voice worth hearing, and in that
    case the right answer is to pair two adapters rather than accept both from
    one.
    """

    name: str

    def speak(self, text: str, language: str) -> "SpokenAudio":
        """Synthesise ``text`` in ``language`` and return the audio bytes.

        Returns bytes rather than a URL. A URL would mean the audio lives on
        the provider's host for an undocumented lifetime, and a page that
        plays it would break silently once it expired -- so the bytes are
        fetched once, here, and kept with the run that produced them.
        """
        ...


@runtime_checkable
class AudioTranslator(Provider, Speaker, Protocol):
    """Everything an execution mode needs to turn commentary into speech.

    Named for what it does end to end, because that is the capability being
    offered: audio in one language in, audio in another out. Whether the
    provider reaches that through one endpoint or three is its own business.
    """

    def capabilities(self) -> dict:
        """What this model's audio translation actually does, in words.

        Asked by the page rather than written on it. The steps differ per
        provider -- one vendor dubs in a single call, another needs
        recognise, translate and speak chained -- and a page that hardcoded
        either would start lying the day the provider was swapped.
        """
        ...


@runtime_checkable
class LiveSession(Protocol):
    """One open realtime conversation: audio in, translated audio out.

    A session is stateful and short-lived, so it is its own object rather than
    a method on the provider. The split between :meth:`send` and
    :meth:`drain` is what makes it usable from one thread: reading a socket
    blocks, and a loop that blocked on the far end would stop feeding it,
    which the server answers with ``client_too_slow``. An adapter is expected
    to read on a thread of its own and have :meth:`drain` hand over whatever
    has arrived.
    """

    spec: AudioSpec
    """The format this session expects to be fed."""

    output_spec: AudioSpec
    """The format it emits. Not necessarily the same: Gemini takes 16 kHz and
    returns 24, so one rate for both directions is wrong."""

    max_chunk_bytes: int
    """Largest chunk :meth:`send` will accept in one go. The vendor's limit,
    reported rather than assumed, so the caller splits correctly instead of
    discovering it as an error from the far end."""

    def send(self, pcm: bytes) -> None:
        """Hand over the next chunk of mono PCM16 at :attr:`rate`."""
        ...

    def drain(self) -> Iterator[LiveEvent]:
        """Yield everything that has arrived since the last call, then stop.

        Never blocks waiting for more. An empty iterator means the far end has
        said nothing yet, which during a slow-mode cold boot is the normal
        state for half a minute.
        """
        ...

    def wait_ready(self, timeout_s: float) -> None:
        """Block until the session will accept audio, or raise.

        Worth its own method because the two modes differ by two orders of
        magnitude here -- fast is nearly immediate, slow documents "30s or
        more" -- and audio sent before the far end is ready is simply lost.
        """
        ...

    @property
    def closed(self) -> bool:
        """True once the far end has gone, whether cleanly or not."""
        ...

    def close(self) -> None:
        """Finish. Safe to call twice, and safe to call on a dead session."""
        ...


@runtime_checkable
class LiveTranslator(Protocol):
    """A provider that can translate a stream rather than a file.

    Kept apart from :class:`AudioTranslator` because the two are separately
    available and separately priced: the same vendor reaches file translation
    through three REST calls and live translation through a socket, with
    different languages on each. A provider offering one says nothing about
    whether it offers the other, so the mode asks for the capability it needs
    and gets a clear refusal rather than a wrong-shaped object.
    """

    name: str

    def open_live(
        self,
        source: str,
        target: str,
        *,
        mode: str = "fast",
        voice_id: int | None = None,
    ) -> LiveSession:
        """Open a session, or raise :class:`~.models.NotConfigured`.

        Does not wait for readiness -- that is :meth:`LiveSession.wait_ready`,
        so a caller can start pulling audio while a slow session boots.
        """
        ...

    def live_modes(self) -> tuple[dict, ...]:
        """The modes this provider offers, each ``{"name", "detail"}``.

        Asked rather than hardcoded: one vendor has a fast and a slow
        pipeline, another has neither, and a page that listed one vendor's
        names would offer a control the other cannot honour.
        """
        ...

    def live_note(self, target: str) -> str:
        """A warning about ``target``, or an empty string.

        Returns a warning rather than refusing, because a vendor's published
        language list is sometimes incomplete -- and the provider is the only
        thing that knows whose list it is.
        """
        ...

    def live_languages(self) -> tuple[str, ...]:
        """Tags this provider will accept on a live session.

        Separate from :meth:`Provider.languages` on purpose: the live and
        file paths do not carry the same set, and showing the file list beside
        a live control is how somebody ends up picking a language that is
        refused only once a match has started.
        """
        ...


@runtime_checkable
class AudioFeed(Protocol):
    """An open stream of audio, until somebody stops it."""

    url: str
    """Where it is actually reading from. Worth keeping: a run that produced
    nothing is a different story depending on whether the URL was wrong."""

    def blocks(self) -> Iterator[AudioBlock]:
        """Yield audio as it arrives, forever, until :meth:`stop`."""
        ...

    def stop(self) -> None:
        """Finish. Safe to call twice and safe to call on a dead feed."""
        ...


@runtime_checkable
class AudioSource(Protocol):
    """Where a channel's audio comes from.

    Injected rather than constructed, which is the whole point: the use case
    decides *what* to translate, and this decides *how* audio arrives. Keeping
    them apart is what lets a live run be tested without ffmpeg, and what
    stops the app from knowing that streams are HLS at all.
    """

    def resolve(self, channel: str) -> str:
        """The current URL for ``channel``, or raise."""
        ...

    def open(self, channel: str, spec: AudioSpec, *, url: str = "") -> AudioFeed:
        """Open a feed delivering ``spec``. ``url`` skips discovery."""
        ...
