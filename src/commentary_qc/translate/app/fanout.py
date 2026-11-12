"""One pull, many listeners.

The whole premise of this project is that a channel's audio is fetched once
and read by everything that wants it -- that is why the quality engine and
the translator can both run without doubling the load on the vendor's CDN.
Live translation into several languages at once is the same idea one level
down: one English commentator, one ffmpeg process, and a separate model
session per output language.

Doing it the obvious way does not work. Each :class:`~..app.live.LiveRun`
opens its own feed, so three target languages would mean three ffmpeg
processes pulling the same HLS playlist. Besides being wasteful, they would
drift: three independent pulls of a live stream start at whatever segment the
CDN is serving at that instant, so the Greek output could be eight seconds
ahead of the German one and nobody comparing them would know why.

So the fan-out goes here, at the port, and nothing above it changes. A
:class:`SharedSource` satisfies :class:`~..core.ports.AudioSource` exactly as
the ffmpeg one does; a live run opens it, gets a feed, and never learns that
two other runs are reading the same bytes.

**Back-pressure rule, and it is the project's existing one:** a listener that
cannot keep up loses its *oldest* audio, not its newest. The reader thread
never blocks on a slow consumer, because blocking it would stall every other
listener and ultimately the pull itself -- and a realtime session answers a
slow client by hanging up. Drops are counted and reported rather than
smoothed over.
"""

from __future__ import annotations

import logging
import threading
import time
from queue import Empty, Queue
from typing import Any, Iterator

from commentary_qc.translate.core.models import AudioBlock, AudioSpec

log = logging.getLogger(__name__)

#: Blocks a single listener may fall behind by. A block is a fraction of a
#: second of audio, so this is a second or two of slack -- enough to ride out
#: a provider's socket write taking longer than usual, nowhere near enough to
#: let one listener accumulate a backlog worth sending.
BRANCH_QUEUE = 24

#: How long a listener waits on an empty queue before looking again. Only a
#: liveness interval: it decides how quickly a stopped branch notices, not
#: how quickly audio arrives.
POLL_S = 0.25

#: How long a shared pull waits for the rest of its group to join before it
#: starts reading. Generous on purpose: a leg has to open a model session
#: before it asks for audio, and a realtime session's cold boot is documented
#: in seconds rather than milliseconds. It returns the instant the expected
#: number of listeners have arrived, so the full wait only happens when one
#: of them never does.
ASSEMBLE_S = 8.0

#: Sentinel pushed to every branch when the pull ends, so a listener's
#: iteration finishes instead of waiting for audio that is never coming.
_END = object()


class _Branch:
    """One listener's view of a shared pull.

    Satisfies :class:`~..core.ports.AudioFeed`: it yields blocks and it can
    be stopped, and stopping it leaves the other listeners untouched.
    """

    def __init__(self, parent: "_Pump", url: str) -> None:
        self._parent = parent
        self.url = url
        self._queue: Queue[Any] = Queue(maxsize=BRANCH_QUEUE)
        self._stopped = threading.Event()
        #: Blocks this listener was too slow to take. Reported, because a
        #: translation with a hole in it should say so rather than sound
        #: mysteriously disjointed.
        self.dropped = 0

    # -- the feed side ---------------------------------------------------
    def blocks(self) -> Iterator[AudioBlock]:
        while not self._stopped.is_set():
            try:
                item = self._queue.get(timeout=POLL_S)
            except Empty:
                continue
            if item is _END:
                return
            yield item

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._parent.leave(self)

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    # -- the pump side ---------------------------------------------------
    def offer(self, block: Any) -> None:
        """Hand this listener a block, dropping the oldest if it is behind.

        Never blocks. The reader thread calls this once per listener per
        block, and one listener stuck on a slow socket must not be able to
        hold up the others or the pull.
        """
        if self._stopped.is_set():
            return
        try:
            self._queue.put_nowait(block)
            return
        except Exception:  # noqa: BLE001 - a full queue is the only case
            pass
        try:
            self._queue.get_nowait()
            self.dropped += 1
        except Empty:  # pragma: no cover - drained between the two calls
            pass
        try:
            self._queue.put_nowait(block)
        except Exception:  # noqa: BLE001 - refilled in between; give up on it
            self.dropped += 1

    def close(self) -> None:
        """Tell this listener the pull has ended."""
        try:
            self._queue.put_nowait(_END)
        except Exception:  # noqa: BLE001 - full queue; make room for the end
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(_END)
            except Exception:  # noqa: BLE001
                log.debug("could not deliver the end marker to a branch")


class _Pump:
    """One real feed, read by one thread, copied to every branch."""

    def __init__(
        self,
        inner_feed: Any,
        url: str,
        *,
        expect: int = 1,
        assemble_s: float = ASSEMBLE_S,
    ) -> None:
        self.feed = inner_feed
        self.url = url
        self.branches: list[_Branch] = []
        self.blocks_read = 0
        self.started = time.time()
        #: How many listeners to wait for before reading the first block, and
        #: how long to wait for them. See :meth:`_assemble`.
        self.expect = max(1, int(expect))
        self.assemble_s = max(0.0, float(assemble_s))
        self.assembled_s = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._ready = threading.Event()
        self._on_empty: Any = None

    def join(self, label: str = "") -> _Branch:
        branch = _Branch(self, self.url)
        with self._lock:
            self.branches.append(branch)
            enough = len(self.branches) >= self.expect
        if enough:
            self._ready.set()
        return branch

    def leave(self, branch: _Branch) -> None:
        with self._lock:
            if branch in self.branches:
                self.branches.remove(branch)
            empty = not self.branches
        if empty:
            # Nobody is listening any more, so stop paying for the pull. A
            # shared source that kept reading after its last listener left
            # would hold an ffmpeg process open for the life of the process.
            self.stop()

    def start(self) -> "_Pump":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._work, name="fanout-pump", daemon=True
        )
        self._thread.start()
        return self

    def _assemble(self) -> None:
        """Wait for the group's other listeners before reading any audio.

        Without this the fan-out works and is still subtly wrong. Each leg
        opens its own model session before it asks for audio, and sessions do
        not boot in step -- one may take a second longer than another -- so
        the first listener to arrive would start consuming audio that the
        others never see. Three translations each missing a different opening
        second are not comparable, and comparing them is the only reason to
        run them together.

        Bounded, because a leg whose session is refused never arrives, and
        the ones that did boot should still get their audio.
        """
        if self.expect <= 1 or self.assemble_s <= 0:
            return
        began = time.perf_counter()
        self._ready.wait(self.assemble_s)
        self.assembled_s = round(time.perf_counter() - began, 2)
        with self._lock:
            joined = len(self.branches)
        if joined < self.expect:
            log.info(
                "started the shared pull on %s with %d of %d listeners after "
                "%.1fs", self.url, joined, self.expect, self.assembled_s,
            )

    def _work(self) -> None:
        try:
            self._assemble()
            for block in self.feed.blocks():
                if self._done.is_set():
                    break
                self.blocks_read += 1
                with self._lock:
                    listeners = list(self.branches)
                for branch in listeners:
                    branch.offer(block)
        except Exception as exc:  # noqa: BLE001 - the pull failing is news
            log.warning("the shared pull on %s ended: %s", self.url, exc)
            with self._lock:
                listeners = list(self.branches)
            # A failed pull reaches the listeners as a gap, which is already
            # a value every live run knows how to report. Inventing a new
            # error path here would mean each run handling the same event
            # twice, in two shapes.
            for branch in listeners:
                branch.offer(AudioBlock(gap=True, reason=str(exc) or "the pull ended"))
        finally:
            self._done.set()
            with self._lock:
                listeners = list(self.branches)
            for branch in listeners:
                branch.close()
            try:
                self.feed.stop()
            except Exception:  # noqa: BLE001
                log.debug("the shared feed did not stop cleanly")
            if self._on_empty is not None:
                try:
                    self._on_empty(self)
                except Exception:  # noqa: BLE001
                    log.debug("the pump's release hook failed")

    def stop(self) -> None:
        if self._done.is_set():
            return
        self._done.set()
        try:
            # Kill the pull rather than waiting for the loop: it is blocked
            # on a pipe that only produces audio in real time.
            self.feed.stop()
        except Exception:  # noqa: BLE001
            log.debug("the shared feed did not stop cleanly")

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "url": self.url,
                "running": self.running,
                "listeners": len(self.branches),
                "blocks": self.blocks_read,
                "dropped": sum(b.dropped for b in self.branches),
                "expected": self.expect,
                "assembled_s": self.assembled_s,
                "elapsed_s": round(time.time() - self.started, 1),
            }


class SharedSource:
    """An :class:`~..core.ports.AudioSource` that pulls once per channel.

    Wraps another source -- in production the ffmpeg one -- and hands out as
    many feeds as are asked for, all reading the same bytes. A pull is keyed
    by channel *and* sample rate: two providers wanting different rates
    genuinely need different pulls, because resampling is ffmpeg's job and it
    does it better from the source than we would from a decode.

    One pull is started by the first listener and stopped by the last, so
    this holds nothing open on its own and needs no lifecycle of its own.
    """

    def __init__(
        self, inner: Any, *, expect: int = 1, assemble_s: float = ASSEMBLE_S
    ) -> None:
        self.inner = inner
        #: How many listeners each pull should wait for. A group sets this
        #: to its number of languages so every leg hears the same opening
        #: second; one is the ordinary single-listener case and waits for
        #: nobody.
        self.expect = max(1, int(expect))
        self.assemble_s = float(assemble_s)
        self._pumps: dict[tuple[str, int], _Pump] = {}
        self._urls: dict[str, str] = {}
        self._openers: dict[tuple[str, int], threading.Lock] = {}
        self._lock = threading.Lock()

    # -- the port --------------------------------------------------------
    def resolve(self, channel: str) -> str:
        """The channel's URL, probed once and remembered.

        Three legs starting together would otherwise each run discovery on
        the same channel within the same second, which is three probes of the
        CDN to learn the same thing.
        """
        with self._lock:
            known = self._urls.get(channel)
        if known:
            return known
        url = self.inner.resolve(channel)
        with self._lock:
            self._urls[channel] = url
        return url

    def _opener(self, key: tuple[str, int]) -> threading.Lock:
        """The lock that serialises opening one channel-and-rate.

        One lock per key rather than one lock overall, because opening a pull
        means discovery and spawning ffmpeg -- holding the source's own lock
        across that would stop every other channel from starting while one
        was connecting.
        """
        with self._lock:
            lock = self._openers.get(key)
            if lock is None:
                lock = self._openers[key] = threading.Lock()
            return lock

    def open(self, channel: str, spec: AudioSpec, *, url: str = "") -> _Branch:
        key = (channel, int(getattr(spec, "rate", 0)))
        # Serialised per key. Without it, two legs of the same group can both
        # find no pull, both spawn ffmpeg, and one of those processes is then
        # thrown away -- having already been started, and having already taken
        # a second copy of the stream from the vendor. Worse, the discarded
        # pull is the one some legs were about to join, so the group can end
        # up split across two pulls that are seconds apart.
        with self._opener(key):
            with self._lock:
                pump = self._pumps.get(key)
                if pump is not None and pump.running:
                    return pump.join()
                # Either nothing was pulling this, or what was has finished.
                # A finished pump is replaced rather than reused: its reader
                # thread is gone and its feed is closed.
                self._pumps.pop(key, None)
            stream_url = url or self.resolve(channel)
            feed = self.inner.open(channel, spec, url=stream_url)
            pump = _Pump(
                feed,
                getattr(feed, "url", stream_url) or stream_url,
                expect=self.expect,
                assemble_s=self.assemble_s,
            )
            branch = pump.join()
            with self._lock:
                self._pumps[key] = pump
                pump._on_empty = lambda p, k=key: self._release(k, p)
            pump.start()
            return branch

    # -- housekeeping ----------------------------------------------------
    def _release(self, key: tuple[str, int], pump: _Pump) -> None:
        with self._lock:
            if self._pumps.get(key) is pump:
                self._pumps.pop(key, None)
            # The URL is dropped with the pull. A live stream's playlist is
            # resolved per broadcast, and a remembered URL would outlive the
            # broadcast it belonged to -- which is the one thing the
            # discovery rule for this project forbids.
            self._urls.pop(key[0], None)

    def stop_all(self) -> int:
        with self._lock:
            pumps = list(self._pumps.values())
        for pump in pumps:
            pump.stop()
        return len(pumps)

    def stats(self) -> list[dict[str, Any]]:
        with self._lock:
            pumps = list(self._pumps.values())
        return [p.stats() for p in pumps]


__all__ = ["ASSEMBLE_S", "BRANCH_QUEUE", "POLL_S", "SharedSource"]
