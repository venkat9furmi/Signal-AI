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

from commentary_qc.translate.infra.audio import to_wav
from commentary_qc.translate.core.models import (
    AudioSegment,
    ProviderFailed,
    RateLimited,
    SpokenAudio,
    Transcript,
    Translation,
    Utterance,
)
from commentary_qc.translate.settings import TranslateSettings
from commentary_qc.translate.adapters.mock.realtime import (
    MAX_CHUNK_BYTES,
    SPEC,
    MockLiveSession,
)

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

    def speak(self, text: str, language: str) -> SpokenAudio:
        """Audible, free, and obviously synthetic.

        It has to make a real sound: the page's whole claim is that you can
        press play and hear the translation, and a silent or absent file would
        let a broken player pass for a working one. So this builds a short
        tone sequence -- one note per word, pitched from the word's own hash,
        with a gap between words.

        It is deliberately nothing like speech. Anyone who presses play hears
        a run of beeps and knows immediately they are listening to the
        stand-in rather than a voice that got the commentary wrong.
        """
        started = time.perf_counter()
        self._tick()
        words = [w for w in (text or "").split() if w][:40]
        rate = 16_000
        if not words:
            return SpokenAudio(
                audio=to_wav(np.zeros(rate // 4, dtype=np.float32), rate, rate),
                media_type="audio/wav", voice="mock-beeps", model=self.model,
                provider=self.name, language=language,
                latency_s=time.perf_counter() - started, credits="0",
            )
        pieces = []
        for word in words:
            seed = int(hashlib.sha1(word.encode("utf-8")).hexdigest()[:6], 16)
            # Roughly a two-octave span starting near the middle of a voice.
            freq = 180.0 + (seed % 24) * 28.0
            n = int(rate * (0.10 + (seed % 7) * 0.02))
            t_ax = np.arange(n, dtype=np.float32) / rate
            # A short fade at each end, so the sequence does not click.
            fade = np.minimum(1.0, np.minimum(t_ax, (n / rate) - t_ax) * 60.0)
            pieces.append(0.25 * np.sin(2 * np.pi * freq * t_ax) * fade)
            pieces.append(np.zeros(int(rate * 0.06), dtype=np.float32))
        audio = np.concatenate(pieces).astype(np.float32)
        return SpokenAudio(
            audio=to_wav(audio, rate, rate),
            media_type="audio/wav",
            voice="mock-beeps",
            model=self.model,
            provider=self.name,
            language=language,
            latency_s=time.perf_counter() - started,
            credits="0",
        )

    def voices(self) -> tuple[dict, ...]:
        return ({"id": 0, "name": "mock-beeps", "language": "any",
                 "gender": "", "age": None,
                 "description": "One beep per word. Not a voice."},)

    # -- housekeeping ----------------------------------------------------
    def open_live(
        self,
        source: str,
        target: str,
        *,
        mode: str = "fast",
        voice_id: int | None = None,
    ) -> MockLiveSession:
        """A free session shaped like the real one. See mock_realtime."""
        return MockLiveSession(source, target, mode=mode)

    #: Everything MMC broadcasts in, plus a handful more. Deliberately
    #: generous: the stand-in is here to prove the path, and a mock that
    #: refused a language would make a working pipeline look broken.
    LIVE_LANGUAGES = (
        "en-us", "en-gb", "it-it", "pt-pt", "pt-br", "de-de", "el-gr",
        "fr-fr", "es-es", "nl-nl", "pl-pl", "tr-tr",
    )

    def live_languages(self) -> tuple[str, ...]:
        return self.LIVE_LANGUAGES

    def live_modes(self) -> tuple[dict, ...]:
        return (
            {"name": "fast", "detail": "Ready in a fraction of a second."},
        )

    def live_note(self, target: str) -> str:
        want = (target or "").strip().lower()
        if want and want != "auto" and want not in self.LIVE_LANGUAGES:
            return f"{want} is not in the stand-in's list; it will still run"
        return ""

    def live_capabilities(self) -> dict:
        return {
            "provider": self.name,
            "model": "mock/realtime-chime",
            "transport": "in-process",
            "endpoint": "none -- nothing leaves this machine",
            "modes": list(self.live_modes()),
            "wire": {"rate": SPEC.rate, "format": SPEC.describe()},
            "languages": list(self.live_languages()),
            "limits": {
                "max_session_s": 4 * 60 * 60,
                "idle_s": 60.0,
                "setup_s": 10.0,
                "boot_s": 1.0,
                "max_chunk_bytes": MAX_CHUNK_BYTES,
            },
            "caveats": [
                "This is the offline stand-in. It answers every few seconds "
                "with a chime and a fixed phrase, so it proves the streaming "
                "path works and says nothing about translation quality.",
            ],
        }

    def capabilities(self) -> dict:
        return {
            "provider": self.name,
            "model": self.model,
            "voice_model": "mock-beeps",
            "audio_in": True,
            "audio_out": True,
            "steps": [
                {"name": "Hear it", "endpoint": "none",
                 "detail": "Words are picked from a football vocabulary using a "
                           "hash of the audio, so the same clip always gives the "
                           "same transcript. It is not listening."},
                {"name": "Translate it", "endpoint": "none",
                 "detail": "The text is tagged with the target language and "
                           "otherwise left alone, so nobody can mistake it for "
                           "a translation."},
                {"name": "Speak it", "endpoint": "none",
                 "detail": "One beep per word. Proves the audio path end to "
                           "end without spending a credit."},
            ],
            "not_available": [],
        }

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
