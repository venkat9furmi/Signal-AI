"""Camb AI's speech-synthesis wire format, on its own.

Split from ``mappers/camb.py`` because it maps a different kind of endpoint.
Transcription and translation are asynchronous jobs -- create, poll, fetch --
and everything in that file is shaped around a ``task_id``. ``POST
/tts-stream`` is none of that: one call, raw audio bytes in the response body,
and the only metadata in a header. Mixing the two in one module made the job
helpers look as though they applied to this, and they do not.

Shapes mapped here, from the API reference read 2026-11-08:

* ``POST /tts-stream`` JSON -> streamed audio bytes.
    Required: ``text`` (3-3000 chars), ``language`` (locale tag), ``voice_id``
    (integer). Optional: ``speech_model``, ``output_configuration``,
    ``voice_settings``.
    Response header ``X-Credits-Required`` carries the cost of that one call.
* ``GET  /list-voices`` -> voices. The documentation disagrees with itself
  about whether this is a bare array or ``{"voices": [...]}``, so both are
  accepted.
* ``GET  /target-languages`` -> same shape as ``/source-languages``.
"""

from __future__ import annotations

from typing import Any, Mapping

from commentary_qc.translate.core.models import ProviderFailed

#: Camb's output format names mapped to what the browser needs in a
#: ``Content-Type``. Raw PCM has no container, so it is not offered to the
#: page: an ``<audio>`` element cannot play a headerless byte stream, and the
#: whole point of this feature is that somebody presses play.
MEDIA_TYPES: dict[str, str] = {
    "wav": "audio/wav",
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "adts": "audio/aac",
    "pcm_s16le": "audio/x-pcm",
    "pcm_s32le": "audio/x-pcm",
}

#: Containers a browser will actually play back.
PLAYABLE = ("wav", "flac", "mp3", "adts")

#: The documented bounds on ``text``. Enforced here so an over-long transcript
#: is trimmed by us, deliberately, rather than refused by a 422 that costs a
#: round trip and reads as a bug.
MIN_TEXT = 3
MAX_TEXT = 3000


def media_type(audio_format: str) -> str:
    return MEDIA_TYPES.get((audio_format or "").lower(), "application/octet-stream")


def tts_body(
    text: str,
    language: str,
    *,
    voice_id: int,
    speech_model: str = "mars-8.1-flash-beta",
    audio_format: str = "wav",
    sample_rate: int | None = None,
    speaking_rate: float | None = None,
) -> dict[str, Any]:
    """The JSON body for ``POST /tts-stream``.

    ``language`` is passed through lowercased: the documentation states the
    locale tags are case-sensitive and lowercase, and a mismatch between the
    tag and the chosen ``speech_model`` is a 422 rather than a fallback.
    """
    clean = " ".join((text or "").split())
    if len(clean) < MIN_TEXT:
        raise ProviderFailed(
            f"there is nothing to speak: {len(clean)} characters, and the voice "
            f"API needs at least {MIN_TEXT}"
        )
    if len(clean) > MAX_TEXT:
        # Trimmed at a word boundary so the audio ends on a word rather than
        # mid-syllable. A segment this long means the segmenter let a phrase
        # run away, which is worth seeing in the output.
        clean = clean[:MAX_TEXT].rsplit(" ", 1)[0]

    fmt = (audio_format or "wav").lower()
    if fmt not in MEDIA_TYPES:
        raise ProviderFailed(
            f"{fmt!r} is not one of Camb's output formats {sorted(MEDIA_TYPES)}"
        )
    output: dict[str, Any] = {"format": fmt}
    if sample_rate:
        output["sample_rate"] = int(sample_rate)

    body: dict[str, Any] = {
        "text": clean,
        "language": (language or "en-us").strip().lower(),
        "voice_id": int(voice_id),
        "speech_model": speech_model,
        "output_configuration": output,
    }
    if speaking_rate is not None:
        body["voice_settings"] = {"speaking_rate": float(speaking_rate)}
    return body


def credits_required(headers: Mapping[str, str] | Any) -> str:
    """Whatever ``X-Credits-Required`` said, verbatim.

    Returned as a string and not parsed into a number. It is the only credit
    figure Camb publishes anywhere -- there is no balance, quota or usage
    endpoint -- and it is documented as "number of credits required for this
    request" with no unit. Converting it into a float and calling it a cost
    would be inventing precision we were not given.
    """
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    for name in ("X-Credits-Required", "x-credits-required"):
        value = getter(name)
        if value:
            return str(value).strip()
    return ""


def voices(payload: Any) -> tuple[dict[str, Any], ...]:
    """``/list-voices`` -> the voices, whichever shape it came in.

    The reference page shows ``{"voices": [...]}`` in its example and declares
    a bare array in its schema, and does not say which is right. Accepting
    both costs three lines; guessing costs an outage on the day they settle it.
    """
    items = payload
    if isinstance(payload, dict):
        for key in ("voices", "data", "items", "results"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    if not isinstance(items, list):
        raise ProviderFailed(f"unreadable voice list: {str(payload)[:200]}")
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ident = item.get("id")
        if ident is None:
            continue
        out.append({
            "id": int(ident),
            "name": str(item.get("voice_name") or f"voice {ident}"),
            "language": str(item.get("language") or "").lower(),
            "gender": _gender(item.get("gender")),
            "age": item.get("age"),
            "description": str(item.get("description") or ""),
        })
    return tuple(out)


def _gender(value: Any) -> str:
    """Camb's integer genders, as documented: 1 male, 2 female."""
    return {1: "male", 2: "female"}.get(value, "")
