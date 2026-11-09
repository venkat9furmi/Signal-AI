"""Getting a channel's audio, behind a port.

The app used to build an :class:`~commentary_qc.audio_source.FFmpegSource`
itself and call discovery directly. That is a concrete dependency on a
subprocess sitting in the middle of a use case: it made a live run impossible
to exercise without spawning ffmpeg, and it meant the layer that decides
*what* to translate also knew *how* audio arrives.

So the app now asks for an :class:`~commentary_qc.translate.core.ports.AudioSource`
and gets whatever it was given. In production that is :class:`FFmpegAudioSource`,
which resolves the channel against the CDN and pulls it with ffmpeg at
whatever rate the session asked for. In a test it can be a few arrays.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Iterator

from commentary_qc.audio_source import FFmpegSource
from commentary_qc.config import SourceConfig
from commentary_qc.discovery import resolve
from commentary_qc.translate.core.models import AudioBlock, AudioSpec

log = logging.getLogger(__name__)


class FFmpegFeed:
    """One open pull, yielding :class:`AudioBlock` objects.

    A thin translation from the QC engine's own ``Block`` into the domain's,
    so nothing above this layer imports from the capture code. It is thin on
    purpose: the supervision, the reconnect and the stall detection are the
    engine's and are worth reusing exactly as they are.
    """

    def __init__(self, feed: FFmpegSource, url: str, spec: AudioSpec) -> None:
        self._feed = feed
        self.url = url
        self.spec = spec

    def blocks(self) -> Iterator[AudioBlock]:
        for block in self._feed.blocks():
            yield AudioBlock(
                samples=block.samples,
                wall=block.wall,
                gap=bool(block.gap),
                reason=block.reason or "",
            )

    def stop(self) -> None:
        self._feed.stop()


class FFmpegAudioSource:
    """Resolve a channel, then pull it with ffmpeg.

    Holds the two things the app should not: where streams live, and how to
    decode them. Construct it once at the composition root and hand it to
    whatever needs audio.
    """

    def __init__(self, source: SourceConfig, ffmpeg_bin: str = "ffmpeg") -> None:
        self.source = source
        self.ffmpeg_bin = ffmpeg_bin

    def config_for(self, channel: str) -> SourceConfig:
        return dataclasses.replace(self.source, channel=channel)

    def resolve(self, channel: str) -> str:
        """The current URL for ``channel``.

        Lets ``StreamNotFound`` and ``ProbeFailed`` propagate: "this channel
        is not live" and "we cannot reach the stream host" are different
        things to tell somebody, and flattening them into one error is how an
        outage on our side comes to look like every channel stopping at once.
        """
        return resolve(self.config_for(channel)).url

    def open(
        self, channel: str, spec: AudioSpec, *, url: str = ""
    ) -> FFmpegFeed:
        """Open a feed delivering ``spec`` for ``channel``.

        ``url`` skips discovery, which is what makes a local file usable as a
        channel in a test.
        """
        cfg = self.config_for(channel)
        target = url or self.resolve(channel)
        feed = FFmpegSource(target, cfg, self.ffmpeg_bin, rate=spec.rate)
        return FFmpegFeed(feed, target, spec)


__all__ = ["FFmpegAudioSource", "FFmpegFeed"]
