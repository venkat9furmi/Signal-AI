"""CLI entry point: wire discovery -> capture -> analyzer -> engine.

    python -m commentary_qc.run football-01
    python -m commentary_qc.run <url-or-file> --metrics
    python -m commentary_qc.run football-01 --duration 60 --config thresholds.json

Emits one JSON object per line on stdout: alerts always, plus one metrics
record per window under ``--metrics``. Logs go to stderr, so stdout stays a
clean event stream that can be piped straight into Pub/Sub or a file.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import signal
import sys
import time

from commentary_qc.audio_source import FFmpegSource
from commentary_qc.config import Settings
from commentary_qc.discovery import StreamNotFound, resolve
from commentary_qc.engine import AlertEngine
from commentary_qc.metrics import WindowAnalyzer

log = logging.getLogger("commentary_qc")


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="commentary-qc",
        description="Live commentary audio QC: dead air, loudness, clipping, crowd balance.",
    )
    p.add_argument(
        "target",
        help="Channel name (e.g. football-01), or an explicit stream URL / local file.",
    )
    p.add_argument("--config", help="JSON settings file overriding thresholds and source.")
    p.add_argument("--metrics", action="store_true", help="Emit one metrics record per window.")
    p.add_argument("--duration", type=float, default=0.0, help="Stop after N seconds (0 = forever).")
    p.add_argument("--vad", choices=("silero", "webrtc"), help="Override VAD backend.")
    p.add_argument("--header", action="append", default=[], metavar="K:V",
                   help="Extra HTTP header for the pull; repeatable.")
    p.add_argument("--print-config", action="store_true", help="Dump effective settings and exit.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    settings = Settings.load(args.config)
    # A target containing a scheme or a path separator is an explicit source;
    # anything else is a channel name to be discovered.
    looks_like_url = "://" in args.target or "/" in args.target or "\\" in args.target
    src = dataclasses.replace(
        settings.source,
        url=args.target if looks_like_url else settings.source.url,
        channel=settings.source.channel if looks_like_url else args.target,
    )
    for h in args.header:
        key, _, value = h.partition(":")
        src.headers[key.strip()] = value.strip()
    analyzer_cfg = (
        dataclasses.replace(settings.analyzer, vad_backend=args.vad)
        if args.vad else settings.analyzer
    )
    settings = dataclasses.replace(
        settings, source=src, analyzer=analyzer_cfg,
        emit_metrics=settings.emit_metrics or args.metrics,
    )

    if args.print_config:
        print(settings.to_json())
        return 0

    try:
        resolved = resolve(settings.source)
    except StreamNotFound as exc:
        log.error("%s", exc)
        return 2

    analyzer = WindowAnalyzer(settings.analyzer)
    engine = AlertEngine(settings.thresholds, settings.source.channel)
    source = FFmpegSource(resolved.url, settings.source, settings.ffmpeg_bin)

    log.info(
        "channel=%s url=%s (%s) vad=%s",
        settings.source.channel, resolved.url, resolved.kind, analyzer.vad_name,
    )

    stopping = {"flag": False}

    def _handle(signum, frame):  # noqa: ANN001
        log.info("signal %s, shutting down", signum)
        stopping["flag"] = True
        source.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    started = time.monotonic()
    in_gap = False
    windows = 0
    try:
        for block in source.blocks():
            if stopping["flag"]:
                break
            if block.gap:
                if not in_gap:
                    in_gap = True
                    for alert in engine.on_gap(block.wall, block.reason):
                        _emit(alert.to_dict())
                # Analyzer state describes a connection that no longer exists.
                analyzer.reset()
                continue
            if in_gap:
                in_gap = False
                _emit(engine.on_reconnect(block.wall).to_dict())

            for window in analyzer.push(block.samples, block.wall):
                windows += 1
                if settings.emit_metrics:
                    _emit({"type": "metrics", "channel": settings.source.channel,
                           **window.to_dict()})
                for alert in engine.push(window):
                    _emit(alert.to_dict())

            if args.duration and time.monotonic() - started >= args.duration:
                break
    finally:
        source.stop()

    log.info(
        "stopped after %.1fs, %d windows, %d reconnects, active=%s",
        time.monotonic() - started, windows, source.reconnects, engine.active_alerts() or "none",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
