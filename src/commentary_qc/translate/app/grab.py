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

from commentary_qc.config import ANALYSIS_RATE
from commentary_qc.discovery import StreamNotFound
from commentary_qc.translate.core.models import AudioSpec, NoAudio
from commentary_qc.translate.core.ports import AudioSource

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
    audio: AudioSource,
    *,
    timeout_s: float = 25.0,
    url: str | None = None,
    rate: int = ANALYSIS_RATE,
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
    want = int(max(1.0, seconds) * rate)
    collected: list[np.ndarray] = []
    gaps: list[str] = []
    have = 0
    deadline = time.monotonic() + timeout_s

    feed = audio.open(channel, AudioSpec(rate), url=url or "")
    stream_url = feed.url
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
            if block.empty:
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
    captured = np.concatenate(collected)[:want]
    return Grab(
        samples=captured,
        rate=rate,
        url=stream_url,
        channel=channel,
        seconds=captured.size / float(rate),
        gaps=gaps,
    )


__all__ = ["Grab", "grab", "StreamNotFound"]
