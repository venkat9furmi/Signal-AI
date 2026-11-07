"""Capture worker: ffmpeg -> float32 mono PCM, supervised.

One process per stream. ffmpeg does the protocol work (HLS, RTMP, Icecast,
file, or a loopback device -- all the same code path), we read raw PCM off its
stdout.

Two failure modes have to be distinguished, because they mean different things
operationally:

* ffmpeg exits      -- the pull broke; reconnect.
* ffmpeg keeps running but stops producing audio -- the encoder stalled or the
  CDN is serving a frozen playlist. This is invisible to a plain read loop,
  which just blocks forever. A reader thread plus a queue timeout catches it.

Both surface as a ``gap`` event so the runner can raise ``stream_down`` rather
than silently reporting dead air on an absent stream.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np

from .config import ANALYSIS_RATE, SourceConfig

log = logging.getLogger(__name__)

#: Read granularity off the ffmpeg pipe. 100 ms keeps alert latency low
#: without one syscall per frame.
_READ_SAMPLES = ANALYSIS_RATE // 10


@dataclass(slots=True)
class Block:
    """One chunk of audio, or a gap marker when the stream is not delivering."""

    samples: np.ndarray | None
    wall: float
    gap: bool = False
    reason: str = ""


class FFmpegSource:
    """Supervised ffmpeg pull yielding :class:`Block` objects forever."""

    #: URL schemes that denote a continuous feed rather than finite media.
    LIVE_SCHEMES = ("http://", "https://", "rtmp://", "rtmps://", "rtsp://", "srt://", "udp://")

    def __init__(self, url: str, cfg: SourceConfig, ffmpeg_bin: str = "ffmpeg") -> None:
        self.url = url
        self.cfg = cfg
        self.ffmpeg_bin = shutil.which(ffmpeg_bin) or ffmpeg_bin
        self.reconnects = 0
        # A live feed reconnects forever; a local file is finite and EOF means
        # done. Without this distinction, analysing a recording replays it in
        # an endless loop.
        self.is_live = url.startswith(self.LIVE_SCHEMES)
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()

    # -- command ----------------------------------------------------------
    def _command(self) -> list[str]:
        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
        ]
        if self.url.startswith(("http://", "https://")):
            # ffmpeg's own reconnect handles brief CDN hiccups without tearing
            # down the process and losing analyzer state.
            cmd += [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_on_network_error", "1",
                "-reconnect_delay_max", "5",
                "-user_agent", self.cfg.user_agent,
            ]
            if self.cfg.headers:
                blob = "".join(f"{k}: {v}\r\n" for k, v in self.cfg.headers.items())
                cmd += ["-headers", blob]
        cmd += [
            "-i", self.url,
            "-vn",
            "-map", "0:a:0",
            "-ac", "1",
            "-ar", str(ANALYSIS_RATE),
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-",
        ]
        return cmd

    # -- lifecycle --------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        self._kill()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass

    def blocks(self):
        """Yield audio blocks, reconnecting with backoff until :meth:`stop`."""
        backoff = self.cfg.reconnect_min_s
        while not self._stop.is_set():
            try:
                delivered = yield from self._run_once()
            except FileNotFoundError:
                raise
            except Exception as exc:  # noqa: BLE001 - supervisor keeps the worker alive
                log.warning("capture error on %s: %s", self.url, exc)
                delivered = False
            finally:
                self._kill()

            if self._stop.is_set() or not self.is_live:
                return
            # A connection that delivered audio is a transient drop; reset the
            # backoff so a mid-match blip recovers in ~1 s. A connection that
            # never delivered means the stream is down, so back off.
            backoff = self.cfg.reconnect_min_s if delivered else min(
                backoff * 2, self.cfg.reconnect_max_s
            )
            self.reconnects += 1
            yield Block(None, time.time(), gap=True, reason=f"reconnect in {backoff:.0f}s")
            self._stop.wait(backoff)

    def _run_once(self):
        """One ffmpeg lifetime. Returns True if it produced any audio."""
        cmd = self._command()
        log.info("starting capture: %s", self.url)
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        proc = self._proc
        assert proc.stdout is not None

        q: queue.Queue = queue.Queue(maxsize=64)
        errors: list[str] = []
        nbytes = _READ_SAMPLES * 4  # float32

        def reader() -> None:
            try:
                while True:
                    buf = proc.stdout.read(nbytes)  # type: ignore[union-attr]
                    if not buf:
                        break
                    q.put(buf)
            except Exception:  # noqa: BLE001
                pass
            finally:
                q.put(None)

        def drain_stderr() -> None:
            # stderr must be drained or ffmpeg blocks on a full pipe once it
            # has logged more than the buffer holds.
            for line in iter(proc.stderr.readline, b""):  # type: ignore[union-attr]
                text = line.decode("utf-8", "replace").strip()
                if text:
                    errors.append(text)
                    log.debug("ffmpeg: %s", text)

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=drain_stderr, daemon=True).start()

        delivered = False
        tail = b""
        while not self._stop.is_set():
            try:
                buf = q.get(timeout=self.cfg.stall_timeout_s)
            except queue.Empty:
                log.warning("stream stalled: no audio for %.0fs", self.cfg.stall_timeout_s)
                yield Block(None, time.time(), gap=True, reason="stalled")
                break
            if buf is None:
                if errors:
                    log.warning("ffmpeg exited: %s", errors[-1])
                # Reaching the end of a finite file is not a transport fault,
                # so it must not raise stream_down.
                if self.is_live or errors:
                    yield Block(
                        None, time.time(), gap=True, reason=errors[-1] if errors else "eof"
                    )
                break

            tail += buf
            usable = len(tail) - (len(tail) % 4)
            if usable:
                samples = np.frombuffer(tail[:usable], dtype="<f4")
                tail = tail[usable:]
                delivered = True
                yield Block(samples, time.time())
        return delivered
