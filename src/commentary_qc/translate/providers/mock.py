"""A provider that costs nothing, for shadow runs and tests.

Not a stub that returns a fixed string. It is deterministic in the audio it is
given -- the same segment always produces the same transcript -- which is what
makes it useful for two jobs a fixed string cannot do:

* **Shadow mode with no spend.** The whole pipeline can run on all 52 live
  channels for a day to find out whether the segmenter cuts sensibly, what the
  queue depth looks like and how much audio a match actually produces, without
  touching a credit. Those are the questions worth answering before paying.
* **Testing failure.** ``fail_every`` and ``latency_s`` let the tests drive the
  retry, timeout and budget paths deliberately, instead of hoping a real
  provider misbehaves while someone is watching.
"""

from __future__ import annotations

import hashlib
import time
from typing import Sequence

import numpy as np

from ..models import (
    AudioSegment,
    ProviderFailed,
    RateLimited,
    Transcript,
    Translation,
    Utterance,
)
from ..settings import TranslateSettings

#: Football words, so a shadow transcript reads like commentary at a glance
#: and an obviously wrong segmentation is visible by eye.
_WORDS = (
    "the ball", "a cross", "offside", "the keeper", "a corner", "the far post",
    "midfield", "a free kick", "the referee", "a yellow card", "the goal line",
    "a header", "the counter", "a tackle", "stoppage time", "the penalty spot",
)


class MockProvider:
    """Deterministic, offline, free."""

    name = "mock"
    model = "mock/deterministic-v1"

    def __init__(
        self,
        settings: TranslateSettings | None = None,
        *,
        latency_s: float = 0.0,
        fail_every: int = 0,
        rate_limit_every: int = 0,
    ) -> None:
        self.settings = settings
        self.latency_s = latency_s
        #: Fail every Nth call, to exercise the pipeline's error handling.
        self.fail_every = fail_every
        self.rate_limit_every = rate_limit_every
        self.calls = 0
        self.transcribed_seconds = 0.0

    # -- deterministic nonsense -----------------------------------------
    @staticmethod
    def _seed(samples: np.ndarray, extra: str = "") -> int:
        """A stable number from the audio itself.

        Hashing the bytes means identical audio gives an identical transcript,
        which is what lets a test assert on the output and a shadow run be
        re-read without the words having changed underneath it.
        """
        audio = np.asarray(samples, dtype=np.float32).ravel()
        head = audio[:4096].tobytes()
        # The length goes into the hash too. Hashing only the opening made two
        # clips that begin the same way -- a holding loop, say -- produce the
        # same words, which is exactly the bug a shadow run is meant to expose.
        digest = hashlib.sha256(
            head + str(audio.size).encode() + extra.encode()
        ).digest()
        return int.from_bytes(digest[:8], "big")

    def _tick(self) -> None:
        self.calls += 1
        if self.latency_s:
            time.sleep(self.latency_s)
        if self.rate_limit_every and self.calls % self.rate_limit_every == 0:
            raise RateLimited("mock provider is pretending to be rate limited", 0.1)
        if self.fail_every and self.calls % self.fail_every == 0:
            raise ProviderFailed("mock provider is pretending to fail")

    # -- capabilities ----------------------------------------------------
    def transcribe(self, segment: AudioSegment, language: str) -> Transcript:
        started = time.perf_counter()
        self._tick()
        self.transcribed_seconds += segment.duration_s
        seed = self._seed(segment.samples, language)
        # One utterance per ~4 s, which is roughly how a real transcriber
        # segments speech, so downstream code meets a realistic shape.
        count = max(1, int(segment.duration_s // 4))
        step = segment.duration_s / count
        lines = []
        for i in range(count):
            n = len(_WORDS)
            words = [_WORDS[(seed >> (i * 5 + j * 3)) % n] for j in range(3)]
            lines.append(
                Utterance(
                    text=" ".join(words) + ".",
                    start_s=round(i * step, 2),
                    end_s=round((i + 1) * step, 2),
                    speaker="SPEAKER_0",
                )
            )
        return Transcript(
            tuple(lines),
            language=language or "auto",
            provider=self.name,
            model=self.model,
            latency_s=time.perf_counter() - started,
        )

    def translate(self, texts: Sequence[str], source: str, target: str) -> Translation:
        started = time.perf_counter()
        self._tick()
        # Marked, not transformed. A reader of a shadow run must never mistake
        # mock output for a real translation, and reversing or rewriting the
        # words would make it look like one.
        out = tuple(f"[{target}] {t}" for t in texts if t.strip())
        return Translation(
            out,
            source_language=source,
            target_language=target,
            provider=self.name,
            model=self.model,
            latency_s=time.perf_counter() - started,
        )

    # -- housekeeping ----------------------------------------------------
    def languages(self) -> dict[str, str]:
        return {
            "en-us": "English", "it-it": "Italian", "pt-pt": "Portuguese",
            "de-de": "German", "el-gr": "Greek", "fr-fr": "French",
        }

    def healthcheck(self) -> dict:
        return {
            "ok": True,
            "provider": self.name,
            "languages": len(self.languages()),
            "note": "offline stand-in - costs nothing, means nothing",
        }

    def close(self) -> None:
        pass
