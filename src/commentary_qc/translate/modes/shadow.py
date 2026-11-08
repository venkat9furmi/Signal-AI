"""Shadow mode: the testing wiring, and the only one built.

What it does end to end, for one deliberate request: take a few seconds of a
channel's audio, hear it, translate it, speak the translation back, and keep
both pieces of audio so a person can listen to the pair and judge it. Nobody
but whoever asked ever sees the result.

Why it is a mode and not simply a function: everything a production wiring
would have to change is gathered in one place here -- which provider gets
built, what it may spend, whether the audio is kept, who the output reaches.
Standing a ``standard`` mode up beside it means writing those choices again
with different values, not re-deriving the pipeline.
"""

from __future__ import annotations

import dataclasses
import logging
import time
import uuid

import numpy as np

from .. import providers as provider_registry
from ..audio import to_wav
from ..budget import Budget
from ..mappers.languages import primary
from ..models import (
    AudioSegment,
    NoAudio,
    NotConfigured,
    OutOfCredit,
    RateLimited,
    SpokenAudio,
    TranslateError,
    TranslationRun,
)
from ..settings import TranslateSettings
from .base import ModeInfo, Wiring

log = logging.getLogger(__name__)

#: Below this RMS the grab is silence, a dead channel or room tone. Sending it
#: would cost a credit to be told there were no words in it, so it is refused
#: before the request is made rather than after it is billed.
SILENCE_RMS = 0.002


class ShadowMode:
    """The test wiring. Real audio, real model, no audience."""

    #: What this mode changes about the configured settings. Declared as data
    #: so the page can show it: a mode that silently narrowed the scope would
    #: be indistinguishable from a bug in the scope.
    OVERRIDES = {
        # Shadow runs are asked for by hand, one at a time, so the "only
        # channels a commentator claimed" rule that stops the continuous
        # pipeline spending credit on holding clips does not apply here.
        "claimed_only": False,
    }

    info = ModeInfo(
        name="shadow",
        title="Shadow",
        purpose="Try the model on real commentary and listen to what it "
                "produces. Nothing is published and no commentator sees it.",
        audience="only whoever pressed the button",
        built=True,
        publishes=False,
        retains_audio=True,
    )

    def __init__(self, settings: TranslateSettings) -> None:
        self.settings = settings
        # Process-level on purpose: a daily cap that reset per request would
        # not be a cap at all.
        self.budget = Budget(
            settings.shadow_daily_minutes, settings.shadow_per_channel_minutes
        )

    # -- the wiring ------------------------------------------------------
    def wire(self, settings: TranslateSettings, provider: str = "") -> Wiring:
        name = (provider or settings.provider).strip().lower()
        adjusted = dataclasses.replace(settings, provider=name, **self.OVERRIDES)
        return Wiring(
            mode=self.info.name,
            settings=adjusted,
            provider=provider_registry.build(name, adjusted),
            budget=self.budget,
            publishes=self.info.publishes,
            retains_audio=self.info.retains_audio,
            overrides=dict(self.OVERRIDES),
        )

    # -- one run ---------------------------------------------------------
    def run(
        self,
        wiring: Wiring,
        samples: np.ndarray,
        rate: int,
        *,
        channel: str = "",
        source_language: str = "auto",
        target_language: str = "",
        source_url: str = "",
    ) -> TranslationRun:
        """Hear, translate, speak. Returns a run whatever happens.

        Never raises for a provider's sake. Every failure becomes a run with a
        status and with the audio that was sent, because "the model refused"
        and "nothing came back" have to look different on the page -- and the
        heard audio is worth keeping either way, since the first question
        about a bad translation is always what the audio sounded like.
        """
        target = (target_language or wiring.settings.target_language).lower()
        audio = np.asarray(samples, dtype=np.float32).ravel()
        if audio.size == 0:
            raise NoAudio(f"no audio was captured from {channel or 'the source'}")

        seconds = audio.size / float(rate or 1)
        started = time.perf_counter()
        run_id = uuid.uuid4().hex[:16]
        heard = to_wav(audio, rate, wiring.settings.upload_rate)
        provider_name = getattr(wiring.provider, "name", "")
        provider_model = getattr(wiring.provider, "model", "")

        def finish(**kw) -> TranslationRun:
            kw.setdefault("heard_audio", heard)
            kw.setdefault("source_language", source_language)
            return TranslationRun(
                id=run_id,
                created=time.time(),
                mode=self.info.name,
                channel=channel,
                seconds=seconds,
                provider=provider_name,
                model=provider_model,
                target_language=target,
                source_url=source_url,
                total_s=time.perf_counter() - started,
                **kw,
            )

        level = float(np.sqrt(np.mean(np.square(audio))))
        if level < SILENCE_RMS:
            return finish(
                status="skipped",
                error=f"the audio was silent ({level:.5f} RMS), so nothing was sent",
            )

        allowed, why = wiring.budget.allow(channel or "shadow", seconds)
        if not allowed:
            return finish(status="skipped", error=why)

        segment = AudioSegment(
            channel=channel or "shadow",
            session=run_id,
            seq=0,
            t0=0.0,
            t1=seconds,
            wall=time.time(),
            samples=audio,
            rate=rate,
            language=source_language,
            speech_ratio=1.0,
        )

        # Charged here rather than after the result: the provider bills on
        # receiving the audio, so a failure further down still cost us.
        wiring.budget.charge(channel or "shadow", seconds)
        transcribe_s = translate_s = speak_s = 0.0
        heard_text = ""
        detected = source_language
        try:
            mark = time.perf_counter()
            transcript = wiring.provider.transcribe(segment, source_language)
            transcribe_s = time.perf_counter() - mark
            heard_text = transcript.text
            detected = transcript.language or source_language
            if not heard_text.strip():
                return finish(
                    status="empty",
                    source_language=detected,
                    transcribe_s=transcribe_s,
                    error="the model heard no words in this audio",
                )

            if primary(detected) == primary(target):
                # No sense paying to translate Portuguese into Portuguese.
                spoken_text = heard_text
            else:
                mark = time.perf_counter()
                translation = wiring.provider.translate(
                    [u.text for u in transcript.utterances] or [heard_text],
                    detected,
                    target,
                )
                translate_s = time.perf_counter() - mark
                spoken_text = translation.text

            mark = time.perf_counter()
            voice: SpokenAudio = wiring.provider.speak(spoken_text, target)
            speak_s = time.perf_counter() - mark

            return finish(
                status="ok",
                source_language=detected,
                source_text=heard_text,
                target_text=spoken_text,
                voice=voice.voice,
                credits=voice.credits,
                transcribe_s=transcribe_s,
                translate_s=translate_s,
                speak_s=speak_s,
                spoken_audio=voice.audio,
                spoken_type=voice.media_type,
            )
        except OutOfCredit as exc:
            # Latch it. Every later request would get the same refusal, and a
            # page that says "no credit" once beats a log full of it.
            wiring.budget.block(str(exc))
            return finish(status="no_credit", source_language=detected,
                          source_text=heard_text, error=str(exc),
                          transcribe_s=transcribe_s, translate_s=translate_s)
        except RateLimited as exc:
            return finish(status="rate_limited", source_language=detected,
                          source_text=heard_text,
                          error=f"{exc} (retry after {exc.retry_after:.0f}s)",
                          transcribe_s=transcribe_s, translate_s=translate_s)
        except NotConfigured as exc:
            return finish(status="not_configured", source_language=detected,
                          source_text=heard_text, error=str(exc),
                          transcribe_s=transcribe_s)
        except TranslateError as exc:
            return finish(status="error", source_language=detected,
                          source_text=heard_text, error=str(exc),
                          transcribe_s=transcribe_s, translate_s=translate_s,
                          speak_s=speak_s)
        except Exception as exc:  # noqa: BLE001 - a run always reports
            log.exception("shadow run failed on %s", channel)
            return finish(status="error", source_language=detected,
                          source_text=heard_text,
                          error=f"{type(exc).__name__}: {exc}",
                          transcribe_s=transcribe_s, translate_s=translate_s)
