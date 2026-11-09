"""Camb AI's JSON on one side, our domain types on the other.

Pure functions, no HTTP, no clock, no logging of consequence. Every quirk of
the vendor's wire format lives in this file and nowhere else, which has two
payoffs: the adapter in ``providers/camb.py`` is reduced to transport, and
these mappings can be tested against the JSON the documentation prints without
a network or a key.

Shapes this maps, from the API reference (read 2026-11-07):

* ``POST /transcribe``        multipart -> ``{"task_id": "..."}``
* ``GET  /transcribe/{task}`` -> ``{"status": "SUCCESS", "run_id": 123}``
* ``GET  /transcription-result/{run}``
      -> ``{"transcript": [{"start": 5.0, "end": 12.0, "text": "...",
                            "speaker": "SPEAKER_0"}]}``
* ``POST /translate``         JSON -> ``{"task_id": "..."}``
* ``GET  /translate/{task}``  -> ``{"status": ..., "run_id": ...}``
* ``GET  /translation-result/{run}`` -> ``{"texts": ["..."]}``
* ``GET  /source-languages``  -> ``[{"id": 1, "language": "English",
                                     "short_name": "en-US"}]``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from commentary_qc.translate.core.models import (
    AudioSegment,
    OutOfCredit,
    ProviderFailed,
    Transcript,
    Translation,
    Utterance,
)

#: Terminal job states. ``PENDING`` is the only one worth waiting on.
PENDING = "PENDING"
SUCCESS = "SUCCESS"
TERMINAL = ("SUCCESS", "ERROR", "TIMEOUT", "PAYMENT_REQUIRED")


@dataclass(frozen=True, slots=True)
class JobState:
    """Where an asynchronous job has got to."""

    status: str
    run_id: int | None = None
    message: str = ""

    @property
    def pending(self) -> bool:
        return self.status not in TERMINAL

    @property
    def ok(self) -> bool:
        return self.status == SUCCESS


# -- requests -------------------------------------------------------------
def transcribe_fields(
    segment: AudioSegment,
    language: str,
    *,
    transcription_mode: str = "fast",
    max_segment_s: float = 7.0,
    min_segment_s: float = 1.0,
    max_chars: int = 42,
) -> dict[str, str]:
    """The non-file half of the multipart transcription request.

    ``formatting_options`` is a JSON *string* inside a multipart field, not a
    nested object -- the API reference is explicit about that and sending it as
    an object is a 422.
    """
    import json

    return {
        "language": language or "auto",
        "transcription_mode": transcription_mode,
        # Our audio is a bare commentary feed with no music bed to protect,
        # and the cleaner is tuned for recordings rather than live speech.
        "run_audio_cleaning": "false",
        "include_music_in_fg": "false",
        "formatting_options": json.dumps(
            {
                "max_segment_duration_in_seconds": max_segment_s,
                "min_segment_duration_in_seconds": min_segment_s,
                "max_characters_in_segment": max_chars,
            }
        ),
        "project_name": f"qc-{segment.channel}-{segment.seq}"[:255],
    }


def translate_body(
    texts: Sequence[str],
    source: str,
    target: str,
    *,
    formality: int = 1,
) -> dict[str, Any]:
    """The translation request body.

    ``formality`` is 1 (formal) or 2 (informal); broadcast commentary is
    closer to formal, and the vendor rejects anything else.
    """
    if formality not in (1, 2):
        raise ValueError("formality must be 1 (formal) or 2 (informal)")
    return {
        "source_language": source,
        "target_language": target,
        "texts": list(texts),
        "formality": formality,
    }


# -- responses ------------------------------------------------------------
def task_id(payload: Any) -> str:
    """Pull the task id out of a create-response."""
    if isinstance(payload, dict):
        for key in ("task_id", "taskId", "id"):
            value = payload.get(key)
            if value:
                return str(value)
    raise ProviderFailed(f"no task id in {_short(payload)}")


def job_state(payload: Any) -> JobState:
    """Read a poll response.

    ``PAYMENT_REQUIRED`` is raised here rather than returned: it is the one
    status where carrying on is actively harmful, and raising it at the point
    it is seen means every caller handles it the same way.
    """
    if not isinstance(payload, dict):
        raise ProviderFailed(f"unreadable job status: {_short(payload)}")
    status = str(payload.get("status") or "").strip().upper() or PENDING
    if status == "PAYMENT_REQUIRED":
        raise OutOfCredit("Camb AI reports the account is out of credit")
    run = payload.get("run_id")
    try:
        run_id = int(run) if run is not None else None
    except (TypeError, ValueError):
        run_id = None
    # The sample code in the docs reads `message` on failure although the
    # schema does not list it, so take it if it is there and do not rely on it.
    return JobState(status, run_id, str(payload.get("message") or ""))


def transcript(
    payload: Any,
    *,
    language: str = "",
    provider: str = "camb",
    model: str = "",
    latency_s: float = 0.0,
) -> Transcript:
    """Map a transcription result onto :class:`~..models.Transcript`.

    Two shapes arrive at the same endpoint: the structured list (no query
    parameters) and an exported blob (``data_type=raw_data``). We ask for the
    structured one, but handling the string keeps a stray parameter from
    looking like an empty transcript.
    """
    if not isinstance(payload, dict):
        raise ProviderFailed(f"unreadable transcript: {_short(payload)}")
    body = payload.get("transcript")
    if isinstance(body, str):
        text = body.strip()
        lines = (Utterance(text),) if text else ()
        return Transcript(lines, language, provider, model, latency_s, payload)
    if body is None:
        raise ProviderFailed(f"no transcript in {_short(payload)}")
    if not isinstance(body, list):
        raise ProviderFailed(f"transcript is a {type(body).__name__}, not a list")

    out: list[Utterance] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        out.append(
            Utterance(
                text=text,
                start_s=_number(item.get("start")),
                end_s=_number(item.get("end")),
                speaker=str(item.get("speaker") or ""),
            )
        )
    return Transcript(tuple(out), language, provider, model, latency_s, payload)


def translation(
    payload: Any,
    *,
    expected: int,
    source: str = "",
    target: str = "",
    provider: str = "camb",
    model: str = "",
    latency_s: float = 0.0,
) -> Translation:
    """Map a translation result, insisting the batch came back whole.

    The caller pairs results with the lines it sent by position. A short or
    long list means that pairing is wrong, and silently zipping them would
    attach the wrong translation to a line -- a failure that reads as a
    plausible caption, which is the worst kind.
    """
    if not isinstance(payload, dict):
        raise ProviderFailed(f"unreadable translation: {_short(payload)}")
    texts = payload.get("texts")
    if not isinstance(texts, list):
        raise ProviderFailed(f"no texts in {_short(payload)}")
    if len(texts) != expected:
        raise ProviderFailed(
            f"asked for {expected} translations and got {len(texts)}; "
            "the result cannot be matched to the input"
        )
    return Translation(
        tuple(str(t) for t in texts), source, target, provider, model, latency_s, payload
    )


def languages(payload: Any) -> dict[str, str]:
    """``[{"id":1,"language":"English","short_name":"en-US"}]`` -> tag -> name.

    ``short_name`` is the tag the rest of the API wants; ``id`` is the
    deprecated numeric form and is ignored.
    """
    if isinstance(payload, dict):
        for key in ("languages", "data", "items", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ProviderFailed(f"unreadable language list: {_short(payload)}")
    out: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("short_name") or item.get("language_code") or "").strip()
        if not tag:
            continue
        out[tag.lower()] = str(item.get("language") or tag)
    if not out:
        raise ProviderFailed("the language list was empty")
    return out


def error_message(payload: Any, status: int = 0) -> str:
    """A sentence from whatever the vendor returned on a failure.

    FastAPI validation errors arrive as
    ``{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}``; plainer
    errors use a string ``detail``. Both get flattened, because what the log
    needs is one readable line.
    """
    prefix = f"HTTP {status}" if status else "provider error"
    if isinstance(payload, dict):
        detail = payload.get("detail", payload.get("message", payload.get("error")))
        if isinstance(detail, list):
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    where = ".".join(str(x) for x in (item.get("loc") or []))
                    parts.append(f"{where}: {item.get('msg')}" if where else str(item.get("msg")))
                else:
                    parts.append(str(item))
            if parts:
                return f"{prefix}: " + "; ".join(parts)
        if detail:
            return f"{prefix}: {detail}"
    if payload:
        return f"{prefix}: {_short(payload)}"
    return prefix


# -- helpers --------------------------------------------------------------
def _number(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _short(value: Any, limit: int = 200) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
