"""Tests for live translation: the realtime protocol, the adapter, the loop.

Three levels, as in ``test_translate.py``:

1. **The mapper** -- the message shapes, against the JSON Camb's reference
   prints, with no socket at all.
2. **The adapter** -- the real :class:`CambLiveSession` against a **fake Camb
   realtime server on localhost**. This is the only honest way to test a
   WebSocket client: the handshake, the opening-message deadline, binary audio
   frames, the error codes that end a session and the close codes that follow
   them. No key, no credit, no waiting for the real service to misbehave.
3. **The loop** -- :class:`LiveRun` end to end against a generated WAV, so the
   pacing, the captions, the budget and the assembled audio are all checked
   without the network.

    python tests/test_live_translate.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.config import SourceConfig  # noqa: E402
from commentary_qc.translate.core.models import AudioBlock, AudioSpec  # noqa: E402
from commentary_qc.translate.core.ports import AudioSource  # noqa: E402
from commentary_qc.translate.infra.audio_source import (  # noqa: E402
    FFmpegAudioSource,
)
from commentary_qc.translate.infra.audio import pcm16_bytes, wav_from_pcm16  # noqa: E402
from commentary_qc.translate.app.live import (  # noqa: E402
    AtCapacity,
    LiveRun,
    LiveRunner,
)
from commentary_qc.translate.adapters.camb import protocol as rt  # noqa: E402
from commentary_qc.translate.core.models import (  # noqa: E402
    NotConfigured,
    OutOfCredit,
    RateLimited,
    TranslateError,
    TranslationRun,
)
from commentary_qc.translate.app.modes import ModeRegistry  # noqa: E402
from commentary_qc.translate.adapters.camb.provider import CambProvider  # noqa: E402
from commentary_qc.translate.adapters.mock.provider import MockProvider  # noqa: E402
from commentary_qc.translate.adapters.mock.realtime import MockLiveSession  # noqa: E402
from commentary_qc.translate.infra.runs import RunStore  # noqa: E402
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


def table_info(path, what: str = "table_info") -> set[str]:
    """Column or index names, from a connection that is then closed.

    Closing matters on Windows: an open SQLite handle keeps the file locked,
    and the temporary directory then refuses to go away at the end of the run.
    """
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA {what}(translate_runs)")}
    finally:
        conn.close()


def ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"


def have_ffmpeg() -> bool:
    try:
        subprocess.run(
            [ffmpeg_bin(), "-version"],
            capture_output=True, timeout=20, check=True,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# ======================================================================
# 1. The protocol, as pure functions
# ======================================================================
def test_realtime_mapper() -> None:
    section("realtime protocol: the message shapes")

    hello = rt.session_update("en-us", "de-de")
    check("the opening message is a session.update",
          hello["type"] == "session.update")
    check("carrying the languages as locale tags",
          hello["session"]["source_language"] == "en-us"
          and hello["session"]["target_language"] == "de-de")
    check("asking for text as well as audio, so a bad result is diagnosable",
          hello["session"]["output_modalities"] == ["text", "audio"])
    check("fast is the default mode", hello["session"]["mode"] == "fast")
    check("the key is NOT in the message by default -- it goes in the header, "
          "so it never lands in a transcript of the session",
          "auth" not in hello["session"])
    check("a key is included only when explicitly asked for",
          rt.session_update("en-us", "de-de", api_key="k")["session"]["auth"]
          == {"api_key": "k"})

    tags = rt.session_update("EN-US", " DE-DE ")["session"]
    check("tags are lowercased and trimmed, since the server matches "
          "case-insensitively but our own comparisons do not",
          tags["source_language"] == "en-us" and tags["target_language"] == "de-de")

    check("a cloned voice is sent as the documented object",
          rt.session_update("en-us", "de-de", voice_id=12)["session"]["voice"]
          == {"type": "cloned", "voice_id": 12})
    check("and is left out otherwise, because a stock id is refused "
          "with voice_not_found",
          "voice" not in rt.session_update("en-us", "de-de")["session"])

    try:
        rt.session_update("en-us", "de-de", mode="medium")
        check("an unknown mode is refused before a connection is spent", False)
    except NotConfigured as exc:
        check("an unknown mode is refused before a connection is spent",
              "fast" in str(exc))
    try:
        rt.session_update("en-us", "")
        check("a missing target is refused unless the source is auto", False)
    except NotConfigured:
        check("a missing target is refused unless the source is auto", True)
    check("auto needs no target", "target_language" not in
          rt.session_update("auto", "")["session"])

    # -- audio in
    pcm = b"\x01\x02" * 100
    msg = rt.append_audio(pcm)
    check("audio is base64 inside JSON, not a binary frame -- the server "
          "ignores binary frames from the client",
          msg["type"] == "input_audio_buffer.append"
          and base64.b64decode(msg["audio"]) == pcm)
    try:
        rt.append_audio(b"\x00" * (rt.MAX_APPEND_BYTES + 2))
        check("an oversized append is refused here rather than by the server",
              False)
    except TranslateError as exc:
        check("an oversized append is refused here rather than by the server",
              "limit" in str(exc))
    check("the chunk size helper lands on a whole sample",
          rt.chunk_bytes(0.1) == 4800 and rt.chunk_bytes(0.1) % 2 == 0)

    # -- events out: BOTH documented transports for audio
    binary = rt.parse_event(b"\x10\x20\x30\x40")
    check("a binary frame is audio -- the AsyncAPI reading, where the event "
          "has no type field to check",
          binary is not None and binary.kind == rt.AUDIO
          and binary.audio == b"\x10\x20\x30\x40")
    inline = rt.parse_event(json.dumps({
        "type": "response.audio.delta",
        "delta": base64.b64encode(b"abcd").decode(),
    }))
    check("a base64 delta is also audio -- the reference prose reading. The "
          "docs contradict each other, so both work",
          inline is not None and inline.kind == rt.AUDIO and inline.audio == b"abcd")
    check("a corrupt base64 delta yields no audio rather than raising",
          rt.parse_event(json.dumps(
              {"type": "response.audio.delta", "delta": "!!!not base64!!!"}
          )).audio == b"")

    created = rt.parse_event(json.dumps({
        "type": "session.created",
        "session": {"id": "abc-123", "mode": "fast", "source_language": "en-us"},
    }))
    check("session.created is readiness", created.kind == rt.READY)
    check("and carries the resolved session", created.raw["id"] == "abc-123")
    check("session.starting is its own phase, so a slow boot does not look "
          "like a hang", rt.parse_event('{"type":"session.starting"}').kind
          == rt.STARTING)

    said = rt.parse_event(json.dumps(
        {"type": "response.text.done", "text": "the ball goes wide"}))
    check("the translated text arrives final",
          said.kind == rt.SAID and said.final and said.text == "the ball goes wide")
    check("and additively before that",
          rt.parse_event(json.dumps(
              {"type": "response.text.delta", "delta": "the "}
          )).final is False)
    heard = rt.parse_event(json.dumps({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "der Ball", "language": "de-DE",
    }))
    check("so does the source transcript",
          heard.kind == rt.HEARD and heard.text == "der Ball" and heard.final)

    check("an event we do not act on is ignored, not an error -- the server's "
          "vocabulary is bigger than ours and will grow",
          rt.parse_event('{"type":"session.updated","session":{}}') is None)
    check("so is an event type we have never seen",
          rt.parse_event('{"type":"response.invented.future"}') is None)
    check("a non-JSON text frame is reported rather than crashing the reader",
          rt.parse_event("not json at all").kind == rt.NOTE)

    # -- errors
    err = rt.parse_event(json.dumps({"type": "error", "error": {
        "type": "billing_error", "code": "insufficient_credits",
        "message": "out of credit"}}))
    check("an error names its code", err.kind == rt.FAILED
          and err.code == "insufficient_credits")
    check("and is marked fatal, because the server closes the socket on it",
          err.fatal)
    survivable = rt.parse_event(json.dumps({"type": "error", "error": {
        "code": "session_update_field_not_supported", "message": "ignored"}}))
    check("a documented survivable error does not end the session",
          survivable.fatal is False)
    unknown = rt.parse_event(json.dumps({"type": "error", "error": {
        "code": "something_new", "message": "?"}}))
    check("an UNKNOWN code is treated as fatal -- streaming into a session "
          "the server has hung up on would report no fault at all",
          unknown.fatal)
    codeless = rt.parse_event(json.dumps({"type": "error", "error": {
        "message": "commit is not supported in this version"}}))
    check("the codeless error shape in the other half of the docs still parses",
          codeless.kind == rt.FAILED and "commit" in codeless.text)

    check("no credit becomes OutOfCredit, so the budget can latch shut",
          isinstance(rt.error_for("insufficient_credits", "x"), OutOfCredit))
    check("a key problem becomes NotConfigured, not a model failure",
          isinstance(rt.error_for("invalid_api_key", "x"), NotConfigured))
    check("and names the setting to fix",
          "CAMB_API_KEY" in str(rt.error_for("missing_api_key", "no key")))
    check("a rate limit becomes RateLimited, which is retried",
          isinstance(rt.error_for("rate_limited", "x"), RateLimited))
    check("an unsupported language is configuration, not a fault",
          isinstance(rt.error_for("unsupported_language", "x"), NotConfigured))
    check("anything else is a plain TranslateError",
          type(rt.error_for("session_failed", "x")) is TranslateError)

    # -- languages, and the finding that matters to MMC
    ok, note = rt.supported("de-de")
    check("German is in the published realtime list", ok and not note)
    ok, note = rt.supported("pt-pt")
    check("Portuguese falls back within the language to pt-br", ok and "pt-br" in note)
    ok, note = rt.supported("it-it")
    check("Italian is NOT in the published list", ok is False)
    check("and the warning says it may still work, because the docs never "
          "print the per-mode lists", "may" in note)
    check("Greek is not either", rt.supported("el-gr")[0] is False)
    check("auto is always fine", rt.supported("auto") == (True, ""))

    check("the setup deadline takes the SHORTER of the two documented values",
          rt.SETUP_DEADLINE_S == 10.0)
    check("close code 1006 is named, although the vendor does not list it -- "
          "it is the one the live endpoint actually uses when it refuses a key",
          1006 in rt.CLOSE_MEANING)
    check("and the unexplained-close message names the setting to check",
          "CAMB_API_KEY" in rt.UNEXPLAINED_CLOSE)
    check("the wire format is the SDK-documented 24 kHz mono PCM16",
          (rt.WIRE_RATE, rt.WIRE_WIDTH, rt.WIRE_CHANNELS) == (24_000, 2, 1))


# ======================================================================
# 2. A fake Camb realtime server, and the real adapter against it
# ======================================================================
class FakeRealtime:
    """Camb's realtime endpoint, as much of it as the adapter touches.

    Scripted per connection by the query string so one server covers every
    case: ``?script=ok`` translates, ``?script=nocredit`` fails the documented
    way, and so on. Everything it asserts comes from the API reference.
    """

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 0
        self.seen: list[dict] = []
        self._server = None
        self._thread = None

    def start(self) -> str:
        from websockets.sync.server import serve

        self._server = serve(self._handle, self.host, 0)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return f"ws://{self.host}:{self.port}/v1/realtime"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    # -- one connection --------------------------------------------------
    def _handle(self, ws) -> None:
        try:
            self._converse(ws)
        except Exception:  # noqa: BLE001 - a client hanging up is normal here
            pass

    def _converse(self, ws) -> None:
        path = getattr(ws.request, "path", "") or ""
        script = "ok"
        if "script=" in path:
            script = path.split("script=", 1)[1].split("&")[0]
        key = dict(ws.request.headers).get("x-api-key", "")
        record: dict = {"script": script, "key": key, "path": path, "audio": 0}
        self.seen.append(record)

        if script == "badkey" or not key:
            ws.send(json.dumps({"type": "error", "error": {
                "type": "authentication_error", "code": "invalid_api_key",
                "message": "the key was rejected", "param": None}}))
            ws.close(1008, "invalid_api_key")
            return

        first = json.loads(ws.recv(timeout=10))
        record["hello"] = first
        if first.get("type") != "session.update":
            ws.send(json.dumps({"type": "error", "error": {
                "code": "first_event_not_session_update",
                "message": "the first event must be session.update"}}))
            ws.close(1008, "first_event_not_session_update")
            return

        session = dict(first.get("session") or {})
        if script == "unsupported":
            ws.send(json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "code": "unsupported_language",
                "message": f"{session.get('target_language')} is not supported"}}))
            ws.close(1008, "unsupported_language")
            return
        if script == "nocredit":
            ws.send(json.dumps({"type": "error", "error": {
                "type": "billing_error", "code": "insufficient_credits",
                "message": "this account has no credit left"}}))
            ws.close(1008, "insufficient_credits")
            return
        if script == "mute":
            # What the live endpoint was observed doing on 2026-11-08 when it
            # refused a key: it accepts the upgrade and then drops the
            # connection with no error event and no close frame, so there is
            # nothing to report but close code 1006. The fake could not show
            # this until it was seen for real.
            ws.close(1006, "")
            return
        if script == "nostart":
            # Connects, never becomes ready. The shape of a slow-mode session
            # that will not boot.
            while True:
                try:
                    ws.recv(timeout=30)
                except Exception:  # noqa: BLE001
                    return

        ws.send(json.dumps({"type": "session.starting"}))
        resolved = {
            "id": "fake-session-1",
            "mode": session.get("mode", "fast"),
            "source_language": session.get("source_language", "auto"),
            "target_language": session.get("target_language", ""),
            "output_modalities": session.get("output_modalities", ["text", "audio"]),
        }
        ws.send(json.dumps({"type": "session.created", "session": resolved}))
        ws.send(json.dumps({"type": "session.updated", "session": resolved}))

        heard = 0
        while True:
            try:
                raw = ws.recv(timeout=20)
            except Exception:  # noqa: BLE001 - the client finished
                return
            if isinstance(raw, (bytes, bytearray)):
                # The server ignores binary from the client; if the adapter
                # ever sends binary audio, nothing comes back and the test
                # that counts audio fails.
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") != "input_audio_buffer.append":
                continue
            pcm = base64.b64decode(event.get("audio") or "")
            record["audio"] += len(pcm)
            heard += len(pcm)

            if script == "midfail" and record["audio"] > 20_000:
                ws.send(json.dumps({"type": "error", "error": {
                    "type": "server_error", "code": "session_failed",
                    "message": "the pipeline fell over mid-match"}}))
                ws.close(1011, "session_failed")
                return
            if script == "noisy" and record["audio"] > 20_000:
                # A survivable error. The session must keep working.
                ws.send(json.dumps({"type": "error", "error": {
                    "code": "session_update_field_not_supported",
                    "message": "a field was ignored"}}))
                record["warned"] = True

            while heard >= rt.chunk_bytes(1.0):
                heard -= rt.chunk_bytes(1.0)
                ws.send(json.dumps({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "der Ball geht ins Aus",
                }))
                ws.send(json.dumps({
                    "type": "response.text.done",
                    "text": "the ball goes out of play",
                }))
                if script == "inline":
                    # The other documented transport.
                    ws.send(json.dumps({
                        "type": "response.audio.delta",
                        "delta": base64.b64encode(_tone(0.25)).decode(),
                    }))
                else:
                    ws.send(_tone(0.25))  # a binary frame, as the AsyncAPI says
                ws.send(json.dumps({"type": "response.audio.done"}))


def _tone(seconds: float, hz: float = 440.0) -> bytes:
    n = int(rt.WIRE_RATE * seconds)
    t = np.arange(n, dtype=np.float32) / rt.WIRE_RATE
    return pcm16_bytes(0.3 * np.sin(2 * np.pi * hz * t))


def provider_for(url: str, key: str = "test-key") -> CambProvider:
    settings = TranslateSettings(provider="camb", camb_api_key=key)
    prov = CambProvider(settings)
    prov._live_url = url
    return prov


def test_camb_live_adapter(url: str, fake: FakeRealtime) -> None:
    section("the Camb realtime adapter, against a fake server")

    prov = provider_for(url + "?script=ok")
    session = prov.open_live("de-de", "en-us", mode="fast")
    try:
        session.wait_ready(10.0)
        check("the session opens and becomes ready", not session.closed)
        check("the server resolved it and we kept the id",
              session.session_id == "fake-session-1")
        sent = fake.seen[-1]
        check("the key went in the x-api-key REQUEST HEADER, as documented",
              sent["key"] == "test-key")
        check("the mode is in the query string as well as the message, so a "
              "server that ignores one still gets the other",
              "mode=fast" in sent["path"])
        check("the first message was the session.update",
              sent["hello"]["type"] == "session.update")

        for _ in range(12):
            session.send(_tone(0.1))
        deadline = time.monotonic() + 10
        audio, heard, said = b"", [], []
        while time.monotonic() < deadline:
            for event in session.drain():
                if event.kind == rt.AUDIO and event.audio:
                    audio += event.audio
                elif event.kind == rt.HEARD and event.final:
                    heard.append(event.text)
                elif event.kind == rt.SAID and event.final:
                    said.append(event.text)
            if audio and heard and said:
                break
            time.sleep(0.05)
        check("translated audio came back as binary frames", len(audio) > 1000,
              f"{len(audio)} bytes")
        check("with the source transcript", bool(heard) and "Ball" in heard[0])
        check("and the translated text", bool(said) and "goes out" in said[0])
        stats = session.stats()
        check("the session reports what it has sent, in seconds",
              stats["sent_seconds"] > 1.0, str(stats["sent_seconds"]))
        check("audio reached the server as base64 in JSON -- it counted the "
              "bytes, so nothing was sent as a binary frame",
              fake.seen[-1]["audio"] > 20_000)
    finally:
        session.close()
    check("closing is clean and idempotent", session.closed)
    session.close()

    # -- the other documented audio transport
    prov = provider_for(url + "?script=inline")
    session = prov.open_live("de-de", "en-us")
    try:
        session.wait_ready(10.0)
        for _ in range(12):
            session.send(_tone(0.1))
        deadline, got = time.monotonic() + 10, b""
        while time.monotonic() < deadline and not got:
            for event in session.drain():
                if event.kind == rt.AUDIO and event.audio:
                    got += event.audio
            time.sleep(0.05)
        check("a server that sends base64 audio in JSON works just as well -- "
              "which is the point of accepting both", len(got) > 1000)
    finally:
        session.close()

    # -- the failures
    prov = provider_for(url + "?script=nocredit")
    try:
        session = prov.open_live("de-de", "en-us")
        session.wait_ready(10.0)
        check("no credit is reported as OutOfCredit", False, "no exception")
    except OutOfCredit as exc:
        check("no credit is reported as OutOfCredit", "no credit" in str(exc))
    except Exception as exc:  # noqa: BLE001
        check("no credit is reported as OutOfCredit", False, repr(exc))

    prov = provider_for(url + "?script=badkey")
    try:
        session = prov.open_live("de-de", "en-us")
        session.wait_ready(10.0)
        check("a rejected key is NotConfigured, not a model failure", False)
    except NotConfigured:
        check("a rejected key is NotConfigured, not a model failure", True)
    except Exception as exc:  # noqa: BLE001
        check("a rejected key is NotConfigured, not a model failure",
              False, repr(exc))

    prov = provider_for(url + "?script=unsupported")
    try:
        session = prov.open_live("de-de", "it-it")
        session.wait_ready(10.0)
        check("an unsupported language is named as such", False)
    except NotConfigured as exc:
        check("an unsupported language is named as such", "it-it" in str(exc))
    except Exception as exc:  # noqa: BLE001
        check("an unsupported language is named as such", False, repr(exc))

    prov = provider_for(url + "?script=nostart")
    session = prov.open_live("de-de", "en-us", mode="slow")
    try:
        start = time.perf_counter()
        try:
            session.wait_ready(1.0)
            check("a session that never boots times out", False)
        except TranslateError as exc:
            check("a session that never boots times out",
                  time.perf_counter() - start < 4.0)
            check("and the message mentions the slow-mode cold boot, because "
                  "that is the usual innocent cause", "cold boot" in str(exc))
    finally:
        session.close()

    prov = provider_for(url + "?script=mute")
    try:
        session = prov.open_live("de-de", "en-us")
        session.wait_ready(10.0)
        check("a session dropped with no reason at all is reported as a key "
              "or plan problem, not as a bare close code", False)
    except NotConfigured as exc:
        check("a session dropped with no reason at all is reported as a key "
              "or plan problem, not as a bare close code",
              "CAMB_API_KEY" in str(exc), str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        check("a session dropped with no reason at all is reported as a key "
              "or plan problem, not as a bare close code", False, repr(exc))

    prov = provider_for(url + "?script=midfail")
    session = prov.open_live("de-de", "en-us")
    try:
        session.wait_ready(10.0)
        raised = ""
        for _ in range(40):
            try:
                session.send(_tone(0.1))
            except TranslateError as exc:
                raised = str(exc)
                break
            list(session.drain())
            time.sleep(0.02)
        check("a session that fails mid-stream raises on the next send, "
              "rather than silently swallowing the audio", bool(raised), raised)
    finally:
        session.close()

    prov = provider_for(url + "?script=noisy")
    session = prov.open_live("de-de", "en-us")
    try:
        session.wait_ready(10.0)
        notes, audio_after = [], b""
        for _ in range(40):
            session.send(_tone(0.1))
            for event in session.drain():
                if event.kind == rt.FAILED:
                    notes.append(event)
                elif event.kind == rt.AUDIO and event.audio and notes:
                    audio_after += event.audio
            time.sleep(0.01)
        check("a documented survivable error does NOT tear down a working "
              "session", not session.closed)
        check("the warning was delivered, so a silently degraded session is "
              "visible", bool(notes) and not notes[0].fatal,
              f"{len(notes)} warnings")
        check("and translation carried on afterwards, which is the whole "
              "point of calling it survivable", len(audio_after) > 1000,
              f"{len(audio_after)} bytes after the warning")
    finally:
        session.close()

    prov = provider_for(url, key="")
    try:
        prov.open_live("de-de", "en-us")
        check("no key at all is refused before a socket is opened", False)
    except NotConfigured as exc:
        check("no key at all is refused before a socket is opened",
              "CAMB_API_KEY" in str(exc))

    caps = provider_for(url).live_capabilities()
    check("the provider describes its own live path, so the page cannot "
          "describe the wrong one", caps["transport"] == "WebSocket")
    check("and publishes the caveat about the language list",
          any("Italian" in c for c in caps["caveats"]))
    check("the live language list is the realtime one, not the much longer "
          "file-translation one",
          set(provider_for(url).live_languages()) == set(rt.REALTIME_LANGUAGES))


# ======================================================================
# 3. The loop
# ======================================================================
def real_audio() -> FFmpegAudioSource:
    """The production source: discovery plus an ffmpeg pull."""
    return FFmpegAudioSource(SourceConfig(), ffmpeg_bin())


class FakeAudio:
    """An AudioSource made of arrays, so a live run needs no ffmpeg.

    This is the point of the port. Before it, exercising the streaming loop
    meant spawning a process and waiting for a real stream; now the loop can
    be driven in milliseconds with audio chosen to provoke a particular case
    -- including a mid-run gap, which is awkward to arrange for real.
    """

    def __init__(self, seconds: float = 6.0, gap_after: int = -1) -> None:
        self.seconds = seconds
        self.gap_after = gap_after
        self.opened: list[tuple[str, int]] = []

    def resolve(self, channel: str) -> str:
        return f"fake://{channel}"

    def open(self, channel: str, spec: AudioSpec, *, url: str = "") -> "FakeFeed":
        self.opened.append((channel, spec.rate))
        return FakeFeed(self, spec, url or self.resolve(channel))


class FakeFeed:
    def __init__(self, source: FakeAudio, spec: AudioSpec, url: str) -> None:
        self.url = url
        self._source = source
        self._spec = spec
        self._stopped = False

    def blocks(self):
        rng = np.random.default_rng(5)
        per = max(1, self._spec.rate // 10)          # 100 ms
        total = int(self._spec.rate * self._source.seconds)
        sent = 0
        index = 0
        while sent < total and not self._stopped:
            if index == self._source.gap_after:
                yield AudioBlock(gap=True, reason="a pretend reconnect")
                index += 1
                continue
            take = min(per, total - sent)
            block = rng.normal(0, 0.2, take).astype(np.float32)
            yield AudioBlock(samples=np.clip(block, -1, 1), wall=time.time())
            sent += take
            index += 1

    def stop(self) -> None:
        self._stopped = True


def commentary_wav(path: Path, seconds: float = 12.0, rate: int = 48_000) -> Path:
    """Speech-shaped noise in bursts, so the gaps look like sentence ends."""
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    rng = np.random.default_rng(11)
    env = (np.sin(2 * np.pi * 0.45 * t) > -0.25).astype(np.float32)
    sig = np.clip(rng.normal(0, 0.22, t.size).astype(np.float32) * env, -1, 1)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(np.round(sig * 32767).astype("<i2").tobytes())
    return path


def test_mock_live_session() -> None:
    section("the offline session stands in for a real one")

    session = MockLiveSession("auto", "en-us")
    session.wait_ready(5.0)
    check("it reports the wire rate the real one uses", session.rate == rt.WIRE_RATE)
    kinds = [e.kind for e in session.drain()]
    check("it announces starting and then ready, like the real protocol",
          rt.STARTING in kinds and rt.READY in kinds)

    for _ in range(40):
        session.send(_tone(0.1))
    events = list(session.drain())
    check("it answers in bursts rather than per chunk, so a caller that "
          "assumed one reply per send would fail here too",
          len(events) < 40 and any(e.kind == rt.AUDIO for e in events))
    check("the audio it returns is at the wire rate and non-empty",
          sum(len(e.audio) for e in events) > rt.chunk_bytes(0.5))
    try:
        session.send(b"\x00" * (rt.MAX_APPEND_BYTES + 2))
        check("it refuses an oversized append, as the real server does", False)
    except TranslateError:
        check("it refuses an oversized append, as the real server does", True)
    session.close()
    try:
        session.send(_tone(0.1))
        check("sending into a closed session raises", False)
    except TranslateError:
        check("sending into a closed session raises", True)


def test_live_run(tmp: Path) -> None:
    section("a live run end to end, on generated audio")

    if not have_ffmpeg():
        check("ffmpeg is available (set FFMPEG_BIN)", False, "skipping the loop")
        return

    media = commentary_wav(tmp / "commentary.wav", seconds=12.0)
    settings = TranslateSettings(
        provider="mock", live_seconds=12.0,
        shadow_daily_minutes=10.0, shadow_per_channel_minutes=10.0,
    )
    modes = ModeRegistry(settings)
    store = RunStore(str(tmp / "live-runs.db"))
    runner = LiveRunner(store, max_concurrent=1)

    run = modes.get("shadow").live(
        modes.wire("shadow", "mock"), "football-41",
        audio=real_audio(),
        target_language="en-us", seconds=12.0, url=str(media),
    )
    runner.start(run)

    second = modes.get("shadow").live(
        modes.wire("shadow", "mock"), "football-42",
        audio=real_audio(), seconds=5.0,
        url=str(media),
    )
    try:
        runner.start(second)
        check("a second live session is refused while one is running -- the "
              "free plan allows one", False)
    except AtCapacity as exc:
        check("a second live session is refused while one is running -- the "
              "free plan allows one", "football-41" in str(exc))

    check("the run finishes", run.wait(90), "timed out")
    snap = run.snapshot()
    check("it ended in the done phase", snap["phase"] == "done", snap["error"])
    check("it sent roughly the audio it was given",
          10.0 <= snap["sent_seconds"] <= 13.0, str(snap["sent_seconds"]))
    check("it got translated audio back", snap["spoken_seconds"] > 0.0)
    check("it timed how long the session took to accept audio",
          snap["ready_s"] > 0.0)
    check("and how long until the model said anything",
          snap["first_audio_s"] > 0.0)
    check("captions pair what was heard with what was said",
          bool(snap["captions"]) and snap["captions"][0]["heard"]
          and snap["captions"][0]["said"])

    spoken = run.spoken_wav()
    with wave.open(io.BytesIO(spoken)) as fh:
        check("the translated audio is a playable WAV at the wire rate",
              fh.getframerate() == rt.WIRE_RATE and fh.getnchannels() == 1)
        data = np.frombuffer(fh.readframes(fh.getnframes()), dtype="<i2")
    check("and it is audible, not a silent file passing for a working player",
          float(np.sqrt(np.mean((data / 32768.0) ** 2))) > 0.01)
    with wave.open(io.BytesIO(run.heard_wav())) as fh:
        check("the commentary that went in is kept too, so the pair can be "
              "compared by ear", fh.getnframes() > rt.WIRE_RATE * 8)

    stored = store.recent(limit=5)
    check("the finished run was filed without anybody polling for it",
          bool(stored) and stored[0]["id"] == run.id)
    check("and is labelled as a live run, not a clip -- they are different "
          "products with different languages and billing",
          stored[0]["kind"] == "live")
    check("the budget was charged for what was streamed",
          modes.get("shadow").budget.snapshot()["spent_s"] > 9.0)

    # -- stopping early
    run2 = modes.get("shadow").live(
        modes.wire("shadow", "mock"), "football-43",
        audio=real_audio(),
        target_language="en-us", seconds=600.0, url=str(media),
    )
    runner.start(run2)
    time.sleep(1.5)
    run2.stop("stopped by the test")
    check("a run stops when asked, well inside its own limit",
          run2.wait(30), "did not stop")
    check("and says why it stopped",
          any("stopped by the test" in n for n in run2.snapshot()["notes"]))
    check("the tail was still collected, so the last sentence is not lost",
          run2.snapshot()["phase"] in ("done", "failed"))

    # -- a dead channel
    dead = modes.get("shadow").live(
        modes.wire("shadow", "mock"), "football-41",
        audio=real_audio(),
        target_language="en-us", seconds=10.0,
        url=str(tmp / "there-is-no-such-file.wav"),
    )
    runner.start(dead)
    check("a source that delivers nothing fails rather than hanging",
          dead.wait(60))
    check("and says so", dead.snapshot()["phase"] == "failed"
          and bool(dead.snapshot()["error"]))

    # -- the budget stops a run
    tight = TranslateSettings(
        provider="mock", live_seconds=600.0,
        shadow_daily_minutes=5.0 / 60.0, shadow_per_channel_minutes=5.0 / 60.0,
    )
    tight_modes = ModeRegistry(tight)
    capped = tight_modes.get("shadow").live(
        tight_modes.wire("shadow", "mock"), "football-44",
        audio=real_audio(),
        target_language="en-us", seconds=600.0, url=str(media),
    )
    LiveRunner(None).start(capped)
    check("the budget stops a live run against the clock, since a realtime "
          "session is billed by the clock and not per call",
          capped.wait(60) and capped.snapshot()["sent_seconds"] < 30.0,
          str(capped.snapshot()["sent_seconds"]))
    store.close()


def test_live_language_warning(tmp: Path) -> None:
    section("the provider decides what to warn about, and the run repeats it")

    # The caveat belongs to the vendor, not to the app. Camb publishes 14
    # realtime languages and Italian is not among them, so it is Camb's
    # adapter that must say so -- and the app must not know why.
    camb = CambProvider(TranslateSettings(provider="camb", camb_api_key="k"))
    check("Camb's adapter warns about Italian, which its realtime list omits",
          "it-it" in camb.live_note("it-it"), camb.live_note("it-it"))
    check("and says nothing about German, which it carries",
          camb.live_note("de-de") == "", camb.live_note("de-de"))
    check("the app holds no vendor language list of its own: a provider that "
          "supports a language simply does not warn",
          MockProvider(TranslateSettings()).live_note("it-it") == "")

    if not have_ffmpeg():
        check("ffmpeg is available (set FFMPEG_BIN)", False, "skipping")
        return
    media = commentary_wav(tmp / "it.wav", seconds=6.0)
    settings = TranslateSettings(
        provider="mock", live_seconds=6.0,
        shadow_daily_minutes=10.0, shadow_per_channel_minutes=10.0,
    )
    modes = ModeRegistry(settings)
    run = modes.get("shadow").live(
        modes.wire("shadow", "mock"), "football-21",
        audio=real_audio(),
        target_language="zz-zz", seconds=6.0, url=str(media),
    )
    LiveRunner(None).start(run)
    run.wait(60)
    snap = run.snapshot()
    check("a run surfaces whatever warning its provider gave",
          "zz-zz" in snap["language_warning"], snap["language_warning"])
    check("but it was attempted anyway, because a published language list is "
          "sometimes incomplete and refusing would be guessing",
          snap["phase"] == "done", snap["error"])


def test_live_run_without_ffmpeg(tmp: Path) -> None:
    section("a live run against a fake source, with no ffmpeg at all")

    settings = TranslateSettings(
        provider="mock", live_seconds=6.0,
        shadow_daily_minutes=10.0, shadow_per_channel_minutes=10.0,
    )
    modes = ModeRegistry(settings)
    fake = FakeAudio(seconds=6.0, gap_after=20)
    run = modes.get("shadow").live(
        modes.wire("shadow", "mock"), "football-41",
        audio=fake, target_language="en-us", seconds=6.0,
    )
    LiveRunner(None).start(run)
    check("it finishes", run.wait(40))
    snap = run.snapshot()
    check("and it worked, with no process spawned anywhere",
          snap["phase"] == "done", snap["error"])
    check("the source was asked for the rate the SESSION wanted, not a rate "
          "the app chose", fake.opened and fake.opened[0][1] == rt.WIRE_RATE,
          str(fake.opened))
    check("discovery went through the port too", snap["url"].startswith("fake://"))
    check("a gap mid-run is recorded rather than smoothed over",
          any("reconnect" in note for note in snap["notes"]), str(snap["notes"]))
    check("and the run carried on afterwards", snap["sent_seconds"] > 3.0,
          str(snap["sent_seconds"]))


def test_run_store_migration(tmp: Path) -> None:
    section("an older translate.db gains the new column")

    path = tmp / "old.db"
    old_columns = (
        "id TEXT PRIMARY KEY, created REAL NOT NULL, mode TEXT NOT NULL, "
        "channel TEXT, seconds REAL, provider TEXT, model TEXT, voice TEXT, "
        "source_language TEXT, source_text TEXT, target_language TEXT, "
        "target_text TEXT, status TEXT, error TEXT, transcribe_s REAL, "
        "translate_s REAL, speak_s REAL, total_s REAL, credits TEXT, "
        "source_url TEXT, heard_audio BLOB, spoken_audio BLOB, "
        "heard_type TEXT, spoken_type TEXT"
    )
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE translate_runs ({old_columns})")
    conn.execute(
        "INSERT INTO translate_runs (id, created, mode, channel) "
        "VALUES ('old-1', 1000.0, 'shadow', 'football-31')"
    )
    conn.commit()
    conn.close()

    store = RunStore(str(path))
    check("opening it adds the missing column rather than failing every "
          "insert -- CREATE TABLE IF NOT EXISTS does nothing to a table that "
          "already exists",
          "kind" in table_info(path))
    store.put(TranslationRun(id="new-1", created=2000.0, mode="shadow",
                             kind="live", channel="football-41"))
    rows = store.recent(limit=5)
    check("the old row survived", any(r["id"] == "old-1" for r in rows))
    check("and the new one went in", any(r["id"] == "new-1" for r in rows))
    store.close()


def test_audio_helpers() -> None:
    section("the audio helpers the live path adds")

    pcm = pcm16_bytes(np.array([0.0, 1.0, -1.0, 0.5], dtype=np.float32))
    ints = np.frombuffer(pcm, dtype="<i2")
    check("full scale maps to 32767, not 32768, so it cannot wrap to the "
          "most negative value", int(ints[1]) == 32767)
    check("and negative full scale is symmetrical", int(ints[2]) == -32767)
    check("it is little-endian 16-bit, two bytes a sample", len(pcm) == 8)
    check("samples beyond full scale are clipped, not normalised -- the point "
          "is to hear what went out on air",
          np.frombuffer(
              pcm16_bytes(np.array([4.0], dtype=np.float32)), dtype="<i2"
          )[0] == 32767)

    blob = wav_from_pcm16(_tone(0.5), rt.WIRE_RATE)
    with wave.open(io.BytesIO(blob)) as fh:
        check("a header is put on raw PCM so a browser can play it",
              fh.getframerate() == rt.WIRE_RATE and fh.getsampwidth() == 2)
        check("and the length is right", abs(fh.getnframes() / rt.WIRE_RATE - 0.5) < 0.01)
    odd = wav_from_pcm16(_tone(0.1) + b"\x01", rt.WIRE_RATE)
    with wave.open(io.BytesIO(odd)) as fh:
        check("a trailing half sample is dropped rather than shifting every "
              "sample after it by a byte", fh.getnframes() == int(rt.WIRE_RATE * 0.1))


def main() -> int:
    import tempfile

    print("=" * 70)
    print("Commentary QC - live translation")
    print("=" * 70)

    fake = FakeRealtime()
    url = fake.start()
    print(f"fake Camb realtime on {url}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        try:
            test_realtime_mapper()
            test_audio_helpers()
            test_mock_live_session()
            test_camb_live_adapter(url, fake)
            test_live_run_without_ffmpeg(tmp)
            test_run_store_migration(tmp)
            test_live_run(tmp)
            test_live_language_warning(tmp)
        finally:
            fake.stop()

    print("\n" + "=" * 70)
    print(f"{PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
