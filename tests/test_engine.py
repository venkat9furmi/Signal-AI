"""Rule-engine tests: the thermostat gap and the repeat-based filler rule.

Run: python -m pytest tests -q   (or: python tests/test_engine.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.config import AnalyzerConfig, Thresholds  # noqa: E402
from commentary_qc.engine import AlertEngine  # noqa: E402
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
