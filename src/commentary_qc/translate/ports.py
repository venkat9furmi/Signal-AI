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

from typing import Protocol, Sequence, runtime_checkable

from .models import AudioSegment, SpokenAudio, Transcript, Translation


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
