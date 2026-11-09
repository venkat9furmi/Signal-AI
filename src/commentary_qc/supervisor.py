"""Watch every live MMC channel at once.

One monitor thread per live channel: pull, analyse, alert, store. A channel
that starts broadcasting gets a thread; one that stops has its thread retired.
Nothing needs starting by hand.

Threads rather than processes: ffmpeg is a separate process anyway, and both
numpy and onnxruntime release the GIL during the heavy work, so ~11 channels at
35x real time sit comfortably on a fraction of one core. A crash in one monitor
is contained -- it is logged, the channel is marked down, and the next poll
restarts it without touching the others.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid

from commentary_qc.audio_source import FFmpegSource
from commentary_qc.config import Settings, Thresholds
from commentary_qc.engine import AlertEngine
from commentary_qc.metrics import WindowAnalyzer
from commentary_qc.registry import Channel, ChannelStatus, Registry
from commentary_qc.scanner import describe
from commentary_qc.store import EventStore, read_open_claims

log = logging.getLogger(__name__)


def human_duration(seconds: float) -> str:
    """Compact duration for log lines: 8s, 1m 32s, 16m, 1h 04m."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m}m {r:02d}s" if m < 10 else f"{m}m"
    h, r = divmod(s, 3600)
    return f"{h}h {r // 60:02d}m"


class OutageTracker:
    """Which channels are missing audio right now.

    A stream that stops looks identical from one channel: the audio just ends.
    The difference is visible only across channels -- when several drop within
    seconds of each other it is our connection or the vendor's server, and when
    one drops alone it is that channel's encoder. Telling the two apart is the
    difference between calling one commentator and calling the vendor.
    """

    def __init__(self, window_s: float = 45.0) -> None:
        self.window_s = window_s
        #: Set by the supervisor, so "how many of us" has a denominator.
        self.total = 0
        self._gaps: dict[str, float] = {}
        self._lock = threading.Lock()

    def gap(self, channel: str) -> None:
        with self._lock:
            self._gaps[channel] = time.time()

    def ok(self, channel: str) -> None:
        with self._lock:
            self._gaps.pop(channel, None)

    def others(self, channel: str) -> int:
        """How many OTHER channels lost audio in the same moment."""
        cutoff = time.time() - self.window_s
        with self._lock:
            return sum(1 for name, t in self._gaps.items() if name != channel and t >= cutoff)

    def attribute(self, channel: str) -> str:
        """Plain words for who is likely at fault."""
        others = self.others(channel)
        if others == 0:
            return "only this channel - most likely the encoder or the vendor"
        together = others + 1
        if self.total and together >= max(2, self.total // 2):
            return f"{together} channels at once - most likely our connection or the stream server"
        return f"{together} channels at once - the stream server, not one encoder"


class ChannelMonitor:
    """Runs the full pipeline for one channel until told to stop."""

    def __init__(
        self,
        channel: Channel,
        url: str,
        settings: Settings,
        store: EventStore,
        notifier=None,
        shutdown: threading.Event | None = None,
        outage: "OutageTracker | None" = None,
        translator=None,
    ) -> None:
        self.channel = channel
        self.url = url
        self.settings = settings
        self.store = store
        self.notifier = notifier
        #: The translation pipeline, or None when the feature is off. A
        #: passenger: it is asked for a tap once per connection and otherwise
        #: receives nothing but buffered audio. Nothing in this class waits on
        #: it, so a slow or broken provider cannot delay an alert.
        self.translator = translator
        # Service-wide shutdown, shared with the supervisor. Needed as well as
        # the per-monitor flag: when the whole process group is signalled, every
        # ffmpeg dies at once and a monitor can observe EOF before its own stop
        # flag is set, reporting a shutdown as a stream outage.
        self._shutdown = shutdown or threading.Event()
        self.outage = outage or OutageTracker()
        # Latest window and firing rules, read by the supervisor's status line.
        self.last_window: dict | None = None
        self.active: list[str] = []
        #: The open claim on this channel, if a commentator has picked it.
        self.claim: dict | None = None
        self._last_state = 0.0
        # A session id ties every alert and metric from one broadcast together,
        # which is what lets Slack keep a single thread per match.
        self.session = uuid.uuid4().hex[:12]
        self.started = time.time()
        self._stop = threading.Event()
        self._source: FFmpegSource | None = None
        self.thread = threading.Thread(
            target=self._run, name=f"monitor-{channel.name}", daemon=True
        )

    def start(self) -> "ChannelMonitor":
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._source:
            self._source.stop()

    def _store_this_window(self, seq: int) -> bool:
        """Every window for a claimed channel, a sample of the rest.

        The measurements are for the trend chart, the match report and a
        future classifier. For a channel nobody is commentating on, a tenth of
        them is plenty - and storing all of them for every channel is what put
        the database a minute behind real time.
        """
        if self.claim:
            return True
        every = max(1, self.settings.metrics_sample_unclaimed)
        return seq % every == 0

    def _stopping(self) -> bool:
        return self._stop.is_set() or self._shutdown.is_set()

    def _emit(self, alert: dict) -> None:
        # Log the alert here rather than in the notifier: alerts must reach the
        # log file the moment they happen, whether or not Slack is configured.
        dur = human_duration(alert.get("held_s") or 0)
        if alert["event"] in ("FIRE", "ESCALATE"):
            body = f"{alert['message']}  (held {dur})"
        else:
            body = f"resolved after {dur}"
        log.info(
            "%-6s %-8s %-16s %s",
            alert["event"], self.channel.short, alert["rule"], body,
        )
        self.store.record_alert(self.channel.name, self.session, alert)
        # claimed-only delivery: messages are for the commentator, and a
        # channel nobody has claimed has no commentator to message. Measured
        # over the week to 2026-09-26, every one of 3,155 alerts was on an
        # unclaimed channel -- 523 of them `stream_down` from channels simply
        # finishing their broadcast. All of it still lands in the database and
        # on the page; none of it is worth a Slack message.
        if not self.claim:
            return
        if self.notifier:
            try:
                self.notifier.send(self.channel, self.session, alert)
            except Exception:  # noqa: BLE001 - delivery must not kill monitoring
                log.exception("notifier failed for %s", self.channel.name)

    def _run(self) -> None:
        # Per-channel threshold overrides sit on top of the global defaults, so
        # a hot channel can be retuned without affecting the rest.
        th = self.settings.thresholds
        if self.channel.thresholds:
            th = dataclasses.replace(th, **self.channel.thresholds)

        analyzer = WindowAnalyzer(self.settings.analyzer)
        engine = AlertEngine(th, self.channel.name)
        source_cfg = dataclasses.replace(self.settings.source, channel=self.channel.name)
        self._source = FFmpegSource(self.url, source_cfg, self.settings.ffmpeg_bin)

        log.info("ON AIR %-8s %s", self.channel.short, self.channel.label)
        self.store.set_channel_state(
            self.channel.name, live=True, session=self.session, since=self.started
        )

        tap = None
        tap_claimed = bool(self.claim)
        tap_gen = -1
        if self.translator is not None:
            tap_gen = getattr(self.translator, "generation", 0)
            try:
                tap = self.translator.tap(
                    self.channel.name, self.session,
                    language=self.channel.language or "",
                    claimed=bool(self.claim),
                )
            except Exception:  # noqa: BLE001 - never block monitoring on this
                log.exception("could not start translating %s", self.channel.name)

        in_gap = False
        try:
            for block in self._source.blocks():
                if self._stopping():
                    break
                if block.gap:
                    # Our own shutdown kills ffmpeg, which surfaces as EOF.
                    # Reporting that as stream_down would page someone every
                    # time the service restarts.
                    if self._stopping():
                        break
                    if not in_gap:
                        in_gap = True
                        self.outage.gap(self.channel.name)
                        reason = f"{block.reason}; {self.outage.attribute(self.channel.name)}"
                        for alert in engine.on_gap(block.wall, reason):
                            self._emit(alert.to_dict())
                        # Say so now. Live state is otherwise only refreshed by
                        # arriving audio, so without this the page would keep
                        # showing the last good reading through the outage.
                        self.store.set_channel_state(
                            self.channel.name, live=False, session=self.session,
                            since=self.started, active=["stream_down"],
                        )
                    analyzer.reset()
                    if tap is not None:
                        # Audio either side of an outage is not one phrase.
                        tap.reset()
                    continue
                if in_gap:
                    in_gap = False
                    self.outage.ok(self.channel.name)
                    self._emit(engine.on_reconnect(block.wall).to_dict())

                # Two things change whether this channel should be translated:
                # a claim arriving or being released, and somebody moving the
                # mode or the model on the page. Comparing both against what
                # we last saw keeps this off the per-block path while still
                # letting translation be switched on in the middle of a match.
                if self.translator is not None:
                    gen = getattr(self.translator, "generation", 0)
                    if bool(self.claim) != tap_claimed or gen != tap_gen:
                        tap_claimed = bool(self.claim)
                        tap_gen = gen
                        try:
                            if tap is not None:
                                tap.close()
                            tap = self.translator.tap(
                                self.channel.name, self.session,
                                language=self.channel.language or "",
                                claimed=tap_claimed,
                            )
                        except Exception:  # noqa: BLE001
                            log.exception("could not retap %s", self.channel.name)
                            tap = None

                if tap is not None:
                    tap.audio(block.samples, block.wall, analyzer.elapsed)

                for window in analyzer.push(block.samples, block.wall):
                    row = window.to_dict()
                    self.last_window = row
                    if tap is not None:
                        # The speech ratio the QC rules already needed is what
                        # tells the segmenter where the sentence ended, so
                        # finding the pauses costs no second pass over the
                        # audio and no second VAD.
                        tap.window(row.get("speech_ratio"))
                    if self.settings.emit_metrics and self._store_this_window(window.seq):
                        self.store.record_metrics(self.channel.name, self.session, row)
                    alerts = engine.push(window)
                    for alert in alerts:
                        self._emit(alert.to_dict())
                    self.active = engine.active_alerts()
                    # Refresh live state often enough for the page (which polls
                    # every 5 s) and no more: this is one database write per
                    # channel per update, and there are dozens of channels.
                    now = time.monotonic()
                    if (now - self._last_state >= self.settings.state_write_every_s
                            or alerts):
                        self._last_state = now
                        self.store.set_channel_state(
                            self.channel.name, live=True, session=self.session,
                            since=self.started, active=self.active, last_metric=row,
                        )
        except Exception:  # noqa: BLE001 - one channel must not take down the rest
            log.exception("monitor for %s died", self.channel.name)
        finally:
            self._source.stop()
            if tap is not None:
                # The last phrase before a channel goes off air is usually the
                # full-time summary; dropping it loses the end of every match.
                try:
                    tap.close()
                except Exception:  # noqa: BLE001 - teardown must not raise
                    log.debug("could not flush the translation tap", exc_info=True)
            if self.translator is not None:
                self.translator.release(self.channel.name)
            self.store.set_channel_state(self.channel.name, live=False, session=self.session)
            self.store.clear_since(self.channel.name)
            log.info("OFF    %-8s stopped watching", self.channel.short)


class Supervisor:
    """Keeps a monitor running for every channel that should be watched.

    Two things decide what that is:

    * the configured list (``channels.json``), watched whenever it is on air;
    * **claims**: a commentator picking their channel on the web page. A claim
      starts monitoring within a few seconds, on any channel the platform
      carries, and releasing it stops monitoring again unless the channel is
      also configured. MMC's channels cannot be told apart from anyone else's,
      so the people doing the work are what scopes it.

    The web app owns the claims file and this process only reads it, so each
    database keeps a single writer.
    """

    def __init__(
        self,
        registry: Registry,
        settings: Settings,
        store: EventStore,
        notifier=None,
        status_every_s: float = 300.0,
        claims_path: str | None = None,
        claim_poll_s: float = 3.0,
        retention_days: float = 7.0,
        translator=None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.store = store
        self.notifier = notifier
        #: The translation pipeline, handed to each monitor. None when the
        #: feature is switched off, which is the default.
        self.translator = translator
        #: How often to log a line per channel even when nothing is wrong.
        #: Without it the log falls silent between alerts and there is no way
        #: to tell a healthy feed from a dead recorder.
        self.status_every_s = status_every_s
        self.claims_path = claims_path
        #: Claims are a cheap local read, so they are checked far more often
        #: than the CDN is polled: a commentator should see their channel start
        #: in seconds, not wait up to half a minute.
        self.claim_poll_s = claim_poll_s
        self.started = time.time()
        #: Measurements older than this are deleted; 0 keeps everything.
        self.retention_days = retention_days
        self._last_prune = float("-inf")
        self._last_status = 0.0
        self._last_poll = float("-inf")
        self._monitors: dict[str, ChannelMonitor] = {}
        self._claims: dict[str, dict] = {}
        #: Channels watched only because someone claimed them.
        self._claim_only: set[str] = set()
        self._outage = OutageTracker()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info(
            "supervising %d configured channels%s, polling every %.0fs",
            len(self.registry.channels),
            f" + claims from {self.claims_path}" if self.claims_path else "",
            self.registry.poll_interval_s,
        )
        self.store.mark_all_down()
        try:
            while not self._stop.is_set():
                self._sync_claims()
                if time.monotonic() - self._last_poll >= self.registry.poll_interval_s:
                    self._last_poll = time.monotonic()
                    self._reconcile()
                self._log_status()
                self._prune()
                self._heartbeat()
                self._stop.wait(self.claim_poll_s)
        finally:
            for monitor in list(self._monitors.values()):
                monitor.stop()
            for monitor in list(self._monitors.values()):
                monitor.thread.join(timeout=10)
            self._monitors.clear()
            self._heartbeat(stopped=True)
            log.info("supervisor stopped")

    def _prune(self, every_s: float = 6 * 3600) -> None:
        """Keep the measurements file from growing for ever."""
        if time.monotonic() - self._last_prune < every_s:
            return
        self._last_prune = time.monotonic()
        try:
            n = self.store.prune(self.retention_days)
        except Exception:  # noqa: BLE001 - housekeeping must not end the loop
            log.exception("prune failed")
            return
        if n:
            log.info("pruned %d measurement rows older than %.0f days",
                     n, self.retention_days)

    def _heartbeat(self, stopped: bool = False) -> None:
        self.store.heartbeat("recorder", self.started, {
            "monitoring": sorted(self._monitors),
            "claimed": sorted(self._claims),
            "configured": sorted(set(self.registry.channels) - self._claim_only),
            "slack": getattr(self.notifier, "mode", "off"),
            "stopped": stopped,
            # The page runs in another container and cannot see the pipeline's
            # counters directly, so they ride along with the heartbeat.
            "translate": self._translate_stats(),
        })

    def _translate_stats(self) -> dict:
        if self.translator is None:
            return {"mode": "off", "running": False}
        try:
            return self.translator.stats()
        except Exception:  # noqa: BLE001 - the heartbeat must always be written
            log.debug("could not read translation stats", exc_info=True)
            return {"mode": "unknown", "running": False}

    def _log_status(self) -> None:
        """Periodic per-channel line so a quiet log still proves life."""
        now = time.monotonic()
        if now - self._last_status < self.status_every_s:
            return
        self._last_status = now
        for monitor in sorted(self._monitors.values(), key=lambda m: m.channel.name):
            w = monitor.last_window
            if not w:
                continue
            faults = ", ".join(monitor.active) if monitor.active else "ok"
            log.info(
                "STATUS %-8s %7.1f LUFS/min  speech %3.0f%%  peak %6.1f dBTP  "
                "voice %+6.1f dB over crowd  repeat %.2f  [%s]",
                monitor.channel.short, w["lufs_minute"], w["speech_ratio"] * 100,
                w["true_peak_dbtp"], w["voice_to_crowd_med"], w.get("loop_score", 0.0), faults,
            )

    # -- claims -------------------------------------------------------------
    def _sync_claims(self) -> None:
        """Start monitoring newly claimed channels; stop released ones."""
        if not self.claims_path:
            return
        try:
            claims = read_open_claims(self.claims_path)
        except Exception:  # noqa: BLE001 - a bad read must not end the loop
            log.exception("could not read claims")
            return
        old, self._claims = self._claims, claims

        for name, claim in claims.items():
            if old.get(name, {}).get("id") == claim.get("id"):
                continue
            if name not in self.registry.channels:
                self.registry.add(describe(name))
                self._claim_only.add(name)
            channel = self.registry.channels[name]
            who = claim.get("commentator") or "unnamed"
            what = f" - {claim['fixture']}" if claim.get("fixture") else ""
            log.info("CLAIM  %-8s %s%s", channel.short, who, what)
            monitor = self._monitors.get(name)
            if monitor:
                monitor.claim = claim
                if self.notifier:
                    try:
                        self.notifier.claimed(channel, monitor.session, claim)
                    except Exception:  # noqa: BLE001
                        log.exception("notifier claimed failed for %s", name)
                continue
            # Probe now rather than waiting for the next liveness poll.
            status = self.registry.probe_now(name)
            if status.live:
                self._start(status)
            else:
                log.info("WAIT   %-8s claimed, not broadcasting yet", channel.short)

        for name in set(old) - set(claims):
            channel = self.registry.channels.get(name)
            log.info("FREE   %-8s claim released", channel.short if channel else name)
            monitor = self._monitors.get(name)
            if monitor:
                # Releasing closes the Slack thread. Without this a channel on
                # the standing list keeps its monitor running, so nothing was
                # ever posted and the thread simply stopped mid-conversation -
                # indistinguishable, to a reader, from the system dying.
                if self.notifier and monitor.claim and channel:
                    try:
                        self.notifier.session_ended(channel, monitor.session)
                    except Exception:  # noqa: BLE001
                        log.exception("notifier session_ended failed for %s", name)
                monitor.claim = None
            if name in self._claim_only:
                self._claim_only.discard(name)
                self._stop_channel(name, "claim released")
                self.registry.remove(name)

    # -- liveness -----------------------------------------------------------
    def _reconcile(self) -> None:
        """Bring running monitors in line with what is actually broadcasting."""
        try:
            up, down = self.registry.poll()
        except Exception:  # noqa: BLE001 - a failed poll must not end the loop
            log.exception("liveness poll failed")
            return

        for status in up:
            self._start(status)
        for status in down:
            self._stop_channel(status.channel.name, "channel went off air")

        # A monitor whose thread has died (crash, or a source that gave up)
        # while the channel is still live gets restarted on the next pass.
        for name, monitor in list(self._monitors.items()):
            if not monitor.thread.is_alive():
                del self._monitors[name]
                status = self.registry.status(name)
                if status and status.live and not self._stop.is_set():
                    log.warning("restarting monitor for %s", name)
                    self._start(status)

    def _start(self, status: ChannelStatus) -> None:
        name = status.channel.name
        if name in self._monitors or not status.url:
            return
        claim = self._claims.get(name)
        monitor = ChannelMonitor(
            status.channel, status.url, self.settings, self.store, self.notifier,
            shutdown=self._stop, outage=self._outage, translator=self.translator,
        )
        monitor.claim = claim
        self._monitors[name] = monitor.start()
        self._outage.total = len(self._monitors)
        # Same rule for the thread itself: no claim, no Slack thread.
        if self.notifier and claim:
            try:
                self.notifier.session_started(status.channel, monitor.session, claim)
            except Exception:  # noqa: BLE001
                log.exception("notifier session_started failed for %s", name)

    def _stop_channel(self, name: str, why: str) -> None:
        monitor = self._monitors.pop(name, None)
        if not monitor:
            return
        log.info("%s: %s", name, why)
        monitor.stop()
        if self.notifier and monitor.claim:
            try:
                self.notifier.session_ended(monitor.channel, monitor.session)
            except Exception:  # noqa: BLE001
                log.exception("notifier session_ended failed for %s", name)
