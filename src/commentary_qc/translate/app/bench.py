"""The bench: one piece of audio, several models, judged side by side.

This is what makes the translate package a test harness rather than an
integration. A single run tells you a model produced *something*; it cannot
tell you whether another model would have done better. So a bench captures
audio **once** and hands the identical samples to every candidate, which is
the only comparison that means anything -- two runs on two different fifteen
seconds of a match are comparing the match, not the models.

Three deliberate choices, each because the alternative produces a confident
wrong answer:

* **Identical audio, not simultaneous audio.** Fanning live streams at several
  models at once would compare whatever each happened to receive. One capture
  replayed is reproducible, re-runnable when a model is updated, and fair.
* **Blind by default.** Entries are labelled A, B, C in shuffled order and the
  vendor names are withheld until somebody asks to reveal them. People who
  know which one costs money hear it differently.
* **All or nothing on budget.** The whole fan-out is checked against the cap
  before any of it is sent. Half a bench is not a comparison, and discovering
  that after paying for two of four candidates is the worst of both.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

from commentary_qc.translate.core.languages import CHANNEL_LANGUAGE_TAGS, primary
from commentary_qc.translate.core.models import NotConfigured, TranslateError, TranslationRun
from commentary_qc.translate.settings import TranslateSettings

log = logging.getLogger(__name__)

#: The six languages MMC actually broadcasts in, as translation targets. The
#: bench defaults to asking about these rather than a vendor's whole
#: catalogue: a model offering ninety languages none of which is Greek is of
#: no use here, and the coverage table is the first thing worth knowing about
#: a candidate.
MMC_TARGETS: tuple[str, ...] = tuple(
    CHANNEL_LANGUAGE_TAGS[name] for name in (
        "English", "Italian", "Portuguese", "German", "Greek", "French",
    )
)

#: Blind labels, in order.
LABELS = tuple("ABCDEFGHIJKLMNOP")

#: How many candidates run at once. Different vendors do not contend with each
#: other -- the work is network-bound -- but a handful of parallel uploads is
#: enough, and a wide fan-out makes each measured latency less trustworthy.
LANES = 4

#: Phases of a bench.
CAPTURING = "capturing"
TRANSLATING = "translating"
DONE = "done"
FAILED = "failed"

#: Score range the page offers. Five points, because a judgement finer than
#: that is not reliable from listening once, and three is too coarse to
#: separate "usable" from "good".
MIN_SCORE, MAX_SCORE = 1, 5


@dataclass(frozen=True, slots=True)
class Candidate:
    """One entrant: a model, a target language, and optional settings.

    ``overrides`` exists because the question is often not "which vendor" but
    "which voice" or "which of this vendor's models" -- and those are the same
    comparison with the same machinery.
    """

    provider: str
    target: str
    overrides: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def key(self) -> str:
        extra = ",".join(f"{k}={v}" for k, v in sorted(self.overrides.items()))
        return f"{self.provider}->{self.target}" + (f"[{extra}]" if extra else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "target": self.target,
            "overrides": dict(self.overrides),
            "key": self.key,
        }


@dataclass(slots=True)
class Entry:
    """One candidate's result, plus the blind label it is shown under."""

    label: str
    candidate: Candidate
    run: TranslationRun | None = None
    error: str = ""

    def to_dict(self, *, revealed: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "label": self.label,
            "target": self.candidate.target,
            "error": self.error,
        }
        if revealed:
            out["candidate"] = self.candidate.to_dict()
        if self.run is not None:
            run = self.run.to_dict()
            if not revealed:
                # The point of a blind test. The provider name, the model name
                # and the voice all identify the entrant, so all three go.
                for giveaway in ("provider", "model", "voice", "credits"):
                    run.pop(giveaway, None)
            out["run"] = run
            out["run_id"] = self.run.id
            out["status"] = self.run.status
            out["seconds"] = round(self.run.total_s, 2)
            out["text"] = self.run.target_text
            out["heard"] = self.run.source_text
            out["has_audio"] = bool(self.run.spoken_audio)
        return out


class Bench:
    """One comparison in progress or finished.

    Mutable fields are written by the bench thread and read by the API thread,
    the same arrangement :class:`~.live.LiveRun` uses: single assignments of
    immutable values, with the entry list copied under a lock.
    """

    def __init__(
        self,
        channel: str,
        candidates: Sequence[Candidate],
        *,
        mode: str = "shadow",
        seconds: float = 15.0,
        source_language: str = "auto",
        label: str = "",
        note: str = "",
    ) -> None:
        if not candidates:
            raise NotConfigured("a bench needs at least one candidate")
        self.id = uuid.uuid4().hex[:16]
        self.created = time.time()
        self.mode = mode
        self.channel = channel
        self.seconds = seconds
        self.source_language = source_language
        self.label = label
        self.note = note

        # Shuffled so the display order carries no information. A tester who
        # knows the expensive model is always first is not blind.
        order = list(candidates)
        random.shuffle(order)
        self.entries: list[Entry] = [
            Entry(label=LABELS[i % len(LABELS)], candidate=c)
            for i, c in enumerate(order)
        ]

        self.phase = CAPTURING
        self.error = ""
        self.revealed = False
        self.source_url = ""
        self.heard_audio = b""
        self.heard_type = "audio/wav"
        self.captured_seconds = 0.0
        self.gaps: list[str] = []
        self.started = time.perf_counter()
        self.elapsed_s = 0.0

        self._lock = threading.Lock()
        self._finished = threading.Event()

    # -- state -----------------------------------------------------------
    @property
    def running(self) -> bool:
        return not self._finished.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    def finish(self) -> None:
        self.elapsed_s = round(time.perf_counter() - self.started, 2)
        self._finished.set()

    def reveal(self) -> None:
        self.revealed = True

    def runs(self) -> list[TranslationRun]:
        with self._lock:
            return [e.run for e in self.entries if e.run is not None]

    def set_result(self, entry: Entry, run: TranslationRun) -> None:
        with self._lock:
            entry.run = run

    def to_dict(self, *, scores: dict[str, list[dict]] | None = None) -> dict[str, Any]:
        with self._lock:
            entries = [e.to_dict(revealed=self.revealed) for e in self.entries]
        if scores:
            for item in entries:
                item["scores"] = scores.get(item.get("run_id", ""), [])
        return {
            "id": self.id,
            "created": self.created,
            "mode": self.mode,
            "channel": self.channel,
            "phase": self.phase,
            "running": self.running,
            "error": self.error,
            "revealed": self.revealed,
            "label": self.label,
            "note": self.note,
            "seconds": round(self.captured_seconds or self.seconds, 2),
            "source_language": self.source_language,
            "source_url": self.source_url,
            "gaps": list(self.gaps),
            "elapsed_s": self.elapsed_s or round(time.perf_counter() - self.started, 2),
            "has_source_audio": bool(self.heard_audio),
            "entries": entries,
            "count": len(entries),
        }


def run_bench(
    bench: Bench,
    mode: Any,
    settings: TranslateSettings,
    samples: np.ndarray,
    rate: int,
    *,
    lanes: int = LANES,
    fallback_language: str = "",
) -> Bench:
    """Hand ``samples`` to every candidate and collect the results.

    Synchronous; the caller decides whether to put it on a thread. Never
    raises for a candidate's sake -- a model that fails is a result, and
    "three of four worked" is exactly the kind of thing a bench exists to
    show.
    """
    bench.phase = TRANSLATING
    audio = np.asarray(samples, dtype=np.float32).ravel()

    def one(entry: Entry) -> None:
        wiring = None
        try:
            wiring = mode.wire(
                _with_overrides(settings, entry.candidate.overrides),
                entry.candidate.provider,
            )
            run = mode.run(
                wiring,
                audio,
                rate,
                channel=bench.channel,
                source_language=bench.source_language,
                target_language=entry.candidate.target,
                source_url=bench.source_url,
                fallback_language=fallback_language,
            )
            # The heard audio is identical for every entry; it lives on the
            # bench instead. Keeping N copies of the same WAV in N database
            # rows is how a comparison tool ends up the largest file on disk.
            run = dataclasses.replace(run, heard_audio=b"", bench=bench.id)
            bench.set_result(entry, run)
        except (NotConfigured, TranslateError) as exc:
            entry.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - one failure is one result
            log.exception("bench candidate %s failed", entry.candidate.key)
            entry.error = f"{type(exc).__name__}: {exc}"
        finally:
            if wiring is not None:
                try:
                    wiring.close()
                except Exception:  # noqa: BLE001
                    pass

    with ThreadPoolExecutor(max_workers=max(1, min(lanes, len(bench.entries)))) as pool:
        list(pool.map(one, list(bench.entries)))

    bench.phase = DONE if any(e.run is not None for e in bench.entries) else FAILED
    if bench.phase == FAILED:
        bench.error = "; ".join(e.error for e in bench.entries if e.error)[:400]
    return bench


def _with_overrides(settings: TranslateSettings, overrides: dict) -> TranslateSettings:
    """Settings with a candidate's tweaks applied, validated as usual.

    Unknown keys are dropped rather than raising: a candidate list is often
    typed by hand, and a misspelled knob should narrow the comparison, not
    abort it. The names that *are* applied go through the dataclass, so an
    out-of-range value still fails loudly.
    """
    if not overrides:
        return settings
    fields = {f.name for f in dataclasses.fields(TranslateSettings)}
    clean = {k: v for k, v in overrides.items() if k in fields}
    dropped = sorted(set(overrides) - set(clean))
    if dropped:
        log.warning("bench candidate used unknown settings: %s", dropped)
    return dataclasses.replace(settings, **clean) if clean else settings


# -- the planning side -------------------------------------------------------
def plan(
    settings: TranslateSettings,
    providers: Iterable[str],
    targets: Iterable[str],
    *,
    overrides: dict[str, dict] | None = None,
) -> list[Candidate]:
    """Every (model, target) pair asked for, as candidates."""
    extra = overrides or {}
    out: list[Candidate] = []
    for name in providers:
        key = (name or "").strip().lower()
        if not key:
            continue
        for target in targets:
            tag = (target or "").strip().lower()
            if tag:
                out.append(Candidate(key, tag, dict(extra.get(key, {}))))
    return out


def cost_estimate(candidates: Sequence[Candidate], seconds: float) -> dict[str, Any]:
    """How much audio this bench will send, before it sends any.

    Expressed in seconds of audio rather than money, because the one number
    every vendor bills on is duration and none of them publishes a balance we
    could quote. Fanning one fifteen-second clip at four models and three
    languages is three minutes of billable audio, which is the kind of
    arithmetic worth seeing in advance.
    """
    total = max(0.0, seconds) * len(candidates)
    return {
        "candidates": len(candidates),
        "clip_seconds": round(seconds, 1),
        "audio_seconds": round(total, 1),
        "audio_minutes": round(total / 60.0, 2),
    }


def coverage(
    settings: TranslateSettings,
    provider_registry: Any,
    targets: Sequence[str] = MMC_TARGETS,
) -> dict[str, Any]:
    """Which models can reach which of our target languages.

    The first thing worth knowing about a candidate, and worth asking before a
    bench rather than discovering mid-run: with all six of MMC's languages as
    targets, several models drop out immediately, and a table saying so is
    more useful than six failed entries.

    Each answer is ``yes``, ``near`` (the model offers that language in another
    locale, which is fine -- Portuguese is Portuguese), ``no``, or ``unknown``
    when the model could not be asked. ``unknown`` is deliberately not ``no``:
    a catalogue we failed to fetch is our problem, not the model's.
    """
    rows = []
    for info in provider_registry.describe(settings):
        name = info["name"]
        row = {
            "provider": name,
            "title": info.get("title", name),
            "selectable": info.get("selectable", False),
            "blocked": info.get("blocked", ""),
            "cost": info.get("cost", "unknown"),
            "targets": {},
            "live": {},
            "catalogue": 0,
            "error": "",
        }
        if not info.get("selectable"):
            row["targets"] = {t: "unknown" for t in targets}
            row["error"] = info.get("blocked", "")
            rows.append(row)
            continue
        provider = None
        try:
            provider = provider_registry.build(name, settings)
            catalogue = {str(t).lower() for t in (provider.languages() or {})}
            row["catalogue"] = len(catalogue)
            for tag in targets:
                row["targets"][tag] = _verdict(tag, catalogue)
            getter = getattr(provider, "live_languages", None)
            if getter is not None:
                live = {str(t).lower() for t in (getter() or ())}
                for tag in targets:
                    row["live"][tag] = _verdict(tag, live)
        except Exception as exc:  # noqa: BLE001 - a table must still render
            row["error"] = str(exc)[:200]
            row["targets"] = row["targets"] or {t: "unknown" for t in targets}
        finally:
            if provider is not None:
                try:
                    provider.close()
                except Exception:  # noqa: BLE001
                    pass
        rows.append(row)
    return {"targets": list(targets), "providers": rows}


def _verdict(tag: str, catalogue: set[str]) -> str:
    want = (tag or "").strip().lower()
    if not catalogue:
        return "unknown"
    if want in catalogue:
        return "yes"
    root = primary(want)
    if any(primary(t) == root for t in catalogue):
        return "near"
    return "no"


# -- keeping them ------------------------------------------------------------
class BenchRunner:
    """Benches this process has run, and the one it is running.

    One at a time. A bench is already a fan-out -- four models and three
    languages is twelve calls -- and two overlapping benches would contend for
    the same rate limits and make every measured latency meaningless, which is
    half of what a bench is for.
    """

    def __init__(self, store: Any | None = None, *, keep: int = 20) -> None:
        self.store = store
        self.keep = max(1, int(keep))
        self._benches: dict[str, Bench] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def start(self, bench: Bench, work: Any) -> Bench:
        """Register ``bench`` and run ``work(bench)`` on a thread."""
        with self._lock:
            busy = next(
                (b for b in self._benches.values() if b.running), None
            )
            if busy is not None:
                raise TranslateError(
                    f"a bench is already running on {busy.channel} "
                    f"({busy.id}); wait for it or stop it"
                )
            self._benches[bench.id] = bench
            self._order.append(bench.id)
            self._forget_old()

        def job() -> None:
            try:
                work(bench)
            except Exception as exc:  # noqa: BLE001 - a bench always reports
                log.exception("bench %s failed", bench.id)
                bench.phase = FAILED
                bench.error = f"{type(exc).__name__}: {exc}"
            finally:
                bench.finish()
                self._file(bench)

        threading.Thread(
            target=job, name=f"bench-{bench.channel}", daemon=True
        ).start()
        return bench

    def _file(self, bench: Bench) -> None:
        if self.store is None:
            return
        try:
            self.store.put_bench(bench)
        except Exception:  # noqa: BLE001
            log.exception("could not store bench %s", bench.id)

    def _forget_old(self) -> None:
        while len(self._order) > self.keep:
            for index, bench_id in enumerate(list(self._order)):
                candidate = self._benches.get(bench_id)
                if candidate is None or not candidate.running:
                    self._order.pop(index)
                    self._benches.pop(bench_id, None)
                    break
            else:
                return

    def get(self, bench_id: str) -> Bench | None:
        with self._lock:
            return self._benches.get(bench_id)

    def current(self) -> Bench | None:
        with self._lock:
            for bench_id in reversed(self._order):
                bench = self._benches.get(bench_id)
                if bench is not None and bench.running:
                    return bench
            for bench_id in reversed(self._order):
                bench = self._benches.get(bench_id)
                if bench is not None:
                    return bench
        return None

    def all(self) -> list[Bench]:
        with self._lock:
            return [
                self._benches[i] for i in reversed(self._order) if i in self._benches
            ]


__all__ = [
    "MMC_TARGETS", "LABELS", "LANES", "MIN_SCORE", "MAX_SCORE",
    "CAPTURING", "TRANSLATING", "DONE", "FAILED",
    "Candidate", "Entry", "Bench", "BenchRunner",
    "run_bench", "plan", "cost_estimate", "coverage",
]
