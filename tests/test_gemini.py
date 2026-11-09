"""Tests for the Gemini live-translation adapter.

Two levels, as with the other adapter:

1. **The protocol** -- every message shape, against the JSON Google's docs
   print, with no socket. This is where the documented contradiction about
   where ``translationConfig`` belongs is pinned down.
2. **The adapter** -- the real :class:`GeminiLiveSession` against a **fake
   Gemini Live server on localhost**: setup, the acknowledgement, audio in,
   translated audio out, transcripts, the ``goAway`` warning, and each failure
   that has its own meaning. No key, no tokens, no bill.

    python tests/test_gemini.py
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.translate.adapters.gemini import protocol as gp  # noqa: E402
from commentary_qc.translate.adapters.gemini.provider import (  # noqa: E402
    GeminiProvider,
)
from commentary_qc.translate.app.live import LiveRunner  # noqa: E402
from commentary_qc.translate.core.models import (  # noqa: E402
    AUDIO,
    FAILED,
    HEARD,
    NOTE,
    READY,
    SAID,
    AudioBlock,
    AudioSpec,
    NotConfigured,
    OutOfCredit,
    RateLimited,
    TranslateError,
)
from commentary_qc.translate.app.modes import ModeRegistry  # noqa: E402
from commentary_qc.translate.settings import TranslateSettings  # noqa: E402

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


def tone(seconds: float, spec: AudioSpec = gp.OUT_SPEC, hz: float = 440.0) -> bytes:
    n = int(spec.rate * seconds)
    t = np.arange(n, dtype=np.float32) / spec.rate
    pcm = np.clip(0.3 * np.sin(2 * np.pi * hz * t), -1, 1)
    return np.round(pcm * 32767.0).astype("<i2").tobytes()


# ======================================================================
# 1. the protocol
# ======================================================================
def test_protocol() -> None:
    section("the Gemini Live wire format")

    hello = gp.setup("el-gr")
    check("the first message is a setup", list(hello) == ["setup"])
    check("the model carries the models/ prefix the docs show",
          hello["setup"]["model"] == f"models/{gp.MODEL}",
          hello["setup"]["model"])
    cfg = hello["setup"]["generationConfig"]
    check("translationConfig sits INSIDE generationConfig -- the docs put it "
          "at the top level in their SDK example and inside here in the raw "
          "JSON and the reference, and raw JSON is what we send",
          "translationConfig" in cfg)
    check("audio is the response modality", cfg["responseModalities"] == ["AUDIO"])
    check("both transcripts are asked for, because text is what makes a bad "
          "translation diagnosable",
          "inputAudioTranscription" in cfg and "outputAudioTranscription" in cfg)
    check("echoing the target language is on by default, so an English "
          "channel translated to English does not go silently quiet",
          cfg["translationConfig"]["echoTargetLanguage"] is True)
    check("there is NO source language field: the model detects it",
          not any("source" in key.lower() for key in cfg))
    check("asking for no text drops both transcripts",
          "inputAudioTranscription" not in
          gp.setup("de", want_text=False)["setup"]["generationConfig"])

    # -- language codes
    check("Greek becomes the bare code the guide prints",
          gp.target_code("el-gr") == "el")
    check("German likewise", gp.target_code("de-de") == "de")
    check("French likewise", gp.target_code("fr-fr") == "fr")
    check("Italian likewise", gp.target_code("it-it") == "it")
    check("European Portuguese keeps its region, because the guide "
          "distinguishes pt-PT from pt-BR and dropping it would quietly pick "
          "the wrong variety of a language MMC broadcasts in",
          gp.target_code("pt-pt") == "pt-PT")
    check("and Brazilian stays Brazilian", gp.target_code("pt-br") == "pt-BR")
    check("an exact code passes through", gp.target_code("en") == "en")
    try:
        gp.target_code("xx-xx")
        check("a language it does not list is refused rather than guessed, "
              "because a wrong target comes back as confident audio in the "
              "wrong language", False)
    except NotConfigured as exc:
        check("a language it does not list is refused rather than guessed, "
              "because a wrong target comes back as confident audio in the "
              "wrong language", "xx-xx" in str(exc))
    try:
        gp.setup("")
        check("an empty target is refused", False)
    except NotConfigured:
        check("an empty target is refused", True)
    check("all six of MMC's languages resolve",
          all(gp.target_code(t) for t in
              ("en-us", "it-it", "pt-pt", "de-de", "el-gr", "fr-fr")))

    # -- the key in the URL
    url = gp.url_for("secret-key")
    check("the key goes in the query string, the only form documented for "
          "this socket", "key=secret-key" in url)
    check("and a URL can be logged without leaking it",
          "secret-key" not in gp.redact(url) and "key=***" in gp.redact(url))
    check("redacting keeps the rest of the URL intact",
          gp.redact("wss://h/p?key=k&alt=x").endswith("&alt=x"))
    try:
        gp.url_for("")
        check("no key means no URL", False)
    except NotConfigured as exc:
        check("no key means no URL", "GEMINI_API_KEY" in str(exc))

    # -- audio in
    pcm = b"\x01\x02" * 100
    msg = gp.append_audio(pcm)
    check("audio goes up as base64 inside a realtimeInput",
          base64.b64decode(msg["realtimeInput"]["audio"]["data"]) == pcm)
    check("with the exact MIME string the docs print, rate included",
          msg["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000")
    check("input is 16 kHz and output 24 -- two specs, because one rate for "
          "both directions would be wrong here",
          gp.IN_SPEC.rate == 16_000 and gp.OUT_SPEC.rate == 24_000)
    check("the end-of-audio marker exists, so the last sentence finishes",
          gp.audio_stream_end()["realtimeInput"]["audioStreamEnd"] is True)
    try:
        gp.append_audio(b"\x00" * (gp.MAX_CHUNK_BYTES + 2))
        check("an oversized chunk is refused here", False)
    except TranslateError:
        check("an oversized chunk is refused here", True)

    # -- events out
    check("setupComplete is readiness",
          gp.parse_event('{"setupComplete":{}}').kind == READY)

    audio = gp.parse_event(json.dumps({"serverContent": {"modelTurn": {"parts": [
        {"inlineData": {"data": base64.b64encode(b"abcd").decode()}}]}}}))
    check("translated audio arrives nested in modelTurn parts",
          audio.kind == AUDIO and audio.audio == b"abcd")
    two = gp.parse_event(json.dumps({"serverContent": {"modelTurn": {"parts": [
        {"inlineData": {"data": base64.b64encode(b"ab").decode()}},
        {"inlineData": {"data": base64.b64encode(b"cd").decode()}}]}}}))
    check("several parts in one turn are joined, not dropped",
          two.audio == b"abcd")
    check("a part with no audio does not break the turn",
          gp.parse_event(json.dumps({"serverContent": {"modelTurn": {
              "parts": [{"text": "hello"}]}}})) is None)

    said = gp.parse_event(json.dumps({"serverContent": {
        "outputTranscription": {"text": "o palo pigainei exo"},
        "turnComplete": True}}))
    check("the translated text comes through",
          said.kind == SAID and said.final and "palo" in said.text)
    heard = gp.parse_event(json.dumps({"serverContent": {
        "inputTranscription": {"text": "the ball goes out", "languageCode": "en-US"}}}))
    check("so does what it heard, with the detected language attached",
          heard.kind == HEARD and heard.raw.get("languageCode") == "en-US")
    check("turnComplete on its own closes the audio",
          gp.parse_event('{"serverContent":{"turnComplete":true}}').final)
    check("an interruption is a note, not a failure",
          gp.parse_event('{"serverContent":{"interrupted":true}}').kind == NOTE)

    away = gp.parse_event(json.dumps({"goAway": {"timeLeft": "30s"}}))
    check("goAway becomes a warning the run can report, so a session that "
          "ends early says why", away.kind == NOTE and "30s" in away.text)

    check("an event we do not act on is ignored rather than fatal",
          gp.parse_event('{"sessionResumptionUpdate":{"newHandle":"h"}}') is None)
    check("so is an event type nobody has seen yet",
          gp.parse_event('{"somethingNew":{}}') is None)
    check("non-JSON is reported rather than crashing the reader",
          gp.parse_event("not json").kind == NOTE)
    check("a corrupt base64 payload yields no audio rather than raising",
          gp.parse_event(json.dumps({"serverContent": {"modelTurn": {"parts": [
              {"inlineData": {"data": "!!!"}}]}}})) is None)

    err = gp.parse_event(json.dumps({"error": {
        "status": "UNAUTHENTICATED", "message": "API key not valid"}}))
    check("an error is fatal and names its status",
          err.kind == FAILED and err.fatal and err.code == "UNAUTHENTICATED")
    check("and carries the exception it means, so the app never reads a "
          "vendor status code", isinstance(err.error, NotConfigured))

    check("a bad key is configuration, not a model failure",
          isinstance(gp.error_for("UNAUTHENTICATED", "x"), NotConfigured))
    check("and names the setting to fix",
          "GEMINI_API_KEY" in str(gp.error_for("PERMISSION_DENIED", "nope")))
    check("exhausted quota latches the budget",
          isinstance(gp.error_for("RESOURCE_EXHAUSTED", "quota exceeded"),
                     OutOfCredit))
    check("but a per-minute limit is a retry, not a dead end -- Gemini "
          "reports both through the same status, so the message decides",
          isinstance(gp.error_for("RESOURCE_EXHAUSTED",
                                  "rate limit, requests per minute"),
                     RateLimited))
    check("a bad argument is configuration",
          isinstance(gp.error_for("INVALID_ARGUMENT", "x"), NotConfigured))
    check("anything else is a plain failure",
          type(gp.error_for("INTERNAL", "x")) is TranslateError)

    ok, note = gp.supported("el-gr")
    check("Greek is supported, with a note that the code is written el",
          ok and "el" in note)
    check("and an unlisted language is reported as unsupported",
          gp.supported("xx-xx")[0] is False)


# ======================================================================
# 2. a fake Gemini Live server, and the real adapter against it
# ======================================================================
class FakeGemini:
    """As much of the Live API as the adapter touches.

    Scripted per connection by the ``script`` query parameter, so one server
    covers every case. Everything it asserts comes from the API reference.
    """

    def __init__(self) -> None:
        self.seen: list[dict] = []
        self._server = None

    def start(self) -> str:
        from websockets.sync.server import serve

        self._server = serve(self._handle, "127.0.0.1", 0)
        port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"ws://127.0.0.1:{port}/v1beta"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def _handle(self, ws) -> None:
        try:
            self._converse(ws)
        except Exception:  # noqa: BLE001 - a client hanging up is normal
            pass

    def _converse(self, ws) -> None:
        path = getattr(ws.request, "path", "") or ""
        script = path.split("script=", 1)[1].split("&")[0] if "script=" in path else "ok"
        key = path.split("key=", 1)[1].split("&")[0] if "key=" in path else ""
        record: dict = {"script": script, "key": key, "audio": 0, "ended": False}
        self.seen.append(record)

        if script == "badkey" or not key:
            ws.send(json.dumps({"error": {
                "status": "UNAUTHENTICATED",
                "message": "API key not valid. Please pass a valid API key."}}))
            ws.close(1008, "UNAUTHENTICATED")
            return

        first = json.loads(ws.recv(timeout=10))
        record["hello"] = first
        if "setup" not in first:
            ws.send(json.dumps({"error": {
                "status": "INVALID_ARGUMENT",
                "message": "the first message must be setup"}}))
            ws.close(1008, "INVALID_ARGUMENT")
            return

        if script == "quota":
            ws.send(json.dumps({"error": {
                "status": "RESOURCE_EXHAUSTED",
                "message": "quota exceeded for this project"}}))
            ws.close(1008, "RESOURCE_EXHAUSTED")
            return
        if script == "mute":
            # Accepts the upgrade, takes the setup, then vanishes with no
            # reason -- which is what a refused model looks like in practice.
            ws.close(1006, "")
            return
        if script == "nostart":
            while True:
                try:
                    ws.recv(timeout=30)
                except Exception:  # noqa: BLE001
                    return

        ws.send(json.dumps({"setupComplete": {}}))

        heard = 0
        while True:
            try:
                raw = ws.recv(timeout=20)
            except Exception:  # noqa: BLE001 - the client finished
                return
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            realtime = event.get("realtimeInput") or {}
            if realtime.get("audioStreamEnd"):
                record["ended"] = True
                continue
            blob = realtime.get("audio") or {}
            if not blob:
                continue
            if blob.get("mimeType") != gp.IN_MIME:
                ws.send(json.dumps({"error": {
                    "status": "INVALID_ARGUMENT",
                    "message": f"bad mime {blob.get('mimeType')!r}"}}))
                ws.close(1008, "INVALID_ARGUMENT")
                return
            pcm = base64.b64decode(blob.get("data") or "")
            record["audio"] += len(pcm)
            heard += len(pcm)

            if script == "goaway" and record["audio"] > 20_000:
                ws.send(json.dumps({"goAway": {"timeLeft": "10s"}}))
                record["warned"] = True
            if script == "midfail" and record["audio"] > 20_000:
                ws.send(json.dumps({"error": {
                    "status": "INTERNAL",
                    "message": "the model fell over mid-match"}}))
                ws.close(1011, "INTERNAL")
                return

            while heard >= gp.IN_SPEC.bytes_for(1.0):
                heard -= gp.IN_SPEC.bytes_for(1.0)
                ws.send(json.dumps({"serverContent": {
                    "inputTranscription": {
                        "text": "the ball goes out of play",
                        "languageCode": "en-US"}}}))
                ws.send(json.dumps({"serverContent": {"modelTurn": {"parts": [
                    {"inlineData": {
                        "data": base64.b64encode(tone(0.25)).decode(),
                        "mimeType": "audio/pcm;rate=24000"}}]}}}))
                ws.send(json.dumps({"serverContent": {
                    "outputTranscription": {"text": "to bali vgainei exo"},
                    "turnComplete": True}}))


def provider_for(url: str, key: str = "test-key") -> GeminiProvider:
    settings = TranslateSettings(
        provider="gemini", gemini_api_key=key, gemini_live_url=url,
    )
    return GeminiProvider(settings)


def test_adapter(url: str, fake: FakeGemini) -> None:
    section("the Gemini adapter, against a fake Live server")

    session = provider_for(url + "?script=ok").open_live("auto", "el-gr")
    try:
        session.wait_ready(10.0)
        check("the session opens and acknowledges", not session.closed)
        sent = fake.seen[-1]
        check("the key went in the query string", sent["key"] == "test-key")
        check("the first message was the setup", "setup" in sent["hello"])
        check("and it asked for Greek as the bare code",
              sent["hello"]["setup"]["generationConfig"]["translationConfig"]
              ["targetLanguageCode"] == "el")
        check("the session reports its two specs, which differ",
              session.spec.rate == 16_000 and session.output_spec.rate == 24_000)

        for _ in range(12):
            session.send(tone(0.1, gp.IN_SPEC))
        deadline = time.monotonic() + 10
        audio, heard, said = b"", [], []
        while time.monotonic() < deadline:
            for event in session.drain():
                if event.kind == AUDIO and event.audio:
                    audio += event.audio
                elif event.kind == HEARD and event.text:
                    heard.append(event.text)
                elif event.kind == SAID and event.text:
                    said.append(event.text)
            if audio and heard and said:
                break
            time.sleep(0.05)
        check("translated audio came back", len(audio) > 1000, f"{len(audio)}B")
        check("with what it heard", bool(heard) and "ball" in heard[0])
        check("and what it said", bool(said) and "bali" in said[0])
        check("the audio reached the server with the exact MIME it demands -- "
              "a wrong one is refused, so this also proves the format",
              fake.seen[-1]["audio"] > 20_000)
        stats = session.stats()
        check("it reports what it sent, in seconds", stats["sent_seconds"] > 1.0)
        check("and the target it resolved", stats["target_language"] == "el")
    finally:
        session.close()
    check("closing told the model the audio ended, so the last sentence "
          "finishes rather than being left waiting",
          fake.seen[-1]["ended"], str(fake.seen[-1]))
    check("and closing is idempotent", session.closed)
    session.close()

    # -- goAway, which a long match will hit
    session = provider_for(url + "?script=goaway").open_live("auto", "de-de")
    try:
        session.wait_ready(10.0)
        notes = []
        for _ in range(40):
            session.send(tone(0.1, gp.IN_SPEC))
            notes += [e for e in session.drain() if e.kind == NOTE]
            if notes:
                break
            time.sleep(0.02)
        check("a goAway warning is surfaced, not swallowed -- a connection "
              "lasts about ten minutes and a match does not",
              any("close this connection" in n.text for n in notes),
              str([n.text for n in notes][:2]))
        check("and it does not kill a session that is still working",
              not session.closed)
    finally:
        session.close()

    # -- the failures
    for script, want, label in (
        ("badkey", NotConfigured, "a rejected key is configuration, not a model fault"),
        ("quota", OutOfCredit, "exhausted quota is reported as out of credit"),
        ("mute", NotConfigured, "a session dropped with no reason names the key "
                                "and the model, rather than a bare close code"),
    ):
        try:
            session = provider_for(url + f"?script={script}").open_live("auto", "el-gr")
            session.wait_ready(10.0)
            check(label, False, "no exception")
            session.close()
        except want:
            check(label, True)
        except Exception as exc:  # noqa: BLE001
            check(label, False, repr(exc))

    session = provider_for(url + "?script=nostart").open_live("auto", "el-gr")
    try:
        start = time.perf_counter()
        try:
            session.wait_ready(1.0)
            check("a session that never acknowledges times out", False)
        except TranslateError:
            check("a session that never acknowledges times out",
                  time.perf_counter() - start < 4.0)
    finally:
        session.close()

    session = provider_for(url + "?script=midfail").open_live("auto", "el-gr")
    try:
        session.wait_ready(10.0)
        raised = ""
        for _ in range(40):
            try:
                session.send(tone(0.1, gp.IN_SPEC))
            except TranslateError as exc:
                raised = str(exc)
                break
            list(session.drain())
            time.sleep(0.02)
        check("a mid-stream failure raises on the next send rather than "
              "silently swallowing the audio", bool(raised), raised)
    finally:
        session.close()

    try:
        provider_for(url, key="").open_live("auto", "el-gr")
        check("no key is refused before a socket is opened", False)
    except NotConfigured as exc:
        check("no key is refused before a socket is opened",
              "GEMINI_API_KEY" in str(exc))

    try:
        provider_for(url).open_live("auto", "xx-xx")
        check("an unsupported language is refused before a connection is "
              "spent", False)
    except NotConfigured as exc:
        check("an unsupported language is refused before a connection is "
              "spent", "xx-xx" in str(exc))


def test_provider_is_honest() -> None:
    section("the provider does not pretend to do what the model cannot")

    p = provider_for("ws://127.0.0.1:1/x")
    caps = p.live_capabilities()
    check("it describes itself as a WebSocket", caps["transport"] == "WebSocket")
    check("and names both rates, because they differ",
          "16000" in caps["wire"]["format"] and "24000" in caps["wire"]["format"])
    check("it offers one mode, because the model has no pipelines to choose "
          "between", len(p.live_modes()) == 1)
    check("Greek and German are both in its list",
          "el" in caps["languages"] and "de" in caps["languages"])
    check("it warns that the free tier trains on what you send",
          any("improve their products" in c for c in caps["caveats"]))
    check("and that a connection is shorter than a match",
          any("10 minutes" in c for c in caps["caveats"]))

    clip = p.capabilities()
    check("the clip path is declared UNAVAILABLE rather than left looking "
          "like an oversight", clip["not_available"][0]["name"] == "Recorded clips")
    for name, call in (("transcribe", lambda: p.transcribe(None, "en")),
                       ("translate", lambda: p.translate([], "en", "el")),
                       ("speak", lambda: p.speak("hi", "el"))):
        try:
            call()
            check(f"{name} refuses rather than half-working", False)
        except NotConfigured as exc:
            check(f"{name} refuses rather than half-working, and says why",
                  "live" in str(exc).lower())

    health = p.healthcheck()
    check("the health check does not open a session, because that would spend "
          "tokens every time the page loaded",
          "costs tokens" in health.get("note", ""))
    check("and it still reports whether the key is set", health["ok"] is True)
    check("no key means not ok",
          provider_for("ws://x", key="").healthcheck()["ok"] is False)


# ======================================================================
# 3. a live run on the real adapter, with a fake stream and a fake server
# ======================================================================
class FakeAudio:
    """An AudioSource of arrays, so no ffmpeg is needed."""

    def __init__(self, seconds: float = 4.0) -> None:
        self.seconds = seconds
        self.opened: list[tuple[str, int]] = []

    def resolve(self, channel: str) -> str:
        return f"fake://{channel}"

    def open(self, channel: str, spec: AudioSpec, *, url: str = ""):
        self.opened.append((channel, spec.rate))
        return _FakeFeed(self.seconds, spec, url or self.resolve(channel))


class _FakeFeed:
    def __init__(self, seconds: float, spec: AudioSpec, url: str) -> None:
        self.url = url
        self._seconds = seconds
        self._spec = spec
        self._stopped = False

    def blocks(self):
        rng = np.random.default_rng(9)
        per = max(1, self._spec.rate // 10)
        total = int(self._spec.rate * self._seconds)
        sent = 0
        while sent < total and not self._stopped:
            take = min(per, total - sent)
            yield AudioBlock(
                samples=np.clip(rng.normal(0, 0.2, take).astype(np.float32), -1, 1),
                wall=time.time(),
            )
            sent += take

    def stop(self) -> None:
        self._stopped = True


def test_live_run(url: str, fake: FakeGemini) -> None:
    section("a live run end to end on Gemini, no ffmpeg and no key")

    settings = TranslateSettings(
        provider="gemini", gemini_api_key="test-key", gemini_live_url=url + "?script=ok",
        live_seconds=4.0, shadow_daily_minutes=10.0,
        shadow_per_channel_minutes=10.0, target_language="el-gr",
    )
    modes = ModeRegistry(settings)
    audio = FakeAudio(seconds=4.0)
    run = modes.get("shadow").live(
        modes.wire("shadow", "gemini"), "football-01",
        audio=audio, target_language="el-gr", seconds=4.0,
    )
    LiveRunner(None).start(run)
    check("the run finishes", run.wait(60))
    snap = run.snapshot()
    check("it ended cleanly", snap["phase"] == "done", snap["error"])
    check("the stream was pulled at 16 kHz, the rate the SESSION asked for -- "
          "not 24, which is what the other adapter wants",
          audio.opened and audio.opened[0][1] == 16_000, str(audio.opened))
    check("translated audio came back", snap["spoken_seconds"] > 0.0)
    check("captions pair what was heard with what was said",
          bool(snap["captions"]) and snap["captions"][0]["heard"]
          and snap["captions"][0]["said"])
    check("the spoken audio is written at 24 kHz, its own rate",
          run.output_spec.rate == 24_000)
    check("and the heard audio at 16", run.spec.rate == 16_000)
    check("the run is labelled with the model that produced it",
          "gemini" in (run.run.model if run.run else ""), str(run.run and run.run.model))


def main() -> int:
    print("=" * 70)
    print("Commentary QC - Gemini live translation")
    print("=" * 70)
    fake = FakeGemini()
    url = fake.start()
    print(f"fake Gemini Live on {url}")
    try:
        test_protocol()
        test_provider_is_honest()
        test_adapter(url, fake)
        test_live_run(url, fake)
    finally:
        fake.stop()
    print("\n" + "=" * 70)
    print(f"{PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
