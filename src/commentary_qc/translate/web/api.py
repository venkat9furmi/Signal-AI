"""The HTTP surface, namespaced by execution mode.

Every path carries the mode: ``/api/translate/shadow/run``,
``/api/translate/standard/run``. Calling one *is* choosing the wiring --
which provider gets built, what it may spend, whether the audio is kept, who
sees the result -- rather than passing a flag that something downstream might
quietly ignore. The modes themselves live in
:mod:`commentary_qc.translate.app.modes`; this file only routes to them.

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

from commentary_qc.config import SourceConfig
from commentary_qc.discovery import ProbeFailed, StreamNotFound
from commentary_qc.translate import adapters as provider_registry
from commentary_qc.translate.app.grab import grab as grab_audio
from commentary_qc.translate.app import bench as bench_mod
from commentary_qc.translate.app.bench import MMC_TARGETS
from commentary_qc.translate.infra.audio import to_wav
from commentary_qc.translate.infra.audio_source import FFmpegAudioSource
from commentary_qc.translate.app.live import AtCapacity, LiveRunner
from commentary_qc.translate.core.channels import clean_channel
from commentary_qc.translate.core.languages import tag_for_channel
from commentary_qc.translate.core.models import ModeNotBuilt, NoAudio, NotConfigured, TranslateError
from commentary_qc.translate.app.modes import ModeRegistry
from commentary_qc.translate.infra.runs import RunStore
from commentary_qc.translate.settings import TranslateSettings

log = logging.getLogger(__name__)


class RunIn(BaseModel):
    """One deliberate run. Nothing here can change what account is billed."""

    channel: str = Field(default="", description="Channel to listen to, e.g. football-31")
    seconds: float | None = Field(default=None, ge=3.0, le=60.0)
    target: str = ""
    provider: str = ""
    source_language: str = "auto"


class LiveIn(BaseModel):
    """Start a live session. Same guarantee as RunIn: nothing here chooses
    which account is billed."""

    channel: str = Field(default="", description="Channel to translate, e.g. football-41")
    seconds: float | None = Field(
        default=None, ge=10.0, le=1800.0,
        description="Hard limit on the session, in seconds.",
    )
    target: str = ""
    provider: str = ""
    source_language: str = "auto"
    live_mode: str = Field(default="", description="fast or slow")


class BenchIn(BaseModel):
    """Put several models on the same clip.

    ``providers`` and ``targets`` multiply: three models and two languages is
    six entries and six times the audio billed, which is why the page shows
    the estimate before the button does anything.
    """

    channel: str = Field(default="", description="Channel to capture, e.g. football-41")
    seconds: float | None = Field(default=None, ge=3.0, le=60.0)
    providers: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    source_language: str = "auto"
    label: str = Field(default="", description="What this comparison is for")
    note: str = ""


class ScoreIn(BaseModel):
    """One person's judgement of one entry."""

    run_id: str = ""
    who: str = ""
    score: int = Field(default=0, ge=bench_mod.MIN_SCORE, le=bench_mod.MAX_SCORE)
    note: str = ""


def _safe_name(value: str) -> str:
    """An id reduced to something safe to put in a response header.

    Ids are ours and are hex, so in practice nothing is stripped. It is here
    because the value arrives from the URL path: a quote or a newline in a
    `content-disposition` is header injection, and the only reason it is not
    reachable today is that an unknown id 404s before the header is built.
    That is a coincidence of ordering, not a defence.
    """
    keep = [c for c in (value or "") if c.isalnum() or c in "-_"]
    return "".join(keep)[:64] or "audio"


def build_router(
    settings: TranslateSettings,
    modes: ModeRegistry,
    runs: RunStore,
    *,
    registry: Any = None,
    source: SourceConfig | None = None,
    ffmpeg_bin: str = "ffmpeg",
    audio_source: Any = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/translate", tags=["translate"])
    # One audio source, built here and handed to every use case. The app layer
    # never learns that audio arrives over HLS through ffmpeg.
    audio = audio_source or FFmpegAudioSource(source or SourceConfig(), ffmpeg_bin)

    def channel_tag(channel: str) -> str:
        """The language a channel is configured to carry, as a tag.

        Used as the source language when a model cannot detect one. We know
        this from ``channels.json`` and it is strictly better than nothing --
        without it, a provider whose transcriber returns no language fails
        every run with "the source language is unknown".
        """
        entry = (getattr(registry, "channels", {}) or {}).get(channel)
        return tag_for_channel(getattr(entry, "language", "") if entry else "")
    live_runs = LiveRunner(runs, max_concurrent=settings.live_concurrent)
    benches = bench_mod.BenchRunner(runs)
    # Hung off the router so the app can wind sessions up on shutdown. A live
    # session the process forgot about would keep an ffmpeg pull and a paid
    # socket open until the container died.
    router.live_runs = live_runs  # type: ignore[attr-defined]
    router.benches = benches  # type: ignore[attr-defined]

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
        out["live"] = {
            "available": bool(info.live and hasattr(wiring.provider, "open_live")),
            "mode": wiring.settings.live_mode,
            "seconds": wiring.settings.live_seconds,
            "concurrent": wiring.settings.live_concurrent,
        }
        describe_live = getattr(wiring.provider, "live_capabilities", None)
        if describe_live is not None:
            try:
                out["live"]["capabilities"] = describe_live()
            except Exception as exc:  # noqa: BLE001
                out["live"]["capabilities_error"] = str(exc)[:200]
        elif out["live"]["available"]:
            out["live"]["available"] = False
            out["live"]["why"] = (
                f"{wiring.provider.name} offers no live translation, only the "
                f"clip path"
            )
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
        try:
            channel = clean_channel(body.channel)
        except NotConfigured as exc:
            wiring.close()
            raise HTTPException(400, str(exc)) from exc
        seconds = body.seconds or wiring.settings.grab_seconds
        try:
            captured = grab_audio(
                channel,
                seconds,
                audio,
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
                fallback_language=channel_tag(captured.channel),
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

    # -- live --------------------------------------------------------------
    @router.post("/{mode}/live")
    def live_start(mode: str, body: LiveIn) -> JSONResponse:
        """Open a realtime session on a channel and start streaming it.

        Returns as soon as the session is accepted, not when it finishes --
        the whole point is that it runs while a match does. The page polls
        ``GET`` for captions and can play the translated audio while it is
        still arriving.
        """
        info = mode_or_501(mode).info
        if not info.live:
            raise HTTPException(
                501, f"{info.name} mode has no live path" + (
                    f": {info.pending}" if info.pending else ""
                )
            )
        try:
            channel = clean_channel(body.channel)
        except NotConfigured as exc:
            raise HTTPException(400, str(exc)) from exc

        wiring = wire_or_501(mode, body.provider)
        if not hasattr(wiring.provider, "open_live"):
            name = wiring.provider.name
            wiring.close()
            raise HTTPException(
                400,
                f"{name} offers no live translation, only the clip path. "
                f"Switch the model to one that does.",
            )
        try:
            run = modes.get(mode).live(
                wiring,
                channel,
                audio=audio,
                source_language=body.source_language or "auto",
                target_language=body.target,
                live_mode=body.live_mode,
                seconds=body.seconds or 0.0,
            )
        except (NotConfigured, TranslateError) as exc:
            wiring.close()
            raise HTTPException(400, str(exc)) from exc

        try:
            live_runs.start(run)
        except AtCapacity as exc:
            # 409, not 429: nothing is rate limited, something else simply has
            # the one session this plan allows.
            wiring.close()
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse(run.snapshot(), status_code=202)

    @router.get("/{mode}/live")
    def live_state(mode: str, id: str = "") -> JSONResponse:
        """The current session, or the one named by ``id``.

        Also reports the languages the live path accepts, which are *not* the
        ones the clip path accepts -- showing the clip list beside a live
        control is how somebody picks a language that is only refused once a
        match has started.
        """
        info = mode_or_501(mode).info
        run = live_runs.get(id) if id else live_runs.current()
        return JSONResponse({
            "mode": info.to_dict(),
            "live": run.snapshot() if run is not None else None,
            "history": [
                {
                    "id": r.id,
                    "channel": r.channel,
                    "phase": r.phase,
                    "running": r.running,
                    "elapsed_s": round((r.ended or r.started) - r.started, 1),
                    "spoken_seconds": round(r.spoken_seconds, 1),
                    "error": r.error,
                }
                for r in live_runs.all()
            ],
            "limits": {
                "seconds": settings.live_seconds,
                "concurrent": settings.live_concurrent,
                "modes": _live_modes(mode),
                "default_mode": settings.live_mode,
            },
            "languages": _live_languages(mode),
        })

    @router.post("/{mode}/live/{run_id}/stop")
    def live_stop(mode: str, run_id: str) -> JSONResponse:
        """Wind a session up. The tail is still collected, so the last
        sentence is not lost."""
        mode_or_501(mode)
        run = live_runs.get(run_id)
        if run is None:
            raise HTTPException(404, f"no live run {run_id}")
        run.stop("stopped from the page")
        return JSONResponse(run.snapshot())

    @router.get("/{mode}/live/{run_id}/audio/{which}")
    def live_audio(mode: str, run_id: str, which: str) -> Response:
        """Audio from a session, playable while it is still running.

        Served from the run in memory rather than from the database: a session
        is not written to the store until it ends, and waiting two minutes to
        hear any of a live translation would defeat the point of testing one.
        A finished run falls through to the stored copy.
        """
        mode_or_501(mode)
        if which not in ("heard", "spoken"):
            raise HTTPException(404, "audio is either 'heard' or 'spoken'")
        run = live_runs.get(run_id)
        blob = b""
        if run is not None:
            blob = run.spoken_wav() if which == "spoken" else run.heard_wav()
        if not blob:
            found = runs.audio(run_id, which)
            if found is None or not found[0]:
                raise HTTPException(
                    404, f"no {which} audio for live run {run_id} yet"
                )
            blob = found[0]
        return Response(
            content=blob,
            media_type="audio/wav",
            headers={
                "content-disposition":
                    f'inline; filename="{_safe_name(run_id)}-{which}.wav"',
                # Deliberately not cached: a running session grows, so the
                # bytes behind this URL are different a second later.
                "cache-control": "no-store",
            },
        )

    def _live_languages(mode: str) -> dict[str, Any]:
        """What the live path accepts, and the caveat that goes with it."""
        try:
            wiring = wire_or_501(mode)
        except HTTPException:
            return {"tags": [], "note": ""}
        note = ""
        try:
            getter = getattr(wiring.provider, "live_languages", None)
            tags = list(getter()) if getter is not None else []
            teller = getattr(wiring.provider, "live_note", None)
            if teller is not None:
                # What the provider wants said about OUR target, in its own
                # words. Hardcoding one vendor's caveat here is how the page
                # starts describing a model it is no longer talking to.
                note = teller(settings.target_language) or ""
        except Exception:  # noqa: BLE001
            tags = []
        finally:
            wiring.close()
        return {
            "tags": sorted(tags),
            "note": note,
            "missing": [
                tag for tag in MMC_TARGETS if tags and tag not in tags
            ],
        }

    def _live_modes(mode: str) -> list[dict]:
        """The modes the chosen provider offers, in its own words."""
        try:
            wiring = wire_or_501(mode)
        except HTTPException:
            return []
        try:
            getter = getattr(wiring.provider, "live_modes", None)
            return list(getter()) if getter is not None else []
        except Exception:  # noqa: BLE001
            return []
        finally:
            wiring.close()

    # -- the bench ---------------------------------------------------------
    @router.get("/{mode}/coverage")
    def coverage(mode: str) -> JSONResponse:
        """Which models can reach which of MMC's six languages.

        Asked before a bench rather than discovered during one. With all six
        as targets several models drop out immediately, and a table saying so
        beats six failed entries and a spent budget.
        """
        mode_or_501(mode)
        table = bench_mod.coverage(settings, provider_registry)
        table["names"] = {
            tag: tag.upper() for tag in table["targets"]
        }
        return JSONResponse(table)

    @router.post("/{mode}/bench")
    def bench_start(mode: str, body: BenchIn) -> JSONResponse:
        """Capture once, hand the same audio to every model, collect the lot.

        Returns as soon as the work is accepted. The capture alone takes as
        long as the clip, and each model then takes its own time, so the page
        polls rather than holding a request open for a minute.
        """
        info = mode_or_501(mode).info
        if not info.built:
            raise HTTPException(501, f"{info.name} mode is not built")
        try:
            channel = clean_channel(body.channel)
        except NotConfigured as exc:
            raise HTTPException(400, str(exc)) from exc

        wanted = [p.strip().lower() for p in body.providers if p.strip()]
        if not wanted:
            # Everything that could actually run. A bench of one is still a
            # useful baseline, so this is not an error.
            wanted = [
                row["name"] for row in provider_registry.describe(settings)
                if row["selectable"]
            ]
        refused = {
            name: provider_registry.blocked_reason(name, settings)
            for name in wanted
            if provider_registry.blocked_reason(name, settings)
        }
        usable = [n for n in wanted if n not in refused]
        if not usable:
            raise HTTPException(400, "no model can be used: " + "; ".join(
                f"{k}: {v}" for k, v in refused.items()
            ) or "none enabled")

        targets = [t.strip().lower() for t in body.targets if t.strip()]
        if not targets:
            targets = [settings.target_language]

        seconds = body.seconds or settings.grab_seconds
        candidates = bench_mod.plan(settings, usable, targets)
        estimate = bench_mod.cost_estimate(candidates, seconds)

        shadow = modes.get(mode)
        budget = getattr(shadow, "budget", None)
        if budget is not None:
            # The whole fan-out, up front. Half a bench is not a comparison,
            # and finding that out after paying for half of it is the worst of
            # both outcomes.
            allowed, why = budget.allow(channel, estimate["audio_seconds"])
            if not allowed:
                raise HTTPException(
                    409,
                    f"{why} -- this bench would send "
                    f"{estimate['audio_minutes']} minutes "
                    f"({estimate['candidates']} entries x {seconds:.0f}s)",
                )

        try:
            run = bench_mod.Bench(
                channel, candidates, mode=mode, seconds=seconds,
                source_language=body.source_language or "auto",
                label=body.label, note=body.note,
            )
        except NotConfigured as exc:
            raise HTTPException(400, str(exc)) from exc

        def work(bench) -> None:
            captured = grab_audio(
                bench.channel, bench.seconds, audio,
                timeout_s=settings.grab_timeout_s,
            )
            bench.source_url = captured.url
            bench.captured_seconds = captured.seconds
            bench.gaps = list(captured.gaps)
            bench.heard_audio = to_wav(
                captured.samples, captured.rate, settings.upload_rate
            )
            bench_mod.run_bench(
                bench, shadow, settings, captured.samples, captured.rate,
                fallback_language=channel_tag(captured.channel),
            )

        try:
            benches.start(run, work)
        except TranslateError as exc:
            raise HTTPException(409, str(exc)) from exc
        out = run.to_dict()
        out["estimate"] = estimate
        out["refused"] = refused
        return JSONResponse(out, status_code=202)

    @router.get("/{mode}/bench")
    def bench_state(mode: str, id: str = "") -> JSONResponse:
        """The running bench, the one named by ``id``, or the last one.

        Falls through to the database when the id is not in memory, so a
        bench from last week opens the same way as the one from a minute ago.
        """
        mode_or_501(mode)
        live = benches.get(id) if id else benches.current()
        if live is not None:
            out = live.to_dict(scores=runs.scores(live.id))
        elif id:
            stored = runs.bench(id)
            if stored is None:
                raise HTTPException(404, f"no bench {id}")
            out = stored
        else:
            out = {}
        return JSONResponse({
            "bench": out or None,
            "recent": runs.benches(limit=12),
            "targets": list(bench_mod.MMC_TARGETS),
            "score_range": [bench_mod.MIN_SCORE, bench_mod.MAX_SCORE],
        })

    @router.post("/{mode}/bench/{bench_id}/reveal")
    def bench_reveal(mode: str, bench_id: str) -> JSONResponse:
        """Show which model was which. One way only -- a bench cannot be
        un-revealed, because the people who saw it cannot un-see it."""
        mode_or_501(mode)
        live = benches.get(bench_id)
        if live is not None:
            live.reveal()
            runs.reveal(bench_id)
            return JSONResponse(live.to_dict(scores=runs.scores(bench_id)))
        if not runs.reveal(bench_id):
            raise HTTPException(404, f"no bench {bench_id}")
        return JSONResponse(runs.bench(bench_id) or {})

    @router.post("/{mode}/bench/{bench_id}/score")
    def bench_score(mode: str, bench_id: str, body: ScoreIn) -> JSONResponse:
        """Record a judgement. One per person per entry, replaced on a
        second go so nobody can out-vote a colleague by clicking twice."""
        mode_or_501(mode)
        if not body.run_id:
            raise HTTPException(400, "say which entry is being scored")
        if not body.who.strip():
            raise HTTPException(400, "say who is scoring, so the bench has a record")
        runs.score(bench_id, body.run_id, body.who, body.score, body.note)
        return JSONResponse({"scores": runs.scores(bench_id)})

    @router.get("/{mode}/scoreboard")
    def scoreboard(mode: str, target: str = "") -> JSONResponse:
        """What the team has concluded so far, per model and language.

        The artifact the bench exists to produce. Ordered by mean score and
        then by how many judgements back it up, because a 5.0 from one person
        is not better evidence than a 4.6 from six.
        """
        mode_or_501(mode)
        return JSONResponse({
            "rows": runs.scoreboard(target or None),
            "target": target,
            "targets": list(bench_mod.MMC_TARGETS),
        })

    @router.get("/{mode}/bench/{bench_id}/audio/source")
    def bench_audio(mode: str, bench_id: str) -> Response:
        """The one clip every entry in this bench heard."""
        mode_or_501(mode)
        live = benches.get(bench_id)
        blob, kind = b"", "audio/wav"
        if live is not None and live.heard_audio:
            blob, kind = live.heard_audio, live.heard_type
        else:
            found = runs.bench_audio(bench_id)
            if found is not None:
                blob, kind = found
        if not blob:
            raise HTTPException(404, f"no source audio kept for bench {bench_id}")
        return Response(
            content=blob,
            media_type=kind,
            headers={
                "content-disposition":
                    f'inline; filename="{_safe_name(bench_id)}-source.wav"',
                "cache-control": "private, max-age=3600, immutable",
            },
        )

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
                "content-disposition":
                    f'inline; filename="{_safe_name(run_id)}-{which}.wav"',
                # Immutable: a run never changes once stored, so the browser
                # may keep it and seeking in the player costs no round trip.
                "cache-control": "private, max-age=3600, immutable",
            },
        )

    return router
