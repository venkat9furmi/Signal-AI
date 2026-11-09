"""The worker that turns live commentary into captions.

One rule shapes every decision in this file: **monitoring is the product and
translation is a passenger.** A provider that is slow, broken, rate-limited or
out of credit must cost the QC engine nothing -- not a dropped measurement, not
a delayed alert, not a stalled capture thread.

That is bought with three things:

* The monitor threads only ever *append to a buffer*. They never call a
  provider, never touch the network, never wait on a lock that a network call
  holds.
* A **bounded queue** between the monitors and the workers. When it is full
  the oldest segment is discarded, because in live commentary the newest audio
  is the only audio worth captioning. A queue that grows instead would turn a
  slow provider into unbounded memory and captions arriving half an hour late.
* Every failure becomes a stored row rather than an exception that escapes.

The mode switch is read here too, every few seconds, from the control row the
page writes -- so flipping shadow to prod, or swapping the model, takes effect
within seconds without restarting the recorder.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

from commentary_qc.config import ANALYSIS_RATE
from commentary_qc.translate.app import control as control_mod
from commentary_qc.translate import adapters as provider_registry
from commentary_qc.translate.app.budget import Budget
from commentary_qc.translate.core.models import (
    AudioSegment,
    Caption,
    NotConfigured,
    OutOfCredit,
    RateLimited,
    TranslateError,
)
from commentary_qc.translate.app.segmenter import Segmenter
from commentary_qc.translate.settings import Mode, TranslateSettings
from commentary_qc.translate.infra.captions import CaptionStore

log = logging.getLogger(__name__)


class ChannelTap:
    """What a monitor thread sees of the translation layer.

    Deliberately tiny. Two methods, both of which do arithmetic and a list
    append, so the cost on the capture path is the cost of buffering -- and
    when the mode is off, no tap is created at all and even that disappears.
    """

    __slots__ = ("pipeline", "segmenter", "channel")

    def __init__(self, pipeline: "TranslationPipeline", segmenter: Segmenter) -> None:
        self.pipeline = pipeline
        self.segmenter = segmenter
        self.channel = segmenter.channel

    def audio(self, samples, wall: float, t: float) -> None:
        self.segmenter.audio(samples, wall, t)

    def window(self, speech_ratio: float | None) -> None:
        segment = self.segmenter.window(speech_ratio)
        if segment is not None:
            self.pipeline.submit(segment)

    def reset(self) -> None:
        """Throw away buffered audio, after a gap in the stream."""
        self.segmenter._reset()  # noqa: SLF001 - the tap owns this segmenter

    def close(self) -> None:
        segment = self.segmenter.flush()
        if segment is not None:
            self.pipeline.submit(segment)


class TranslationPipeline:
    """Segments in, captions out, on its own threads."""

    def __init__(
        self,
        settings: TranslateSettings,
        store: Any,
        control_path: str | None = None,
        provider_factory: Callable[[str, TranslateSettings], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.captions = CaptionStore(store)
        self.control_path = control_path
        self._factory = provider_factory or provider_registry.build

        self.budget = Budget(settings.daily_minutes, settings.per_channel_minutes)
        self._q: queue.Queue = queue.Queue(maxsize=settings.queue_size)
        self._workers: list[threading.Thread] = []
        self._controller: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._provider: Any | None = None
        self._provider_name = ""
        self._taps: dict[str, ChannelTap] = {}

        # Counters, all for the page. "It is running" is not a useful answer;
        # "47 captions, 2 errors, 1 dropped, 4.1 s median" is.
        self.submitted = 0
        self.dropped = 0
        self.completed = 0
        self.failed = 0
        self.skipped = 0
        self._last_error = ""
        self._last_control = 0.0
        self._started = 0.0
        #: Bumped whenever the mode, provider or scope changes. A monitor that
        #: is already running reads this to know its tap decision is stale --
        #: without it, switching the mode on only reached channels that
        #: happened to start a monitor afterwards, so turning translation on
        #: mid-match appeared to do nothing.
        self.generation = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "TranslationPipeline":
        self._started = time.time()
        self._refresh_control(force=True)
        try:
            spent, by_channel = self.captions.spent_today()
            self.budget.seed(spent, by_channel)
        except Exception:  # noqa: BLE001 - a fresh database has no captions table yet
            log.debug("could not read today's translation spend", exc_info=True)
        for i in range(self.settings.workers):
            t = threading.Thread(
                target=self._worker, name=f"translate-{i}", daemon=True
            )
            t.start()
            self._workers.append(t)
        # A thread of its own for the switch. Polling it from `tap()` alone was
        # circular: nothing called `tap()` until the mode changed, and nothing
        # noticed the mode had changed until something called `tap()`, so
        # turning translation on never reached a channel already on air.
        self._controller = threading.Thread(
            target=self._poll_control, name="translate-control", daemon=True
        )
        self._controller.start()
        log.info(
            "translation pipeline started: mode=%s provider=%s target=%s "
            "budget=%.0f min/day",
            self.settings.mode, self.settings.provider,
            self.settings.target_language, self.settings.daily_minutes,
        )
        return self

    def stop(self) -> None:
        self._stop.set()
        for tap in list(self._taps.values()):
            try:
                tap.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        for _ in self._workers:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass
        for t in self._workers:
            t.join(timeout=5)
        self._workers.clear()
        if self._controller is not None:
            self._controller.join(timeout=5)
            self._controller = None
        with self._lock:
            if self._provider is not None:
                try:
                    self._provider.close()
                except Exception:  # noqa: BLE001
                    pass
                self._provider = None

    # -- the switch ------------------------------------------------------
    def _poll_control(self) -> None:
        """Watch the control row the page writes, for as long as we run."""
        while not self._stop.wait(self.settings.control_poll_s):
            try:
                self._refresh_control(force=True)
            except Exception:  # noqa: BLE001 - the switch must never kill this
                log.debug("control poll failed", exc_info=True)

    def _refresh_control(self, force: bool = False) -> None:
        """Re-read the page's control row, at most every ``control_poll_s``."""
        if not self.control_path:
            return
        now = time.monotonic()
        if not force and now - self._last_control < self.settings.control_poll_s:
            return
        self._last_control = now
        try:
            row = control_mod.read(self.control_path)
        except Exception:  # noqa: BLE001 - the switch must never kill the pipeline
            log.debug("could not read the translation control row", exc_info=True)
            return
        if not row.get("set"):
            return
        updated = control_mod.apply(self.settings, row)
        if updated == self.settings:
            return
        was = (self.settings.mode, self.settings.provider)
        self.settings = updated
        self.generation += 1
        if (updated.mode, updated.provider) != was:
            log.info(
                "translation now mode=%s provider=%s (was mode=%s provider=%s)",
                updated.mode, updated.provider, was[0], was[1],
            )
        with self._lock:
            # Force a rebuild on the next call rather than constructing here:
            # this runs on a monitor thread, and building an adapter may fetch
            # a language catalogue.
            if self._provider_name != updated.provider:
                self._provider = None

    def _get_provider(self) -> Any:
        with self._lock:
            if self._provider is None:
                self._provider = self._factory(self.settings.provider, self.settings)
                self._provider_name = self.settings.provider
                log.info("translation provider is now %s", self._provider_name)
            return self._provider

    # -- taps ------------------------------------------------------------
    def tap(
        self,
        channel: str,
        session: str,
        language: str = "",
        claimed: bool = False,
        rate: int = ANALYSIS_RATE,
    ) -> ChannelTap | None:
        """A tap for one monitor, or ``None`` if this channel is not in scope.

        ``None`` is the common case and the important one: with the mode off,
        or on an unclaimed channel, the monitor gets nothing back and does not
        buffer a single sample.
        """
        self._refresh_control()
        if not self.settings.covers(channel, claimed):
            existing = self._taps.pop(channel, None)
            if existing is not None:
                existing.close()
            return None
        tap = ChannelTap(
            self,
            Segmenter(channel, session, self.settings, language=language, rate=rate),
        )
        self._taps[channel] = tap
        log.info(
            "translating %s (%s -> %s, %s mode)",
            channel, language or "auto", self.settings.target_language, self.settings.mode,
        )
        return tap

    def release(self, channel: str) -> None:
        tap = self._taps.pop(channel, None)
        if tap is not None:
            tap.close()

    # -- queue -----------------------------------------------------------
    def submit(self, segment: AudioSegment) -> None:
        """Hand a segment to the workers. Never blocks, never raises.

        Called from a capture thread. When the queue is full the *oldest*
        segment goes, not this one: captioning what was said thirty seconds
        ago in preference to what is being said now gets the priority exactly
        backwards.
        """
        self.submitted += 1
        try:
            self._q.put_nowait(segment)
            return
        except queue.Full:
            pass
        try:
            stale = self._q.get_nowait()
            self.dropped += 1
            if isinstance(stale, AudioSegment):
                self._record(
                    Caption.failed(
                        stale, self.settings.mode,
                        "dropped: the provider could not keep up with the match",
                        status="skipped",
                    )
                )
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(segment)
        except queue.Full:
            self.dropped += 1

    # -- workers ---------------------------------------------------------
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._handle(item)
            except Exception:  # noqa: BLE001 - a worker must outlive any one segment
                log.exception("translation worker failed on %s", item.describe())

    def _handle(self, segment: AudioSegment) -> None:
        mode = self.settings.mode
        if mode == Mode.OFF:
            return

        allowed, why = self.budget.allow(segment.channel, segment.duration_s)
        if not allowed:
            self.skipped += 1
            self._record(Caption.failed(segment, mode, why, status="skipped"))
            return

        started = time.perf_counter()
        provider_name = self.settings.provider
        try:
            provider = self._get_provider()
            provider_name = getattr(provider, "name", provider_name)
            caption = self._run(provider, segment, mode)
        except OutOfCredit as exc:
            # The one failure where carrying on is actively wasteful. Stop,
            # say so once, and let the page show why nothing is happening.
            self.budget.block(str(exc))
            self.failed += 1
            self._last_error = str(exc)
            self.budget.charge(segment.channel, segment.duration_s)
            self._record(Caption.failed(
                segment, mode, str(exc), provider=provider_name,
                cost_s=segment.duration_s, latency_s=time.perf_counter() - started,
            ))
            return
        except RateLimited as exc:
            # Not charged: the provider refused before doing the work. Putting
            # it back would deepen the backlog, so it is recorded and dropped.
            self.failed += 1
            self._last_error = str(exc)
            self._record(Caption.failed(
                segment, mode, f"rate limited: {exc}", provider=provider_name,
                latency_s=time.perf_counter() - started,
            ))
            return
        except NotConfigured as exc:
            self.failed += 1
            self._last_error = str(exc)
            self._record(Caption.failed(
                segment, mode, str(exc), provider=provider_name, status="skipped",
            ))
            return
        except TranslateError as exc:
            self.failed += 1
            self._last_error = str(exc)
            self.budget.charge(segment.channel, segment.duration_s)
            self._record(Caption.failed(
                segment, mode, str(exc), provider=provider_name,
                cost_s=segment.duration_s, latency_s=time.perf_counter() - started,
            ))
            return
        except Exception as exc:  # noqa: BLE001 - an adapter bug is still a caption
            self.failed += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.exception("provider %s raised on %s", provider_name, segment.describe())
            self.budget.charge(segment.channel, segment.duration_s)
            self._record(Caption.failed(
                segment, mode, self._last_error, provider=provider_name,
                cost_s=segment.duration_s, latency_s=time.perf_counter() - started,
            ))
            return

        self.budget.charge(segment.channel, segment.duration_s)
        self.completed += 1
        self._record(caption)

    def _run(self, provider: Any, segment: AudioSegment, mode: str) -> Caption:
        """Transcribe, then translate if the languages differ."""
        started = time.perf_counter()
        transcript = provider.transcribe(segment, segment.language or "auto")
        source_text = transcript.text
        target = self.settings.target_language
        source = transcript.language or segment.language

        target_text = ""
        if not source_text:
            # The provider heard nothing. Worth a row: it is the difference
            # between a quiet channel and a broken adapter.
            return Caption(
                channel=segment.channel, session=segment.session, seq=segment.seq,
                wall=segment.wall, t0=segment.t0, t1=segment.t1, mode=mode,
                status="ok", provider=getattr(provider, "name", ""),
                model=getattr(provider, "model", ""),
                source_language=source, source_text="", target_language=target,
                target_text="", latency_s=time.perf_counter() - started,
                cost_s=segment.duration_s,
            )
        from commentary_qc.translate.core.languages import primary

        if primary(source) and primary(source) == primary(target):
            # Already in the target language. Transcribing English for an
            # English feed is the whole job done; paying to translate it to
            # itself is not.
            target_text = source_text
        else:
            lines = [u.text for u in transcript.utterances if u.text.strip()]
            translated = provider.translate(lines, source, target)
            target_text = translated.text

        return Caption(
            channel=segment.channel, session=segment.session, seq=segment.seq,
            wall=segment.wall, t0=segment.t0, t1=segment.t1, mode=mode,
            status="ok", provider=getattr(provider, "name", ""),
            model=getattr(provider, "model", ""),
            source_language=source, source_text=source_text,
            target_language=target, target_text=target_text,
            latency_s=time.perf_counter() - started, cost_s=segment.duration_s,
        )

    def _record(self, caption: Caption) -> None:
        try:
            self.captions.record(caption)
        except Exception:  # noqa: BLE001 - never let storage kill a worker
            log.exception("could not store a caption for %s", caption.channel)

    # -- reporting -------------------------------------------------------
    def stats(self) -> dict:
        return {
            "mode": self.settings.mode,
            "provider": self.settings.provider,
            "target_language": self.settings.target_language,
            "publishes": self.settings.publishes,
            "running": self.settings.running,
            "started": self._started or None,
            "channels": sorted(self._taps),
            "queued": self._q.qsize(),
            "queue_size": self.settings.queue_size,
            "workers": len(self._workers),
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "dropped": self.dropped,
            "dropped_quiet": sum(t.segmenter.dropped_quiet for t in self._taps.values()),
            "last_error": self._last_error,
            "budget": self.budget.snapshot(),
        }
