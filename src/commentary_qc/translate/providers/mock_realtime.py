"""A live session that costs nothing, for exercising the streaming path.

Not a stub that returns success. It behaves like the real thing in the ways
that break code: it is not ready the instant it opens, it answers in bursts
rather than per chunk, it returns audio at the wire rate so the assembled WAV
is the right length, and it closes when told. That makes it enough to develop
the page against, and enough to prove the loop keeps feeding a session while
reading from it.

What it deliberately does *not* do is sound like speech. The audio is a short
chime per burst, as in the file-translation mock, so that nobody listening to
a run can mistake the stand-in for a model that got the commentary wrong.
"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from typing import Iterator

import numpy as np

from ..mappers import realtime as rt
from ..models import TranslateError

#: How much audio arrives before the session answers. Real sessions respond on
#: utterance boundaries rather than per chunk, and a mock that replied to every
#: chunk would hide a caller that assumed one answer per send.
BURST_S = 2.5

#: A plausible fast-mode delay before the session accepts audio.
READY_DELAY_S = 0.2

_WORDS = (
    "the", "ball", "goes", "wide", "and", "the", "keeper", "collects", "it",
    "now", "a", "free", "kick", "just", "outside", "the", "area",
)


class MockLiveSession:
    """In-process, free, and shaped like the real session."""

    def __init__(
        self,
        source: str,
        target: str,
        *,
        mode: str = rt.FAST,
        ready_delay_s: float = READY_DELAY_S,
        burst_s: float = BURST_S,
    ) -> None:
        self.rate = rt.WIRE_RATE
        self.source = (source or "auto").strip().lower()
        self.target = (target or "en-us").strip().lower()
        self.mode = (mode or rt.FAST).strip().lower()
        self.session_id = "mock-" + hashlib.sha1(
            f"{self.source}{self.target}{time.time()}".encode()
        ).hexdigest()[:10]

        self._burst_bytes = rt.chunk_bytes(burst_s)
        self._events: queue.Queue = queue.Queue()
        self._pending = 0
        self._bursts = 0
        self._sent_bytes = 0
        self._closed = threading.Event()
        self._ready_at = time.monotonic() + max(0.0, ready_delay_s)
        self.opened = time.time()
        self.resolved = {
            "id": self.session_id,
            "mode": self.mode,
            "source_language": self.source,
            "target_language": self.target,
            "output_modalities": ["text", "audio"],
        }
        self._events.put(rt.LiveEvent(kind=rt.STARTING))

    # -- the LiveSession contract ----------------------------------------
    def wait_ready(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < self._ready_at:
            if self._closed.is_set():
                raise TranslateError("the session closed before it was ready")
            if time.monotonic() > deadline:
                raise TranslateError(
                    f"the session was not ready after {timeout_s:.0f}s"
                )
            time.sleep(0.01)
        self._events.put(rt.LiveEvent(kind=rt.READY, raw=dict(self.resolved)))

    def send(self, pcm: bytes) -> None:
        if self._closed.is_set():
            raise TranslateError("the session has closed")
        if not pcm:
            return
        if len(pcm) > rt.MAX_APPEND_BYTES:
            # The real server answers input_audio_too_large and hangs up, so
            # the mock refuses too rather than quietly accepting more than the
            # protocol allows.
            raise TranslateError(
                f"{len(pcm)} bytes exceeds the {rt.MAX_APPEND_BYTES} byte append limit"
            )
        self._sent_bytes += len(pcm)
        self._pending += len(pcm)
        while self._pending >= self._burst_bytes:
            self._pending -= self._burst_bytes
            self._answer()

    def drain(self) -> Iterator[rt.LiveEvent]:
        while True:
            try:
                yield self._events.get_nowait()
            except queue.Empty:
                return

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def close(self) -> None:
        self._closed.set()

    # -- the pretend answer ----------------------------------------------
    def _answer(self) -> None:
        self._bursts += 1
        n = self._bursts
        heard = " ".join(_WORDS[(n * 3) % len(_WORDS):][:4]) or "commentary"
        said = f"[{self.target}] {heard}"
        self._events.put(rt.LiveEvent(kind=rt.HEARD, text=heard, final=True))
        self._events.put(rt.LiveEvent(kind=rt.SAID, text=said, final=True))
        self._events.put(rt.LiveEvent(kind=rt.AUDIO, audio=self._chime(n)))
        self._events.put(rt.LiveEvent(kind=rt.AUDIO, final=True))

    def _chime(self, n: int) -> bytes:
        """A second of two-tone chime as PCM16 at the wire rate."""
        seconds = 1.0
        t = np.arange(int(self.rate * seconds), dtype=np.float32) / self.rate
        hz = 420.0 + 60.0 * (n % 5)
        tone = 0.25 * (
            np.sin(2 * np.pi * hz * t) + 0.5 * np.sin(2 * np.pi * hz * 2 * t)
        )
        fade = int(self.rate * 0.02)
        if fade:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            tone[:fade] *= ramp
            tone[-fade:] *= ramp[::-1]
        return (np.clip(tone, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    def stats(self) -> dict:
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "source_language": self.source,
            "target_language": self.target,
            "ready": time.monotonic() >= self._ready_at and not self.closed,
            "closed": self.closed,
            "sent_seconds": round(
                self._sent_bytes / float(rt.WIRE_RATE * rt.WIRE_WIDTH), 2
            ),
            "received_bytes": 0,
            "elapsed_s": round(time.time() - self.opened, 1),
            "note": "",
            "resolved": dict(self.resolved),
        }


__all__ = ["MockLiveSession", "BURST_S", "READY_DELAY_S"]
