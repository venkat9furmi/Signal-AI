"""Keep the audio, because the platform does not.

The streams are live-only: what goes out tonight is unrecoverable tomorrow. Every
threshold in this project is still a guess precisely because we have measured a
lot of audio and never kept any of it, so nobody has been able to listen and say
whether an alert was fair.

This records each channel to timestamped one-minute segments alongside the
metrics. ffmpeg copies the AAC through without re-encoding, so it costs almost
no CPU and the files are exactly what the bookmaker received.

Segments are named by wall clock (``20260909-201530.aac``), which is what lets a
measurement at a given second be matched to the audio it came from.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import SourceConfig
from .discovery import StreamNotFound, resolve

log = logging.getLogger(__name__)

#: One minute per file. Short enough to pick a moment precisely, long enough
#: that a three-hour match is a few hundred files rather than thousands.
SEGMENT_SECONDS = 60


class ClipRecorder:
    """One ffmpeg per channel, writing timestamped segments until stopped."""

    def __init__(
        self,
        channels: list[str],
        out_dir: str,
        source: SourceConfig,
        ffmpeg_bin: str = "ffmpeg",
        segment_seconds: int = SEGMENT_SECONDS,
    ) -> None:
        self.channels = channels
        self.out = Path(out_dir)
        self.source = source
        self.ffmpeg = shutil.which(ffmpeg_bin) or ffmpeg_bin
        self.segment_seconds = segment_seconds
        self._procs: dict[str, subprocess.Popen] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _command(self, channel: str, url: str) -> list[str]:
        d = self.out / channel
        d.mkdir(parents=True, exist_ok=True)
        return [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
            "-user_agent", self.source.user_agent,
            "-i", url,
            "-vn", "-map", "0:a:0",
            # Stream copy: no re-encode, so these files are bit-for-bit what
            # was broadcast and cost essentially no CPU.
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self.segment_seconds),
            "-reset_timestamps", "1",
            "-strftime", "1",
            str(d / "%Y%m%d-%H%M%S.aac"),
        ]

    def _start_one(self, channel: str) -> None:
        try:
            cfg = dataclasses.replace(self.source, channel=channel, url=None)
            url = resolve(cfg).url
        except StreamNotFound:
            log.warning("clips: %s is not broadcasting", channel)
            return
        proc = subprocess.Popen(
            self._command(channel, url),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._procs[channel] = proc
        log.info("clips: recording %s", channel)

    def start(self) -> "ClipRecorder":
        for ch in self.channels:
            self._start_one(ch)
        self._thread = threading.Thread(target=self._supervise, name="clips", daemon=True)
        self._thread.start()
        return self

    def _supervise(self) -> None:
        """Restart any capture that dies, so a blip does not cost the rest."""
        while not self._stop.wait(20):
            for ch in list(self.channels):
                proc = self._procs.get(ch)
                if proc is None or proc.poll() is not None:
                    log.warning("clips: restarting %s", ch)
                    self._procs.pop(ch, None)
                    self._start_one(ch)

    def stop(self) -> None:
        self._stop.set()
        for ch, proc in self._procs.items():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - teardown must not raise
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._procs.clear()

    def stats(self) -> dict[str, tuple[int, float]]:
        """(file count, megabytes) per channel."""
        out = {}
        for ch in self.channels:
            files = list((self.out / ch).glob("*.aac"))
            out[ch] = (len(files), sum(f.stat().st_size for f in files) / 1e6)
        return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="commentary-qc-clips")
    p.add_argument("channels", nargs="+", help="Channel slugs to record.")
    p.add_argument("--out", default="clips", help="Directory for the segments.")
    p.add_argument("--duration", type=float, default=0.0, help="Stop after N seconds.")
    p.add_argument("--segment", type=int, default=SEGMENT_SECONDS)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    rec = ClipRecorder(args.channels, args.out, SourceConfig(), segment_seconds=args.segment).start()
    try:
        deadline = time.time() + args.duration if args.duration else None
        while deadline is None or time.time() < deadline:
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        rec.stop()
        for ch, (n, mb) in rec.stats().items():
            log.info("clips: %s -> %d files, %.0f MB", ch, n, mb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
