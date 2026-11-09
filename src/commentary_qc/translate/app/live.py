"""One live translation: a channel's audio in, translated audio out, running.

This is the thing the whole translate package exists for. The three-call clip
path in ``modes/shadow.py`` answers "is this model any good" from fifteen
seconds of recorded audio; this answers "can it keep up with a match", which
is a different question and the one that decides whether the feature is real.

Shape of it: one thread per live run. It opens a realtime session, opens its
own ffmpeg pull at the session's wire rate, and then does nothing but move
audio from one to the other while collecting what comes back. The session's
reader thread is what makes that single loop safe -- draining the far end
never blocks, so the loop keeps feeding audio at the rate it arrives, which is
the one thing the server will not forgive (it answers a slow client by hanging
up).

What it does not do is ride out trouble. A reconnect is recorded and the audio
after it is sent into the same session; a fatal error ends the run with the
reason attached. A live translation that silently papered over a gap would be
worse than one that stopped, because the output is audio nobody is checking
against a clock.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from commentary_qc.discovery import ProbeFailed, StreamNotFound
from commentary_qc.translate.core.ports import AudioFeed, AudioSource
from commentary_qc.translate.infra.audio import pcm16_bytes, wav_from_pcm16
from commentary_qc.translate.core.models import (
    AUDIO,
    FAILED,
    HEARD,
    NOTE,
    READY,
    SAID,
    AudioSpec,
    NoAudio,
    NotConfigured,
    OutOfCredit,
    RateLimited,
    TranslateError,
    TranslationRun,
)

log = logging.getLogger(__name__)

#: Phases, in the order a healthy run passes through them. Named rather than
#: boolean because "nothing has happened yet" has four distinct causes -- the
#: channel is being resolved, the socket is opening, the model is booting, or
#: there is simply no speech yet -- and telling somebody which one is the
#: difference between a page that looks broken and one that looks busy.
RESOLVING = "resolving"
CONNECTING = "connecting"
STARTING = "starting"
LISTENING = "listening"
FINISHING = "finishing"
DONE = "done"
FAILED = "failed"

#: How long to keep draining after the audio stops. The far end is still
#: translating the last utterance when we stop sending, and cutting the socket
#: at that moment loses the end of the sentence.
TAIL_S = 4.0

#: How often the budget is re-checked while a run is going. A live session is
#: billed in windows rather than per call, so the cap has to be enforced
#: against the clock rather than at a request boundary -- there isn"t one.
BUDGET_EVERY_S = 5.0

#: The rate used before a session has said what it wants. Only a
#: placeholder: every session reports its own format, and this exists so a
#: WAV written before one opened still has a valid header.
DEFAULT_RATE = 24_000

#: Captions kept for the page. A long run would otherwise grow without limit
#: in memory and then be written into a database row.
MAX_CAPTIONS = 400


@dataclass(slots=True)
class Caption:
    """One utterance, as heard and as translated."""

    at: float
    heard: str = ""
    said: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"at": round(self.at, 1), "heard": self.heard, "said": self.said}


class LiveRun:
    """A realtime translation in progress, pollable from another thread.

    Every mutable field is written only by the run thread and read by the API
    thread. They are plain attributes rather than a lock-guarded structure:
    each is a single assignment of an immutable value, which CPython makes
    atomic, and a snapshot that caught one field a moment stale is harmless
    when it refreshes a second later. The two exceptions -- the caption list
    and the audio buffers -- are copied under a lock, because a list being
    appended to while it is iterated raises.
    """

    def __init__(
        self,
        channel: str,
        wiring: Any,
        *,
        source_language: str = "auto",
        target_language: str = "",
        live_mode: str = "",
        audio: AudioSource | None = None,
        max_seconds: float = 120.0,
        ready_timeout_s: float = 0.0,
        url: str = "",
        voice_id: int | None = None,
        on_finish: Any | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:16]
        self.channel = channel
        self.wiring = wiring
        self.source_language = (source_language or "auto").strip().lower()
        self.target_language = (
            target_language or getattr(wiring.settings, "target_language", "en-us")
        ).strip().lower()
        self.live_mode = (live_mode or "").strip().lower()
        if audio is None:
            raise NotConfigured("a live run needs an audio source")
        self.audio = audio
        self.max_seconds = max(5.0, float(max_seconds))
        # Slow mode documents a cold boot of "30s or more", so one timeout for
        # both modes is either too short to ever start a slow session or far
        # too long to wait on a fast one that is simply broken.
        # A mode name means whatever the provider says it means, so the
        # only thing this layer can know is that some pipelines boot slowly.
        # The provider is asked for the real figure in _stream().
        self.ready_timeout_s = ready_timeout_s or 0.0
        self.url = url
        self.voice_id = voice_id
        self._on_finish = on_finish

        # Replaced in _stream() with whatever the session actually wants.
        # A default is needed because heard_wav() can be called before a
        # session exists, and a WAV with no rate is not a WAV.
        self.spec = AudioSpec(DEFAULT_RATE)
        self.output_spec = AudioSpec(DEFAULT_RATE)

        self.phase = RESOLVING
        self.error = ""
        self.started = time.time()
        self.ended = 0.0
        self.notes: list[str] = []
        self.sent_seconds = 0.0
        self.heard_seconds = 0.0
        self.spoken_seconds = 0.0
        self.first_audio_s = 0.0
        self.ready_s = 0.0
        self.session_info: dict[str, Any] = {}
        self.language_warning = ""
        self.run: TranslationRun | None = None

        self._captions: list[Caption] = []
        self._heard_pcm: list[bytes] = []
        self._spoken_pcm: list[bytes] = []
        self._heard_part = ""
        self._said_part = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._feed: AudioFeed | None = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "LiveRun":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._work, name=f"live-{self.channel}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, why: str = "stopped") -> None:
        """Ask the run to wind up. Returns at once; the thread finishes the
        tail and writes its result."""
        if not self._stop.is_set():
            self._note(why)
        self._stop.set()
        feed = self._feed
        if feed is not None:
            # Kill the pull rather than waiting for the loop to notice: it is
            # blocked reading a pipe that only produces audio in real time.
            feed.stop()

    def wait(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    @property
    def running(self) -> bool:
        return not self._finished.is_set()

    # -- the loop --------------------------------------------------------
    def _work(self) -> None:
        try:
            self._stream()
        except (NotConfigured, OutOfCredit, RateLimited, TranslateError) as exc:
            self._fail(exc)
        except (StreamNotFound, ProbeFailed, NoAudio) as exc:
            self._fail(exc)
        except Exception as exc:  # noqa: BLE001 - a run always reports
            log.exception("live run failed on %s", self.channel)
            self._fail(exc, f"{type(exc).__name__}: {exc}")
        else:
            if self.phase != FAILED:
                self.phase = DONE
        finally:
            self.ended = time.time()
            try:
                self.run = self._result()
            except Exception:  # noqa: BLE001 - never lose the run over its own record
                log.exception("could not assemble the live run record")
            self._shut()
            self._finished.set()
            if self._on_finish is not None:
                try:
                    self._on_finish(self)
                except Exception:  # noqa: BLE001 - filing it must not undo it
                    log.exception("the finish hook for live run %s failed", self.id)

    def _stream(self) -> None:
        provider = self.wiring.provider
        note = getattr(provider, "live_note", None)
        if note is not None:
            try:
                warning = note(self.target_language)
            except Exception:  # noqa: BLE001 - a warning must not stop a run
                warning = ""
            if warning:
                # A warning, never a refusal. A vendor's published language
                # list is sometimes incomplete, and refusing on the strength
                # of documentation we know is partial would be guessing.
                self.language_warning = warning
                self._note(warning)

        allowed, why = self.wiring.budget.allow(self.channel, self.max_seconds)
        if not allowed:
            raise TranslateError(why)

        self.phase = RESOLVING
        stream_url = self.url or self.audio.resolve(self.channel)
        self.url = stream_url

        self.phase = CONNECTING
        if not hasattr(provider, "open_live"):
            raise NotConfigured(
                f"{getattr(provider, 'name', 'this model')} does not offer live "
                f"translation, only the clip path"
            )
        opened = time.perf_counter()
        self._session = provider.open_live(
            self.source_language,
            self.target_language,
            mode=self.live_mode,
            voice_id=self.voice_id,
        )

        self.phase = STARTING
        self._session.wait_ready(self.ready_timeout_s or self._boot_budget())
        self.ready_s = time.perf_counter() - opened
        self.session_info = _stats(self._session)
        self._drain()

        self.phase = LISTENING
        # Whatever the session wants, asked rather than assumed: Camb takes
        # 24 kHz and Gemini 16, and ffmpeg can resample from the source better
        # than we can resample a decode afterwards.
        self.spec = _spec_of(self._session, "spec", self.spec)
        self.output_spec = _spec_of(self._session, "output_spec", self.spec)
        self._feed = self.audio.open(self.channel, self.spec, url=stream_url)
        began = time.perf_counter()
        last_budget = began
        got_audio = False

        for block in self._feed.blocks():
            if self._stop.is_set():
                break
            now = time.perf_counter()
            if now - began >= self.max_seconds:
                self._note(f"reached the {self.max_seconds:.0f}s limit for one run")
                break

            if block.gap:
                # Recorded, not ridden out. The session stays open -- the far
                # end has no idea the stream hiccuped -- but the output after
                # a gap is not continuous with what came before, and anyone
                # judging the translation needs to know that.
                self._note(f"stream gap: {block.reason or 'reconnect'}")
                self._drain()
                continue
            if block.empty:
                self._drain()
                continue
            samples = block.samples
            got_audio = True

            chunk = pcm16_bytes(samples)
            seconds = self.spec.seconds_for(len(chunk))
            with self._lock:
                self._heard_pcm.append(chunk)
            self.heard_seconds += seconds

            self._session.send(chunk)
            self.sent_seconds += seconds
            self.wiring.budget.charge(self.channel, seconds)
            self._drain()

            if self._session.closed:
                self._note(
                    _stats(self._session).get("note")
                    or "the model closed the session"
                )
                break
            if now - last_budget >= BUDGET_EVERY_S:
                last_budget = now
                allowed, why = self.wiring.budget.allow(self.channel, 1.0)
                if not allowed:
                    self._note(why)
                    break

        if not got_audio:
            raise NoAudio(
                f"{self.channel} delivered no audio, so nothing was translated"
            )

        self.phase = FINISHING
        self._feed.stop()
        tail = time.perf_counter() + TAIL_S
        while time.perf_counter() < tail and not self._session.closed:
            if self._drain():
                # Something arrived, so the far end is still finishing the
                # last utterance. Give it the full tail again rather than
                # cutting off mid-sentence.
                tail = time.perf_counter() + TAIL_S
            time.sleep(0.05)
        self._drain()
        self._flush_partials()

    # -- events ----------------------------------------------------------
    def _drain(self) -> bool:
        """Collect whatever the session has said. True if anything arrived."""
        session = self._session
        if session is None:
            return False
        saw = False
        for event in session.drain():
            saw = True
            if event.kind == AUDIO:
                if event.audio:
                    if not self.first_audio_s:
                        self.first_audio_s = time.time() - self.started
                    with self._lock:
                        self._spoken_pcm.append(event.audio)
                    self.spoken_seconds += self.output_spec.seconds_for(
                        len(event.audio)
                    )
            elif event.kind == HEARD:
                if event.final:
                    self._heard_part = event.text or self._heard_part
                    self._close_caption()
                else:
                    self._heard_part += event.text
            elif event.kind == SAID:
                if event.final:
                    self._said_part = event.text or self._said_part
                    self._close_caption()
                else:
                    self._said_part += event.text
            elif event.kind == READY:
                self.session_info = _stats(session)
            elif event.kind == NOTE:
                self._note(event.text)
            elif event.kind == FAILED:
                if event.fatal:
                    # The adapter already decided what its vendor's code
                    # means; reading the code here would put vendor knowledge
                    # back into the app.
                    raise event.error or TranslateError(
                        event.text or "the live session failed"
                    )
                self._note(f"the model reported: {event.text}")
        return saw

    def _close_caption(self) -> None:
        """Pair up a heard line with its translation once both have landed.

        The two arrive as separate events and not reliably in step -- the
        vendor warns the input transcript is not emitted for every session --
        so a caption is written when either side completes and filled in by
        the other if it turns up.
        """
        heard, said = self._heard_part.strip(), self._said_part.strip()
        if not heard and not said:
            return
        with self._lock:
            last = self._captions[-1] if self._captions else None
            if last is not None and bool(last.heard) != bool(last.said):
                # The other half of the caption just arrived.
                if not last.heard and heard:
                    last.heard = heard
                    self._heard_part = ""
                    return
                if not last.said and said:
                    last.said = said
                    self._said_part = ""
                    return
            self._captions.append(
                Caption(at=time.time() - self.started, heard=heard, said=said)
            )
            del self._captions[:-MAX_CAPTIONS]
        self._heard_part = ""
        self._said_part = ""

    def _flush_partials(self) -> None:
        if self._heard_part.strip() or self._said_part.strip():
            self._close_caption()

    def _boot_budget(self) -> float:
        """How long to wait for the session to accept audio.

        Taken from the provider when it says, because the honest figure is
        vendor-specific: one pipeline is ready almost at once and another
        documents a cold boot of half a minute, and a single timeout is
        either too short for the slow one or far too long for a fast one that
        is simply broken.
        """
        caps = getattr(self.wiring.provider, "live_capabilities", None)
        if caps is not None:
            try:
                limits = caps().get("limits") or {}
                setup = float(limits.get("boot_s") or limits.get("setup_s") or 0)
                if setup > 0:
                    return max(20.0, setup * 2)
            except Exception:  # noqa: BLE001
                pass
        return 30.0

    def _note(self, text: str) -> None:
        if not text:
            return
        entry = f"{time.time() - self.started:5.1f}s  {text}"
        with self._lock:
            if self.notes and self.notes[-1].split("  ", 1)[-1] == text:
                return  # a repeated note is noise, not information
            self.notes.append(entry)
            del self.notes[:-50]

    def _fail(self, exc: BaseException, message: str = "") -> None:
        text = message or str(exc) or type(exc).__name__
        self.phase = FAILED
        self.error = text
        self._note(text)
        if isinstance(exc, OutOfCredit):
            # Latch it, as the clip path does: every later run would be
            # refused the same way, and one clear message beats a log of them.
            try:
                self.wiring.budget.block(text)
            except Exception:  # noqa: BLE001
                pass

    def _shut(self) -> None:
        feed, self._feed = self._feed, None
        if feed is not None:
            try:
                feed.stop()
            except Exception:  # noqa: BLE001
                pass
        session, self._session = self._session, None
        if session is not None:
            try:
                self.session_info = _stats(session)
                session.close()
            except Exception:  # noqa: BLE001
                pass
        # The wiring belongs to this run for as long as it lasts -- a live run
        # outlives the request that started it, so the caller cannot close it
        # the way it does for a clip run.
        try:
            self.wiring.close()
        except Exception:  # noqa: BLE001
            pass

    # -- what the page sees ----------------------------------------------
    def snapshot(self, captions: int = 40) -> dict[str, Any]:
        with self._lock:
            recent = [c.to_dict() for c in self._captions[-captions:]]
            notes = list(self.notes[-12:])
            spoken_bytes = sum(len(b) for b in self._spoken_pcm)
        elapsed = (self.ended or time.time()) - self.started
        return {
            "id": self.id,
            "channel": self.channel,
            "phase": self.phase,
            "running": self.running,
            "error": self.error,
            "started": self.started,
            "elapsed_s": round(elapsed, 1),
            "limit_s": self.max_seconds,
            "live_mode": self.live_mode,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "language_warning": self.language_warning,
            "sent_seconds": round(self.sent_seconds, 1),
            "spoken_seconds": round(self.spoken_seconds, 1),
            "spoken_bytes": spoken_bytes,
            "ready_s": round(self.ready_s, 2),
            "first_audio_s": round(self.first_audio_s, 2),
            "captions": recent,
            "notes": notes,
            "session": self.session_info,
            "url": self.url,
            "has_audio": spoken_bytes > 0,
            "run_id": self.run.id if self.run else "",
        }

    def spoken_wav(self) -> bytes:
        """Everything the model has said so far, as a playable WAV.

        Callable while the run is going: the page offers it as soon as there
        is anything, because waiting for a two-minute session to end before
        hearing any of it defeats the point of testing a live path.
        """
        with self._lock:
            pcm = b"".join(self._spoken_pcm)
        return wav_from_pcm16(pcm, self.output_spec.rate) if pcm else b""

    def heard_wav(self) -> bytes:
        with self._lock:
            pcm = b"".join(self._heard_pcm)
        return wav_from_pcm16(pcm, self.spec.rate) if pcm else b""

    def _result(self) -> TranslationRun:
        """The record that goes in the database."""
        with self._lock:
            captions = list(self._captions)
        heard = "\n".join(c.heard for c in captions if c.heard)
        said = "\n".join(c.said for c in captions if c.said)
        status = {
            DONE: "ok",
            FAILED: "error",
        }.get(self.phase, "ok" if self.spoken_seconds else "empty")
        if status == "ok" and not self.spoken_seconds:
            status = "empty"
        return TranslationRun(
            id=self.id,
            created=self.started,
            mode=getattr(self.wiring, "mode", "shadow"),
            kind="live",
            channel=self.channel,
            seconds=round(self.sent_seconds, 2),
            provider=getattr(self.wiring.provider, "name", ""),
            model=_live_model(self.wiring.provider),
            voice=str(self.voice_id or ""),
            source_language=str(
                self.session_info.get("source_language") or self.source_language
            ),
            source_text=heard,
            target_language=self.target_language,
            target_text=said,
            status=status,
            error=self.error or ("; ".join(self.notes[-2:]) if status != "ok" else ""),
            # The clip path's three steps have no equivalent here, so the two
            # timings that do mean something on a socket are recorded instead:
            # how long the session took to accept audio, and how long until it
            # said anything back.
            transcribe_s=round(self.ready_s, 2),
            speak_s=round(self.first_audio_s, 2),
            total_s=round((self.ended or time.time()) - self.started, 2),
            credits="metered per session window; Camb publishes no rate",
            source_url=self.url,
            heard_audio=self.heard_wav(),
            spoken_audio=self.spoken_wav(),
        )


def _spec_of(session: Any, name: str, fallback: AudioSpec) -> AudioSpec:
    """A session's audio format, tolerating an adapter that only has a rate."""
    found = getattr(session, name, None)
    if isinstance(found, AudioSpec):
        return found
    rate = getattr(session, "rate", None)
    return AudioSpec(int(rate)) if rate else fallback


def _stats(session: Any) -> dict[str, Any]:
    getter = getattr(session, "stats", None)
    if getter is None:
        return {}
    try:
        return dict(getter())
    except Exception:  # noqa: BLE001
        return {}


def _live_model(provider: Any) -> str:
    caps = getattr(provider, "live_capabilities", None)
    if caps is not None:
        try:
            return str(caps().get("model") or "")
        except Exception:  # noqa: BLE001
            pass
    return getattr(provider, "model", "")


class AtCapacity(TranslateError):
    """Refused because something else is already running.

    Its own type because it is the one refusal that is purely about timing:
    the same request a minute later will work, and the page should say "wait"
    rather than "broken".
    """


class LiveRunner:
    """The live runs this process has, and the limit on how many at once.

    One at a time by default, and that default is not arbitrary: Camb's free
    plan documents an API concurrency of 1, so a second session opened beside
    the first is refused by the far end after the audio has already started
    flowing. Refusing it here instead means the answer arrives before anything
    is spent, and names the run that is in the way.
    """

    def __init__(
        self,
        store: Any | None = None,
        *,
        max_concurrent: int = 1,
        keep: int = 12,
    ) -> None:
        self.store = store
        self.max_concurrent = max(1, int(max_concurrent))
        self.keep = max(1, int(keep))
        self._runs: dict[str, LiveRun] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def start(self, run: LiveRun) -> LiveRun:
        with self._lock:
            live = [r for r in self._runs.values() if r.running]
            busy = next((r for r in live if r.channel == run.channel), None)
            if busy is not None:
                raise AtCapacity(
                    f"{run.channel} is already being translated "
                    f"(run {busy.id}); stop that one first"
                )
            if len(live) >= self.max_concurrent:
                others = ", ".join(sorted(r.channel for r in live))
                raise AtCapacity(
                    f"only {self.max_concurrent} live translation "
                    f"{'session' if self.max_concurrent == 1 else 'sessions'} "
                    f"at a time, and {others} is running"
                )
            run._on_finish = self._filed
            self._runs[run.id] = run
            self._order.append(run.id)
            self._forget_old()
        return run.start()

    def _filed(self, run: LiveRun) -> None:
        """Write the finished run to the store, once."""
        if self.store is None or run.run is None:
            return
        try:
            self.store.put(run.run)
        except Exception:  # noqa: BLE001
            log.exception("could not store live run %s", run.id)

    def _forget_old(self) -> None:
        """Drop finished runs beyond ``keep``, so their audio is freed.

        Held in memory, not just in the database: a finished run keeps both
        sides of its audio, and a page left open all afternoon would otherwise
        accumulate every one of them.
        """
        while len(self._order) > self.keep:
            for index, run_id in enumerate(list(self._order)):
                candidate = self._runs.get(run_id)
                if candidate is None or not candidate.running:
                    self._order.pop(index)
                    self._runs.pop(run_id, None)
                    break
            else:
                return

    def get(self, run_id: str) -> LiveRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def current(self) -> LiveRun | None:
        """The run somebody is most likely asking about: the live one, else
        the most recent."""
        with self._lock:
            for run_id in reversed(self._order):
                run = self._runs.get(run_id)
                if run is not None and run.running:
                    return run
            for run_id in reversed(self._order):
                run = self._runs.get(run_id)
                if run is not None:
                    return run
        return None

    def all(self) -> list[LiveRun]:
        with self._lock:
            return [self._runs[i] for i in reversed(self._order) if i in self._runs]

    def stop(self, run_id: str, why: str = "stopped from the page") -> bool:
        run = self.get(run_id)
        if run is None:
            return False
        run.stop(why)
        return True

    def stop_all(self, why: str = "shutting down") -> int:
        stopped = 0
        for run in self.all():
            if run.running:
                run.stop(why)
                stopped += 1
        return stopped


__all__ = [
    "LiveRun", "LiveRunner", "AtCapacity", "Caption",
    "TAIL_S", "BUDGET_EVERY_S", "MAX_CAPTIONS",
    "RESOLVING", "CONNECTING", "STARTING", "LISTENING", "FINISHING",
    "DONE", "FAILED",
]
