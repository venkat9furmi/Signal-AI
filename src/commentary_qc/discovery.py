"""Resolve a channel name to a currently-playable stream URL.

The player's URL embeds a per-broadcast UUID
(``/output/support/<uuid>/chunklist.m3u8``) that rotates when the broadcast
restarts, so it can never be hardcoded.

Measured against the live CDN: the UUID segment is *not required*. Both
``/commentaries/<channel>/output/playlist.m3u8`` and
``/commentaries/<channel>/output/support/chunklist.m3u8`` serve the same live
stream, advancing in step with the UUID path. The UUID-free master playlist is
therefore the primary candidate and the discovery problem reduces to a probe.

Candidates are tried in order and validated by content, not status code: the
CDN answers 200 with a bare ``#EXTM3U`` body for paths that exist but carry no
stream, so a status check alone would happily hand back a dead URL.
"""

from __future__ import annotations

import dataclasses
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from commentary_qc.config import SourceConfig

log = logging.getLogger(__name__)

#: Path templates relative to the CDN root, best first. ``playlist.m3u8`` is the
#: standard HLS master and the most likely to survive a vendor reconfiguration.
CANDIDATES = (
    "/commentaries/{channel}/output/playlist.m3u8",
    "/commentaries/{channel}/output/support/chunklist.m3u8",
    "/commentaries/{channel}/output/chunklist.m3u8",
)


@dataclass(slots=True)
class Resolved:
    url: str
    kind: str  # "master" | "media"
    channel: str


class StreamNotFound(RuntimeError):
    """No candidate URL for this channel is currently serving a live stream."""


class ProbeFailed(StreamNotFound):
    """The CDN never answered at all: DNS failure, timeout, refused connection.

    Different from "not live": the stream may be broadcasting perfectly well and
    we simply cannot see it -- our network, not theirs. It subclasses
    StreamNotFound so callers that only care about "can I play it" are
    unchanged, while callers that must not mistake an outage on our side for
    every channel stopping can tell the two apart. The page scanner once
    reported "0 channels broadcasting" for hours for exactly this reason.
    """


#: Returned by _fetch when no HTTP response arrived at all.
_NO_ANSWER = object()


def _fetch(url: str, cfg: SourceConfig, timeout: float = 10.0):
    """Body text, None if the server answered without a stream, or _NO_ANSWER."""
    req = urllib.request.Request(url, headers={"User-Agent": cfg.user_agent, **cfg.headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # The server answered (404 and friends): a real "not here".
        log.debug("probe %s: HTTP %s", url, exc.code)
        return None
    except (urllib.error.URLError, OSError) as exc:
        log.debug("probe failed %s: %s", url, exc)
        return _NO_ANSWER


def classify(body: str) -> str | None:
    """Return "master", "media", or None if the body is not a live playlist."""
    if not body or not body.lstrip().startswith("#EXTM3U"):
        return None
    if "#EXT-X-STREAM-INF" in body:
        return "master"
    if "#EXTINF" in body:
        # A VOD playlist is a finished recording, not a live feed.
        return None if "#EXT-X-ENDLIST" in body else "media"
    return None


def resolve(cfg: SourceConfig) -> Resolved:
    """Find the live URL for ``cfg.channel``, or raise :class:`StreamNotFound`.

    An explicit ``cfg.url`` short-circuits discovery, so an operator can always
    pin a worker to a URL the vendor hands us directly.
    """
    if cfg.url:
        return Resolved(cfg.url, "explicit", cfg.channel)

    tried: list[str] = []
    answered = False
    for template in CANDIDATES:
        url = cfg.hls_base.rstrip("/") + template.format(channel=cfg.channel)
        tried.append(url)
        body = _fetch(url, cfg)
        if body is _NO_ANSWER:
            continue
        answered = True
        kind = classify(body) if body else None
        if kind:
            # DEBUG: this runs on every liveness poll, and the supervisor
            # already logs a line when a channel actually comes on air.
            log.debug("resolved %s -> %s (%s)", cfg.channel, url, kind)
            return Resolved(url, kind, cfg.channel)

    if not answered:
        raise ProbeFailed(f"stream server unreachable while probing {cfg.channel!r}")
    raise StreamNotFound(
        f"no live stream for channel {cfg.channel!r}; tried:\n  " + "\n  ".join(tried)
    )


def is_live(cfg: SourceConfig, channel: str) -> bool:
    """Cheap liveness probe used by the multi-stream runner and health checks."""
    probe = dataclasses.replace(cfg, channel=channel, url=None)
    try:
        resolve(probe)
        return True
    except StreamNotFound:
        return False
