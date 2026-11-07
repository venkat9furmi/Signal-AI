"""Cutting a live feed into phrases.

A transcriber given fixed twenty-second blocks produces a transcript with a
broken word at each seam, and a translator given half a sentence invents the
other half. Both get much better input if the cuts land where the commentator
stopped talking.

We already know where those are. The QC analyzer computes a speech ratio for
every 1.024 s window using Silero VAD, for the dead-air and crowd rules. The
segmenter reads that same number, so finding the pauses costs nothing extra --
no second VAD, no second pass over the audio.

The rules, in order of precedence:

1. Hard stop at ``segment_max_s``. Commentary around a goal can run a minute
   without a gap, and a caption that arrives a minute late is not a caption.
2. Otherwise, close at a pause: ``segment_gap_windows`` consecutive quiet
   windows, once the segment is at least ``segment_min_s`` long.
3. Drop a segment that is mostly not speech. A holding clip, a crowd bed and a
   silent channel all look alike to a transcriber: billable and empty.
"""

from __future__ import annotations

import logging

import numpy as np

from ..config import ANALYSIS_RATE
from .models import AudioSegment
from .settings import TranslateSettings

log = logging.getLogger(__name__)


class Segmenter:
    """Accumulates audio for one channel and emits whole phrases."""

    def __init__(
        self,
        channel: str,
        session: str,
        settings: TranslateSettings,
        language: str = "",
        rate: int = ANALYSIS_RATE,
    ) -> None:
        self.channel = channel
        self.session = session
        self.settings = settings
        self.language = language
        self.rate = rate
        self.seq = 0
        #: Segments thrown away for having no speech in them. Reported, not
        #: hidden: it is the difference between "the provider is broken" and
        #: "nobody is talking".
        self.dropped_quiet = 0

        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._t0: float | None = None
        self._wall0 = 0.0
        self._last_t = 0.0
        self._quiet_run = 0
        self._speech_sum = 0.0
        self._speech_n = 0

    # -- input ----------------------------------------------------------
    def audio(self, samples: np.ndarray, wall: float, t: float) -> None:
        """Add a block of audio. Called from the monitor thread; cheap by design.

        A copy, because the caller's array is a view onto the buffer ffmpeg
        just filled and will be reused. Appending to a list and concatenating
        once at the end beats growing an array per 100 ms block.
        """
        if samples is None or samples.size == 0:
            return
        if self._t0 is None:
            self._t0 = t
            self._wall0 = wall
        self._chunks.append(np.array(samples, dtype=np.float32, copy=True))
        self._samples += samples.size
        self._last_t = t + samples.size / float(self.rate)

    def window(self, speech_ratio: float | None) -> AudioSegment | None:
        """Note one analysis window's speech ratio, and close a segment if due.

        ``None`` means the analyzer had no verdict -- during a gap, or before
        it has filled its first window. It is treated as neither speech nor
        silence, so an outage does not look like the end of a sentence.
        """
        if speech_ratio is not None:
            self._speech_sum += float(speech_ratio)
            self._speech_n += 1
            if speech_ratio < self.settings.segment_gap_speech:
                self._quiet_run += 1
            else:
                self._quiet_run = 0

        held = self._held_s
        if held <= 0:
            return None
        if held >= self.settings.segment_max_s:
            return self._close("full")
        if (held >= self.settings.segment_min_s
                and self._quiet_run >= self.settings.segment_gap_windows):
            return self._close("pause")
        return None

    def flush(self) -> AudioSegment | None:
        """Close whatever is held, at the end of a broadcast.

        Worth having: the last phrase before a channel goes off air is often
        the full-time summary, and discarding it loses the end of every match.
        """
        if self._held_s >= self.settings.segment_min_s / 2:
            return self._close("flush")
        self._reset()
        return None

    # -- output ---------------------------------------------------------
    @property
    def _held_s(self) -> float:
        return self._samples / float(self.rate)

    @property
    def speech_ratio(self) -> float:
        return self._speech_sum / self._speech_n if self._speech_n else 0.0

    def _close(self, why: str) -> AudioSegment | None:
        t0 = self._t0 or 0.0
        wall = self._wall0
        held = self._held_s
        ratio = self.speech_ratio
        chunks, self._chunks = self._chunks, []
        self._reset()

        if ratio < self.settings.min_speech_ratio:
            self.dropped_quiet += 1
            log.debug(
                "%s: dropped %.1fs with %.0f%% speech (%s)",
                self.channel, held, ratio * 100, why,
            )
            return None

        self.seq += 1
        return AudioSegment(
            channel=self.channel,
            session=self.session,
            seq=self.seq,
            t0=t0,
            t1=t0 + held,
            wall=wall,
            samples=np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32),
            rate=self.rate,
            language=self.language,
            speech_ratio=round(ratio, 4),
        )

    def _reset(self) -> None:
        self._chunks = []
        self._samples = 0
        self._t0 = None
        self._wall0 = 0.0
        self._quiet_run = 0
        self._speech_sum = 0.0
        self._speech_n = 0
