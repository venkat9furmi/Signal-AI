"""Camb AI's realtime WebSocket protocol, as pure functions.

Separate from ``mappers/camb.py`` because it is a different protocol entirely:
that file is shaped around the create/poll/fetch ``task_id`` dance of the REST
endpoints, and this one is a long-lived socket exchanging events. Nothing here
opens a connection -- the whole point is that the message shapes can be tested
without one.

Two things in the vendor's documentation are contradictory, and both are
handled rather than chosen between:

* **Output audio transport.** The AsyncAPI schema and the Python SDK say the
  server returns ``response.audio.delta`` as a raw binary frame with no
  ``type`` field. The reference prose and the Chatterbox page say it is a JSON
  event with a base64 ``delta``. :func:`parse_event` accepts either, because
  guessing wrong means a session that connects, reports progress and produces
  no audio -- the most expensive kind of wrong.
* **The first-message deadline.** The reference says 60 s, the Chatterbox page
  says 10 s. :data:`SETUP_DEADLINE_S` takes the short one: being early is free.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

from ..models import (
    NotConfigured,
    OutOfCredit,
    RateLimited,
    TranslateError,
)

#: The realtime endpoint. Not under ``camb_base_url``: it is a different host
#: altogether, so pointing the REST base at a local fake would not redirect
#: this, and the tests override it explicitly.
REALTIME_URL = "wss://realtime.camb.ai/v1/realtime"

#: Audio format in both directions, from the SDK tutorial -- the protocol
#: reference does not state it. Mono PCM16 little-endian at 24 kHz.
WIRE_RATE = 24_000
WIRE_WIDTH = 2
WIRE_CHANNELS = 1

#: The documented ceiling on one ``input_audio_buffer.append``, as *decoded*
#: bytes. 256 KiB is about 5.5 s of audio, so real-time pacing never
#: approaches it; the check exists so a buffering bug fails loudly here
#: instead of as ``input_audio_too_large`` from the far end.
MAX_APPEND_BYTES = 256 * 1024

#: How long the server waits for the opening ``session.update``.
SETUP_DEADLINE_S = 10.0

#: Idle limit: this long with nothing sent and the server closes with
#: ``idle_timeout``. We send audio continuously, so it only bites during a
#: stream reconnect -- which is exactly when a session is worth keeping.
IDLE_LIMIT_S = 60.0

#: Hard cap on one session.
MAX_SESSION_S = 4 * 60 * 60

#: Modes. ``fast`` starts accepting audio almost immediately; ``slow`` carries
#: more languages but documents a cold boot of "30s or more", which is a long
#: time to stare at a page that says "starting".
FAST = "fast"
SLOW = "slow"
MODES = (FAST, SLOW)

#: The fourteen languages the realtime reference prints, verbatim and sorted.
#:
#: **Read the caveat before trusting this.** The docs say slow mode "supports
#: the full language list" and fast mode fewer, but never print either list
#: separately -- so whether these fourteen are the fast list, the whole list,
#: or neither is genuinely unresolved. What matters for MMC: ``it-it`` and
#: ``el-gr`` are absent, and those are the Italian and Greek channels, 20 of
#: the 52. :func:`supported` reports that as a warning rather than refusing,
#: because an undocumented language that works is likelier than this list
#: being complete, and a real session is the only way to find out.
REALTIME_LANGUAGES: tuple[str, ...] = (
    "ar-ae", "ar-eg", "ar-sa", "de-de", "en-gb", "en-us", "es-es",
    "fr-ca", "fr-fr", "hi-in", "ja-jp", "ko-kr", "pt-br", "zh-cn",
)

#: Error codes after which the server closes the socket. Reconnecting is the
#: only way on from any of these; some are worth retrying and some are not,
#: which :func:`error_for` decides.
FATAL_CODES = frozenset({
    "missing_api_key", "invalid_api_key", "permission_denied",
    "insufficient_credits", "billing_stopped", "billing_unavailable",
    "rate_limited", "voice_not_found", "session_update_timeout",
    "first_event_not_session_update", "invalid_session_configuration",
    "unsupported_language", "automatic_language_detection_unavailable",
    "event_too_large", "input_audio_too_large", "idle_timeout",
    "keepalive_timeout", "max_session_duration_reached", "client_too_slow",
    "session_start_failed", "session_failed", "server_shutting_down",
})

#: Codes the session survives. Worth naming explicitly: treating one of these
#: as fatal would tear down a working session over a field the server simply
#: ignored.
SURVIVABLE_CODES = frozenset({
    "invalid_event", "unsupported_event", "session_update_field_not_supported",
    "target_language_unsupported_for_mode", "session_update_failed",
})

#: Close codes, and what each one means for a retry. 1012 and 1013 are the
#: server asking us to come back; 1008 means something about the request has
#: to change first, so retrying it unchanged just spends another connection.
CLOSE_MEANING: dict[int, str] = {
    1000: "the session ended normally",
    # Not in the vendor's table, but the one the real endpoint actually uses
    # when it refuses a key: it accepts the upgrade and then drops the
    # connection with no close frame at all, so there is no code and no
    # reason to report. Measured against the live endpoint, 2026-11-08.
    1006: "the connection dropped without saying why",
    1008: "the session was rejected and will be again unless something changes",
    1009: "a message we sent was too large",
    1011: "the server failed; a retry usually works",
    1012: "the server is restarting; reconnect for a new session",
    1013: "too many sessions; retry shortly",
}
RETRYABLE_CLOSE = frozenset({1011, 1012, 1013})

#: What to tell somebody whose session closed before it ever became ready and
#: without the server naming a reason. The documented failures all arrive as an
#: ``error`` event first; this is the undocumented one, and in practice it
#: means the key or the plan rather than anything about the audio.
UNEXPLAINED_CLOSE = (
    "the model closed the session immediately without saying why. That is "
    "what this endpoint does when the key is refused or realtime is not "
    "enabled for the account, so check CAMB_API_KEY and that the plan "
    "includes realtime speech to speech."
)


# -- what we send ------------------------------------------------------------
def session_update(
    source: str,
    target: str,
    *,
    mode: str = FAST,
    voice_id: int | None = None,
    want_text: bool = True,
    api_key: str = "",
) -> dict[str, Any]:
    """The opening message. Must be the first thing sent, or the server
    answers ``first_event_not_session_update`` and hangs up.

    ``api_key`` is only included when given, and only belongs here for a
    client that cannot set request headers. We can, so the adapter sends it as
    ``x-api-key`` and leaves this out -- a key in the first message would end
    up in any transcript of the session.
    """
    want = (mode or FAST).strip().lower()
    if want not in MODES:
        raise NotConfigured(f"realtime mode must be one of {MODES}, not {want!r}")

    src = (source or "auto").strip().lower()
    dst = (target or "").strip().lower()
    if src != "auto" and not dst:
        raise NotConfigured("a target language is required unless the source is auto")

    session: dict[str, Any] = {
        "mode": want,
        "source_language": src,
        # Both modalities: the text is what makes a bad translation diagnosable.
        # Audio alone leaves you listening to a language you may not read, with
        # no way to tell a mishearing from a mistranslation.
        "output_modalities": ["text", "audio"] if want_text else ["audio"],
    }
    if dst:
        session["target_language"] = dst
    if voice_id:
        # Only a voice the account owns is accepted here; a stock id is
        # refused with voice_not_found. Hence opt-in rather than defaulted to
        # the REST voice, which is a stock one.
        session["voice"] = {"type": "cloned", "voice_id": int(voice_id)}
    if api_key:
        session["auth"] = {"api_key": api_key}
    return {"type": "session.update", "session": session}


def append_audio(pcm: bytes) -> dict[str, Any]:
    """Wrap PCM16 bytes as an ``input_audio_buffer.append``.

    Base64 inside JSON, not a binary frame: the server ignores binary frames
    from the client. That costs a third in bandwidth and is not negotiable.
    """
    if len(pcm) > MAX_APPEND_BYTES:
        raise TranslateError(
            f"audio chunk of {len(pcm)} bytes exceeds the "
            f"{MAX_APPEND_BYTES} byte limit for one append"
        )
    return {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm).decode("ascii"),
    }


def chunk_bytes(seconds: float) -> int:
    """Bytes of wire audio in ``seconds``, rounded to a whole sample."""
    return int(max(0.0, seconds) * WIRE_RATE) * WIRE_WIDTH * WIRE_CHANNELS


# -- what comes back ---------------------------------------------------------
#: Event kinds we hand upward. Deliberately fewer than the server's set: the
#: caller cares about audio, text, readiness and trouble, and collapsing the
#: rest here keeps the protocol out of the streaming loop.
READY = "ready"
STARTING = "starting"
AUDIO = "audio"
HEARD = "heard"
SAID = "said"
FAILED = "failed"
NOTE = "note"


@dataclass(slots=True)
class LiveEvent:
    """One thing the server said."""

    kind: str
    text: str = ""
    audio: bytes = b""
    code: str = ""
    final: bool = False
    fatal: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def parse_event(frame: bytes | str) -> LiveEvent | None:
    """Turn one WebSocket frame into a :class:`LiveEvent`, or ``None``.

    ``None`` means "nothing the caller needs" -- an event we do not act on, or
    a keepalive. It is not an error: the server's vocabulary is larger than
    ours and will grow, and a session that fell over on an unrecognised event
    would break on the vendor's next release.

    A ``bytes`` frame is audio. That is the AsyncAPI reading of
    ``response.audio.delta``, and it has no ``type`` field to check, so frame
    type is the only signal there is.
    """
    if isinstance(frame, (bytes, bytearray, memoryview)):
        data = bytes(frame)
        return LiveEvent(kind=AUDIO, audio=data) if data else None

    try:
        payload = json.loads(frame)
    except (ValueError, TypeError):
        return LiveEvent(
            kind=NOTE, text="the server sent something that was not JSON"
        )
    if not isinstance(payload, dict):
        return None

    kind = str(payload.get("type") or "")

    if kind == "session.created":
        session = payload.get("session") or {}
        return LiveEvent(kind=READY, raw=session if isinstance(session, dict) else {})
    if kind == "session.starting":
        return LiveEvent(kind=STARTING)
    if kind == "session.updated":
        return None

    if kind == "response.audio.delta":
        # The other reading of the same event: base64 in JSON.
        return LiveEvent(kind=AUDIO, audio=_b64(payload.get("delta")))
    if kind == "response.audio.done":
        return LiveEvent(kind=AUDIO, final=True)

    if kind == "response.text.delta":
        return LiveEvent(kind=SAID, text=str(payload.get("delta") or ""))
    if kind == "response.text.done":
        return LiveEvent(kind=SAID, text=str(payload.get("text") or ""), final=True)

    if kind == "conversation.item.input_audio_transcription.delta":
        return LiveEvent(kind=HEARD, text=str(payload.get("delta") or ""))
    if kind == "conversation.item.input_audio_transcription.completed":
        return LiveEvent(
            kind=HEARD,
            text=str(payload.get("transcript") or ""),
            final=True,
            raw=payload,
        )

    if kind == "session.source_language_changed":
        was = payload.get("previous_language")
        now = payload.get("language") or "?"
        return LiveEvent(
            kind=NOTE,
            text=(f"the source language is now {now}" if not was
                  else f"the source language changed from {was} to {now}"),
            raw=payload,
        )

    if kind == "error":
        err = payload.get("error")
        err = err if isinstance(err, dict) else {}
        code = str(err.get("code") or "")
        message = str(err.get("message") or "the server reported an error")
        # No code at all is the Chatterbox shape. Unknown codes are treated as
        # fatal: the server closes the socket on most of them, and a caller
        # that kept streaming into a dead session would report no fault at all.
        fatal = code in FATAL_CODES or (code not in SURVIVABLE_CODES)
        return LiveEvent(kind=FAILED, text=message, code=code, fatal=fatal, raw=err)

    return None


def _b64(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        return b""
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        return b""


# -- turning a failure into our own vocabulary -------------------------------
def error_for(code: str, message: str) -> TranslateError:
    """The exception that matches a server error code.

    The mapping matters because the caller acts on the type: out of credit
    latches the budget shut, a rate limit is retried, and a key problem is
    reported as configuration rather than as the model failing.
    """
    key = (code or "").strip().lower()
    text = message or key or "the realtime session failed"
    if key in {"missing_api_key", "invalid_api_key", "permission_denied"}:
        return NotConfigured(f"{text} (CAMB_API_KEY)")
    if key in {"insufficient_credits", "billing_stopped", "billing_unavailable"}:
        return OutOfCredit(text)
    if key == "rate_limited":
        return RateLimited(text, retry_after=5.0)
    if key in {"unsupported_language", "target_language_unsupported_for_mode"}:
        return NotConfigured(text)
    return TranslateError(text)


def supported(tag: str) -> tuple[bool, str]:
    """Is ``tag`` in the published realtime list, and what to say if not.

    Returns a warning, not a refusal. See :data:`REALTIME_LANGUAGES`.
    """
    want = (tag or "").strip().lower()
    if not want or want == "auto":
        return True, ""
    if want in REALTIME_LANGUAGES:
        return True, ""
    root = want.split("-", 1)[0]
    near = [t for t in REALTIME_LANGUAGES if t.split("-", 1)[0] == root]
    if near:
        return True, f"{want} is not listed for realtime; {near[0]} is the nearest"
    return False, (
        f"{want} is not in the 14 languages the realtime docs list. It may "
        f"still work -- the docs never print the per-mode lists -- but expect "
        f"unsupported_language."
    )


__all__ = [
    "REALTIME_URL", "WIRE_RATE", "WIRE_WIDTH", "WIRE_CHANNELS",
    "MAX_APPEND_BYTES", "SETUP_DEADLINE_S", "IDLE_LIMIT_S", "MAX_SESSION_S",
    "FAST", "SLOW", "MODES", "REALTIME_LANGUAGES",
    "FATAL_CODES", "SURVIVABLE_CODES", "CLOSE_MEANING", "RETRYABLE_CLOSE",
    "READY", "STARTING", "AUDIO", "HEARD", "SAID", "FAILED", "NOTE",
    "LiveEvent", "session_update", "append_audio", "chunk_bytes",
    "parse_event", "error_for", "supported",
]
