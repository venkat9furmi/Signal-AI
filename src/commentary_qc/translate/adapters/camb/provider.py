"""Camb AI adapter: transport only.

Everything about the shape of Camb's JSON lives in ``mappers/camb.py``. What
is left here is the awkward part of talking to a remote service: auth, a
multipart body, retries that distinguish "try again" from "stop", and the
create/poll/fetch dance their asynchronous jobs require.

``urllib`` rather than ``requests`` on purpose -- it is in the standard
library, and this project has stayed free of dependencies it does not need.

The base URL is a setting, which is what makes this testable: ``tests/
test_translate.py`` points it at a local fake that speaks the same JSON, so
the retry logic, the polling and the error paths are all exercised without a
key and without spending a credit.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Sequence

from commentary_qc.translate.infra.audio import to_wav
from commentary_qc.translate.adapters.camb import mapper as m
from commentary_qc.translate.adapters.camb import tts as t
from commentary_qc.translate.core.languages import LanguageMap
from commentary_qc.translate.core.models import (
    AudioSegment,
    JobTimeout,
    NotConfigured,
    OutOfCredit,
    ProviderFailed,
    RateLimited,
    SpokenAudio,
    Transcript,
    Translation,
)
from commentary_qc.translate.adapters.camb import protocol as rt
from commentary_qc.translate.settings import TranslateSettings
from commentary_qc.translate.adapters.camb.realtime import CambLiveSession

log = logging.getLogger(__name__)


class CambProvider:
    """Speech recognition and translation through Camb AI's REST API."""

    name = "camb"
    #: What gets recorded alongside each caption, so a change of model is
    #: visible in the data rather than inferred from the date.
    model = "camb/transcribe+translate+tts"
    #: Live sessions this vendor will hold open at once. One, because Camb's
    #: free plan documents an API concurrency of 1 -- and a second session is
    #: refused by the far end only after audio has started flowing, which
    #: means after the meter has started. Declared here so the app can refuse
    #: a multi-language group before anything is spent; the app does not read
    #: vendor plans, it asks the adapter.
    max_live_sessions = 1

    def __init__(self, settings: TranslateSettings, opener: Any | None = None) -> None:
        self.settings = settings
        self.base = settings.camb_base_url.rstrip("/")
        self._key = settings.camb_api_key
        self._opener = opener or urllib.request.build_opener()
        self.languages_map = LanguageMap()
        self._languages_at = 0.0
        # Injected by the tests so the realtime path can run against a local
        # WebSocket server. None means the real library.
        self._connect: Any | None = None
        self._live_url = rt.REALTIME_URL

    # -- plumbing -------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        if not self._key:
            raise NotConfigured(
                "CAMB_API_KEY is not set, so the Camb adapter cannot be used. "
                "Switch the provider to 'mock' or supply a key."
            )
        return {"x-api-key": self._key, "accept": "application/json"}

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        multipart: tuple[dict[str, str], str, bytes] | None = None,
        query: dict[str, str] | None = None,
        retries: int = 2,
        raw: bool = False,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = self._headers()
        data: bytes | None = None
        if multipart is not None:
            fields, filename, blob = multipart
            data, content_type = _encode_multipart(fields, filename, blob)
            headers["content-type"] = content_type
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"

        last: Exception | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._opener.open(
                    req, timeout=timeout or self.settings.timeout_s
                ) as resp:
                    payload = resp.read()
                    headers = resp.headers
                # Speech synthesis answers with audio and puts the only
                # metadata it gives -- the credit cost -- in a header, so that
                # caller needs the bytes and the headers, not parsed JSON.
                if raw:
                    return payload, headers
                return json.loads(payload.decode("utf-8")) if payload else {}
            except urllib.error.HTTPError as exc:
                payload = _read_json(exc)
                message = m.error_message(payload, exc.code)
                # 402 is the documented "no credit" code and 403 can mean the
                # same for a key without permission. Neither is worth a retry
                # and both must stop the pipeline rather than loop.
                if exc.code in (402,) or "PAYMENT_REQUIRED" in str(payload).upper():
                    raise OutOfCredit(message) from exc
                if exc.code == 401:
                    raise NotConfigured(f"Camb rejected the API key ({message})") from exc
                if exc.code == 429:
                    retry_after = _retry_after(exc)
                    if attempt >= retries:
                        raise RateLimited(message, retry_after) from exc
                    last = RateLimited(message, retry_after)
                    time.sleep(min(retry_after, 10.0))
                    continue
                if 500 <= exc.code < 600 and attempt < retries:
                    last = ProviderFailed(message)
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise ProviderFailed(message) from exc
            except urllib.error.URLError as exc:
                last = ProviderFailed(f"cannot reach Camb AI: {exc.reason}")
                if attempt >= retries:
                    raise last from exc
                time.sleep(1.0 * (attempt + 1))
            except json.JSONDecodeError as exc:
                raise ProviderFailed(f"Camb returned something that is not JSON: {exc}") from exc
        raise last or ProviderFailed("request failed for no reason we recorded")

    def _await_job(self, kind: str, task: str) -> int:
        """Poll a job to a terminal state and return its run id.

        Backs off from ``poll_interval_s`` so a fast job is collected quickly
        and a slow one does not hammer the API, and gives up at
        ``job_timeout_s`` -- a transcript that arrives after the move it
        describes is of no use to anyone, and the worker is needed for the
        audio queued behind it.
        """
        deadline = time.monotonic() + self.settings.job_timeout_s
        wait = self.settings.poll_interval_s
        while True:
            state = m.job_state(self._call("GET", f"/{kind}/{task}"))
            if state.ok:
                if state.run_id is None:
                    raise ProviderFailed(f"{kind} job {task} succeeded with no run id")
                return state.run_id
            if not state.pending:
                raise ProviderFailed(
                    f"{kind} job {task} ended as {state.status}"
                    + (f": {state.message}" if state.message else "")
                )
            if time.monotonic() >= deadline:
                raise JobTimeout(
                    f"{kind} job {task} was still {state.status} after "
                    f"{self.settings.job_timeout_s:.0f}s"
                )
            time.sleep(wait)
            wait = min(wait * self.settings.poll_backoff, self.settings.poll_max_interval_s)

    # -- capability: transcription ---------------------------------------
    def transcribe(self, segment: AudioSegment, language: str) -> Transcript:
        # perf_counter, not monotonic: on Windows monotonic ticks at ~15.6 ms,
        # so a quick call measures as exactly zero and every latency figure on
        # the page would be quantised to that step.
        started = time.perf_counter()
        tag = self.languages_map.resolve(language) if self.languages_map.known else language
        blob = to_wav(segment.samples, segment.rate, self.settings.upload_rate)
        fields = m.transcribe_fields(
            segment, tag, transcription_mode=self.settings.camb_transcription_mode
        )
        created = self._call(
            "POST",
            "/transcribe",
            multipart=(fields, f"{segment.channel}-{segment.seq}.wav", blob),
        )
        run_id = self._await_job("transcribe", m.task_id(created))
        result = self._call("GET", f"/transcription-result/{run_id}")
        return m.transcript(
            result,
            language=tag,
            provider=self.name,
            model=self.model,
            latency_s=time.perf_counter() - started,
        )

    # -- capability: translation -----------------------------------------
    def translate(self, texts: Sequence[str], source: str, target: str) -> Translation:
        lines = [t for t in texts if t.strip()]
        if not lines:
            return Translation((), source, target, self.name, self.model, 0.0)
        started = time.perf_counter()
        src = self.languages_map.resolve(source) if self.languages_map.known else source
        dst = self.languages_map.resolve(target) if self.languages_map.known else target
        if src == "auto":
            # Camb's translation endpoint needs a real source language; only
            # transcription accepts "auto". Without a known source there is
            # nothing honest to send.
            raise NotConfigured(
                "the source language is unknown, and translation cannot detect it"
            )
        created = self._call("POST", "/translate", body=m.translate_body(lines, src, dst))
        run_id = self._await_job("translate", m.task_id(created))
        result = self._call("GET", f"/translation-result/{run_id}")
        return m.translation(
            result,
            expected=len(lines),
            source=src,
            target=dst,
            provider=self.name,
            model=self.model,
            latency_s=time.perf_counter() - started,
        )

    # -- capability: speech ----------------------------------------------
    def speak(self, text: str, language: str) -> SpokenAudio:
        """Say ``text`` in ``language`` and hand back the audio bytes.

        ``/tts-stream`` rather than ``/tts``: the asynchronous one is marked
        deprecated in the reference, and it is also three round trips to this
        one's one. The response body is the audio, which is why this is the
        only call in the adapter that does not parse JSON.
        """
        started = time.perf_counter()
        tag = self.languages_map.resolve(language) if self.languages_map.known else language
        fmt = self.settings.camb_audio_format
        body = t.tts_body(
            text,
            tag,
            voice_id=self.settings.camb_voice_id,
            speech_model=self.settings.camb_speech_model,
            audio_format=fmt,
        )
        # A longer timeout than the job endpoints get: this one streams the
        # whole of the audio back in the single response, so the clock has to
        # cover synthesis as well as the request.
        audio, headers = self._call(
            "POST", "/tts-stream", body=body, raw=True,
            timeout=max(self.settings.timeout_s, self.settings.job_timeout_s),
        )
        if not audio:
            raise ProviderFailed("the voice API returned no audio")
        return SpokenAudio(
            audio=audio,
            media_type=t.media_type(fmt),
            voice=str(self.settings.camb_voice_id),
            model=self.settings.camb_speech_model,
            provider=self.name,
            language=tag,
            latency_s=time.perf_counter() - started,
            credits=t.credits_required(headers),
        )

    def voices(self) -> tuple[dict, ...]:
        """The voices this key may use."""
        return t.voices(self._call("GET", "/list-voices"))

    # -- live ------------------------------------------------------------
    def open_live(
        self,
        source: str,
        target: str,
        *,
        mode: str = rt.FAST,
        voice_id: int | None = None,
    ) -> CambLiveSession:
        """Open a realtime session on ``wss://realtime.camb.ai/v1/realtime``.

        A wholly different protocol from the rest of this adapter, and a
        different host, so none of the REST plumbing above is reused and
        pointing ``camb_base_url`` at a fake does not redirect it. The tests
        set ``_live_url`` and ``_connect`` instead.
        """
        if not self._key:
            raise NotConfigured(
                "CAMB_API_KEY is not set, so no live session can open."
            )
        return CambLiveSession(
            self._key,
            source,
            target,
            mode=mode,
            voice_id=voice_id,
            url=self._live_url,
            connect=self._connect,
        )

    def live_languages(self) -> tuple[str, ...]:
        """The realtime list, which is *not* the file-translation list.

        Returned from the documented constant rather than fetched: Camb
        publishes no discovery endpoint for realtime, and the source and
        target language endpoints belong to the REST path, which carries far
        more languages. Showing those beside a live control is how somebody
        picks a language that is only refused once a match has started.
        """
        return rt.REALTIME_LANGUAGES

    def live_modes(self) -> tuple[dict, ...]:
        """Camb has two pipelines, and the difference matters to a caller."""
        return (
            {"name": rt.FAST,
             "detail": "Accepts audio almost immediately. The fourteen "
                       "languages Camb publishes for realtime."},
            {"name": rt.SLOW,
             "detail": "More languages, but the docs document a cold boot of "
                       "30 seconds or more before it takes audio."},
        )

    def live_note(self, target: str) -> str:
        """What to say about ``target`` before a session is opened."""
        _, warning = rt.supported(target)
        return warning

    def live_capabilities(self) -> dict:
        """What the live path does, for the page.

        Stated separately from :meth:`capabilities` because it is a different
        product with different languages and its own billing window, and a
        page that showed the file-translation steps beside a live button would
        be describing the wrong thing.
        """
        return {
            "provider": self.name,
            "model": "camb/realtime-speech-to-speech",
            "transport": "WebSocket",
            "endpoint": rt.REALTIME_URL,
            "modes": [
                {
                    "name": rt.FAST,
                    "detail": "Accepts audio almost immediately. The fourteen "
                              "languages Camb publishes for realtime.",
                },
                {
                    "name": rt.SLOW,
                    "detail": "More languages, but the docs document a cold "
                              "boot of 30 seconds or more before the session "
                              "takes audio.",
                },
            ],
            "wire": {
                "rate": rt.WIRE_RATE,
                "format": "PCM16 mono, base64 inside JSON going out, "
                          "raw frames coming back",
            },
            "languages": list(rt.REALTIME_LANGUAGES),
            "limits": {
                "max_session_s": rt.MAX_SESSION_S,
                "idle_s": rt.IDLE_LIMIT_S,
                "setup_s": rt.SETUP_DEADLINE_S,
                # Slow mode documents "30 seconds or more"; the app doubles
                # whatever it is told to get a timeout it can wait on.
                "boot_s": 35.0 if self.settings.live_mode == rt.SLOW else 5.0,
            },
            "caveats": [
                "The docs print one list of 14 realtime languages and say "
                "slow mode carries more, without printing either per-mode "
                "list. Italian and Greek are absent from the published list.",
                "Camb documents no rate per minute for realtime and no "
                "balance endpoint. Sessions are billed in windows and "
                "finalised on close.",
            ],
        }

    # -- housekeeping ----------------------------------------------------
    def capabilities(self) -> dict:
        """What this provider's audio translation actually does.

        Three chained calls rather than one, and worth stating plainly on the
        page: Camb does have a single end-to-end dubbing endpoint, but it
        takes a URL it can fetch the media from, and our audio is a few
        seconds held in memory on a machine with no public address. So the
        reachable path is recognise, translate, speak -- which has the side
        benefit that each step's output is visible when one of them is wrong.
        """
        return {
            "provider": self.name,
            "model": self.model,
            "voice_model": self.settings.camb_speech_model,
            "audio_in": True,
            "audio_out": True,
            "steps": [
                {
                    "name": "Hear it",
                    "endpoint": "POST /transcribe",
                    "detail": "The audio is uploaded as a 16 kHz mono WAV and "
                              "comes back as timed text in the language it was "
                              "spoken in.",
                },
                {
                    "name": "Translate it",
                    "endpoint": "POST /translate",
                    "detail": "The lines are translated as a batch, in order. "
                              "Skipped when the commentary is already in the "
                              "target language.",
                },
                {
                    "name": "Speak it",
                    "endpoint": "POST /tts-stream",
                    "detail": f"The translation is synthesised as "
                              f"{self.settings.camb_audio_format.upper()} audio "
                              f"by {self.settings.camb_speech_model} and returned "
                              f"in one response.",
                },
            ],
            "not_available": [
                {
                    "name": "End-to-end dubbing",
                    "endpoint": "POST /dub",
                    "detail": "Camb can dub audio in a single call, but it "
                              "fetches the media from a URL. Our segments live "
                              "in memory on a host with no public address, so "
                              "there is nothing to give it.",
                },
            ],
        }

    def languages(self, max_age_s: float = 3600.0) -> dict[str, str]:
        """The provider's own catalogue, cached for an hour.

        Fetched rather than hardcoded because the docs deprecate the numeric
        ids without printing the replacement tags, and warn against assuming
        them. One call settles it.
        """
        now = time.monotonic()
        if self.languages_map.known and now - self._languages_at < max_age_s:
            return self.languages_map.catalogue
        catalogue = m.languages(self._call("GET", "/source-languages"))
        self.languages_map.update(catalogue)
        self._languages_at = now
        return catalogue

    def healthcheck(self) -> dict:
        """Never raises: the page asks this question and wants an answer."""
        if not self._key:
            return {"ok": False, "provider": self.name, "error": "no API key set"}
        started = time.perf_counter()
        try:
            catalogue = self.languages()
        except Exception as exc:  # noqa: BLE001 - reporting beats propagating
            return {"ok": False, "provider": self.name, "error": str(exc)[:300]}
        return {
            "ok": True,
            "provider": self.name,
            "languages": len(catalogue),
            "latency_s": round(time.perf_counter() - started, 3),
        }

    def close(self) -> None:
        pass


# -- multipart ------------------------------------------------------------
def _encode_multipart(
    fields: dict[str, str], filename: str, blob: bytes
) -> tuple[bytes, str]:
    """One file plus text fields, as ``multipart/form-data``.

    Hand-rolled because it is twenty lines and the alternative is a
    dependency. The boundary is random per request so a value inside a field
    can never terminate the body early.
    """
    boundary = f"----qc{uuid.uuid4().hex}"
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    out += f"--{boundary}\r\n".encode()
    out += (
        f'Content-Disposition: form-data; name="media_file"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    out += blob
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _read_json(exc: urllib.error.HTTPError) -> Any:
    try:
        return json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _retry_after(exc: urllib.error.HTTPError) -> float:
    for header in ("retry-after", "Retry-After", "x-ratelimit-reset"):
        value = exc.headers.get(header) if exc.headers else None
        if value:
            try:
                return max(0.5, float(value))
            except (TypeError, ValueError):
                continue
    return 5.0
