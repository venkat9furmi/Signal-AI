"""Tests for the translation layer.

Three levels, each answering a different question:

1. **Mappers** -- does our code read the vendor's JSON correctly? Run against
   the exact shapes Camb's API reference prints, so no key and no network.
2. **Parts** -- do the segmenter, the budget and the mode switch behave?
3. **The whole pipeline** -- against a *fake Camb AI* served from localhost,
   which is the only honest way to test the create/poll/fetch dance, the retry
   on 429 and the stop on 402 without waiting for the real service to
   misbehave. This is the same approach ``test_slack_delivery.py`` takes.

    python tests/test_translate.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.store import EventStore  # noqa: E402
from commentary_qc.translate import TranslateSettings, TranslationPipeline  # noqa: E402
from commentary_qc.translate.app import control as control_mod  # noqa: E402
from commentary_qc.translate.infra.audio import to_wav, wav_seconds  # noqa: E402
from commentary_qc.translate.app.budget import Budget  # noqa: E402
from commentary_qc.translate.adapters.camb import mapper as cm  # noqa: E402
from commentary_qc.translate.adapters.camb import tts as tm  # noqa: E402
from commentary_qc.translate.core.languages import LanguageMap  # noqa: E402
from commentary_qc.translate.core.models import (  # noqa: E402
    AudioSegment,
    ModeNotBuilt,
    NotConfigured,
    OutOfCredit,
    ProviderFailed,
    RateLimited,
)
from commentary_qc.translate.app.modes import ModeRegistry  # noqa: E402
from commentary_qc.translate.adapters import build, describe  # noqa: E402
from commentary_qc.translate.adapters.camb.provider import CambProvider  # noqa: E402
from commentary_qc.translate.adapters.mock.provider import MockProvider  # noqa: E402
from commentary_qc.translate.infra.runs import RunStore  # noqa: E402
from commentary_qc.translate.app.segmenter import Segmenter  # noqa: E402
from commentary_qc.translate.settings import Mode  # noqa: E402
from commentary_qc.translate.infra.captions import CaptionStore  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def segment(seconds: float = 10.0, rate: int = 16_000, channel: str = "football-01",
            language: str = "it-it", seq: int = 1) -> AudioSegment:
    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float32) / rate
    samples = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    return AudioSegment(
        channel=channel, session="sess", seq=seq, t0=0.0, t1=seconds,
        wall=time.time(), samples=samples, rate=rate, language=language,
        speech_ratio=0.9,
    )


# ======================================================================
# 1. Mappers, against the JSON the API reference prints
# ======================================================================
def test_mappers() -> None:
    section("mappers: Camb AI JSON -> our types")

    check("task id", cm.task_id({"task_id": "abc-123"}) == "abc-123")
    try:
        cm.task_id({"oops": 1})
        check("a missing task id is an error", False)
    except ProviderFailed:
        check("a missing task id is an error", True)

    state = cm.job_state({"status": "SUCCESS", "run_id": 4321})
    check("job success carries the run id", state.ok and state.run_id == 4321)
    check("PENDING is not terminal", cm.job_state({"status": "PENDING"}).pending)
    check("ERROR is terminal but not ok",
          not cm.job_state({"status": "ERROR"}).pending
          and not cm.job_state({"status": "ERROR"}).ok)
    # The one status that must stop the pipeline rather than be retried.
    try:
        cm.job_state({"status": "PAYMENT_REQUIRED"})
        check("PAYMENT_REQUIRED raises OutOfCredit", False)
    except OutOfCredit:
        check("PAYMENT_REQUIRED raises OutOfCredit", True)

    # Exactly the example from the get-transcription-run-result page.
    doc = {"transcript": [
        {"start": 5.0, "end": 12.0, "text": "Welcome to our discussion on AI.",
         "speaker": "SPEAKER_0"},
        {"start": 13.0, "end": 18.0, "text": "Thank you for having me.",
         "speaker": "SPEAKER_1"},
    ]}
    tr = cm.transcript(doc, language="en-us")
    check("transcript: two utterances", len(tr.utterances) == 2)
    check("transcript: times in seconds",
          tr.utterances[0].start_s == 5.0 and tr.utterances[0].end_s == 12.0)
    check("transcript: speakers kept", tr.utterances[1].speaker == "SPEAKER_1")
    check("transcript: joined text",
          tr.text == "Welcome to our discussion on AI. Thank you for having me.")
    check("an empty utterance is dropped",
          len(cm.transcript({"transcript": [{"text": "  "}, {"text": "x"}]}).utterances) == 1)
    check("an exported string transcript still reads",
          cm.transcript({"transcript": "one line"}).text == "one line")

    tl = cm.translation({"texts": ["Ciao", "Bene"]}, expected=2, target="it-it")
    check("translation: order preserved", tl.texts == ("Ciao", "Bene"))
    # The important one: a short batch must fail loudly, because silently
    # zipping would attach the wrong translation to a line.
    try:
        cm.translation({"texts": ["only one"]}, expected=2)
        check("a short batch is refused", False)
    except ProviderFailed as exc:
        check("a short batch is refused", "cannot be matched" in str(exc))

    langs = cm.languages([
        {"id": 1, "language": "English", "short_name": "en-US"},
        {"id": 2, "language": "Italian", "short_name": "it-IT"},
    ])
    check("languages: tags lowercased", set(langs) == {"en-us", "it-it"})
    check("languages: names kept", langs["it-it"] == "Italian")

    # FastAPI's validation error shape, which is what a bad field produces.
    msg = cm.error_message(
        {"detail": [{"loc": ["body", "texts"], "msg": "field required",
                     "type": "value_error.missing"}]}, 422)
    check("a validation error flattens to one line",
          "texts" in msg and "field required" in msg, msg)
    check("a string detail is used as-is",
          "no credit" in cm.error_message({"detail": "no credit"}, 402))

    body = cm.translate_body(["a", "b"], "it-it", "en-us")
    check("translate body has the documented fields",
          body["source_language"] == "it-it" and body["texts"] == ["a", "b"]
          and body["formality"] == 1)
    fields = cm.transcribe_fields(segment(), "it-it")
    opts = json.loads(fields["formatting_options"])
    check("formatting_options is a JSON string, not an object",
          isinstance(fields["formatting_options"], str)
          and opts["max_characters_in_segment"] == 42)


def test_languages() -> None:
    section("languages: our names -> whatever the provider actually offers")
    m = LanguageMap({"en-us": "English", "pt-br": "Portuguese", "it-it": "Italian"})
    check("an exact tag passes through", m.resolve("it-it") == "it-it")
    # The docs warn against assuming pt-pt exists. Falling back within the
    # same language is right; falling back across languages is not.
    check("pt-pt falls back to pt-br", m.resolve("pt-pt") == "pt-br")
    check("auto passes through", m.resolve("auto") == "auto")
    try:
        m.resolve("el-gr")
        check("a language the provider lacks is refused", False)
    except NotConfigured as exc:
        check("a language the provider lacks is refused", "el-gr" in str(exc))
    check("an unknown catalogue permits everything",
          LanguageMap().resolve("el-gr") == "el-gr")


def test_audio() -> None:
    section("audio: 48 kHz float -> 16 kHz WAV")
    rate = 48_000
    t = np.arange(rate * 2, dtype=np.float32) / rate
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    blob = to_wav(tone, rate, 16_000)
    check("it is a RIFF/WAVE file", blob[:4] == b"RIFF" and blob[8:12] == b"WAVE")
    check("duration survives the downsample", abs(wav_seconds(blob) - 2.0) < 0.01,
          f"{wav_seconds(blob):.3f}s")
    check("16-bit mono at 16 kHz is a third of the bytes",
          abs(len(blob) - 2 * 16_000 * 2) < 100, str(len(blob)))
    # A sample at exactly +1.0 must not wrap to the most negative value.
    hot = to_wav(np.array([1.0, 1.0, -1.0], dtype=np.float32), 16_000, 16_000)
    ints = np.frombuffer(hot[44:], dtype="<i2")
    check("full scale does not wrap", int(ints[0]) == 32767, str(ints[:3]))
    twenty = to_wav(np.zeros(20 * 16_000, dtype=np.float32), 16_000, 16_000)
    check("a 20 s segment is far inside the 20 MB upload limit",
          len(twenty) < 1_000_000, f"{len(twenty) / 1e6:.2f} MB")


# ======================================================================
# 2. Parts
# ======================================================================
def test_segmenter() -> None:
    section("segmenter: cutting at pauses, not at a stopwatch")
    cfg = TranslateSettings(
        mode=Mode.SHADOW, segment_min_s=4.0, segment_max_s=10.0,
        segment_gap_windows=2, min_speech_ratio=0.3,
    )
    rate = 16_000
    block = np.full(rate, 0.1, dtype=np.float32)  # one second

    # Talking continuously: must close at the hard maximum.
    seg = Segmenter("football-01", "s", cfg, rate=rate)
    out = None
    for i in range(12):
        seg.audio(block, time.time(), float(i))
        got = seg.window(1.0)
        if got is not None and out is None:
            out = got
    check("a continuous talker is cut at segment_max_s",
          out is not None and 9.0 <= out.duration_s <= 11.0,
          f"{out.duration_s if out else None}")

    # A pause after the minimum: must close early, at the pause.
    seg = Segmenter("football-01", "s", cfg, rate=rate)
    out = None
    for i in range(5):
        seg.audio(block, time.time(), float(i))
        out = seg.window(1.0) or out
    check("no cut before segment_min_s is reached", out is None)
    for i in range(2):
        seg.audio(block, time.time(), 5.0 + i)
        out = seg.window(0.0) or out
    check("two quiet windows close the phrase",
          out is not None and 6.0 <= out.duration_s <= 8.0,
          f"{out.duration_s if out else None}")
    check("the segment carries its own speech ratio",
          out is not None and 0.6 < out.speech_ratio < 0.8, f"{out.speech_ratio}")

    # A holding clip or a silent channel: billable and empty. Must be dropped.
    seg = Segmenter("football-01", "s", cfg, rate=rate)
    produced = []
    for i in range(14):
        seg.audio(block, time.time(), float(i))
        got = seg.window(0.0)
        if got:
            produced.append(got)
    check("audio with no speech in it is never sent", not produced)
    check("and the drop is counted, not hidden", seg.dropped_quiet >= 1)

    # A gap in the stream must not look like the end of a sentence.
    seg = Segmenter("football-01", "s", cfg, rate=rate)
    for i in range(3):
        seg.audio(block, time.time(), float(i))
        seg.window(1.0)
    for _ in range(5):
        seg.window(None)
    check("windows with no verdict do not close a phrase", seg._quiet_run == 0)

    check("flush returns the last phrase at the end of a broadcast",
          _flush_gives_a_segment(cfg, rate))


def _flush_gives_a_segment(cfg, rate: int) -> bool:
    seg = Segmenter("football-01", "s", cfg, rate=rate)
    block = np.full(rate, 0.1, dtype=np.float32)
    for i in range(3):
        seg.audio(block, time.time(), float(i))
        seg.window(1.0)
    return seg.flush() is not None


def test_budget() -> None:
    section("budget: the guard on the free credits")
    now = [1_700_000_000.0]
    b = Budget(daily_minutes=1.0, per_channel_minutes=0.5, clock=lambda: now[0])

    ok, _ = b.allow("a", 20.0)
    check("a small request is allowed", ok)
    b.charge("a", 20.0)
    ok, why = b.allow("a", 20.0)
    check("the per-channel cap bites first", not ok and "this channel" in why, why)
    ok, _ = b.allow("b", 20.0)
    check("another channel is still allowed", ok)
    b.charge("b", 20.0)
    ok, why = b.allow("c", 30.0)
    check("the daily cap stops everyone", not ok and "daily" in why, why)

    now[0] += 86_400
    ok, _ = b.allow("a", 30.0)
    check("the cap resets at the UTC day boundary", ok)

    b.block("Camb AI says the account is out of credit")
    ok, why = b.allow("a", 1.0)
    check("a refusal from the provider latches shut", not ok and "out of credit" in why)
    check("and is reported", b.snapshot()["blocked"] is True)
    b.unblock()
    check("it can be cleared by hand", b.allow("a", 1.0)[0])

    b2 = Budget(daily_minutes=10.0)
    b2.seed(540.0, {"x": 540.0})
    check("a restart adopts the spend already in the database",
          b2.snapshot()["spent_minutes"] == 9.0)
    check("so the cap is not handed back on every restart",
          not b2.allow("x", 120.0)[0])


def test_settings_and_modes() -> None:
    section("settings and the mode switch")
    try:
        TranslateSettings(mode="sideways")
        check("an unknown mode is refused", False)
    except ValueError:
        check("an unknown mode is refused", True)
    try:
        TranslateSettings(segment_min_s=30.0, segment_max_s=10.0)
        check("a min above the max is refused", False)
    except ValueError as exc:
        check("a min above the max is refused", "could ever close" in str(exc))
    try:
        TranslateSettings.from_mapping({"segment_min_sec": 5})
        check("a misspelled setting fails loudly", False)
    except ValueError as exc:
        check("a misspelled setting fails loudly", "unknown" in str(exc))
    try:
        TranslateSettings(provider="camb", enabled_providers=("mock",))
        check("a provider that is not enabled cannot be selected", False)
    except ValueError:
        check("a provider that is not enabled cannot be selected", True)

    off = TranslateSettings(mode=Mode.OFF)
    check("off: nothing is in scope", not off.covers("football-01", claimed=True))
    shadow = TranslateSettings(mode=Mode.SHADOW)
    check("shadow runs on a claimed channel", shadow.covers("football-01", True))
    check("shadow does not publish", not shadow.publishes)
    check("shadow skips unclaimed channels", not shadow.covers("football-02", False))
    standard = TranslateSettings(mode=Mode.STANDARD)
    check("standard publishes", standard.publishes)
    check("the old name still loads", TranslateSettings(mode="prod").mode == Mode.STANDARD)
    picked = TranslateSettings(mode=Mode.STANDARD, channels=("football-41",))
    check("an explicit channel list narrows it further",
          picked.covers("football-41", True) and not picked.covers("football-01", True))
    anyone = TranslateSettings(mode=Mode.SHADOW, claimed_only=False)
    check("claimed_only off covers an unclaimed channel",
          anyone.covers("football-09", False))

    s = TranslateSettings(camb_api_key="secret-key-value")
    check("the API key never leaves in a response",
          s.to_dict()["camb_api_key"] is True
          and "secret-key-value" not in json.dumps(s.to_dict()))


def test_control(tmp: Path) -> None:
    section("control: the switch the page writes and the recorder reads")
    path = str(tmp / "claims-control.db")
    blank = control_mod.read(path)
    check("an untouched switch reads as off", blank["mode"] == Mode.OFF and not blank["set"])
    check("a missing file does not raise", control_mod.read(str(tmp / "nope.db"))["mode"] == "off")

    control_mod.ensure(path)
    row = control_mod.write(path, mode=Mode.SHADOW, provider="mock", by="tester")
    check("a write round-trips", row["mode"] == Mode.SHADOW and row["set"])
    check("the author is recorded", control_mod.read(path)["updated_by"] == "tester")
    try:
        control_mod.write(path, mode="sideways")
        check("an impossible mode is rejected at the page, not the recorder", False)
    except ValueError:
        check("an impossible mode is rejected at the page, not the recorder", True)

    base = TranslateSettings(mode=Mode.OFF, enabled_providers=("mock",))
    applied = control_mod.apply(base, {"mode": "prod", "provider": "camb", "target": "en-us"})
    check("the page cannot enable a provider the deployment left out",
          applied.provider == "mock")
    with_camb = TranslateSettings(mode=Mode.OFF, enabled_providers=("mock", "camb"))
    applied = control_mod.apply(
        with_camb, {"mode": "prod", "provider": "camb", "target": "en-us"})
    check("asking for Camb with no key stays off, rather than failing per segment",
          applied.mode == Mode.OFF)
    keyed = TranslateSettings(
        mode=Mode.OFF, enabled_providers=("mock", "camb"), camb_api_key="k")
    applied = control_mod.apply(
        keyed, {"mode": "prod", "provider": "camb", "target": "en-us"})
    check("with a key it goes to standard", applied.mode == Mode.STANDARD
          and applied.provider == "camb")


def test_registry() -> None:
    section("provider registry")
    s = TranslateSettings(enabled_providers=("mock",))
    check("mock builds", isinstance(build("mock", s), MockProvider))
    try:
        build("camb", s)
        check("a disabled provider cannot be built by any path", False)
    except NotConfigured as exc:
        check("a disabled provider cannot be built by any path",
              "not enabled" in str(exc))
    try:
        build("camb", TranslateSettings(enabled_providers=("mock", "camb")))
        check("a provider with no key is refused before any audio is captured",
              False)
    except NotConfigured as exc:
        check("a provider with no key is refused before any audio is captured",
              "CAMB_API_KEY" in str(exc))
    try:
        build("whisper-xl", s)
        check("an unknown provider is refused", False)
    except NotConfigured:
        check("an unknown provider is refused", True)

    rows = {r["name"]: r for r in describe(
        TranslateSettings(enabled_providers=("mock", "camb")))}
    check("the page is told why Camb cannot be picked",
          not rows["camb"]["selectable"] and "CAMB_API_KEY" in rows["camb"]["blocked"])
    check("and that mock costs nothing", rows["mock"]["cost"] == "free")

    mock = MockProvider(s)
    a = mock.transcribe(segment(seq=1), "it-it")
    b = MockProvider(s).transcribe(segment(seq=1), "it-it")
    check("the mock is deterministic in its audio", a.text == b.text)
    c = mock.transcribe(segment(seq=2, seconds=11.0), "it-it")
    check("a different length gives different words", c.text != a.text)
    tl = mock.translate(["uno", "due"], "it-it", "en-us")
    check("mock output is marked so it cannot pass for a translation",
          all(t.startswith("[en-us]") for t in tl.texts))


# ======================================================================
# 3. The whole pipeline, against a fake Camb AI
# ======================================================================
class FakeCamb(BaseHTTPRequestHandler):
    """A stand-in that speaks Camb's documented JSON.

    Behaviour is driven by class attributes so a test can make it slow, make
    it rate-limit, or make it refuse payment -- the three things that are
    impossible to arrange on demand with the real service.
    """

    calls: list[tuple[str, str]] = []
    keys: list[str] = []
    pending_polls = 0
    fail_with = 0          # an HTTP status to return instead of working
    fail_times = 0         # how many times, then recover
    uploads: list[int] = []
    spoken: list[dict] = []
    _poll_counts: dict[str, int] = {}

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    # -- helpers --------------------------------------------------------
    def _send(self, code: int, payload: dict, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _maybe_fail(self) -> bool:
        cls = type(self)
        if cls.fail_with and cls.fail_times > 0:
            cls.fail_times -= 1
            headers = {"retry-after": "0.1"} if cls.fail_with == 429 else {}
            self._send(cls.fail_with, {"detail": f"forced {cls.fail_with}"}, headers)
            return True
        return False

    # -- routes ---------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        cls = type(self)
        cls.calls.append(("POST", self.path))
        cls.keys.append(self.headers.get("x-api-key", ""))
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        if self._maybe_fail():
            return
        if self.path == "/transcribe":
            # Prove the audio really arrived as a WAV in a multipart body.
            cls.uploads.append(len(body))
            assert b"RIFF" in body, "no WAV in the upload"
            assert b'name="media_file"' in body, "no media_file field"
            assert b'name="language"' in body, "no language field"
            self._send(200, {"task_id": "task-tr-1"})
        elif self.path == "/translate":
            payload = json.loads(body)
            cls._poll_counts["texts"] = len(payload["texts"])
            self._send(200, {"task_id": "task-tl-1"})
        elif self.path == "/tts-stream":
            payload = json.loads(body)
            cls.spoken.append(payload)
            # The documented contract, asserted here so a mapper that drifts
            # out of it fails in a test rather than as a 422 in production.
            assert 3 <= len(payload["text"]) <= 3000, "text out of bounds"
            assert isinstance(payload["voice_id"], int), "voice_id must be an int"
            assert payload["language"] == payload["language"].lower(), "tag must be lowercase"
            assert payload["output_configuration"]["format"] == "wav"
            wav = to_wav(np.zeros(1600, dtype=np.float32), 16_000, 16_000)
            self.send_response(200)
            self.send_header("content-type", "audio/wav")
            self.send_header("content-length", str(len(wav)))
            # The only credit figure Camb publishes anywhere.
            self.send_header("X-Credits-Required", "42")
            self.end_headers()
            self.wfile.write(wav)
        else:
            self._send(404, {"detail": "no such path"})

    def do_GET(self) -> None:  # noqa: N802
        cls = type(self)
        cls.calls.append(("GET", self.path))
        cls.keys.append(self.headers.get("x-api-key", ""))
        if self._maybe_fail():
            return
        if self.path in ("/source-languages", "/target-languages"):
            self._send(200, [
                {"id": 1, "language": "English", "short_name": "en-US"},
                {"id": 2, "language": "Italian", "short_name": "it-IT"},
                {"id": 3, "language": "German", "short_name": "de-DE"},
            ])
        elif self.path == "/list-voices":
            # The wrapped shape. Camb's reference shows this in its example
            # and a bare array in its schema, so both have to work.
            self._send(200, {"voices": [
                {"id": 147320, "voice_name": "Gary", "gender": 1, "age": 35,
                 "language": "en-us", "description": "A rich, warm voice"},
                {"id": 20305, "voice_name": "Alice", "gender": 2, "age": 65,
                 "language": "en-us", "description": "A warm, steady voice"},
            ]})
        elif m := re.match(r"^/(transcribe|translate)/(.+)$", self.path):
            kind, task = m.group(1), m.group(2)
            seen = cls._poll_counts.get(task, 0) + 1
            cls._poll_counts[task] = seen
            if seen <= cls.pending_polls:
                self._send(200, {"status": "PENDING", "run_id": None})
            else:
                self._send(200, {"status": "SUCCESS", "run_id": 99 if kind == "transcribe" else 77})
        elif self.path.startswith("/transcription-result/"):
            self._send(200, {"transcript": [
                {"start": 0.0, "end": 4.0, "text": "palla in mezzo", "speaker": "SPEAKER_0"},
                {"start": 4.0, "end": 8.0, "text": "e il portiere respinge", "speaker": "SPEAKER_0"},
            ]})
        elif self.path.startswith("/translation-result/"):
            n = cls._poll_counts.get("texts", 2)
            self._send(200, {"texts": ["ball into the middle",
                                       "and the keeper pushes it away"][:n]})
        else:
            self._send(404, {"detail": "no such path"})

    @classmethod
    def reset(cls) -> None:
        cls.calls, cls.keys, cls.uploads, cls.spoken = [], [], [], []
        cls.pending_polls = 0
        cls.fail_with = 0
        cls.fail_times = 0
        cls._poll_counts = {}


def test_camb_adapter(base: str) -> None:
    section("Camb adapter, against a local fake")
    FakeCamb.reset()
    s = TranslateSettings(
        mode=Mode.SHADOW, provider="camb", camb_api_key="test-key",
        camb_base_url=base, target_language="en-us", poll_interval_s=0.01,
        poll_max_interval_s=0.05, job_timeout_s=3.0,
    )
    p = CambProvider(s)

    cat = p.languages()
    check("the catalogue is fetched, not assumed", set(cat) >= {"en-us", "it-it"})
    check("the key is sent as x-api-key", FakeCamb.keys and FakeCamb.keys[0] == "test-key")

    tr = p.transcribe(segment(seconds=8.0), "it-it")
    check("transcription returns both utterances", len(tr.utterances) == 2)
    check("with the words the fake sent", tr.text == "palla in mezzo e il portiere respinge")
    check("the audio really went up as a WAV",
          FakeCamb.uploads and FakeCamb.uploads[0] > 1000, str(FakeCamb.uploads))
    check("latency is measured", tr.latency_s > 0)
    paths = [c[1] for c in FakeCamb.calls]
    check("it followed create -> poll -> result",
          "/transcribe" in paths
          and any(x.startswith("/transcribe/task-tr-1") for x in paths)
          and any(x.startswith("/transcription-result/99") for x in paths), str(paths))

    tl = p.translate(["palla in mezzo", "e il portiere respinge"], "it-it", "en-us")
    check("translation comes back in order",
          tl.texts == ("ball into the middle", "and the keeper pushes it away"))

    # Polling: a job that is not ready must be waited for, not treated as failed.
    FakeCamb.reset()
    FakeCamb.pending_polls = 3
    tr = p.transcribe(segment(seconds=8.0), "it-it")
    polls = sum(1 for c in FakeCamb.calls if c[1].startswith("/transcribe/task"))
    check("a pending job is polled until it is ready", tr.text and polls >= 4, f"{polls} polls")

    # A job that never finishes must give up rather than hold the worker.
    FakeCamb.reset()
    FakeCamb.pending_polls = 10_000
    slow = CambProvider(TranslateSettings(
        mode=Mode.SHADOW, provider="camb", camb_api_key="k", camb_base_url=base,
        poll_interval_s=0.01, poll_max_interval_s=0.02, job_timeout_s=0.15,
    ))
    started = time.monotonic()
    try:
        slow.transcribe(segment(seconds=4.0), "it-it")
        check("a job that never finishes times out", False)
    except Exception as exc:  # JobTimeout
        took = time.monotonic() - started
        check("a job that never finishes times out",
              type(exc).__name__ == "JobTimeout" and took < 2.0, f"{type(exc).__name__} {took:.2f}s")

    # 429 is retried; the fake asks for 0.1 s and then works.
    FakeCamb.reset()
    FakeCamb.fail_with, FakeCamb.fail_times = 429, 1
    tr = p.transcribe(segment(seconds=6.0), "it-it")
    check("a rate limit is retried, not dropped", bool(tr.text))

    # 500 is retried too.
    FakeCamb.reset()
    FakeCamb.fail_with, FakeCamb.fail_times = 500, 1
    tr = p.transcribe(segment(seconds=6.0), "it-it")
    check("a server error is retried", bool(tr.text))

    # 402 must not be retried at all.
    FakeCamb.reset()
    FakeCamb.fail_with, FakeCamb.fail_times = 402, 99
    try:
        p.transcribe(segment(seconds=6.0), "it-it")
        check("no credit raises OutOfCredit", False)
    except OutOfCredit:
        posts = sum(1 for c in FakeCamb.calls if c[0] == "POST")
        check("no credit raises OutOfCredit and is not retried", posts == 1, f"{posts} posts")

    # 401 must say the key is wrong, not "the provider failed".
    FakeCamb.reset()
    FakeCamb.fail_with, FakeCamb.fail_times = 401, 99
    try:
        p.transcribe(segment(seconds=6.0), "it-it")
        check("a bad key is named as such", False)
    except NotConfigured as exc:
        check("a bad key is named as such", "key" in str(exc).lower())

    # No key at all: refuse before building a request.
    nokey = CambProvider(TranslateSettings(
        mode=Mode.OFF, provider="camb", camb_base_url=base))
    try:
        nokey.transcribe(segment(), "it-it")
        check("no key is refused before any call", False)
    except NotConfigured:
        check("no key is refused before any call", True)
    check("and healthcheck says so rather than raising",
          nokey.healthcheck()["ok"] is False)
    FakeCamb.reset()
    check("healthcheck passes with a key", CambProvider(s).healthcheck()["ok"] is True)


def test_pipeline(tmp: Path, base: str) -> None:
    section("the pipeline end to end")
    FakeCamb.reset()
    db = str(tmp / "pipe.db")
    store = EventStore(db).start()
    captions = CaptionStore(store)
    try:
        s = TranslateSettings(
            mode=Mode.SHADOW, provider="camb", camb_api_key="test-key",
            camb_base_url=base, target_language="en-us", poll_interval_s=0.01,
            poll_max_interval_s=0.02, job_timeout_s=3.0, workers=1, queue_size=4,
            segment_min_s=2.0, segment_max_s=4.0, min_speech_ratio=0.3,
        )
        pipe = TranslationPipeline(s, store).start()

        tap = pipe.tap("football-21", "sess-1", language="Italian", claimed=True)
        check("a claimed channel gets a tap", tap is not None)
        rate = 48_000
        block = np.full(rate // 10, 0.1, dtype=np.float32)
        for i in range(60):  # 6 s of audio
            tap.audio(block, time.time(), i * 0.1)
            if i % 10 == 9:
                tap.window(1.0)
        _wait(lambda: pipe.completed >= 1, 10.0)
        check("audio became a caption", pipe.completed >= 1,
              f"completed={pipe.completed} failed={pipe.failed}")

        store.close()
        store = EventStore(db, schema=False).start()
        captions = CaptionStore(store)
        rows = captions.recent(channel="football-21")
        check("the caption is stored", bool(rows), str(len(rows)))
        if rows:
            r = rows[0]
            check("it records the Italian it heard",
                  "palla" in (r["source_text"] or ""), r["source_text"])
            check("and the English it produced",
                  "ball" in (r["target_text"] or ""), r["target_text"])
            check("tagged as a shadow run", r["mode"] == Mode.SHADOW)
            check("with the audio it cost", (r["cost_s"] or 0) > 0)
            check("and which model did it", r["provider"] == "camb")

        # Scope: the mode and the claim both have to agree.
        check("an unclaimed channel gets no tap",
              pipe.tap("football-02", "s", language="English", claimed=False) is None)
        pipe.settings = type(s)(**{**s.to_dict(), "mode": Mode.OFF,
                                   "camb_api_key": "test-key"})
        check("with the mode off, no channel gets a tap",
              pipe.tap("football-21", "s", language="Italian", claimed=True) is None)
        pipe.stop()
    finally:
        store.close()


def test_pipeline_protects_monitoring(tmp: Path) -> None:
    section("the pipeline never blocks the capture path")
    db = str(tmp / "slow.db")
    store = EventStore(db).start()
    try:
        s = TranslateSettings(
            mode=Mode.SHADOW, provider="mock", target_language="en-us",
            workers=1, queue_size=2, segment_min_s=1.0, segment_max_s=2.0,
            min_speech_ratio=0.3,
        )
        # A provider slower than the match, which is the case that matters.
        pipe = TranslationPipeline(
            s, store, provider_factory=lambda name, st: MockProvider(st, latency_s=0.4)
        ).start()
        tap = pipe.tap("football-01", "s", language="English", claimed=True)

        rate = 48_000
        block = np.full(rate // 10, 0.1, dtype=np.float32)
        started = time.monotonic()
        for i in range(400):  # 40 s of audio, submitted as fast as possible
            tap.audio(block, time.time(), i * 0.1)
            if i % 10 == 9:
                tap.window(1.0)
        elapsed = time.monotonic() - started
        # The whole point: handing 40 s of audio over must take milliseconds.
        check("40 s of audio is handed over without waiting", elapsed < 2.0,
              f"{elapsed:.2f}s")
        check("the queue stayed bounded", pipe._q.qsize() <= s.queue_size,
              str(pipe._q.qsize()))
        check("the backlog was dropped rather than grown", pipe.dropped > 0,
              f"dropped={pipe.dropped} submitted={pipe.submitted}")
        _wait(lambda: pipe.completed >= 1, 5.0)
        check("and work still got done", pipe.completed >= 1, str(pipe.completed))
        pipe.stop()

        store.close()
        store = EventStore(db, schema=False).start()
        skipped = CaptionStore(store).recent(statuses=("skipped",))
        check("a dropped segment leaves a row saying so, not a silent gap",
              bool(skipped) and "could not keep up" in (skipped[0]["error"] or ""),
              str(len(skipped)))
    finally:
        store.close()


def test_pipeline_failures(tmp: Path) -> None:
    section("failures become rows, not exceptions")
    db = str(tmp / "fail.db")
    store = EventStore(db).start()
    try:
        # A budget big enough that the cap is not what stops anything here:
        # the previous version of this test capped the day at three seconds,
        # so the second segment was skipped before the mock ever got a chance
        # to fail and the failure path was never actually exercised.
        s = TranslateSettings(
            mode=Mode.SHADOW, provider="mock", workers=1, queue_size=8,
            segment_min_s=1.0, segment_max_s=2.0, min_speech_ratio=0.3,
            daily_minutes=10.0,
        )
        pipe = TranslationPipeline(
            s, store, provider_factory=lambda n, st: MockProvider(st, fail_every=2)
        ).start()
        for i in range(6):
            pipe.submit(segment(seconds=2.0, rate=48_000, seq=i, language="en-us"))
        _wait(lambda: pipe.completed + pipe.failed >= 6, 10.0)
        check("every segment was accounted for",
              pipe.completed + pipe.failed + pipe.skipped == 6,
              f"ok={pipe.completed} failed={pipe.failed} skipped={pipe.skipped}")
        check("a provider failure did not kill the worker",
              pipe.failed >= 1 and pipe.completed >= 1,
              f"ok={pipe.completed} failed={pipe.failed}")
        check("the pipeline is still alive", any(t.is_alive() for t in pipe._workers))
        pipe.stop()

        store.close()
        store = EventStore(db, schema=False).start()
        cs = CaptionStore(store)
        errs = cs.recent(statuses=("error",))
        check("the failure is stored with its reason",
              bool(errs) and "pretending to fail" in (errs[0]["error"] or ""))
        summary = cs.summary(time.time() - 3600)
        check("the summary counts errors and successes separately",
              summary["errors"] >= 1 and summary["ok"] >= 1 and summary["captions"] == 6,
              str(summary))
        check("and reports a median latency, not a mean dragged by one timeout",
              summary["latency_median_s"] is not None, str(summary))
        spent, by_channel = cs.spent_today()
        check("spend is readable back for the next restart", spent > 0 and by_channel)
    finally:
        store.close()


def test_daily_cap_stops_the_pipeline(tmp: Path) -> None:
    """The cap, on its own, with nothing else able to interfere."""
    section("the daily cap stops the pipeline")
    db = str(tmp / "cap.db")
    store = EventStore(db).start()
    try:
        s = TranslateSettings(
            mode=Mode.SHADOW, provider="mock", workers=1, queue_size=8,
            min_speech_ratio=0.3,
            daily_minutes=0.05,  # three seconds, so the second segment is refused
        )
        pipe = TranslationPipeline(s, store).start()
        for i in range(5):
            pipe.submit(segment(seconds=2.0, rate=48_000, seq=i, language="en-us"))
        _wait(lambda: pipe.completed + pipe.skipped >= 5, 10.0)
        check("the first segment is sent", pipe.completed == 1, str(pipe.completed))
        check("the rest are refused by the cap", pipe.skipped == 4, str(pipe.skipped))
        pipe.stop()
        store.close()
        store = EventStore(db, schema=False).start()
        rows = CaptionStore(store).recent(statuses=("skipped",))
        check("each refusal says the budget is the reason",
              bool(rows) and "budget" in (rows[0]["error"] or ""),
              (rows[0]["error"] if rows else "no rows"))
    finally:
        store.close()


def test_out_of_credit_stops(tmp: Path) -> None:
    section("running out of credit stops, rather than retrying forever")
    db = str(tmp / "credit.db")
    store = EventStore(db).start()
    try:
        class Broke:
            name = "broke"
            model = "broke"

            def transcribe(self, seg, language):
                raise OutOfCredit("the account is out of credit")

            def translate(self, texts, source, target):
                raise OutOfCredit("the account is out of credit")

            def languages(self):
                return {}

            def healthcheck(self):
                return {"ok": False}

            def close(self):
                pass

        s = TranslateSettings(mode=Mode.SHADOW, provider="mock", workers=1,
                              min_speech_ratio=0.3)
        pipe = TranslationPipeline(s, store, provider_factory=lambda n, st: Broke()).start()
        for i in range(5):
            pipe.submit(segment(seconds=2.0, rate=48_000, seq=i))
        _wait(lambda: pipe.failed + pipe.skipped >= 5, 10.0)
        check("it stops asking after the first refusal", pipe.failed == 1,
              f"failed={pipe.failed} skipped={pipe.skipped}")
        check("the rest are skipped, not retried", pipe.skipped >= 4)
        snap = pipe.budget.snapshot()
        check("the page can say why nothing is happening",
              snap["blocked"] and "out of credit" in snap["blocked_reason"])
        pipe.stop()
    finally:
        store.close()


def test_switching_on_mid_match(tmp: Path) -> None:
    """Turning translation on must reach a channel that is already on air.

    This is a regression test for a real bug: the mode was only consulted when
    a monitor started or its claim changed, so flipping the switch during a
    match silently did nothing until the next broadcast. Worse, nothing polled
    the control row unless a tap was requested, and no tap was requested until
    the mode changed -- a circle that never closed.
    """
    section("switching on in the middle of a match")
    db = str(tmp / "switch.db")
    control = str(tmp / "switch-claims.db")
    store = EventStore(db).start()
    try:
        s = TranslateSettings(
            mode=Mode.OFF, provider="mock", min_speech_ratio=0.3,
            control_poll_s=0.05, workers=1,
        )
        pipe = TranslationPipeline(s, store, control_path=control).start()
        check("off to begin with",
              pipe.tap("football-21", "s", language="Italian", claimed=True) is None)
        gen_before = pipe.generation

        # The page writes the switch. Nothing else happens -- no monitor
        # restarts, no claim changes.
        control_mod.write(control, mode=Mode.SHADOW, provider="mock", by="test")
        check("the running pipeline notices on its own",
              _wait(lambda: pipe.settings.mode == Mode.SHADOW, 3.0),
              pipe.settings.mode)
        check("and says its configuration moved, so monitors re-ask",
              pipe.generation > gen_before,
              f"{gen_before} -> {pipe.generation}")
        tap = pipe.tap("football-21", "s", language="Italian", claimed=True)
        check("a channel already on air now gets a tap", tap is not None)

        # And back off again, which must also reach a running monitor.
        gen_before = pipe.generation
        control_mod.write(control, mode=Mode.OFF, by="test")
        check("switching off is noticed too",
              _wait(lambda: pipe.settings.mode == Mode.OFF, 3.0))
        check("and bumps the counter as well", pipe.generation > gen_before)
        check("so the tap is withdrawn",
              pipe.tap("football-21", "s", language="Italian", claimed=True) is None)
        pipe.stop()
    finally:
        store.close()


def _wait(cond, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()



# ---------------------------------------------------------------------------
# Speech synthesis: the mapper, then the adapter against the fake.
# ---------------------------------------------------------------------------
def test_tts_mappers() -> None:
    section("Speech mappers, against the documented shapes")

    body = tm.tts_body("palla in mezzo", "EN-US", voice_id=147320)
    check("the locale tag is lowercased, as the docs require",
          body["language"] == "en-us")
    check("voice_id is an integer", isinstance(body["voice_id"], int))
    check("the format goes inside output_configuration",
          body["output_configuration"] == {"format": "wav"})
    check("whitespace is collapsed, so a newline cannot inflate the text",
          tm.tts_body("a  b\n c", "en-us", voice_id=1)["text"] == "a b c")

    try:
        tm.tts_body("hi", "en-us", voice_id=1)
        check("text under the documented minimum is refused here, not by a 422", False)
    except ProviderFailed as exc:
        check("text under the documented minimum is refused here, not by a 422",
              "at least 3" in str(exc))
    long_text = tm.tts_body("goal " * 2000, "en-us", voice_id=1)
    check("an over-long transcript is trimmed to the documented 3000",
          len(long_text["text"]) <= 3000)
    check("and trimmed at a word boundary, so the audio ends on a word",
          not long_text["text"].endswith("goa"))
    try:
        tm.tts_body("hello there", "en-us", voice_id=1, audio_format="ogg")
        check("a format Camb does not offer is refused", False)
    except ProviderFailed:
        check("a format Camb does not offer is refused", True)

    check("wav maps to the type a browser plays",
          tm.media_type("wav") == "audio/wav")
    check("mp3 is audio/mpeg, not audio/mp3", tm.media_type("mp3") == "audio/mpeg")
    check("raw PCM is not offered as playable", "pcm_s16le" not in tm.PLAYABLE)

    check("the credit header is read whatever its case",
          tm.credits_required({"x-credits-required": "17"}) == "17")
    check("a response without it gives an empty string, not a fake zero",
          tm.credits_required({}) == "")

    wrapped = tm.voices({"voices": [{"id": 1, "voice_name": "Gary", "gender": 1}]})
    bare = tm.voices([{"id": 1, "voice_name": "Gary", "gender": 1}])
    check("both documented voice-list shapes are accepted", wrapped == bare)
    check("the integer gender is turned into a word", wrapped[0]["gender"] == "male")


def test_camb_speech(base: str) -> None:
    section("Camb speech, against the local fake")
    FakeCamb.reset()
    s = TranslateSettings(
        mode=Mode.SHADOW, provider="camb", camb_api_key="test-key",
        camb_base_url=base, poll_interval_s=0.01, job_timeout_s=2.0,
    )
    provider = CambProvider(s)

    spoken = provider.speak("e il portiere respinge", "it-it")
    check("the audio comes back as bytes, not a URL", isinstance(spoken.audio, bytes))
    check("and it is a real WAV a browser can play", spoken.audio[:4] == b"RIFF")
    check("the media type is carried with it", spoken.media_type == "audio/wav")
    check("the credit figure the provider reported is kept verbatim",
          spoken.credits == "42")
    check("one call, not a create/poll/fetch dance",
          [c for c in FakeCamb.calls if c[1] == "/tts-stream"]
          == [("POST", "/tts-stream")])
    check("the key was sent", "test-key" in FakeCamb.keys)
    check("latency is measured", spoken.latency_s > 0)

    voices = provider.voices()
    check("the voice list is read through the mapper", len(voices) == 2)
    check("and names a concrete voice", voices[0]["name"] == "Gary")

    caps = provider.capabilities()
    check("the provider describes its own three steps", len(caps["steps"]) == 3)
    check("and says plainly that one-call dubbing is out of reach",
          any("dub" in str(x.get("endpoint", "")).lower()
              for x in caps["not_available"]))

    # No credit must stop, not retry: a refusal repeated three times is three
    # refusals and still no audio.
    FakeCamb.reset()
    FakeCamb.fail_with, FakeCamb.fail_times = 402, 99
    try:
        provider.speak("palla in mezzo", "it-it")
        check("no credit raises OutOfCredit rather than returning silence", False)
    except OutOfCredit:
        check("no credit raises OutOfCredit rather than returning silence", True)
    check("and it was not retried",
          len([c for c in FakeCamb.calls if c[1] == "/tts-stream"]) == 1)
    FakeCamb.reset()


# ---------------------------------------------------------------------------
# Execution modes: the wiring, and what a mode refuses.
# ---------------------------------------------------------------------------
def test_execution_modes() -> None:
    section("Execution modes")
    s = TranslateSettings(provider="mock")
    modes = ModeRegistry(s)

    check("both wirings are declared",
          [m["name"] for m in modes.describe()] == ["shadow", "standard"])
    check("only shadow is built",
          [m["built"] for m in modes.describe()] == [True, False])
    check("the default is the mode that neither spends much nor publishes",
          modes.default == "shadow")

    wiring = modes.wire("shadow")
    check("shadow builds a provider", wiring.provider.name == "mock")
    check("shadow publishes nothing", not wiring.publishes)
    check("shadow keeps the audio", wiring.retains_audio)
    check("what the mode changed is visible, not implied",
          wiring.overrides == {"claimed_only": False})
    check("and it is actually applied", wiring.settings.claimed_only is False)
    check("the configured settings are not mutated by wiring a mode",
          s.claimed_only is True)

    try:
        modes.wire("standard")
        check("standard refuses rather than falling back to shadow", False)
    except ModeNotBuilt as exc:
        check("standard refuses rather than falling back to shadow",
              "not built" in str(exc))
    check("and it says what building it needs",
          "budget" in modes.get("standard").info.pending)

    try:
        modes.get("staging")
        check("an unknown mode names the ones that exist", False)
    except ModeNotBuilt as exc:
        check("an unknown mode names the ones that exist", "shadow" in str(exc))

    # The budget has to outlive a request, or a daily cap is no cap at all.
    first = modes.wire("shadow")
    second = modes.wire("shadow")
    check("every wiring shares one budget", first.budget is second.budget)

    try:
        modes.wire("shadow", "camb")
        check("a model with no key cannot be switched on by a request", False)
    except NotConfigured:
        check("a model with no key cannot be switched on by a request", True)


def test_shadow_run() -> None:
    section("A shadow run, end to end")
    s = TranslateSettings(provider="mock")
    modes = ModeRegistry(s)
    mode = modes.get("shadow")
    rate = 48_000
    t = np.arange(rate * 6, dtype=np.float32) / rate
    speech = (0.2 * np.sin(2 * np.pi * 220 * t)
              * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)

    run = mode.run(modes.wire("shadow"), speech, rate, channel="football-31",
                   source_language="pt-pt", target_language="en-us")
    check("the run succeeded", run.status == "ok", run.error)
    check("there is text for what was heard", bool(run.source_text))
    check("and text for what was said", bool(run.target_text))
    check("the heard audio is kept so it can be played back",
          run.heard_audio[:4] == b"RIFF")
    check("and so is the spoken audio", run.spoken_audio[:4] == b"RIFF")
    check("the audio bytes stay out of the JSON the page polls",
          "heard_audio" not in run.to_dict() and run.to_dict()["has_heard"])
    check("each step is timed separately",
          run.transcribe_s >= 0 and run.speak_s > 0 and run.total_s > 0)
    check("it is labelled with the mode that produced it", run.mode == "shadow")

    silent = mode.run(modes.wire("shadow"), np.zeros(rate * 5, dtype=np.float32),
                      rate, channel="football-01")
    check("silence is refused before anything is sent", silent.status == "skipped")
    check("and the reason says so", "silent" in silent.error)
    check("but the audio is still kept, so the refusal can be checked by ear",
          bool(silent.heard_audio))

    # Same language in and out: there is nothing to pay a translator for.
    same = mode.run(modes.wire("shadow"), speech, rate, channel="football-01",
                    source_language="en-us", target_language="en-us")
    check("translating a language into itself is skipped", same.translate_s == 0.0)
    check("and the text still comes through", bool(same.target_text))

    # 9 s of allowance against a 6 s clip: the first run fits, the second
    # cannot. Set from the clip length rather than guessed, which is the
    # mistake that made an earlier version of this test assert nothing.
    capped = TranslateSettings(provider="mock", shadow_daily_minutes=9.0 / 60.0)
    small = ModeRegistry(capped)
    first = small.get("shadow").run(small.wire("shadow"), speech, rate,
                                    channel="football-01")
    second = small.get("shadow").run(small.wire("shadow"), speech, rate,
                                     channel="football-01")
    check("the first run fits inside the daily cap", first.status == "ok",
          first.error)
    check("the second is refused by it", second.status == "skipped")
    check("and the refusal explains which cap was hit",
          "daily" in second.error.lower() or "cap" in second.error.lower(),
          second.error)

    failing = TranslateSettings(provider="mock")
    reg = ModeRegistry(failing)
    wiring = reg.wire("shadow")
    wiring.provider = MockProvider(failing, fail_every=1)
    broken = reg.get("shadow").run(wiring, speech, rate, channel="football-02")
    check("a provider failure becomes a run, not an exception",
          broken.status == "error", broken.status)
    check("with the reason attached", bool(broken.error))
    check("and with the audio that was sent, so it can be listened to",
          bool(broken.heard_audio))


def test_run_store(tmp: Path) -> None:
    section("The run store")
    store = RunStore(str(tmp / "runs.db"))
    s = TranslateSettings(provider="mock")
    modes = ModeRegistry(s)
    rate = 16_000
    t = np.arange(rate * 4, dtype=np.float32) / rate
    speech = (0.2 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)

    run = modes.get("shadow").run(modes.wire("shadow"), speech, rate,
                                  channel="football-31", target_language="en-us")
    store.put(run)
    rows = store.recent(limit=5, mode="shadow")
    check("the run is stored", len(rows) == 1)
    check("and its audio is flagged as present",
          rows[0]["has_heard"] and rows[0]["has_spoken"])
    check("the blobs are not in the listing", "heard_audio" not in rows[0])

    heard = store.audio(run.id, "heard")
    check("the heard audio comes back byte for byte",
          heard is not None and heard[0] == run.heard_audio)
    check("with the media type it was stored with", heard[1] == "audio/wav")
    check("a run that does not exist gives nothing, not an error",
          store.audio("nosuchrun", "heard") is None)

    spend = store.spent_today(mode="shadow")
    check("spending is counted from the database, so it survives a restart",
          spend["seconds"] == round(run.seconds, 1), str(spend))
    check("and is attributed per channel", "football-31" in spend["by_channel"])
    check("the last credit figure the provider gave is readable",
          store.last_credits(mode="shadow") == "0")

    # Old audio is dropped while the record of what was tried stays.
    store.KEEP_AUDIO = 2
    for i in range(4):
        later = modes.get("shadow").run(modes.wire("shadow"), speech, rate,
                                        channel=f"football-{i:02d}")
        store.put(later)
    rows = store.recent(limit=20, mode="shadow")
    with_audio = [r for r in rows if r["has_heard"]]
    check("only the most recent runs keep their audio", len(with_audio) == 2,
          f"{len(with_audio)} of {len(rows)}")
    check("but every run is still on the record", len(rows) == 5)
    store.close()


def main() -> int:
    import tempfile

    print("=" * 70)
    print("Commentary QC - translation layer")
    print("=" * 70)

    server = HTTPServer(("127.0.0.1", 0), FakeCamb)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"fake Camb AI on {base}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        try:
            test_mappers()
            test_languages()
            test_audio()
            test_segmenter()
            test_budget()
            test_settings_and_modes()
            test_control(tmp)
            test_registry()
            test_tts_mappers()
            test_execution_modes()
            test_shadow_run()
            test_run_store(tmp)
            test_camb_adapter(base)
            test_camb_speech(base)
            test_pipeline(tmp, base)
            test_pipeline_protects_monitoring(tmp)
            test_pipeline_failures(tmp)
            test_daily_cap_stops_the_pipeline(tmp)
            test_switching_on_mid_match(tmp)
            test_out_of_credit_stops(tmp)
        finally:
            server.shutdown()

    print("\n" + "=" * 70)
    print(f"{PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
