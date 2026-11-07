"""Background sweep of every channel the platform could be broadcasting.

The commentator page must list whatever channel a person is actually working
on, not the handful this deployment happens to be recording. Those are
different sets: the platform carries ~120 channel slots and around 48 were live
on a Champions League night, while a given run might be watching seven of them.
A picker that shows only the monitored ones is useless to anyone outside that
list.

So discovery is separate from monitoring. This sweeps the candidate slots,
caches what answered, and refreshes in the background -- roughly 125 tiny HTTP
requests a minute, which the CDN does not notice.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import SourceConfig
from .discovery import ProbeFailed, StreamNotFound, resolve
from .registry import Channel

log = logging.getLogger(__name__)

#: Channel slots to probe. Football runs 1-113 with gaps; the racing and other
#: families use a small numbered range. Slots that do not exist simply 404.
FOOTBALL_MAX = 113
OTHER_FAMILIES = ("cricket", "rugby", "horses", "greyhounds")
OTHER_MAX = 3

#: Language blocks observed in the vendor panel. These label the picker only --
#: they do NOT identify whose channel it is, since MMC commentates across
#: several languages and other providers share the same blocks.
LANGUAGE_BLOCKS: tuple[tuple[int, int, str], ...] = (
    (1, 10, "English"), (11, 20, "Spanish"), (21, 30, "Italian"),
    (31, 40, "Portuguese"), (41, 50, "German"), (51, 60, "Greek"),
    (61, 70, "English"), (100, 101, "Greek"), (102, 108, "French"),
)


def candidates() -> list[str]:
    out = [f"football-{n:02d}" for n in range(1, FOOTBALL_MAX + 1)]
    for fam in OTHER_FAMILIES:
        out += [f"{fam}-{n:02d}" for n in range(1, OTHER_MAX + 1)]
    return out


def describe(slug: str) -> Channel:
    """Best-effort label and language for a discovered slug."""
    family, _, num = slug.rpartition("-")
    try:
        n = int(num)
    except ValueError:
        return Channel(slug, slug.replace("-", " ").title())
    if family == "football":
        lang = next((l for lo, hi, l in LANGUAGE_BLOCKS if lo <= n <= hi), "")
        return Channel(slug, f"Football Ch.{n}", lang)
    pretty = {"horses": "Horses", "greyhounds": "Greyhounds",
              "cricket": "Cricket", "rugby": "Rugby"}.get(family, family.title())
    return Channel(slug, f"{pretty} Ch.{n}", "")


class LiveScanner:
    """Keeps a cached list of every channel currently broadcasting."""

    def __init__(self, source: SourceConfig, interval_s: float = 30.0, workers: int = 24) -> None:
        self.source = source
        self.interval_s = interval_s
        self.workers = workers
        self._live: dict[str, Channel] = {}
        self._checked = 0.0
        self._errors = 0
        self._probed = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "LiveScanner":
        self._thread = threading.Thread(target=self._run, name="scanner", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def live(self) -> dict[str, Channel]:
        with self._lock:
            return dict(self._live)

    def checked_at(self) -> float:
        with self._lock:
            return self._checked

    def health(self) -> dict:
        """Whether the last sweep could actually see the platform."""
        with self._lock:
            ok = self._checked > 0 and self._errors < max(1, self._probed // 2)
            return {"checked_at": self._checked, "errors": self._errors,
                    "probed": self._probed, "ok": ok}

    def _probe(self, slug: str) -> str:
        """'live', 'down', or 'error' (could not reach the server)."""
        import dataclasses

        cfg = dataclasses.replace(self.source, channel=slug, url=None)
        try:
            resolve(cfg)
            return "live"
        except ProbeFailed:
            return "error"
        except StreamNotFound:
            return "down"
        except Exception:  # noqa: BLE001 - one bad probe must not end the sweep
            return "error"

    def sweep(self) -> dict[str, Channel]:
        slugs = candidates()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(self._probe, slugs))
        found = [s for s, r in zip(slugs, results) if r == "live"]
        errors = results.count("error")
        live = {s: describe(s) for s in sorted(found, key=_sort_key)}
        with self._lock:
            # A sweep that could not reach the server says nothing about what
            # is on air. Keep the last good list and report the failure, rather
            # than announcing that every channel has stopped.
            if errors < max(1, len(slugs) // 2):
                self._live = live
            self._checked = time.time()
            self._errors = errors
            self._probed = len(slugs)
        if errors:
            log.warning("scanner: %d of %d probes could not reach the server", errors, len(slugs))
        return live

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                live = self.sweep()
                log.info("scanner: %d channels broadcasting", len(live))
            except Exception:  # noqa: BLE001
                log.exception("scanner sweep failed")
            self._stop.wait(self.interval_s)


def _sort_key(slug: str) -> tuple[str, int]:
    family, _, num = slug.rpartition("-")
    try:
        return (family, int(num))
    except ValueError:
        return (family, 0)
