"""One commentator, several languages, one pull.

Two things are being tested and they fail in different ways.

The **fan-out** fails quietly. Three legs each opening their own ffmpeg would
still work -- you would get three translations and never know they were three
seconds out of step with each other, which is fatal for the one thing a
multi-language run is for, namely comparing them. So the tests here count the
pulls, not just the output.

The **group** fails loudly, and the question is whether it fails in a way
somebody can act on: more languages than the plan allows, a provider that
holds one session open, a leg that dies while the others are working. The
last of those is the one that matters most -- a Greek session refused for an
unsupported language must not cost you the German translation that was fine.

No ffmpeg, no key, no credit: the audio source is a port and the model is the
offline stand-in, so all of this runs in milliseconds.

    python tests/test_multilanguage.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from commentary_qc.translate.app.fanout import BRANCH_QUEUE, SharedSource  # noqa: E402
from commentary_qc.translate.app.group import (  # noqa: E402
    LiveGroup,
    TooManyTargets,
)
from commentary_qc.translate.app.live import AtCapacity, LiveRunner  # noqa: E402
from commentary_qc.translate.app.modes import ModeRegistry  # noqa: E402
from commentary_qc.translate.core.models import (  # noqa: E402
    AudioBlock,
    AudioSpec,
    NotConfigured,
)
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
        print(f"  FAIL  {name}\n          {detail}")


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


# ======================================================================
# Fakes
# ======================================================================
class CountingAudio:
    """An AudioSource of arrays that records how often it was really used.

    The counters are the test. A fan-out that works produces the right audio
    whether or not it shared the pull, so the only way to tell the two apart
    is to ask the source how many times it was opened.
    """

    def __init__(
        self, blocks: int = 20, fail_after: int = -1, pace: float = 0.003
    ) -> None:
        self.blocks = blocks
        self.fail_after = fail_after
        self.pace = pace
        self.opens: list[tuple[str, int]] = []
        self.resolves: list[str] = []
        self.feeds: list["CountingFeed"] = []

    def resolve(self, channel: str) -> str:
        self.resolves.append(channel)
        return f"fake://{channel}/{len(self.resolves)}"

    def open(self, channel: str, spec: AudioSpec, *, url: str = "") -> "CountingFeed":
        self.opens.append((channel, int(spec.rate)))
        feed = CountingFeed(self, url or f"fake://{channel}", spec)
        self.feeds.append(feed)
        return feed


class CountingFeed:
    def __init__(self, source: CountingAudio, url: str, spec: AudioSpec) -> None:
        self.url = url
        self._source = source
        # A block is a tenth of a second of audio at whatever rate the session
        # asked for, not a fixed sample count. The offline model answers in
        # bursts of real time, so a feed whose blocks shrank as the rate rose
        # would quietly stop producing any output.
        self._per = max(1, int(spec.rate) // 10)
        self.stopped = False
        self._gate = threading.Event()
        #: Held open until released, so a test can inspect the branches
        #: mid-stream rather than racing the pump to the end.
        self.hold = False

    def blocks(self):
        rng = np.random.default_rng(3)
        for index in range(self._source.blocks):
            if self.stopped:
                return
            if index == self._source.fail_after:
                raise RuntimeError("the pull died")
            if self.hold and index == 1:
                self._gate.wait(5.0)
            yield AudioBlock(
                samples=rng.normal(0, 0.2, self._per).astype(np.float32),
                wall=time.time(),
            )
            # Paced, not flat out. A real feed delivers a block every tenth of
            # a second, which is what makes a 24-block queue two seconds of
            # slack; a fake one that yields as fast as the CPU allows
            # overflows every consumer's queue and turns a fan-out test into a
            # thread-scheduling test. Three milliseconds is enough room for
            # the readers and still finishes in well under a second.
            if self._source.pace:
                time.sleep(self._source.pace)
        # Hold the generator open briefly after the audio so a test that
        # wants to look at a *running* pump is not racing its shutdown.
        while self.hold and not self.stopped:
            time.sleep(0.01)

    def release(self) -> None:
        self._gate.set()

    def stop(self) -> None:
        self.stopped = True
        self._gate.set()


def drain(branch, limit: int = 500) -> int:
    """Count the blocks a branch delivers until its feed ends."""
    seen = 0
    for _ in branch.blocks():
        seen += 1
        if seen >= limit:
            break
    return seen


# ======================================================================
# 1. The fan-out
# ======================================================================
def test_one_pull_many_listeners() -> None:
    section("three listeners, one ffmpeg")

    audio = CountingAudio(blocks=30)
    shared = SharedSource(audio)
    spec = AudioSpec(16_000)

    legs = [shared.open("football-41", spec) for _ in range(3)]
    counts: list[int] = [0, 0, 0]
    threads = [
        threading.Thread(target=lambda i=i: counts.__setitem__(i, drain(legs[i])))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    check("the underlying source was opened once, not three times",
          len(audio.opens) == 1, f"opened {len(audio.opens)} times")
    check("every listener got audio", all(c > 0 for c in counts), str(counts))
    check("and they all got the same amount, because it is the same audio",
          len(set(counts)) == 1, str(counts))
    check("discovery ran once for the channel, not once per leg",
          len(audio.resolves) == 1, f"resolved {len(audio.resolves)} times")
    check("the pull is stopped once the last listener has gone",
          audio.feeds[0].stopped)


def test_the_listeners_are_lined_up_before_any_audio_is_read() -> None:
    section("every leg hears the same opening second")

    audio = CountingAudio(blocks=10)
    shared = SharedSource(audio, expect=3, assemble_s=5.0)
    spec = AudioSpec(16_000)

    first = shared.open("football-41", spec)
    # The pump is running but must not have read anything yet: two listeners
    # are still opening their sessions.
    time.sleep(0.2)
    pump = shared._pumps[("football-41", 16_000)]
    check("the pull holds off while the group is still assembling",
          pump.blocks_read == 0, f"already read {pump.blocks_read}")

    rest = [shared.open("football-41", spec) for _ in range(2)]
    counts = [0, 0, 0]
    legs = [first, *rest]
    threads = [
        threading.Thread(target=lambda i=i: counts.__setitem__(i, drain(legs[i])))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    check("and once they have joined, all three get the whole stream",
          counts == [10, 10, 10], str(counts))


def test_a_leg_that_never_arrives_does_not_hold_up_the_rest() -> None:
    section("the wait is bounded, because a session can be refused")

    audio = CountingAudio(blocks=6)
    # Three expected, two will ever join: the third session was refused.
    shared = SharedSource(audio, expect=3, assemble_s=0.4)
    spec = AudioSpec(16_000)
    legs = [shared.open("football-41", spec) for _ in range(2)]

    began = time.perf_counter()
    counts = [0, 0]
    threads = [
        threading.Thread(target=lambda i=i: counts.__setitem__(i, drain(legs[i])))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    took = time.perf_counter() - began

    check("the legs that did boot still got their audio",
          counts == [6, 6], str(counts))
    check("after waiting the grace period and no longer",
          0.3 <= took < 4.0, f"took {took:.2f}s")
    pump_stats = shared.stats()
    check("and the wait is reported, so a slow start is explainable",
          not pump_stats or pump_stats[0]["assembled_s"] >= 0.3
          or audio.feeds[0].stopped,
          str(pump_stats))


def test_three_legs_opening_at_the_same_instant_get_one_pull() -> None:
    section("the race: legs arriving together, not one after another")

    # Legs of a group open their feed as soon as their own session is ready,
    # and sessions boot within milliseconds of each other. Opening is not
    # instant -- it is discovery plus spawning ffmpeg -- so without a lock
    # per channel every leg can look, find nothing, and start its own pull.
    # The slow resolve below makes that window wide enough to hit every time
    # rather than once in eight runs.
    class SlowAudio(CountingAudio):
        def resolve(self, channel: str) -> str:
            time.sleep(0.15)
            return super().resolve(channel)

    audio = SlowAudio(blocks=12)
    shared = SharedSource(audio, expect=3, assemble_s=3.0)
    spec = AudioSpec(16_000)

    branches: list = [None, None, None]
    ready = threading.Barrier(3)

    def arrive(i: int) -> None:
        ready.wait(5)
        branches[i] = shared.open("football-41", spec)

    threads = [threading.Thread(target=arrive, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    check("three simultaneous arrivals still produced one pull",
          len(audio.opens) == 1, f"opened {len(audio.opens)} times")
    check("and discovery ran once, not once per leg",
          len(audio.resolves) == 1, f"resolved {len(audio.resolves)} times")

    counts = [0, 0, 0]
    readers = [
        threading.Thread(target=lambda i=i: counts.__setitem__(i, drain(branches[i])))
        for i in range(3)
    ]
    for t in readers:
        t.start()
    for t in readers:
        t.join(10)
    check("all three read the same stream from the same place",
          counts == [12, 12, 12], str(counts))


def test_different_rates_get_their_own_pull() -> None:
    section("two sample rates genuinely need two pulls")

    audio = CountingAudio(blocks=5)
    shared = SharedSource(audio)
    a = shared.open("football-41", AudioSpec(16_000))
    b = shared.open("football-41", AudioSpec(24_000))
    check("a second rate opens a second pull, because resampling is ffmpeg's "
          "job and it does it better from the source",
          len(audio.opens) == 2, f"{audio.opens}")
    check("and they are at the rates that were asked for",
          sorted(r for _, r in audio.opens) == [16_000, 24_000])
    a.stop()
    b.stop()


def test_a_slow_listener_loses_its_own_audio() -> None:
    section("back-pressure: the slow one suffers, nobody else does")

    audio = CountingAudio(blocks=BRANCH_QUEUE * 3, pace=0.0)
    shared = SharedSource(audio)
    spec = AudioSpec(16_000)
    quick = shared.open("football-41", spec)
    slow = shared.open("football-41", spec)       # never read from

    got = drain(quick)
    check("the listener that kept up received every block",
          got == BRANCH_QUEUE * 3, f"got {got} of {BRANCH_QUEUE * 3}")
    check("the listener that never read dropped its oldest audio rather than "
          "stalling the pull",
          slow.dropped > 0, f"dropped {slow.dropped}")
    check("and it never buffered more than its queue allows",
          slow._queue.qsize() <= BRANCH_QUEUE, str(slow._queue.qsize()))
    slow.stop()


def test_a_dead_pull_reaches_every_listener() -> None:
    section("the pull dying is news, and it is news everyone gets")

    audio = CountingAudio(blocks=20, fail_after=4)
    shared = SharedSource(audio)
    spec = AudioSpec(16_000)
    legs = [shared.open("football-41", spec) for _ in range(2)]

    seen: list[list[AudioBlock]] = [[], []]
    def read(i: int) -> None:
        for block in legs[i].blocks():
            seen[i].append(block)
    threads = [threading.Thread(target=read, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    check("both listeners were told, not just the one that happened to be "
          "reading when it happened",
          all(any(b.gap for b in s) for s in seen),
          str([[b.gap for b in s] for s in seen]))
    check("the failure arrives as a gap, which every live run already knows "
          "how to report",
          any(b.gap and "pull died" in (b.reason or "") for b in seen[0]),
          str([b.reason for b in seen[0] if b.gap]))
    check("and both iterations ended rather than hanging on audio that was "
          "never coming",
          all(not t.is_alive() for t in threads))


def test_the_url_is_not_remembered_past_the_broadcast() -> None:
    section("a resolved URL belongs to one broadcast")

    audio = CountingAudio(blocks=3)
    shared = SharedSource(audio)
    spec = AudioSpec(16_000)
    first = shared.open("football-41", spec)
    drain(first)
    for _ in range(100):
        if not shared._urls:
            break
        time.sleep(0.01)
    check("the URL is forgotten when the pull ends, so a restarted broadcast "
          "is resolved again rather than fetched from a dead path",
          "football-41" not in shared._urls, str(shared._urls))
    shared.open("football-41", spec).stop()
    check("and the next listener causes a fresh resolve",
          len(audio.resolves) == 2, f"resolved {len(audio.resolves)} times")


def test_stop_all() -> None:
    section("the process can let go of everything")

    audio = CountingAudio(blocks=10_000)
    shared = SharedSource(audio)
    held = [
        shared.open("football-41", AudioSpec(16_000)),
        shared.open("football-21", AudioSpec(16_000)),
    ]
    check("two channels, two pulls", len(audio.opens) == 2)
    check("stop_all reports what it stopped", shared.stop_all() == 2)
    check("and every feed is closed", all(f.stopped for f in audio.feeds))
    for leg in held:
        leg.stop()


# ======================================================================
# 2. The group
# ======================================================================
def wiring_stub(max_live_sessions: int = 0) -> SimpleNamespace:
    """Just enough wiring to reach the arithmetic in the constructor."""
    provider = SimpleNamespace(name="stub")
    if max_live_sessions:
        provider.max_live_sessions = max_live_sessions
    return SimpleNamespace(
        provider=provider,
        settings=SimpleNamespace(target_language="en-us"),
        budget=SimpleNamespace(allow=lambda c, s: (True, ""), charge=lambda c, s: None),
    )


def test_the_group_refuses_before_it_spends() -> None:
    section("what a group says no to, and when")

    try:
        LiveGroup("football-41", wiring_stub(), [], audio=CountingAudio())
        check("no languages is refused", False)
    except NotConfigured as exc:
        check("no languages is refused", "which language" in str(exc))

    try:
        LiveGroup(
            "football-41", wiring_stub(), ["el-gr", "de-de", "it-it"],
            audio=CountingAudio(), max_targets=2,
        )
        check("more languages than the cap is refused", False)
    except TooManyTargets as exc:
        check("more languages than the cap is refused, naming the setting",
              "QC_LIVE_TARGETS_MAX" in str(exc), str(exc))

    try:
        LiveGroup(
            "football-41", wiring_stub(max_live_sessions=1), ["el-gr", "de-de"],
            audio=CountingAudio(),
        )
        check("a provider that holds one session open refuses two languages",
              False)
    except TooManyTargets as exc:
        check("a provider that holds one session open refuses two languages, "
              "before anything is spent",
              "one after another" in str(exc), str(exc))

    group = LiveGroup(
        "football-41", wiring_stub(max_live_sessions=1), ["el-gr"],
        audio=CountingAudio(),
    )
    check("and one language on that same provider is fine",
          len(group.legs) == 1)

    duplicated = LiveGroup(
        "football-41", wiring_stub(), ["el-gr", "de-de", "el-gr", "DE-DE"],
        audio=CountingAudio(),
    )
    check("asking for a language twice does not open two metered sessions "
          "producing the same audio",
          duplicated.targets == ["el-gr", "de-de"], str(duplicated.targets))


def test_the_budget_covers_the_whole_group() -> None:
    section("half a comparison is worth less than none")

    asked: list[float] = []
    wiring = wiring_stub()
    wiring.budget = SimpleNamespace(
        allow=lambda c, s: (asked.append(s) or False, "only 20s left today"),
        charge=lambda c, s: None,
    )
    group = LiveGroup(
        "football-41", wiring, ["el-gr", "de-de", "it-it"],
        audio=CountingAudio(), max_seconds=60.0,
    )
    check("three languages for 60s each needs 180s of audio",
          group.budget_needed() == 180.0, str(group.budget_needed()))
    try:
        group.start()
        check("a group that cannot afford every leg does not start one", False)
    except Exception as exc:  # noqa: BLE001
        check("a group that cannot afford every leg does not start one",
              "180s" in str(exc) and "20s left" in str(exc), str(exc))
    check("and the budget was asked about the whole group, not one leg",
          asked and asked[0] == 180.0, str(asked))


def live_group(targets: list[str], blocks: int = 60, seconds: float = 30.0):
    """A real group on the offline model, driven from arrays."""
    settings = TranslateSettings(
        provider="mock",
        live_seconds=seconds,
        shadow_daily_minutes=60.0,
        shadow_per_channel_minutes=60.0,
        live_targets_max=4,
    )
    modes = ModeRegistry(settings)
    audio = CountingAudio(blocks=blocks)
    group = modes.get("shadow").live_group(
        modes.wire("shadow", "mock"),
        "football-41",
        targets,
        audio=audio,
        seconds=seconds,
        configured_language="en-us",
    )
    return group, audio, modes, settings


def test_a_group_refused_at_the_last_moment_releases_the_channel() -> None:
    section("a refusal must not leave the channel held")

    # The budget is the one check that happens after the runner has taken the
    # legs on, because it depends on how many there are. This is the case
    # that wedged a real channel: the group was registered, start() raised,
    # and every leg reported itself as running for the life of the process.
    settings = TranslateSettings(
        provider="mock", live_seconds=30.0,
        shadow_daily_minutes=60.0, shadow_per_channel_minutes=60.0,
    )
    modes = ModeRegistry(settings)
    runner = LiveRunner(None, max_concurrent=1)

    group = modes.get("shadow").live_group(
        modes.wire("shadow", "mock"), "football-41", ["el-gr"],
        audio=CountingAudio(blocks=10), seconds=30.0,
    )
    group.wiring.budget = SimpleNamespace(
        allow=lambda c, s: (False, "no credit left today"),
        charge=lambda c, s: None,
    )
    try:
        runner.start_group(group)
        check("a group whose budget refuses it does not start", False)
    except Exception as exc:  # noqa: BLE001
        check("a group whose budget refuses it does not start",
              "no credit left today" in str(exc), str(exc))

    check("a run that was never started does not report itself as running",
          not group.running and all(not l.running for l in group.legs))
    check("and the runner has forgotten it",
          runner.group(group.id) is None and not runner.all())

    allowed = modes.get("shadow").live_group(
        modes.wire("shadow", "mock"), "football-41", ["de-de"],
        audio=CountingAudio(blocks=60), seconds=30.0,
    )
    try:
        runner.start_group(allowed)
        check("so the very next group on that channel is accepted", True)
    except Exception as exc:  # noqa: BLE001
        check("so the very next group on that channel is accepted", False, str(exc))
    allowed.stop()
    allowed.wait(60)


def test_three_languages_from_one_pull() -> None:
    section("the feature: one channel, three languages, at once")

    group, audio, _, _ = live_group(["el-gr", "de-de", "fr-fr"])
    group.start()
    check("it finishes", group.wait(60), "timed out")

    snap = group.snapshot()
    check("one pull served all three languages",
          len(audio.opens) == 1, f"opened {len(audio.opens)} times")
    check("three legs came back", len(snap["legs"]) == 3)
    check("each leg translated into the language it was given",
          sorted(l["target_language"] for l in snap["legs"])
          == ["de-de", "el-gr", "fr-fr"],
          str([l["target_language"] for l in snap["legs"]]))
    check("every leg produced audio",
          all(l["spoken_seconds"] > 0 for l in snap["legs"]),
          str([l["spoken_seconds"] for l in snap["legs"]]))
    check("every leg finished cleanly",
          all(l["phase"] == "done" for l in snap["legs"]),
          str([(l["phase"], l["error"]) for l in snap["legs"]]))
    # The real invariant, and it took a flaky assertion to find it: the legs
    # may only differ by audio that was *dropped*, and a drop must be visible.
    # The first version of this check demanded they always match, which is
    # true in production -- blocks arrive a tenth of a second apart, so a
    # 24-block queue is over two seconds of slack -- but not in a test, where
    # the fake feed yields as fast as the CPU allows. Under load one leg gets
    # starved of scheduling, its queue overflows and it correctly loses its
    # oldest audio. The assertion was wrong, not the fan-out.
    dropped = sum(p.get("dropped", 0) for p in snap["pull"])
    spread = (max(l["sent_seconds"] for l in snap["legs"])
              - min(l["sent_seconds"] for l in snap["legs"]))
    if dropped:
        check("legs differ only by audio that was dropped, and the drop is "
              "reported rather than silent",
              spread > 0 and dropped > 0,
              f"spread {spread}s with {dropped} dropped")
    else:
        check("with nothing dropped, every leg got exactly the same audio",
              spread < 0.25, str([l["sent_seconds"] for l in snap["legs"]]))

    check("the pull is reported once and the billing three times, because "
          "that is the actual shape of the cost",
          abs(snap["billed_seconds"]
              - sum(l["sent_seconds"] for l in snap["legs"])) < 0.01,
          f"sent {snap['sent_seconds']} billed {snap['billed_seconds']}")
    check("and what was charged is three times one pull, give or take "
          "anything a starved leg dropped",
          snap["billed_seconds"] <= snap["sent_seconds"] * 3 + 0.01,
          f"charged {snap['billed_seconds']} for {snap['sent_seconds']} pulled")


def test_one_language_failing_leaves_the_others_alone() -> None:
    section("a leg that dies takes only its own language down")

    group, _, _, _ = live_group(["el-gr", "de-de"])
    group.start()
    # Kill one leg the way a provider refusal would: from the outside, while
    # the other is working.
    time.sleep(0.05)
    group.legs[0].stop("pretending the model refused this language")
    check("the group still finishes", group.wait(60), "timed out")

    snap = group.snapshot()
    survivors = [l for l in snap["legs"] if l["spoken_seconds"] > 0]
    check("the other language still produced audio",
          any(l["target_language"] == "de-de" and l["spoken_seconds"] > 0
              for l in snap["legs"]),
          str([(l["target_language"], l["spoken_seconds"]) for l in snap["legs"]]))
    check("and the group is not reported as a failure because one leg went",
          not snap["error"], snap["error"])
    check("at least one leg survived", bool(survivors))


def test_stopping_a_group_stops_every_language() -> None:
    section("stop means stop, for all of it")

    group, _, _, _ = live_group(["el-gr", "de-de"], blocks=100_000)
    group.start()
    for _ in range(200):
        if any(l.sent_seconds > 0 for l in group.legs):
            break
        time.sleep(0.01)
    group.stop("stopped from the page")
    check("every leg winds up", group.wait(60), "timed out")
    check("none of them is still running",
          not group.running and all(not l.running for l in group.legs))


def test_the_runner_counts_groups_not_legs() -> None:
    section("a three-language group is one thing somebody started")

    group, _, modes, _ = live_group(["el-gr", "de-de", "fr-fr"], blocks=100_000)
    runner = LiveRunner(None, max_concurrent=1)
    runner.start_group(group)
    check("a group of three starts under a one-at-a-time limit, because the "
          "limit is about channels",
          group.running, "the group did not start")
    check("the runner can find it again", runner.group(group.id) is group)
    check("and reports it as the current group",
          runner.current_group() is group)

    second = modes.get("shadow").live(
        modes.wire("shadow", "mock"), "football-41",
        audio=CountingAudio(blocks=10), target_language="it-it",
    )
    try:
        runner.start(second)
        check("a single run on a channel the group holds is refused", False)
    except AtCapacity as exc:
        check("a single run on a channel the group holds is refused, naming it",
              "football-41" in str(exc), str(exc))

    other = modes.get("shadow").live(
        modes.wire("shadow", "mock"), "football-21",
        audio=CountingAudio(blocks=10), target_language="it-it",
    )
    try:
        runner.start(other)
        check("and so is a second channel, because that is a second pull",
              False)
    except AtCapacity as exc:
        check("and so is a second channel, because that is a second pull",
              "football-41" in str(exc), str(exc))

    group.stop()
    group.wait(60)
    check("every leg was filed as finished, so the group is not left running",
          not group.running)


def test_identity_rides_along() -> None:
    section("what the commentary says the channel is")

    fixtures = [
        SimpleNamespace(id="403", home="Bayern Munich", away="Borussia Dortmund",
                        competition="Bundesliga", phase="live"),
        SimpleNamespace(id="404", home="Arsenal", away="Manchester City",
                        competition="Premier League", phase="live"),
    ]
    group, _, _, _ = live_group(["de-de"])
    group._fixtures = fixtures
    group.configured_language = "en-us"

    ident = group.identity()
    check("identity is present before anything has been heard",
          "language" in ident and "match" in ident, str(ident))
    check("and it does not claim a language from nothing",
          not ident["language"]["tag"], str(ident["language"]))
    check("nor contradict the channel's configuration on no evidence",
          ident["language_matches"] and "warning" not in ident)

    # Now put a transcript in front of it, the way a provider's input
    # transcription would.
    leg = group.legs[0]
    from commentary_qc.translate.app.live import Caption
    leg._captions.append(Caption(
        at=1.0,
        heard="Good evening and welcome to the Allianz Arena for Bayern "
              "Munich against Borussia Dortmund, the biggest fixture in the "
              "German calendar and there is a real atmosphere here tonight.",
        said="Guten Abend",
    ))
    ident = group.identity()
    check("the fixture is named from the team names the commentator said",
          ident["match"].get("id") == "403", str(ident["match"]))
    check("and it says both sides were heard, which is what makes it worth "
          "acting on",
          ident["match"]["both_sides"] and ident["match"]["confidence"] >= 0.9,
          str(ident["match"]))
    check("the language heard is English", ident["language"]["name"] == "English",
          str(ident["language"]))
    check("which agrees with the channel, so no warning",
          ident["language_matches"] and "warning" not in ident)

    group.configured_language = "it-it"
    ident = group.identity()
    check("a channel listed as Italian carrying English is flagged",
          not ident["language_matches"] and "warning" in ident,
          str(ident.get("warning")))
    check("and the warning says both what was listed and what was heard",
          "it-it" in ident["warning"] and "English" in ident["warning"],
          ident["warning"])

    group.stop()
    group.wait(30)


def main() -> int:
    test_one_pull_many_listeners()
    test_three_legs_opening_at_the_same_instant_get_one_pull()
    test_the_listeners_are_lined_up_before_any_audio_is_read()
    test_a_leg_that_never_arrives_does_not_hold_up_the_rest()
    test_different_rates_get_their_own_pull()
    test_a_slow_listener_loses_its_own_audio()
    test_a_dead_pull_reaches_every_listener()
    test_the_url_is_not_remembered_past_the_broadcast()
    test_stop_all()
    test_the_group_refuses_before_it_spends()
    test_the_budget_covers_the_whole_group()
    test_a_group_refused_at_the_last_moment_releases_the_channel()
    test_three_languages_from_one_pull()
    test_one_language_failing_leaves_the_others_alone()
    test_stopping_a_group_stops_every_language()
    test_the_runner_counts_groups_not_legs()
    test_identity_rides_along()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
