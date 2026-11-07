"""The HTTP surface for the translation layer.

A router rather than routes bolted onto ``web.py``: the page is already large,
and a feature that may well be removed again should be removable in one line.

Everything the page can change goes through ``/api/translate/control``, which
writes the row the recorder reads. Everything else is a read. The API key is
never accepted here and never returned -- it comes from the environment, so no
request can redirect the billing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import control as control_mod
from . import providers as provider_registry
from .mappers.languages import CHANNEL_LANGUAGE_TAGS, LanguageMap, tag_for_channel
from .models import AudioSegment, TranslateError
from .settings import Mode, TranslateSettings
from .store import CaptionStore

log = logging.getLogger(__name__)


class ControlIn(BaseModel):
    """What the page may change. Nothing here can cost money by itself --
    only ``mode`` leaving ``off`` starts spending, which is why it is a
    separate, explicit field rather than a side effect of picking a model."""

    mode: str | None = Field(default=None, description="off | shadow | prod")
    provider: str | None = None
    target: str | None = None
    channels: list[str] | None = None
    by: str = ""


class ProbeIn(BaseModel):
    """A single deliberate call, to prove the configured model works.

    Synthetic audio by default, so pressing the button costs one short
    segment and needs no live channel. ``channel`` only labels the result.
    """

    seconds: float = Field(default=6.0, ge=1.0, le=30.0)
    language: str = "auto"
    target: str | None = None
    provider: str | None = None
    channel: str = "probe"


def build_router(
    store: Any,
    settings: TranslateSettings,
    control_path: str,
    registry: Any = None,
    claims: Any = None,
) -> APIRouter:
    """The router.

    ``store`` is the recorder's database, read-only here, and ``claims`` is the
    separate file this process owns. They have to be two arguments because the
    two live in different files -- asking the recorder's store for claims
    returns nothing, which made ``/eligible`` report that no channel was being
    translated while three of them were.
    """
    router = APIRouter(prefix="/api/translate", tags=["translate"])
    captions = CaptionStore(store)

    def effective() -> TranslateSettings:
        """Settings as the recorder is running them: config plus the switch."""
        row = control_mod.read(control_path)
        return control_mod.apply(settings, row) if row.get("set") else settings

    # -- state ----------------------------------------------------------
    @router.get("/state")
    def state(window_minutes: float = 60.0) -> JSONResponse:
        """Everything the page needs in one request."""
        now = time.time()
        live = effective()
        since = now - max(1.0, window_minutes) * 60.0
        try:
            summary = captions.summary(since, mode=None)
            per_channel = captions.channels_seen(since)
        except Exception:  # noqa: BLE001 - a database without the table yet
            summary, per_channel = {}, []
        row = store.service("recorder")
        info = (row or {}).get("info") or {}
        return JSONResponse({
            "now": now,
            "mode": live.mode,
            "modes": [
                {
                    "name": Mode.OFF,
                    "title": "Off",
                    "detail": "Nothing runs. No audio is buffered and no credit "
                              "can be spent.",
                },
                {
                    "name": Mode.SHADOW,
                    "title": "Shadow",
                    "detail": "Runs on real commentary and stores every result, "
                              "but publishes nothing. This is where the output "
                              "gets judged before anyone relies on it.",
                },
                {
                    "name": Mode.PROD,
                    "title": "Production",
                    "detail": "The same pipeline, with captions shown to "
                              "commentators.",
                },
            ],
            "provider": live.provider,
            "providers": provider_registry.describe(live),
            "target_language": live.target_language,
            "languages": sorted(CHANNEL_LANGUAGE_TAGS.values()),
            "channels": list(live.channels),
            "claimed_only": live.claimed_only,
            "control": control_mod.read(control_path),
            "settings": live.to_dict(),
            "summary": summary,
            "per_channel": per_channel,
            # The recorder publishes the live pipeline counters in its
            # heartbeat, so the page reports what is actually happening in the
            # other container rather than what this one would do.
            "pipeline": info.get("translate") or {},
            "recorder_running": bool(row and now - (row.get("last_seen") or 0) < 30),
            "window_minutes": window_minutes,
        })

    # -- the switch ------------------------------------------------------
    @router.post("/control")
    def set_control(body: ControlIn) -> JSONResponse:
        try:
            row = control_mod.write(
                control_path,
                mode=body.mode,
                provider=body.provider,
                target=body.target,
                channels=body.channels,
                by=body.by,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        live = control_mod.apply(settings, row)
        # Say plainly when the request was accepted but will not take effect,
        # rather than leaving the page showing a mode the recorder ignored.
        note = ""
        if row["mode"] != live.mode:
            note = (
                f"stored as {row['mode']}, but the recorder will stay "
                f"{live.mode}: {row['provider']} cannot run in this deployment"
            )
        elif row["provider"] != live.provider:
            note = f"provider {row['provider']} is not enabled; staying on {live.provider}"
        return JSONResponse({"ok": True, "control": row, "effective": live.to_dict(),
                             "note": note})

    # -- output ----------------------------------------------------------
    @router.get("/feed")
    def feed(
        channel: str | None = None,
        limit: int = 40,
        mode: str | None = None,
        minutes: float = 180.0,
    ) -> JSONResponse:
        since = time.time() - max(1.0, minutes) * 60.0
        try:
            rows = captions.recent(channel=channel, limit=limit, mode=mode, since=since)
        except Exception:  # noqa: BLE001
            rows = []
        return JSONResponse({"captions": rows, "count": len(rows)})

    # -- proving it works -------------------------------------------------
    @router.post("/probe")
    def probe(body: ProbeIn) -> JSONResponse:
        """Send one segment and report exactly what came back.

        Deliberately synchronous and deliberately outside the pipeline: when
        the question is "does the model work at all", an answer that arrives
        in this response beats a row appearing in a table some seconds later.
        """
        live = effective()
        name = (body.provider or live.provider).strip().lower()
        import dataclasses

        use = dataclasses.replace(
            live, target_language=(body.target or live.target_language)
        )
        started = time.perf_counter()
        try:
            provider = provider_registry.build(name, use)
        except TranslateError as exc:
            raise HTTPException(400, str(exc)) from exc

        segment = AudioSegment(
            channel=body.channel, session="probe", seq=0, t0=0.0,
            t1=body.seconds, wall=time.time(),
            samples=_speechlike(body.seconds, use.upload_rate),
            rate=use.upload_rate, language=body.language, speech_ratio=1.0,
        )
        out: dict[str, Any] = {
            "provider": name,
            "target": use.target_language,
            "seconds": body.seconds,
            "health": {},
        }
        try:
            out["health"] = provider.healthcheck()
            transcript = provider.transcribe(segment, body.language)
            out["transcript"] = transcript.text
            out["utterances"] = [
                {"text": u.text, "start_s": u.start_s, "end_s": u.end_s}
                for u in transcript.utterances
            ]
            out["source_language"] = transcript.language
            out["transcribe_latency_s"] = round(transcript.latency_s, 3)
            if transcript.text:
                translated = provider.translate(
                    [u.text for u in transcript.utterances],
                    transcript.language or body.language,
                    use.target_language,
                )
                out["translation"] = translated.text
                out["translate_latency_s"] = round(translated.latency_s, 3)
            out["ok"] = True
        except TranslateError as exc:
            out["ok"] = False
            out["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - the probe reports, never 500s
            log.exception("translation probe failed")
            out["ok"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            out["total_latency_s"] = round(time.perf_counter() - started, 3)
            try:
                provider.close()
            except Exception:  # noqa: BLE001
                pass
        return JSONResponse(out)

    @router.get("/languages")
    def languages() -> JSONResponse:
        """What the configured provider actually offers.

        Worth asking rather than assuming: Camb's documentation deprecates
        its numeric language ids without printing the replacement tags, so the
        mapping from "Portuguese" to a tag is a guess until this answers.
        """
        live = effective()
        try:
            provider = provider_registry.build(live.provider, live)
            catalogue = provider.languages()
            provider.close()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)[:300], "catalogue": {}})
        mapping = LanguageMap(catalogue)
        ours = {}
        for name, tag in sorted(CHANNEL_LANGUAGE_TAGS.items()):
            try:
                ours[name] = {"want": tag, "resolved": mapping.resolve(tag), "ok": True}
            except TranslateError as exc:
                ours[name] = {"want": tag, "resolved": None, "ok": False,
                              "error": str(exc)}
        return JSONResponse({
            "ok": True, "provider": live.provider, "catalogue": catalogue,
            "count": len(catalogue), "ours": ours,
        })

    @router.get("/eligible")
    def eligible() -> JSONResponse:
        """Which channels would be translated right now, and why not.

        Answers the question the page would otherwise have to guess at: the
        mode can be production, the model healthy, and still nothing happens
        because nobody has claimed a channel.
        """
        live = effective()
        held: dict = {}
        source = claims if claims is not None else store
        try:
            held = source.active_claims()
        except Exception:  # noqa: BLE001 - the page should still list channels
            log.debug("could not read claims", exc_info=True)
        rows = []
        names = sorted(set(list(getattr(registry, "channels", {}) or {}) + list(held)))
        for name in names:
            ch = (getattr(registry, "channels", {}) or {}).get(name)
            language = getattr(ch, "language", "") or ""
            claimed = name in held
            covered = live.covers(name, claimed)
            why = ""
            if not live.running:
                why = "translation is off"
            elif live.channels and name not in live.channels:
                why = "not in the selected channels"
            elif live.claimed_only and not claimed:
                why = "nobody has claimed it"
            rows.append({
                "channel": name,
                "label": getattr(ch, "label", name),
                "short": getattr(ch, "short", name),
                "language": language,
                "tag": tag_for_channel(language),
                "claimed": claimed,
                "commentator": (held.get(name) or {}).get("commentator", ""),
                "eligible": covered,
                "why_not": why,
            })
        rows.sort(key=lambda r: (not r["eligible"], r["channel"]))
        return JSONResponse({"channels": rows,
                             "eligible": sum(1 for r in rows if r["eligible"])})

    return router


def _speechlike(seconds: float, rate: int) -> np.ndarray:
    """Audio for the probe: speech-shaped noise, not a sine tone.

    A pure tone is a poor test -- a VAD rejects it and a transcriber returns
    nothing, so a working model would look broken. Amplitude-modulated noise
    rolled off above 4 kHz has roughly the envelope of speech, which is enough
    to tell "the call went through" from "the key is wrong". It is not a test
    of accuracy; only real commentary is that.
    """
    n = max(1, int(seconds * rate))
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n).astype(np.float32)
    # One-pole low-pass, roughly -6 dB/octave above ~1 kHz.
    alpha = 0.25
    out = np.empty(n, dtype=np.float32)
    acc = 0.0
    for i in range(n):
        acc = alpha * noise[i] + (1 - alpha) * acc
        out[i] = acc
    t = np.arange(n, dtype=np.float32) / rate
    # Syllables at ~4 Hz, with pauses, which is what a VAD looks for.
    envelope = (0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * t)) * (
        0.3 + 0.7 * (np.sin(2 * np.pi * 0.4 * t) > -0.3)
    )
    return (out * envelope * 0.25).astype(np.float32)
