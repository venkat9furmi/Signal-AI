"""A few seconds of a live channel, on demand.

The continuous pipeline taps audio that a monitor is already pulling. A shadow
run has no monitor: somebody picked a channel on the page and pressed a
button, so this opens its own short-lived ffmpeg pull, takes what it needs and
closes it.

Deliberately its own capture rather than a hook into the recorder's. The two
processes are separate containers, the recorder is only monitoring claimed and
configured channels, and the whole appeal of a shadow run is that it works on
any channel at any time without first arranging for one to be watched. A
fifteen-second pull is cheap; a cross-process audio handoff is not.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np

from ..audio_source import FFmpegSource
from ..config import ANALYSIS_RATE, SourceConfig
from ..discovery import StreamNotFound, resolve
from .models import NoAudio

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Grab:
    """The captured audio and where it came from."""

    samples: np.ndarray
    rate: int
    url: str
    channel: str
    seconds: float
    gaps: list[str] = field(default_factory=list)


def grab(
    channel: str,
    seconds: float,
    source: SourceConfig,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: float = 25.0,
    url: str | None = None,
) -> Grab:
    """Pull ``seconds`` of ``channel`` and return it as float32 mono.

    Raises :class:`~.models.NoAudio` when the channel delivers nothing before
    ``timeout_s``. That is the common case worth a clear message: a channel
    the panel lists is frequently off air, and "no audio arrived in 25
    seconds" is a far more useful answer than an empty result that looks like
    a broken model.

    ``StreamNotFound`` from discovery is left to propagate -- the caller can
    distinguish "this channel is not live" from "it is live but silent", and
    those are different things to tell somebody.
    """
    import dataclasses

    cfg = dataclasses.replace(source, channel=channel)
    stream_url = url or resolve(cfg).url

    want = int(max(1.0, seconds) * ANALYSIS_RATE)
    collected: list[np.ndarray] = []
    gaps: list[str] = []
    have = 0
    deadline = time.monotonic() + timeout_s

    feed = FFmpegSource(stream_url, cfg, ffmpeg_bin)
    try:
        for block in feed.blocks():
            if block.gap:
                # A reconnect mid-grab. Recorded rather than retried: fifteen
                # seconds is not long enough to ride out a reconnect, and a
                # run stitched across one would be audibly wrong.
                gaps.append(block.reason or "gap")
                if time.monotonic() >= deadline:
                    break
                continue
            if block.samples is None or block.samples.size == 0:
                continue
            collected.append(np.asarray(block.samples, dtype=np.float32).ravel())
            have += collected[-1].size
            if have >= want:
                break
            if time.monotonic() >= deadline:
                break
    finally:
        feed.stop()

    if not collected:
        raise NoAudio(
            f"{channel} delivered no audio in {timeout_s:.0f}s"
            + (f" ({gaps[0]})" if gaps else "")
        )
    audio = np.concatenate(collected)[:want]
    return Grab(
        samples=audio,
        rate=ANALYSIS_RATE,
        url=stream_url,
        channel=channel,
        seconds=audio.size / float(ANALYSIS_RATE),
        gaps=gaps,
    )


__all__ = ["Grab", "grab", "StreamNotFound"]
