"""Entry point for continuous multi-channel monitoring.

    python -m commentary_qc.watch --channels channels.json --metrics

Runs until interrupted: discovers which of the configured channels are on air,
watches all of them at once, stores everything, and delivers alerts.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import threading

from commentary_qc.config import Settings
from commentary_qc.notify import SlackNotifier
from commentary_qc.registry import Registry, default_channels
from commentary_qc.store import EventStore
from commentary_qc.supervisor import Supervisor
from commentary_qc.translate import TranslationPipeline, TranslateSettings
from commentary_qc.translate.settings import Mode

log = logging.getLogger("commentary_qc.watch")


class _LogFormat(logging.Formatter):
    """Compact operator-facing format.

    The default `time LEVEL module: message` wastes most of the line on things
    a human watching a match does not need. Time and the message carry the
    information; the level only appears when it is not INFO, which makes
    warnings stand out instead of blending in.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        msg = record.getMessage()
        line = f"{ts}  {record.levelname:<7} {msg}" if record.levelno > logging.INFO else f"{ts}  {msg}"
        # Tracebacks ride on ERROR records (log.exception), so they are kept at
        # every level -- dropping them once hid why every monitor was dying.
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="commentary-qc-watch",
        description="Watch every live MMC channel and alert on audio faults.",
    )
    p.add_argument("--channels", default="channels.json",
                   help="Channels to watch whenever they are on air. Pass '' to watch "
                        "claimed channels only.")
    p.add_argument("--claims", default="claims.db",
                   help="Claims file written by the commentator page. A claimed channel "
                        "is monitored within seconds. Pass '' to ignore claims.")
    p.add_argument("--config", help="JSON settings file (thresholds, source, analyzer).")
    p.add_argument("--db", default="commentary_qc.db", help="SQLite event store path.")
    p.add_argument("--metrics", action="store_true",
                   help="Store one metrics row per second per channel (the training set).")
    p.add_argument("--prune-days", type=float, default=7.0,
                   help="Delete measurements older than this many days (0 = keep all). "
                        "Alerts are always kept.")
    p.add_argument("--poll", type=float, default=30.0,
                   help="Seconds between liveness checks (default 30).")
    p.add_argument("--duration", type=float, default=0.0,
                   help="Stop cleanly after N seconds (0 = run forever). For testing.")
    p.add_argument("--slack-webhook", default=os.environ.get("SLACK_WEBHOOK_URL"))
    p.add_argument("--slack-token", default=os.environ.get("SLACK_BOT_TOKEN"))
    p.add_argument("--slack-channel", default=os.environ.get("SLACK_CHANNEL_ID"))
    p.add_argument("--min-severity", choices=("info", "warning", "critical"), default="warning",
                   help="Lowest severity worth a Slack message (default warning).")
    p.add_argument("--log", help="Also write the log to this file, flushed on every line.")
    p.add_argument("--status-every", type=float, default=300.0,
                   help="Seconds between per-channel status lines (default 300).")
    p.add_argument("--translate", choices=Mode.ALL, default=None,
                   help="Continuous transcription of claimed channels: 'off', "
                        "'shadow' (runs and stores everything but publishes "
                        "nothing) or 'standard'. Separate from the /translate "
                        "page, which runs one clip at a time on demand and needs "
                        "nothing switched on here. Defaults to "
                        "QC_TRANSLATE_MODE, or off.")
    p.add_argument("--translate-provider", default=None,
                   help="Which model: 'mock' (offline and free) or 'camb'. Camb needs "
                        "CAMB_API_KEY. Defaults to QC_TRANSLATE_PROVIDER, or mock.")
    p.add_argument("--translate-target", default=None,
                   help="Language everything is translated into (default en-us).")
    p.add_argument("--translate-daily-minutes", type=float, default=None,
                   help="Hard cap on audio sent to the provider per UTC day. This is the "
                        "free-credit guard; the pipeline stops and says so.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if args.log:
        # FileHandler flushes after every record, so an alert is on disk the
        # instant it fires rather than whenever a buffer happens to fill.
        # Rotate at 20 MB, keep five: a recorder that runs for months should
        # not fill the disk with its own log.
        handlers.append(logging.handlers.RotatingFileHandler(
            args.log, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(_LogFormat())
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
    )

    settings = Settings.load(args.config)
    # Without ffmpeg every monitor dies the instant it starts and is restarted
    # every poll, forever, while the page still looks alive. Refuse to start.
    if not shutil.which(settings.ffmpeg_bin):
        log.error("ffmpeg not found (%r). Put it on PATH or set FFMPEG_BIN to its full path.",
                  settings.ffmpeg_bin)
        return 2
    settings = dataclasses.replace(settings, emit_metrics=settings.emit_metrics or args.metrics)

    if args.channels and os.path.exists(args.channels):
        registry = Registry.load(args.channels, settings.source, args.poll)
    elif args.claims:
        # Claims alone decide what is watched: nothing runs until a
        # commentator picks a channel.
        log.info("no channel list - watching claimed channels only")
        registry = Registry([], settings.source, args.poll)
    else:
        log.warning(
            "%s not found - falling back to every channel seen live during discovery. "
            "This includes other providers' commentary. Write MMC's own channels to %s "
            "before running for real.",
            args.channels, args.channels,
        )
        registry = Registry(default_channels(), settings.source, args.poll)

    store = EventStore(args.db).start()
    notifier = SlackNotifier(
        webhook_url=args.slack_webhook,
        bot_token=args.slack_token,
        channel_id=args.slack_channel,
        min_severity=args.min_severity,
    )
    # The translation layer is optional and off unless asked for. Built here
    # rather than inside the supervisor so that a bad configuration -- a mode
    # with no key, an unknown provider -- fails at startup with one readable
    # line, instead of once per segment on every channel.
    translator = None
    tcfg = TranslateSettings.from_env()
    overrides = {}
    if args.translate:
        overrides["mode"] = args.translate
    if args.translate_provider:
        overrides["provider"] = args.translate_provider
    if args.translate_target:
        overrides["target_language"] = args.translate_target
    if args.translate_daily_minutes is not None:
        overrides["daily_minutes"] = args.translate_daily_minutes
    if overrides:
        tcfg = dataclasses.replace(tcfg, **overrides)
    if tcfg.provider == "camb" and not tcfg.camb_api_key and tcfg.mode != Mode.OFF:
        log.error("--translate %s asks for Camb AI but CAMB_API_KEY is not set; "
                  "staying off. Use --translate-provider mock to run it offline.",
                  tcfg.mode)
        tcfg = dataclasses.replace(tcfg, mode=Mode.OFF)
    # Started even when off: the page writes the mode into the claims file and
    # the pipeline picks it up within seconds, so translation can be switched
    # on mid-match without restarting the recorder. An off pipeline installs
    # no tap, so it costs a few idle threads and nothing else.
    translator = TranslationPipeline(
        tcfg, store, control_path=args.claims or None
    ).start()

    supervisor = Supervisor(
        registry, settings, store, notifier, args.status_every,
        claims_path=args.claims or None, retention_days=args.prune_days,
        translator=translator,
    )

    def _handle(signum, frame):  # noqa: ANN001
        log.info("signal %s, shutting down", signum)
        supervisor.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    if args.duration:
        threading.Timer(args.duration, supervisor.stop).start()

    log.info("=" * 78)
    th = settings.thresholds
    log.info("commentary QC  |  %d configured channels  |  claims %s  |  db %s",
             len(registry.channels), args.claims or "off", args.db)
    log.info("quiet < %.0f (clears > %.0f)  |  loud > %.0f (clears < %.0f)  |  "
             "peak max %.0f dBTP  |  voice/crowd %.0f..%.0f dB  |  filler repeat >= %.2f",
             th.too_quiet_lufs, th.too_quiet_clear_lufs, th.too_loud_lufs,
             th.too_loud_clear_lufs, th.true_peak_dbtp, th.crowd_dominating_db,
             th.voice_dominates_db, th.filler_loop_score)
    log.info("=" * 78)
    log.info("translation  |  mode %s  |  provider %s  |  -> %s  |  %.0f min/day cap",
             tcfg.mode, tcfg.provider, tcfg.target_language, tcfg.daily_minutes)
    log.info("=" * 78)
    try:
        supervisor.run()
    finally:
        if translator is not None:
            translator.stop()
        notifier.close()
        store.close()
    return 0


def write_default_channels(path: str = "channels.json") -> None:
    """Write a starter channel file for editing."""
    data = {"channels": [dataclasses.asdict(c) for c in default_channels()]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
