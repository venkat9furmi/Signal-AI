"""The HTTP surface, namespaced by execution mode.

Every path carries the mode: ``/api/translate/shadow/run``,
``/api/translate/standard/run``. Calling one *is* choosing the wiring --
which provider gets built, what it may spend, whether the audio is kept, who
sees the result -- rather than passing a flag that something downstream might
quietly ignore. The modes themselves live in
:mod:`commentary_qc.translate.modes`; this file only routes to them.

``standard`` is declared and not built, so its paths answer 501 with the
reason. That is deliberate: a 404 would read as a wrong URL, and falling back
to shadow would mean asking for production and silently getting the test
wiring.

Four questions the page asks, and nothing else:

* what does this model's audio translation actually do, and what is it called
* how much credit has it used
* run it on a channel
* give me the audio so I can listen to it

The API key is never accepted here and never returned. It comes from the
environment, so no request can redirect the billing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..config import SourceConfig
from ..discovery import ProbeFailed, StreamNotFound
from . import providers as provider_registry
from .grab import grab as grab_audio
from .mappers.languages import tag_for_channel
from .models import ModeNotBuilt, NoAudio, NotConfigured, TranslateError
from .modes import ModeRegistry
from .runs import RunStore
from .settings import TranslateSettings

log = logging.getLogger(__name__)


class RunIn(BaseModel):
    """One deliberate run. Nothing here can change what account is billed."""

    channel: str = Field(default="", description="Channel to listen to, e.g. football-31")
    seconds: float | None = Field(default=None, ge=3.0, le=60.0)
    target: str = ""
    provider: str = ""
    source_language: str = "auto"


def build_router(
    settings: TranslateSettings,
    modes: ModeRegistry,
    runs: RunStore,
    *,
    registry: Any = None,
    source: SourceConfig | None = None,
    ffmpeg_bin: str = "ffmpeg",
) -> APIRouter:
    router = APIRouter(prefix="/api/translate", tags=["translate"])
    src = source or SourceConfig()

    def mode_or_501(name: str):
        try:
            return modes.get(name)
        except ModeNotBuilt as exc:
            raise HTTPException(404, str(exc)) from exc

    def wire_or_501(name: str, provider: str = ""):
        mode = mode_or_501(name)
        try:
            return mode.wire(settings, provider)
        except ModeNotBuilt as exc:
            # 501: the route is right, the implementation is absent.
            raise HTTPException(501, str(exc)) from exc
        except NotConfigured as exc:
            raise HTTPException(400, str(exc)) from exc

    # -- which wirings exist ---------------------------------------------
    @router.get("/modes")
    def list_modes() -> JSONResponse:
        return JSONResponse({
            "modes": modes.describe(),
            "default": modes.default,
        })

    # -- what the model does ---------------------------------------------
    @router.get("/{mode}/model")
    def model(mode: str, provider: str = "") -> JSONResponse:
        """The model's name and what its audio translation actually does.

        The steps come from the provider rather than from this file. They
        differ per vendor -- one dubs in a single call, another chains
        recognise, translate and speak -- and a page with either hardcoded
        would start lying the day the provider changed.
        """
        info = mode_or_501(mode).info
        wiring = wire_or_501(mode, provider)
        out: dict[str, Any] = {
            "mode": info.to_dict(),
            "provider": wiring.provider.name,
            "model": getattr(wiring.provider, "model", ""),
            "providers": provider_registry.describe(wiring.settings),
            "target_language": wiring.settings.target_language,
            "overrides": wiring.overrides,
            "grab_seconds": wiring.settings.grab_seconds,
        }
        try:
            out["capabilities"] = wiring.provider.capabilities()
        except Exception as exc:  # noqa: BLE001 - describing must not fail
            out["capabilities"] = {}
            out["capabilities_error"] = str(exc)[:200]
        try:
            out["health"] = wiring.provider.healthcheck()
        except Exception as exc:  # noqa: BLE001
            out["health"] = {"ok": False, "error": str(exc)[:200]}
        try:
            catalogue = wiring.provider.languages()
            out["languages"] = [
                {"tag": tag, "name": name} for tag, name in sorted(catalogue.items())
            ]
        except Exception as exc:  # noqa: BLE001 - a language list is not the feature
            out["languages"] = []
            out["languages_error"] = str(exc)[:200]
        try:
            out["voices"] = list(wiring.provider.voices())[:60]
        except Exception as exc:  # noqa: BLE001
            out["voices"] = []
            out["voices_error"] = str(exc)[:200]
        wiring.close()
        return JSONResponse(out)

    # -- what it has cost -------------------------------------------------
    @router.get("/{mode}/credits")
    def credits(mode: str) -> JSONResponse:
        """Spending, from both sides: our own count and the provider's.

        Two numbers because neither alone is the truth. Ours is seconds of
        audio sent, which is what the cap is enforced on and what survives a
        restart. The provider's is whatever ``X-Credits-Required`` said on the
        last call -- the only figure Camb publishes, since it has no balance,
        quota or usage endpoint at all. Saying so on the page is better than
        showing a balance we made up.
        """
        info = mode_or_501(mode).info
        shadow = modes.get(mode)
        budget = getattr(shadow, "budget", None)
        today = runs.spent_today(mode=mode)
        snapshot = budget.snapshot() if budget is not None else {}
        # Adopt the stored figure if it is the larger: the Budget object is
        # process memory and resets on deploy, the database does not.
        if snapshot and today["seconds"] > snapshot.get("spent_s", 0.0) and budget:
            budget.seed(today["seconds"], today["by_channel"])
            snapshot = budget.snapshot()
        return JSONResponse({
            "mode": info.to_dict(),
            "provider": settings.provider,
            "key_set": bool(settings.camb_api_key),
            "budget": snapshot,
            "today": today,
            "provider_credits": {
                "last": runs.last_credits(mode=mode),
                "source": "X-Credits-Required response header",
                "note": "Camb publishes no balance, quota or usage endpoint. "
                        "This is what it charged for the most recent call; the "
                        "running total lives in their Studio, not the API.",
            },
        })

    # -- what you can run it on -------------------------------------------
    @router.get("/{mode}/channels")
    def channels(mode: str) -> JSONResponse:
        """Channels that can be listened to right now.

        Liveness is not asked for here. Probing 52 channels takes a couple of
        seconds, and a shadow run already reports "this channel is not live"
        in a way the page can show -- so the picker lists everything and the
        run tells you if you chose one that is off air.
        """
        mode_or_501(mode)
        rows = []
        for name, channel in sorted((getattr(registry, "channels", {}) or {}).items()):
            language = getattr(channel, "language", "") or ""
            rows.append({
                "channel": name,
                "label": getattr(channel, "label", name),
                "language": language,
                "tag": tag_for_channel(language),
            })
        return JSONResponse({"channels": rows, "count": len(rows)})

    # -- run it ------------------------------------------------------------
    @router.post("/{mode}/run")
    def run(mode: str, body: RunIn) -> JSONResponse:
        """Listen to a channel, translate it, speak it back, store the audio.

        Synchronous on purpose. It takes about as long as the audio plus the
        model, which is some tens of seconds, and a job id to poll would be
        more moving parts than one person pressing one button needs. The page
        says how long it will take instead.
        """
        wiring = wire_or_501(mode, body.provider)
        if not body.channel:
            wiring.close()
            raise HTTPException(400, "say which channel to listen to")
        seconds = body.seconds or wiring.settings.grab_seconds
        try:
            captured = grab_audio(
                body.channel,
                seconds,
                src,
                ffmpeg_bin=ffmpeg_bin,
                timeout_s=wiring.settings.grab_timeout_s,
            )
        except ProbeFailed as exc:
            wiring.close()
            # Our side failed, not theirs. Worth separating: an outage here
            # otherwise reads as every channel having stopped at once.
            raise HTTPException(502, f"could not reach the stream host: {exc}") from exc
        except StreamNotFound as exc:
            wiring.close()
            raise HTTPException(409, f"{body.channel} is not live right now") from exc
        except NoAudio as exc:
            wiring.close()
            raise HTTPException(409, str(exc)) from exc
        except FileNotFoundError as exc:
            wiring.close()
            raise HTTPException(
                500, "ffmpeg was not found, so no audio can be captured"
            ) from exc

        try:
            result = modes.get(mode).run(
                wiring,
                captured.samples,
                captured.rate,
                channel=captured.channel,
                source_language=body.source_language or "auto",
                target_language=body.target,
                source_url=captured.url,
            )
        except TranslateError as exc:
            wiring.close()
            raise HTTPException(400, str(exc)) from exc
        finally:
            wiring.close()

        runs.put(result)
        out = result.to_dict()
        out["gaps"] = captured.gaps
        return JSONResponse(out)

    @router.get("/{mode}/runs")
    def recent(mode: str, limit: int = 12, channel: str | None = None) -> JSONResponse:
        mode_or_501(mode)
        return JSONResponse({
            "runs": runs.recent(limit=limit, mode=mode, channel=channel),
        })

    # -- listen to it ------------------------------------------------------
    @router.get("/{mode}/audio/{run_id}/{which}")
    def audio(mode: str, run_id: str, which: str) -> Response:
        """The audio bytes, for an ``<audio>`` element.

        ``which`` is ``heard`` or ``spoken``: the commentary that went in, and
        the translation that came out. Served as bytes rather than a provider
        URL because a provider URL expires on an undocumented schedule and the
        page would break silently once it had.
        """
        mode_or_501(mode)
        if which not in ("heard", "spoken"):
            raise HTTPException(404, "audio is either 'heard' or 'spoken'")
        found = runs.audio(run_id, which)
        if found is None:
            raise HTTPException(404, f"no {which} audio kept for run {run_id}")
        blob, kind = found
        return Response(
            content=blob,
            media_type=kind,
            headers={
                "content-disposition": f'inline; filename="{run_id}-{which}.wav"',
                # Immutable: a run never changes once stored, so the browser
                # may keep it and seeking in the player costs no round trip.
                "cache-control": "private, max-age=3600, immutable",
            },
        )

    return router
