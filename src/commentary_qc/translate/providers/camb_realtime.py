"""The Camb AI realtime WebSocket, as a :class:`~..ports.LiveSession`.

Transport only. Every message shape lives in ``mappers/realtime.py`` and is
tested without a socket; what is here is the socket, the reader thread and the
lifecycle, which can only be tested against a server.

Why a thread rather than asyncio: everything else in this project is threaded
-- one capture thread per channel, supervised -- and a live translation is one
more of those. ``websockets.sync`` gives a blocking client whose ``send`` and
``recv`` may be used from different threads, which is exactly the shape
needed: the caller feeds audio at the rate it arrives, a reader thread drains
the far end into a queue, and neither waits on the other. A loop that blocked
on ``recv`` between chunks would stop feeding the session, and the server
answers that with ``client_too_slow``.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
from typing import Any, Iterator

from ..mappers import realtime as rt
from ..models import NotConfigured, TranslateError

log = logging.getLogger(__name__)

#: Incoming frame ceiling. The library defaults to 1 MiB; audio deltas are far
#: smaller, but a base64 delta is a third larger than the bytes it carries and
#: there is no documented cap on one, so the limit is lifted rather than
#: letting a big frame kill a working session.
MAX_FRAME = 4 * 1024 * 1024

#: How many events may wait to be collected. Generous: these are small, and
#: the audio arriving faster than the page collects it is the normal case
#: during a burst of speech. If it does fill, the *oldest* goes -- the same
#: rule the segment queue uses, for the same reason.
EVENT_QUEUE = 512


class CambLiveSession:
    """One open realtime session.

    Constructed connected: the socket is opened and the opening
    ``session.update`` is sent before ``__init__`` returns, because the server
    gives roughly ten seconds for that first message and a session object that
    existed but had not introduced itself would be a trap.
    """

    def __init__(
        self,
        api_key: str,
        source: str,
        target: str,
        *,
        mode: str = rt.FAST,
        voice_id: int | None = None,
        url: str = rt.REALTIME_URL,
        connect: Any | None = None,
        open_timeout: float = 15.0,
    ) -> None:
        if not api_key:
            raise NotConfigured("CAMB_API_KEY is not set, so no live session can open")

        self.rate = rt.WIRE_RATE
        self.source = (source or "auto").strip().lower()
        self.target = (target or "").strip().lower()
        self.mode = (mode or rt.FAST).strip().lower()

        # Built before connecting: a bad language or mode should fail without
        # spending a connection, and this validates both.
        hello = rt.session_update(
            self.source, self.target, mode=self.mode, voice_id=voice_id
        )

        self._events: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE)
        self._ready = threading.Event()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._failure: TranslateError | None = None
        self._close_note = ""
        self._sent_bytes = 0
        self._recv_bytes = 0
        self.session_id = ""
        self.resolved: dict[str, Any] = {}
        self.opened = time.time()

        opener = connect or _default_connect()
        target_url = _with_mode(url, self.mode)
        try:
            self._ws = opener(
                target_url,
                additional_headers=[("x-api-key", api_key)],
                open_timeout=open_timeout,
                max_size=MAX_FRAME,
                # The library pings on its own; the docs say the server
                # answers pings with pongs, which keeps a NAT path open
                # through the quiet moments of a match.
                ping_interval=20,
                ping_timeout=20,
            )
        except Exception as exc:  # noqa: BLE001 - every opener raises its own
            raise _opening_failed(exc) from exc

        try:
            self._ws.send(json.dumps(hello))
        except Exception as exc:  # noqa: BLE001
            self._shut()
            raise TranslateError(f"the realtime session refused the opening message: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_forever,
            name=f"camb-live-{self.source}-{self.target}",
            daemon=True,
        )
        self._reader.start()

    # -- reading ---------------------------------------------------------
    def _read_forever(self) -> None:
        try:
            for frame in self._ws:
                self._recv_bytes += len(frame) if isinstance(frame, (bytes, str)) else 0
                event = rt.parse_event(frame)
                if event is None:
                    continue
                if event.kind == rt.READY:
                    self.resolved = dict(event.raw or {})
                    self.session_id = str(self.resolved.get("id") or "")
                    self._ready.set()
                if event.kind == rt.FAILED and event.fatal:
                    with self._lock:
                        self._failure = rt.error_for(event.code, event.text)
                    # Unblock anyone waiting to start. Without this a session
                    # rejected for a bad key would hang in wait_ready until
                    # the timeout, reporting a slow server rather than a key
                    # problem the server named immediately.
                    self._ready.set()
                self._offer(event)
        except Exception as exc:  # noqa: BLE001 - a closed socket arrives as one
            self._close_note = _closed_note(exc)
        finally:
            self._done.set()
            self._ready.set()

    def _offer(self, event: rt.LiveEvent) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            # Drop the oldest. The newest translated audio is the only audio
            # worth hearing, and a page that is behind wants to catch up
            # rather than replay.
            try:
                self._events.get_nowait()
                self._events.put_nowait(event)
            except (queue.Empty, queue.Full):
                pass

    # -- the LiveSession contract ----------------------------------------
    def wait_ready(self, timeout_s: float) -> None:
        self._ready.wait(timeout_s)
        self._raise_if_failed()
        if self._done.is_set():
            # Closed before session.created, and _raise_if_failed found no
            # error event -- so the server named no reason. The documented
            # failures all send one first, which makes this the undocumented
            # path, and in practice it is the key or the plan.
            if not self.session_id:
                raise NotConfigured(
                    f"{rt.UNEXPLAINED_CLOSE}"
                    + (f" ({self._close_note})" if self._close_note else "")
                )
            raise TranslateError(
                self._close_note or "the realtime session closed before it was ready"
            )
        if not self._ready.is_set():
            raise TranslateError(
                f"the realtime session was not ready after {timeout_s:.0f}s"
                + (" (slow mode documents a cold boot of 30s or more)"
                   if self.mode == rt.SLOW else "")
            )

    def send(self, pcm: bytes) -> None:
        self._raise_if_failed()
        if not pcm:
            return
        if self._done.is_set():
            raise TranslateError(
                self._close_note or "the realtime session has closed"
            )
        for piece in _split(pcm, rt.MAX_APPEND_BYTES):
            self._ws.send(json.dumps(rt.append_audio(piece)))
            self._sent_bytes += len(piece)

    def drain(self) -> Iterator[rt.LiveEvent]:
        while True:
            try:
                yield self._events.get_nowait()
            except queue.Empty:
                return

    @property
    def closed(self) -> bool:
        return self._done.is_set()

    def close(self) -> None:
        self._shut()
        self._done.set()

    def _shut(self) -> None:
        ws = getattr(self, "_ws", None)
        if ws is None:
            return
        try:
            ws.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is not news
            pass

    def _raise_if_failed(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise failure

    # -- what the page shows ---------------------------------------------
    def stats(self) -> dict[str, Any]:
        """Enough to tell a working session from a stalled one."""
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "source_language": self.source,
            "target_language": self.target,
            "ready": self._ready.is_set() and not self._done.is_set(),
            "closed": self._done.is_set(),
            "sent_seconds": round(self._sent_bytes / float(rt.WIRE_RATE * rt.WIRE_WIDTH), 2),
            "received_bytes": self._recv_bytes,
            "elapsed_s": round(time.time() - self.opened, 1),
            "note": self._close_note,
            "resolved": self.resolved,
        }


# -- helpers -----------------------------------------------------------------
def _default_connect():
    try:
        from websockets.sync.client import connect
    except Exception as exc:  # noqa: BLE001
        raise NotConfigured(
            "live translation needs the websockets package "
            "(pip install 'websockets>=13')"
        ) from exc
    return connect


def _with_mode(url: str, mode: str) -> str:
    """Put ``mode`` in the query string as well as the session message.

    Belt and braces, and cheap: the documented precedence is session, then
    query, then the ``fast`` default, so the two agreeing costs nothing and a
    server that ignored one still gets the other. It matters most for slow
    mode, where falling back to fast silently would mean a language quietly
    refused.
    """
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["mode"] = mode
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query))
    )


def _split(data: bytes, size: int) -> Iterator[bytes]:
    if len(data) <= size:
        yield data
        return
    # Keep the split on a sample boundary; half a sample lands as a click.
    size -= size % rt.WIRE_WIDTH
    for start in range(0, len(data), size):
        yield data[start:start + size]


def _opening_failed(exc: Exception) -> TranslateError:
    """Name the common causes rather than reporting the library's text.

    The handshake failure for a bad key is an HTTP 401 on the upgrade, which
    surfaces as a library-specific exception whose message says nothing about
    keys.
    """
    text = str(exc) or type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return NotConfigured(
            f"the realtime endpoint rejected the key (HTTP {status}). "
            "Check CAMB_API_KEY, and that the plan includes realtime."
        )
    if status == 402:
        from ..models import OutOfCredit

        return OutOfCredit("the realtime endpoint reports no credit (HTTP 402)")
    if status == 404:
        return NotConfigured(
            "the realtime endpoint was not found (HTTP 404) -- the path may "
            "have moved, or realtime may not be enabled for this account"
        )
    if status:
        return TranslateError(f"the realtime endpoint answered HTTP {status}")
    return TranslateError(f"could not open the realtime session: {text}")


def _closed_note(exc: Exception) -> str:
    """Turn a close into a sentence, using the documented close codes."""
    code = getattr(exc, "code", None)
    reason = (getattr(exc, "reason", "") or "").strip()
    if code is None:
        rcvd = getattr(exc, "rcvd", None)
        code = getattr(rcvd, "code", None)
        reason = reason or (getattr(rcvd, "reason", "") or "").strip()
    if code is None:
        return f"the realtime session ended: {exc}"
    meaning = rt.CLOSE_MEANING.get(int(code), f"close code {code}")
    retry = " (worth reconnecting)" if int(code) in rt.RETRYABLE_CLOSE else ""
    # The docs say the close reason repeats the error code, so it is the most
    # specific thing available and goes first.
    return f"{reason or meaning}{retry}" if reason else f"{meaning}{retry}"


__all__ = ["CambLiveSession", "MAX_FRAME", "EVENT_QUEUE"]
