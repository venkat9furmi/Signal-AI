"""Google's Gemini Live API for speech-to-speech translation, as pure functions.

Nothing here opens a connection. The message shapes are the part worth testing
against the documentation, and they can be tested without a key, a socket or a
credit.

The model is ``gemini-3.5-live-translate-preview``: audio in, translated audio
out, with transcripts of both sides alongside. It is a **dedicated translation
model** rather than a general one steered by a prompt, which is why there is no
system instruction here and no tools -- the docs say it supports neither.

Two things about it shape this file:

* **There is no source-language field.** The input language is detected, and
  the only language we state is the target. So a channel whose configured
  language is wrong costs nothing here, which is the opposite of the REST
  chain's behaviour.
* **The two directions have different sample rates.** 16 kHz in, 24 kHz out.
  A single rate for a session would be wrong, which is why
  :class:`~commentary_qc.translate.core.models.AudioSpec` exists per direction.

One documented contradiction, handled rather than chosen between: the Python
SDK puts ``translationConfig`` at the top level of its config object, while
the raw-JSON examples and the API reference put it inside ``generationConfig``.
:func:`setup` follows the raw-JSON form, because that is what a hand-written
WebSocket client is: the reference and the wire examples agree with each other.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from commentary_qc.translate.core.models import (
    AUDIO,
    FAILED,
    HEARD,
    NOTE,
    READY,
    SAID,
    AudioSpec,
    LiveEvent,
    NotConfigured,
    OutOfCredit,
    RateLimited,
    TranslateError,
)

#: The Gemini Developer API endpoint. The key rides in the query string --
#: the docs show no header form for the WebSocket.
LIVE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

#: The translation model, with the ``models/`` prefix the setup message wants.
MODEL = "gemini-3.5-live-translate-preview"

#: Input: 16 kHz mono PCM16. Output: 24 kHz mono PCM16. Not the same, and the
#: docs are explicit about both.
IN_SPEC = AudioSpec(16_000)
OUT_SPEC = AudioSpec(24_000)

#: The exact MIME string the input message must carry. Not a guess and not
#: assembled loosely: the docs print it verbatim with the rate in it.
IN_MIME = f"audio/pcm;rate={IN_SPEC.rate}"

#: "Send audio in chunks of 100ms."
CHUNK_S = 0.1

#: No append ceiling is documented. This is a self-imposed one, large enough
#: that real-time pacing never approaches it and small enough that a
#: buffering bug fails here rather than as an opaque error from the far end.
MAX_CHUNK_BYTES = 256 * 1024

#: An audio-only session runs 15 minutes without context compression, and a
#: single connection lasts around 10. Beyond that a session must be resumed
#: on a new socket, which is what ``goAway`` warns about.
MAX_SESSION_S = 15 * 60
CONNECTION_S = 10 * 60

#: How long to wait for ``setupComplete``. The docs give no figure; this is
#: generous for a model the docs describe as low-latency.
SETUP_DEADLINE_S = 20.0
BOOT_S = 3.0

#: The 70-plus languages the live-translate guide prints, as it prints them.
#: Mostly bare two-letter codes -- `de`, `el` -- with regional forms only
#: where the guide gives them. MMC's six are all here, Greek included, which
#: is the whole reason this adapter exists.
LANGUAGES: tuple[str, ...] = (
    "af", "ak", "sq", "am", "ar", "hy", "az", "eu", "be", "bn", "bg", "my",
    "ca", "zh-Hans", "zh-Hant", "hr", "cs", "da", "nl", "en", "et", "fil",
    "fi", "fr", "gl", "ka", "de", "el", "gu", "ha", "he", "hi", "hu", "is",
    "id", "it", "ja", "jv", "kn", "kk", "km", "rw", "ko", "lo", "lv", "lt",
    "mk", "ms", "ml", "mr", "mn", "ne", "no", "nb", "fa", "pl", "pt-BR",
    "pt-PT", "pa", "ro", "ru", "sr", "sd", "si", "sk", "sl", "es", "su",
    "sw", "sv", "ta", "te", "th", "tr", "uk", "ur", "uz", "vi", "zu",
)

#: Our tags are BCP-47 with a region (`el-gr`); Gemini wants what its guide
#: prints, which is usually the bare language. Portuguese is the exception
#: that proves the rule: it distinguishes `pt-BR` from `pt-PT`, so dropping
#: the region there would quietly pick the wrong variety of a language MMC
#: actually broadcasts in.
EXACT: dict[str, str] = {
    "pt-pt": "pt-PT",
    "pt-br": "pt-BR",
    "zh-cn": "zh-Hans",
    "zh-tw": "zh-Hant",
    "nb-no": "nb",
}


def target_code(tag: str) -> str:
    """Our language tag as Gemini writes it.

    Raises rather than guessing when the language is absent: unlike the REST
    chain there is no per-model mismatch error to fall back on, and a silently
    wrong target would come back as confident audio in the wrong language.
    """
    want = (tag or "").strip().lower()
    if not want:
        raise NotConfigured("a target language is required")
    if want in EXACT:
        return EXACT[want]
    for candidate in LANGUAGES:
        if candidate.lower() == want:
            return candidate
    root = want.split("-", 1)[0]
    for candidate in LANGUAGES:
        if candidate.lower() == root:
            return candidate
    raise NotConfigured(
        f"Gemini live translation does not list {want!r} "
        f"(it lists {len(LANGUAGES)} languages)"
    )


def supported(tag: str) -> tuple[bool, str]:
    """Is ``tag`` offered, and what to say if not."""
    try:
        code = target_code(tag)
    except NotConfigured as exc:
        return False, str(exc)
    want = (tag or "").strip().lower()
    if code.lower() != want:
        return True, f"{want} is written {code} here; using that"
    return True, ""


# -- what we send ------------------------------------------------------------
def url_for(api_key: str, base: str = LIVE_URL) -> str:
    """The connection URL, with the key in the query string.

    The key ends up in the URL because that is the only form documented for
    this socket. It is therefore kept out of logs: nothing in this adapter
    logs a URL, and :func:`redact` exists for the places that must.
    """
    if not api_key:
        raise NotConfigured(
            "GEMINI_API_KEY is not set, so no live session can open"
        )
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}key={api_key}"


#: Anything that looks like a key in a query string. Applied to arbitrary
#: text, not just to URLs we built, because the WebSocket library echoes the
#: URI inside its own error messages and those end up in stored rows.
_KEYISH = re.compile(
    r"((?:key|access_token|api_key|token)=)([^&\s\"']+)", re.I
)


def redact(text: str) -> str:
    """``text`` with any key in a query string replaced.

    Takes any string rather than only a URL: the places that need it most are
    exception messages, which contain a URL somewhere in the middle of a
    sentence.
    """
    return _KEYISH.sub(lambda m: f"{m.group(1)}***", text or "")


def setup(
    target: str,
    *,
    model: str = MODEL,
    echo_target: bool = True,
    want_text: bool = True,
) -> dict[str, Any]:
    """The one message that must be sent first.

    ``echo_target`` true means the model also speaks when it hears audio
    already in the target language. Defaulted on because the alternative is a
    session that goes silent on an English channel being translated to
    English, which looks exactly like a broken session.
    """
    generation: dict[str, Any] = {
        "responseModalities": ["AUDIO"],
        "translationConfig": {
            "targetLanguageCode": target_code(target),
            "echoTargetLanguage": bool(echo_target),
        },
    }
    if want_text:
        # Both transcripts: the text is what makes a bad result diagnosable.
        # Audio alone leaves somebody listening to a language they may not
        # read, unable to tell a mishearing from a mistranslation.
        generation["inputAudioTranscription"] = {}
        generation["outputAudioTranscription"] = {}
    return {
        "setup": {
            "model": model if model.startswith("models/") else f"models/{model}",
            "generationConfig": generation,
        }
    }


def append_audio(pcm: bytes) -> dict[str, Any]:
    """Wrap PCM16 as a ``realtimeInput``.

    Base64 inside JSON. Binary frames are not documented for this socket, so
    they are not used.
    """
    if len(pcm) > MAX_CHUNK_BYTES:
        raise TranslateError(
            f"audio chunk of {len(pcm)} bytes exceeds the self-imposed "
            f"{MAX_CHUNK_BYTES} byte limit"
        )
    return {
        "realtimeInput": {
            "audio": {
                "data": base64.b64encode(pcm).decode("ascii"),
                "mimeType": IN_MIME,
            }
        }
    }


def audio_stream_end() -> dict[str, Any]:
    """Tell the model the audio paused, so it finishes the sentence.

    Only meaningful while automatic activity detection is on, which is the
    default. Sent when we stop feeding rather than left implicit, because the
    far end is otherwise still waiting for speech that will not arrive.
    """
    return {"realtimeInput": {"audioStreamEnd": True}}


# -- what comes back ---------------------------------------------------------
def parse_event(frame: bytes | str) -> LiveEvent | None:
    """One frame into a :class:`LiveEvent`, or ``None`` for nothing we act on.

    ``None`` is not an error. The server's vocabulary is larger than ours and
    will grow, and a session that fell over on an unrecognised event would
    break on Google's next release.
    """
    if isinstance(frame, (bytes, bytearray, memoryview)):
        # Audio is base64 inside JSON here, so a binary frame is the server
        # doing something undocumented. Decode it as text if we can.
        try:
            frame = bytes(frame).decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        payload = json.loads(frame)
    except (ValueError, TypeError):
        return LiveEvent(
            kind=NOTE, text="the server sent something that was not JSON"
        )
    if not isinstance(payload, dict):
        return None

    if "setupComplete" in payload:
        return LiveEvent(kind=READY, raw={"setupComplete": True})

    if "goAway" in payload:
        away = payload.get("goAway") or {}
        left = away.get("timeLeft") if isinstance(away, dict) else None
        return LiveEvent(
            kind=NOTE,
            text=f"the server will close this connection soon ({left or 'no time given'})"
                 "; a longer run needs a fresh session",
            raw=away if isinstance(away, dict) else {},
        )

    if "sessionResumptionUpdate" in payload:
        return None

    content = payload.get("serverContent")
    if isinstance(content, dict):
        return _from_content(content)

    if "error" in payload:
        err = payload.get("error")
        err = err if isinstance(err, dict) else {}
        message = str(err.get("message") or "the server reported an error")
        status = str(err.get("status") or "")
        code = status or str(err.get("code") or "")
        return LiveEvent(
            kind=FAILED, text=message, code=code, fatal=True, raw=err,
            error=error_for(code, message),
        )
    return None


def _from_content(content: dict[str, Any]) -> LiveEvent | None:
    """Pull one event out of ``serverContent``.

    Audio first: a turn can carry audio, a transcript and a completion flag at
    once, and the audio is the thing that must not be dropped.
    """
    turn = content.get("modelTurn")
    if isinstance(turn, dict):
        chunks = b""
        for part in turn.get("parts") or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData")
            if isinstance(inline, dict):
                chunks += _b64(inline.get("data"))
        if chunks:
            return LiveEvent(kind=AUDIO, audio=chunks)

    said = content.get("outputTranscription")
    if isinstance(said, dict) and said.get("text"):
        return LiveEvent(
            kind=SAID, text=str(said["text"]),
            final=bool(content.get("turnComplete")), raw=said,
        )

    heard = content.get("inputTranscription")
    if isinstance(heard, dict) and heard.get("text"):
        return LiveEvent(
            kind=HEARD, text=str(heard["text"]),
            final=bool(content.get("turnComplete")), raw=heard,
        )

    if content.get("interrupted"):
        return LiveEvent(kind=NOTE, text="the model stopped mid-sentence")
    if content.get("turnComplete") or content.get("generationComplete"):
        return LiveEvent(kind=AUDIO, final=True)
    return None


def _b64(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        return b""
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        return b""


def error_for(code: str, message: str) -> TranslateError:
    """The exception a server error means.

    The type matters because the caller acts on it: no quota latches the
    budget shut, a rate limit is retried, and a key problem reads as
    configuration rather than as the model failing.
    """
    key = (code or "").strip().upper()
    text = message or key or "the live session failed"
    blob = f"{key} {message}".lower()
    if key in {"UNAUTHENTICATED", "PERMISSION_DENIED"} or "api key" in blob:
        return NotConfigured(f"{text} (GEMINI_API_KEY)")
    if key == "RESOURCE_EXHAUSTED" or "quota" in blob:
        # Gemini reports quota and rate limiting through the same status, so
        # the message is the only way to tell "slow down" from "you are out".
        if "rate" in blob or "per minute" in blob:
            return RateLimited(text, retry_after=5.0)
        return OutOfCredit(text)
    if key in {"INVALID_ARGUMENT", "FAILED_PRECONDITION"}:
        return NotConfigured(text)
    return TranslateError(text)


__all__ = [
    "LIVE_URL", "MODEL", "IN_SPEC", "OUT_SPEC", "IN_MIME", "CHUNK_S",
    "MAX_CHUNK_BYTES", "MAX_SESSION_S", "CONNECTION_S", "SETUP_DEADLINE_S",
    "BOOT_S", "LANGUAGES", "EXACT",
    "target_code", "supported", "url_for", "redact", "setup", "append_audio",
    "audio_stream_end", "parse_event", "error_for",
]
