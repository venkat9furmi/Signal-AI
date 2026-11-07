"""Which channels we watch, and which of them are on air right now.

Two separate questions, deliberately kept apart:

* **Scope** -- which channels belong to MMC. The platform carries ~120 channels
  from several providers; alerting on another company's commentator is noise
  nobody at MMC can act on. Scope is configuration, not something to detect.
* **Liveness** -- which of those are broadcasting at this moment. Measured, not
  configured: probing the CDN was verified to agree exactly with the vendor
  panel's OK/Stopped status across all 11 live channels, so we need no
  credentials and no scraping to know who is on air.

Only ~11 of ~120 channels run at once, so the supervisor watches what is live
rather than holding 120 idle ffmpeg processes open.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import SourceConfig
from .discovery import ProbeFailed, StreamNotFound, resolve

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Channel:
    """One monitored channel."""

    #: Stream slug, e.g. "football-13". This is the id everywhere else.
    name: str
    #: Human label for Slack and the UI, e.g. "Football Ch.13".
    label: str = ""
    #: Free-text, e.g. "English". Useful for filtering MMC's own output.
    language: str = ""
    #: Set false to keep a channel in the file but stop watching it.
    enabled: bool = True
    #: Per-channel threshold overrides, applied on top of the defaults.
    thresholds: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.name

    @property
    def short(self) -> str:
        """Compact label for log columns, e.g. 'Ch.52'."""
        # Only football drops its sport: "Cricket Ch.1" must not also read "Ch.1".
        for prefix in ("Football ",):
            if self.label.startswith(prefix):
                return self.label[len(prefix):]
        return self.label


@dataclass(slots=True)
class ChannelStatus:
    channel: Channel
    live: bool
    url: str | None = None
    checked: float = 0.0
    #: The probe could not reach the server, so liveness is unknown.
    error: bool = False


class Registry:
    """The MMC channel list plus a background liveness poller."""

    def __init__(
        self,
        channels: list[Channel],
        source: SourceConfig,
        poll_interval_s: float = 30.0,
    ) -> None:
        self.channels = {c.name: c for c in channels if c.enabled}
        self.source = source
        self.poll_interval_s = poll_interval_s
        self._status: dict[str, ChannelStatus] = {}
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: str, source: SourceConfig, poll_interval_s: float = 30.0) -> "Registry":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data["channels"] if isinstance(data, dict) else data
        channels = [Channel(**c) for c in raw]
        if not channels:
            raise ValueError(f"{path} lists no channels")
        return cls(channels, source, poll_interval_s)

    # -- liveness ---------------------------------------------------------
    def probe(self, channel: Channel) -> ChannelStatus:
        cfg = dataclasses.replace(self.source, channel=channel.name, url=None)
        try:
            resolved = resolve(cfg)
            return ChannelStatus(channel, True, resolved.url, time.time())
        except ProbeFailed:
            return ChannelStatus(channel, False, None, time.time(), error=True)
        except StreamNotFound:
            return ChannelStatus(channel, False, None, time.time())

    def probe_now(self, name: str) -> ChannelStatus:
        """Probe one channel immediately and remember the answer."""
        status = self.probe(self.channels[name])
        if not status.error:
            with self._lock:
                self._status[name] = status
        return status

    # -- claimed channels come and go at runtime --------------------------
    def add(self, channel: Channel) -> None:
        with self._lock:
            self.channels[channel.name] = channel

    def remove(self, name: str) -> None:
        with self._lock:
            self.channels.pop(name, None)
            self._status.pop(name, None)

    def poll(self) -> tuple[list[ChannelStatus], list[ChannelStatus]]:
        """Probe every channel once. Returns (came_up, went_down)."""
        up, down = [], []
        # In parallel: most configured channels are off air most of the time,
        # and each off-air probe tries every candidate URL. Done one after
        # another, 39 channels took long enough to stall the supervisor loop --
        # its heartbeat went stale and claims waited behind the poll.
        items = list(self.channels.items())
        with ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(lambda kv: self.probe(kv[1]), items))
        for (name, channel), status in zip(items, statuses):
            if status.error:
                # Could not reach the server: we do not know, so nothing
                # changes. Stopping every monitor because our own connection
                # blipped would turn one outage into a restart of everything.
                log.debug("liveness unknown for %s (server unreachable)", name)
                continue
            with self._lock:
                was = self._status.get(name)
                self._status[name] = status
            if status.live and (was is None or not was.live):
                up.append(status)
            elif not status.live and was is not None and was.live:
                down.append(status)
        return up, down

    def live(self) -> list[ChannelStatus]:
        with self._lock:
            return [s for s in self._status.values() if s.live]

    def status(self, name: str) -> ChannelStatus | None:
        with self._lock:
            return self._status.get(name)


def default_channels() -> list[Channel]:
    """Channels observed live on the platform, as a starting point.

    NOTE: this is *not* the MMC list -- it is everything that was broadcasting
    during discovery, including other providers. Replace it with MMC's own
    channels in channels.json before running for real, or the system will
    alert on commentary nobody at MMC can fix.
    """
    return [
        Channel("football-01", "Football Ch.1", "English"),
        Channel("football-12", "Football Ch.12", "Spanish"),
        Channel("football-13", "Football Ch.13", "Spanish"),
        Channel("football-22", "Football Ch.22", "Italian"),
        Channel("football-23", "Football Ch.23", "Italian"),
        Channel("football-32", "Football Ch.32", "Portuguese"),
        Channel("football-52", "Football Ch.52", "Greek"),
        Channel("greyhounds-01", "Greyhound Racing", "English"),
        Channel("horses-01", "Racing TV (Horses Ch.1)", "English"),
        Channel("horses-02", "At The Races (Horses Ch.2)", "English"),
        Channel("horses-03", "Irish Horse Racing", "English"),
    ]
