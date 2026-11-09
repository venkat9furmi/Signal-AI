"""Gemini live translation: the socket, the session, and the provider.

Transport only. Every message shape lives in ``protocol.py`` and is tested
without a socket.

The same threading shape as the other live adapter, for the same reason:
``websockets.sync`` allows ``send`` and ``recv`` from different threads, so
the caller feeds audio at the rate it arrives while a reader thread drains the
far end into a queue. Neither waits on the other.

What is different from the other adapter, and worth knowing:

* **Audio in is 16 kHz, audio out is 24 kHz.** Two specs, not one.
* **The key goes in the URL**, because that is the only form documented for
  this socket. So nothing here logs a URL without passing it through
  ``protocol.redact`` first.
* **There is no source language.** The model detects it. Our channel's
  configured language is therefore not needed, and cannot be wrong.
* **A connection lasts about ten minutes** and the server warns with
  ``goAway`` before closing. Longer runs need a fresh session; the warning is
  surfaced as a note rather than swallowed, so a run that ends early says why.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Iterator

from commentary_qc.translate.adapters.gemini import protocol as gp
from commentary_qc.translate.core.models import (
    FAILED,
    READY,
    NotConfigured,
    TranslateError,
)
from commentary_qc.translate.settings import TranslateSettings

log = logging.getLogger(__name__)

#: Incoming frame ceiling. Audio arrives base64 inside JSON, which is a third
#: larger than the bytes it carries, and no cap on one turn is documented.
MAX_FRAME = 8 * 1024 * 1024

#: How many events may wait to be collected before the oldest is dropped.
EVENT_QUEUE = 512


class GeminiLiveSession:
    """One open translation session.

    Constructed connected and introduced: the socket is opened and the setup
    message sent before ``__init__`` returns, because the server expects setup
    first and a session object that existed but had not introduced itself
    would be a trap.
    """

    def __init__(
        self,
        api_key: str,
        target: str,
        *,
        model: str = gp.MODEL,
        echo_target: bool = True,
        url: str = gp.LIVE_URL,
        connect: Any | None = None,
        open_timeout: float = 15.0,
    ) -> None:
        self.spec = gp.IN_SPEC
        self.output_spec = gp.OUT_SPEC
        self.rate = gp.IN_SPEC.rate            # the older single-rate name
        self.max_chunk_bytes = gp.MAX_CHUNK_BYTES
        self.target = (target or "").strip().lower()
        self.model = model
        self.mode = "translate"
        #: Detected by the model, so unknown until it says.
        self.source = "auto"

        # Built before connecting, so an unsupported language fails without
        # spending a connection.
        hello = gp.setup(self.target, model=model, echo_target=echo_target)
        self.target_code = hello["setup"]["generationConfig"][
            "translationConfig"]["targetLanguageCode"]

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
        target_url = gp.url_for(api_key, url)
        try:
            self._ws = opener(
                target_url,
                open_timeout=open_timeout,
                max_size=MAX_FRAME,
                ping_interval=20,
                ping_timeout=20,
            )
        except Exception as exc:  # noqa: BLE001 - every opener raises its own
            raise _opening_failed(exc) from exc

        try:
            self._ws.send(json.dumps(hello))
        except Exception as exc:  # noqa: BLE001
            self._shut()
            raise TranslateError(
                f"the session refused the setup message: {exc}"
            ) from exc

        self._reader = threading.Thread(
            target=self._read_forever,
            name=f"gemini-live-{self.target_code}",
            daemon=True,
        )
        self._reader.start()

    # -- reading ---------------------------------------------------------
    def _read_forever(self) -> None:
        try:
            for frame in self._ws:
                self._recv_bytes += len(frame) if isinstance(frame, (bytes, str)) else 0
                event = gp.parse_event(frame)
                if event is None:
                    continue
                if event.kind == READY:
                    self.resolved = dict(event.raw or {})
                    self.session_id = self.session_id or f"gemini-{int(self.opened)}"
                    self._ready.set()
                if event.kind == FAILED and event.fatal:
                    with self._lock:
                        self._failure = event.error or TranslateError(event.text)
                    # Unblock anyone waiting to start: a session rejected for
                    # a bad key should say so at once rather than time out
                    # looking like a slow server.
                    self._ready.set()
                self._offer(event)
        except Exception as exc:  # noqa: BLE001 - a closed socket arrives as one
            self._close_note = _closed_note(exc)
        finally:
            self._done.set()
            self._ready.set()

    def _offer(self, event: Any) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            # Drop the oldest. The newest translated audio is the only audio
            # worth hearing, and a page that is behind wants to catch up.
            try:
                self._events.get_nowait()
                self._events.put_nowait(event)
            except (queue.Empty, queue.Full):
                pass

    # -- the LiveSession contract ----------------------------------------
    def wait_ready(self, timeout_s: float) -> None:
        self._ready.wait(timeout_s or gp.SETUP_DEADLINE_S)
        self._raise_if_failed()
        if self._done.is_set():
            if not self.resolved:
                raise NotConfigured(
                    "the session closed before it was ready and gave no "
                    "reason. That is what this endpoint does when the key is "
                    "refused or the model is not enabled for the project, so "
                    "check GEMINI_API_KEY and that "
                    f"{self.model} is available to it."
                    + (f" ({self._close_note})" if self._close_note else "")
                )
            raise TranslateError(
                self._close_note or "the session closed before it was ready"
            )
        if not self._ready.is_set():
            raise TranslateError(
                f"the session was not ready after {timeout_s:.0f}s"
            )

    def send(self, pcm: bytes) -> None:
        self._raise_if_failed()
        if not pcm:
            return
        if self._done.is_set():
            raise TranslateError(self._close_note or "the session has closed")
        for piece in _split(pcm, self.max_chunk_bytes, self.spec.width):
            self._ws.send(json.dumps(gp.append_audio(piece)))
            self._sent_bytes += len(piece)

    def drain(self) -> Iterator[Any]:
        while True:
            try:
                yield self._events.get_nowait()
            except queue.Empty:
                return

    @property
    def closed(self) -> bool:
        return self._done.is_set()

    def close(self) -> None:
        """Tell the model the audio stopped, then hang up.

        The end-of-stream marker matters: without it the far end is still
        waiting for speech that will not arrive, and the last sentence is
        never finished.
        """
        if not self._done.is_set():
            try:
                self._ws.send(json.dumps(gp.audio_stream_end()))
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass
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
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "source_language": self.source,
            "target_language": self.target_code,
            "ready": self._ready.is_set() and not self._done.is_set(),
            "closed": self._done.is_set(),
            "sent_seconds": round(self.spec.seconds_for(self._sent_bytes), 2),
            "received_bytes": self._recv_bytes,
            "elapsed_s": round(time.time() - self.opened, 1),
            "note": self._close_note,
            "resolved": dict(self.resolved),
        }


class GeminiProvider:
    """Live translation through Gemini. Live only -- no clip path.

    Deliberately not pretending to do the three-call chain. The clip path
    needs a transcriber, a translator and a speaker, and this model is one
    thing: audio in, translated audio out. A provider that offered methods it
    could not honour would push the discovery of that into a run.
    """

    name = "gemini"
    model = "google/gemini-live-translate"

    def __init__(self, settings: TranslateSettings, connect: Any | None = None) -> None:
        self.settings = settings
        self._key = settings.gemini_api_key
        self._model = settings.gemini_model or gp.MODEL
        self._connect = connect
        self._live_url = settings.gemini_live_url or gp.LIVE_URL

    # -- live ------------------------------------------------------------
    def open_live(
        self,
        source: str,
        target: str,
        *,
        mode: str = "",
        voice_id: int | None = None,
    ) -> GeminiLiveSession:
        """Open a translation session.

        ``source`` is accepted and ignored: this model detects the input
        language and documents no field for it. ``voice_id`` likewise -- the
        translate model's config has no voice field, so offering a choice
        would be offering a control that does nothing.
        """
        if not self._key:
            raise NotConfigured(
                "GEMINI_API_KEY is not set, so no live session can open."
            )
        return GeminiLiveSession(
            self._key,
            target,
            model=self._model,
            echo_target=self.settings.gemini_echo_target,
            url=self._live_url,
            connect=self._connect,
        )

    def live_languages(self) -> tuple[str, ...]:
        return gp.LANGUAGES

    def live_modes(self) -> tuple[dict, ...]:
        return (
            {"name": "translate",
             "detail": "One model, audio to audio. No fast or slow pipeline "
                       "to choose between."},
        )

    def live_note(self, target: str) -> str:
        _, warning = gp.supported(target)
        return warning

    def live_capabilities(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model,
            "transport": "WebSocket",
            "endpoint": gp.LIVE_URL,
            "modes": list(self.live_modes()),
            "wire": {
                "rate": gp.IN_SPEC.rate,
                "format": f"in {gp.IN_SPEC.describe()}, "
                          f"out {gp.OUT_SPEC.describe()}",
            },
            "languages": list(gp.LANGUAGES),
            "limits": {
                "max_session_s": gp.MAX_SESSION_S,
                "connection_s": gp.CONNECTION_S,
                "setup_s": gp.SETUP_DEADLINE_S,
                "boot_s": gp.BOOT_S,
                "max_chunk_bytes": gp.MAX_CHUNK_BYTES,
            },
            "caveats": [
                "This model is a preview. Google may change it, and the free "
                "tier states that input and output are used to improve their "
                "products -- so MMC's commentary becomes training data unless "
                "a paid tier or an agreement says otherwise.",
                "A connection lasts about 10 minutes and a session 15. A full "
                "match needs the session reopening, which the run reports "
                "rather than hiding.",
                "The input language is detected, not declared. A channel whose "
                "configured language is wrong costs nothing here.",
            ],
        }

    # -- the clip path, which this model does not offer -------------------
    def capabilities(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model,
            "audio_in": True,
            "audio_out": True,
            "steps": [
                {
                    "name": "Translate the speech",
                    "endpoint": "WebSocket (live only)",
                    "detail": "Audio goes up as it arrives and translated "
                              "speech comes back on the same socket, with "
                              "transcripts of both sides alongside.",
                },
            ],
            "not_available": [
                {
                    "name": "Recorded clips",
                    "endpoint": "none",
                    "detail": "This model is live only. There is no upload "
                              "endpoint to send a recorded clip to, so the "
                              "clip tab cannot use it.",
                },
            ],
        }

    def languages(self) -> dict[str, str]:
        """The live list, so the page can show something sensible.

        Named the same as the clip path's catalogue because the page asks for
        it either way; the honest answer for this model is its live list.
        """
        return {tag: tag for tag in gp.LANGUAGES}

    def voices(self) -> tuple[dict, ...]:
        return ()

    def transcribe(self, segment: Any, language: str) -> Any:
        raise NotConfigured(
            "Gemini live translation has no clip path: it translates a stream, "
            "not a recorded segment. Use the live tab, or pick another model."
        )

    def translate(self, texts: Any, source: str, target: str) -> Any:
        raise NotConfigured(
            "Gemini live translation has no text endpoint in this adapter."
        )

    def speak(self, text: str, language: str) -> Any:
        raise NotConfigured(
            "Gemini live translation speaks as part of a session; there is no "
            "separate text-to-speech call in this adapter."
        )

    def healthcheck(self) -> dict:
        """Can we reach it, and does the key look usable?

        Deliberately does not open a session: a session costs tokens, and a
        health check that spent credit every time the page loaded would be a
        bill nobody asked for. So it reports what can be known for free.
        """
        return {
            "ok": bool(self._key),
            "model": self._model,
            "live_only": True,
            "error": "" if self._key else "GEMINI_API_KEY is not set",
            "note": "Not probed: opening a session costs tokens, so this "
                    "reports configuration rather than reachability.",
        }

    def close(self) -> None:
        return None


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


def _split(data: bytes, size: int, width: int) -> Iterator[bytes]:
    if len(data) <= size:
        yield data
        return
    # Keep the split on a sample boundary; half a sample lands as a click.
    size -= size % max(1, width)
    for start in range(0, len(data), size):
        yield data[start:start + size]


def _opening_failed(exc: Exception) -> TranslateError:
    """Name the usual causes rather than reporting a library's text."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return NotConfigured(
            f"the live endpoint rejected the key (HTTP {status}). Check "
            "GEMINI_API_KEY, and that the project may use this model."
        )
    if status == 429:
        from commentary_qc.translate.core.models import RateLimited

        return RateLimited("the live endpoint is rate limiting", retry_after=10.0)
    if status == 404:
        return NotConfigured(
            "the live endpoint was not found (HTTP 404) -- the model name may "
            "be wrong, or it may not be available to this project"
        )
    if status:
        return TranslateError(f"the live endpoint answered HTTP {status}")
    return TranslateError(
        f"could not open the live session: {exc or type(exc).__name__}"
    )


def _closed_note(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    reason = (getattr(exc, "reason", "") or "").strip()
    if code is None:
        rcvd = getattr(exc, "rcvd", None)
        code = getattr(rcvd, "code", None)
        reason = reason or (getattr(rcvd, "reason", "") or "").strip()
    if code is None:
        return f"the session ended: {exc}"
    if int(code) == 1000:
        return reason or "the session ended normally"
    return reason or f"the session closed with code {code}"


__all__ = ["GeminiLiveSession", "GeminiProvider", "MAX_FRAME", "EVENT_QUEUE"]
