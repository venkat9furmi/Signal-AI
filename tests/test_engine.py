"""Rule-engine tests: the thermostat gap and the repeat-based filler rule.

Run: python -m pytest tests -q   (or: python tests/test_engine.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.config import (  # noqa: E402
    WINDOW_SECONDS,
    AnalyzerConfig,
    Thresholds,
)
from commentary_qc.engine import (  # noqa: E402
    REPEAT_EVIDENCE,
    AlertEngine,
)
from commentary_qc.metrics import MetricsWindow, RollingFeatures, loop_score  # noqa: E402


def _window(seq: int, **over) -> MetricsWindow:
    """A healthy live-commentary window, with any field overridden."""
    base = dict(
        seq=seq, t=seq * 1.024, wall=1_000_000.0 + seq, lufs=-24.0, lufs_short=-24.0,
        lufs_minute=-24.0, true_peak_dbtp=-6.0, sample_peak_dbfs=-6.5, clip_run=0,
        speech_ratio=0.65, voice_lufs=-25.0, crowd_lufs=-34.0, voice_to_crowd_db=10.0,
        voice_to_crowd_med=10.0, speech_ratio_med=0.65, level_sd=3.0, crowd_lufs_med=-34.0,
        crowd_measured=True, loop_score=0.25, loop_lag_s=40.0,
    )
    base.update(over)
    return MetricsWindow(**base)


def _run(engine: AlertEngine, levels: list[float], start: int = 0) -> list[tuple[str, int]]:
    events = []
    for i, lvl in enumerate(levels, start):
        for a in engine.push(_window(i, lufs_minute=lvl, lufs_short=lvl)):
            if a.rule == "too_quiet":
                events.append((a.event, i))
    return events


def test_too_quiet_thermostat_holds_between_lines() -> None:
    """Fires below -40, stays up at -39..-38, clears only above -37."""
    th = Thresholds()
    engine = AlertEngine(th, "t")
    ev = _run(engine, [-41.0] * 40)
    assert [e for e, _ in ev] == ["FIRE"], ev
    # Recovered past the fire line but not past the clear line: stays up.
    ev = _run(engine, [-38.5] * 90, start=40)
    assert ev == [], ev
    # Genuinely recovered.
    ev = _run(engine, [-35.0] * 40, start=130)
    assert [e for e, _ in ev] == ["CLEAR"], ev


def test_too_quiet_does_not_flap_on_the_line() -> None:
    """A level wandering either side of -40 gives one alert, not a stream of them."""
    th = Thresholds()
    engine = AlertEngine(th, "t")
    rng = np.random.default_rng(3)
    levels = list(-41.5 + np.zeros(40)) + list(-40.0 + rng.uniform(-1.2, 1.2, 1200))
    ev = _run(engine, levels)
    assert [e for e, _ in ev] == ["FIRE"], ev


def test_thermostat_rejects_inverted_clear_level() -> None:
    try:
        Thresholds(too_quiet_clear_lufs=-45.0)
    except ValueError:
        return
    raise AssertionError("a clear level below the fire level was accepted")


def test_peak_over_is_not_called_distortion() -> None:
    """A -0.8 dBTP over is a warning about headroom; only flat tops are distortion."""
    engine = AlertEngine(Thresholds(), "t")
    fired = []
    for i in range(20):
        for a in engine.push(_window(i, true_peak_dbtp=-0.8, clip_run=0)):
            fired.append((a.event, a.rule))
    assert fired == [("FIRE", "peak_over")], fired
    engine = AlertEngine(Thresholds(), "t")
    fired = []
    for i in range(20):
        for a in engine.push(_window(i, true_peak_dbtp=0.6, clip_run=6)):
            fired.append((a.event, a.rule))
    assert fired == [("FIRE", "clipping")], fired


def test_escalation_reannounces_a_fault_that_gets_worse() -> None:
    """Quiet at -41 fires once; sinking to -48 says so again, and only once."""
    engine = AlertEngine(Thresholds(), "t")
    events = []
    for i, lvl in enumerate([-41.0] * 40 + [-41.5] * 20 + [-48.0] * 20 + [-48.5] * 20):
        for a in engine.push(_window(i, lufs_minute=lvl, lufs_short=lvl)):
            if a.rule == "too_quiet":
                events.append((a.event, a.message))
    kinds = [e for e, _ in events]
    assert kinds == ["FIRE", "ESCALATE"], events
    assert events[1][1].startswith("Getting worse"), events[1][1]


def test_loop_score_separates_a_loop_from_live_audio() -> None:
    rng = np.random.default_rng(5)
    cycle = rng.normal(-22, 4, 37)          # one 37-window playout cycle
    looped = np.tile(cycle, 6)[:180] + rng.normal(0, 0.3, 180)
    live = -24 + np.cumsum(rng.normal(0, 1.2, 180)) * 0.3 + rng.normal(0, 3, 180)
    s_loop, lag = loop_score(looped, 15, 90)
    s_live, _ = loop_score(live, 15, 90)
    assert s_loop > 0.9 and lag in (37, 74), (s_loop, lag)
    assert s_live < 0.6, s_live


def test_quiet_speechless_audio_is_not_called_a_clip() -> None:
    """German Ch.44, 2026-09-18: -41 LUFS, no speech, repeat 0.67.

    Calling that a clip would also mask its "too quiet" alert, which is the
    thing somebody actually needs to hear.
    """
    th = Thresholds()
    engine = AlertEngine(th, "t")
    rules = []
    for i in range(300):
        w = _window(i, lufs_minute=-41.4, lufs_short=-41.6, speech_ratio=0.0,
                    speech_ratio_med=0.0, crowd_lufs=-41.6, loop_score=0.67, loop_lag_s=30.0)
        rules += [(a.event, a.rule) for a in engine.push(w)]
    assert ("FIRE", "filler_playing") not in rules, rules
    assert ("FIRE", "too_quiet") in rules, rules


def test_filler_rule_fires_on_a_loop_with_crowd() -> None:
    """The case the old rule missed: a loop with a loud crowd bed in it."""
    cfg = AnalyzerConfig()
    th = Thresholds()
    rng = np.random.default_rng(9)
    cycle = rng.normal(-19, 3, 37)
    roll = RollingFeatures(cfg)
    engine = AlertEngine(th, "t")
    fired = None
    for i in range(420):
        lvl = float(cycle[i % 37] + rng.normal(0, 0.2))
        f = roll.update(lvl, 0.91, -18.5, -30.0, judgeable=True)
        w = _window(i, lufs_short=lvl, crowd_lufs=-18.5, **f)
        for a in engine.push(w):
            if a.rule == "filler_playing" and a.event == "FIRE":
                fired = a
    assert fired is not None, "filler_playing never fired on a crowd-bearing loop"
    assert "repeats" in fired.message, fired.message


def test_a_repeated_identical_fault_is_read_as_a_recording() -> None:
    """The Ch.31 incident, 7 Nov, and the fix for it.

    `clipping` fired seven times with an *identical* message -- "17 samples at
    full scale, true peak +2.5 dBTP" -- about every 30 seconds, while
    `filler_playing` on the same channel only caught up six minutes later. The
    identical numbers are MMC's own holding clip, so for six minutes a
    recording's distortion was reported as the commentator's fault.

    Real speech does not peak at exactly the same sample count twice. So the
    same message, three times over, is proof enough to pause the rules a clip
    can fake -- without waiting for the loudness-envelope test, which needs
    three minutes of history and never covers a channel that was live and then
    switched to a clip.
    """
    th = Thresholds()
    engine = AlertEngine(th, "football-31")

    # Exactly the clip's signature, and deliberately NOT looking like filler
    # to the repeat test: loop_score stays at a live-commentary value, which
    # is the situation that defeated the old masking.
    clip = dict(clip_run=17, true_peak_dbtp=2.5, loop_score=0.30, loop_lag_s=44.0)

    fires, notices, seq = 0, 0, 0
    for _ in range(7):                      # seven cycles, as observed
        for _ in range(8):                  # hold past the 3 s attack
            seq += 1
            for a in engine.push(_window(seq, **clip)):
                if a.rule == "clipping" and a.event == "FIRE":
                    fires += 1
                if a.rule == "clip_repeat":
                    notices += 1
        # the clip comes round again: the fault goes away, then returns
        for _ in range(20):
            seq += 1
            for a in engine.push(_window(seq, loop_score=0.30, loop_lag_s=44.0)):
                pass

    assert fires <= REPEAT_EVIDENCE - 1, (
        f"blamed the commentator {fires} times; at most "
        f"{REPEAT_EVIDENCE - 1} should escape before the repeat is proof"
    )
    assert notices == 1, f"expected one explanation, got {notices}"

    # And once latched, the rules a clip can fake stay quiet.
    before = seq
    extra = 0
    for _ in range(40):
        seq += 1
        for a in engine.push(_window(seq, **clip)):
            if a.rule == "clipping" and a.event == "FIRE":
                extra += 1
    assert extra == 0, f"{extra} more clipping alerts after the verdict"
    assert seq > before


def test_a_changing_fault_is_still_reported() -> None:
    """The counterpart: a fault whose numbers move is a live one.

    Without this the fix would be a way to silence any persistent fault, which
    is worse than the bug it replaces.
    """
    th = Thresholds()
    engine = AlertEngine(th, "football-41")
    fires, seq = 0, 0
    for run_length in (12, 14, 16, 18, 20, 22, 24):
        for _ in range(8):
            seq += 1
            # Different sample count and peak each time, as real speech is.
            for a in engine.push(_window(
                seq, clip_run=run_length,
                true_peak_dbtp=1.0 + run_length / 10.0,
                loop_score=0.30, loop_lag_s=44.0,
            )):
                if a.rule == "clipping" and a.event == "FIRE":
                    fires += 1
        for _ in range(20):
            seq += 1
            for a in engine.push(_window(seq, loop_score=0.30, loop_lag_s=44.0)):
                pass
    assert fires >= 3, f"a changing fault was silenced after {fires} reports"


def test_dead_air_is_never_masked_by_the_repeat_verdict() -> None:
    """Silence and stream loss matter whatever is playing.

    A clip that has gone quiet is still a channel with no audio going to
    bet365, so the one thing the verdict must not do is hide that.
    """
    th = Thresholds()
    engine = AlertEngine(th, "football-31")
    clip = dict(clip_run=17, true_peak_dbtp=2.5, loop_score=0.30, loop_lag_s=44.0)
    seq = 0
    for _ in range(4):
        for _ in range(8):
            seq += 1
            engine.push(_window(seq, **clip))
        for _ in range(20):
            seq += 1
            engine.push(_window(seq, loop_score=0.30, loop_lag_s=44.0))
    assert engine._repeat_until > 0, "the verdict should be latched by now"

    silent = 0
    for _ in range(40):
        seq += 1
        for a in engine.push(_window(
            seq, lufs=-70.0, lufs_short=-70.0, lufs_minute=-70.0,
            speech_ratio=0.0, speech_ratio_med=0.0,
            loop_score=0.30, loop_lag_s=44.0,
        )):
            if a.rule == "silence" and a.event == "FIRE":
                silent += 1
    assert silent == 1, f"dead air was masked by the recording verdict ({silent})"



# -- a capture gap, which is not a fault until it lasts --------------------
def test_a_short_gap_is_not_reported_at_all() -> None:
    """The median real outage lasted 9 seconds.

    Measured over 2,010 of them to 2026-11-12: 53% were over inside fifteen
    seconds, and 556 were our own container failing to resolve the vendor's
    hostname. Firing on the first gap block paged a commentator about a
    reconnect that had already finished, for a fault that was often not
    theirs and sometimes not a broadcast fault at all.
    """
    engine = AlertEngine(Thresholds(), "t")
    assert engine.on_gap(1000.0, "stalled") == [], "fired on the first gap block"
    assert engine.on_gap(1009.0, "reconnect in 1s") == [], "fired at 9 s"
    back = engine.on_reconnect(1010.0)
    assert back is None, "cleared an alert that was never sent"


def test_a_gap_that_lasts_is_reported_once() -> None:
    th = Thresholds()
    hold = th.attack_s["stream_down"]
    engine = AlertEngine(th, "t")
    engine.on_gap(1000.0, "stalled")
    assert engine.on_gap(1000.0 + hold - 1, "reconnect in 8s") == []
    fired = engine.on_gap(1000.0 + hold + 1, "Failed to resolve hostname")
    downs = [a for a in fired if a.rule == "stream_down" and a.event == "FIRE"]
    assert len(downs) == 1, f"expected one stream_down, got {len(downs)}"
    # The latest reason wins: "stalled" usually becomes a named error on the
    # next attempt, and the named one is the one worth reading.
    assert "resolve hostname" in downs[0].message, downs[0].message
    assert downs[0].evidence["down_s"] >= hold

    again = engine.on_gap(1000.0 + hold + 30, "reconnect in 30s")
    assert not [a for a in again if a.rule == "stream_down"], "fired twice for one outage"

    back = engine.on_reconnect(1000.0 + hold + 60)
    assert back is not None and back.event == "CLEAR", "a reported outage never cleared"


def test_a_short_gap_does_not_clear_a_firing_fault() -> None:
    """A nine-second blip must not clear a fault that is genuinely up.

    Clearing on the first gap block meant a real "too quiet" was cleared and
    then re-fired a moment later, which reads as two faults rather than one.
    """
    th = Thresholds()
    engine = AlertEngine(th, "t")
    seq = 0
    for _ in range(int(th.attack_s["too_quiet"] / WINDOW_SECONDS) + 4):
        seq += 1
        engine.push(_window(seq, lufs=-44.0, lufs_short=-44.0, lufs_minute=-44.0))
    assert "too_quiet" in engine.active_alerts(), "the fault never fired"

    engine.on_gap(5000.0, "stalled")
    engine.on_gap(5009.0, "reconnect in 1s")
    assert "too_quiet" in engine.active_alerts(), "a 9 s blip cleared a real fault"
    assert engine.on_reconnect(5010.0) is None

    engine.on_gap(6000.0, "stalled")
    out = engine.on_gap(6000.0 + th.attack_s["stream_down"] + 1, "eof")
    assert any(a.rule == "too_quiet" and a.event == "CLEAR" for a in out),         "a real outage should clear the quality rules, whose inputs are stale"




# -- escalation: "worse" has to mean the same thing on every scale ---------
def test_dead_air_never_escalates() -> None:
    """A dead channel sinking further is not a deteriorating fault.

    `silence` escalated 73 times on 293 fires in the two months to
    2026-11-12 -- a quarter of them -- because a feed drifting from -50 to
    -56 LUFS trips the 6 dB step. It is already silent. There is no worse,
    and nobody can act on the difference.
    """
    engine = AlertEngine(Thresholds(), "t")
    events = []
    for i, lvl in enumerate([-55.0] * 30 + [-70.0] * 30 + [-90.0] * 30):
        for a in engine.push(_window(i, lufs_minute=lvl, lufs_short=lvl,
                                     speech_ratio=0.0)):
            if a.rule == "silence":
                events.append(a.event)
    assert "FIRE" in events, "dead air should still be reported"
    assert "ESCALATE" not in events, f"dead air escalated: {events}"


def test_the_balance_rules_use_their_own_step() -> None:
    """Voice-over-crowd spans 1.4 to 33.4 dB; loudness spans a few.

    `crowd_dominating` escalated on 153 of its 212 fires -- 72% -- because
    one global 6 dB step is ordinary variation on that scale. The step is
    per rule now, so "getting worse" means a comparable amount of worse
    whichever rule says it.
    """
    th = Thresholds()
    engine = AlertEngine(th, "t")
    assert engine._step("crowd_dominating") > engine._step("too_quiet"), \
        "the balance rules need a wider step than the loudness rules"

    # Buried by 5.0 dB, then by 11.0: a 6 dB slide, which used to escalate
    # and now must not, because it is within normal variation for this
    # measure.
    events = []
    for i, bal in enumerate([5.0] * 40 + [-1.0] * 40):
        for a in engine.push(_window(
            i, voice_to_crowd_med=bal, voice_to_crowd_db=bal,
            speech_ratio=0.8, crowd_measured=True, crowd_lufs=-30.0,
        )):
            if a.rule == "crowd_dominating":
                events.append(a.event)
    assert events[:1] == ["FIRE"], events
    assert "ESCALATE" not in events, f"a 6 dB slide still escalated: {events}"


def test_a_genuine_collapse_still_escalates() -> None:
    """The wider step must not become a way to silence a real collapse.

    Ch.31 on 2026-11-07 went from -12.3 dB to -30.0 dB buried in the crowd
    and the escalation fired correctly. That is 17.7 dB and must still be
    reported.
    """
    engine = AlertEngine(Thresholds(), "t")
    events = []
    for i, bal in enumerate([-12.3] * 40 + [-30.0] * 40):
        for a in engine.push(_window(
            i, voice_to_crowd_med=bal, voice_to_crowd_db=bal,
            speech_ratio=0.8, crowd_measured=True, crowd_lufs=-30.0,
        )):
            if a.rule == "crowd_dominating":
                events.append((a.event, a.message))
    kinds = [e for e, _ in events]
    assert kinds[:2] == ["FIRE", "ESCALATE"], events
    assert events[1][1].startswith("Getting worse"), events[1][1]


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception:
                failures += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
