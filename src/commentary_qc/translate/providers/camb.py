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

from ..audio import to_wav
from ..mappers import camb as m
from ..mappers.languages import LanguageMap
from ..models import (
    AudioSegment,
    JobTimeout,
    NotConfigured,
    OutOfCredit,
    ProviderFailed,
    RateLimited,
    Transcript,
    Translation,
)
from ..settings import TranslateSettings

log = logging.getLogger(__name__)


class CambProvider:
    """Speech recognition and translation through Camb AI's REST API."""

    name = "camb"
    #: What gets recorded alongside each caption, so a change of model is
    #: visible in the data rather than inferred from the date.
    model = "camb/transcribe+translate"

    def __init__(self, settings: TranslateSettings, opener: Any | None = None) -> None:
        self.settings = settings
        self.base = settings.camb_base_url.rstrip("/")
        self._key = settings.camb_api_key
        self._opener = opener or urllib.request.build_opener()
        self.languages_map = LanguageMap()
        self._languages_at = 0.0

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
                with self._opener.open(req, timeout=self.settings.timeout_s) as resp:
                    raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
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

    # -- housekeeping ----------------------------------------------------
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
