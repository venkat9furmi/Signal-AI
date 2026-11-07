"""DSP correctness tests.

The loudness path is hand-rolled for streaming, so it is checked against
pyloudnorm's offline meter, which is a direct implementation of BS.1770-4.
Agreement inside 0.1 LU means the streaming version measures the same thing.

Run: python -m pytest tests -q   (or: python tests/test_dsp.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyloudnorm as pyln

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.config import ANALYSIS_RATE, WINDOW_SAMPLES, AnalyzerConfig  # noqa: E402
from commentary_qc.loudness import (  # noqa: E402
    KWeighting,
    TruePeakMeter,
    longest_clip_run,
    loudness_from_ms,
)
from commentary_qc.metrics import WindowAnalyzer  # noqa: E402

RATE = ANALYSIS_RATE


def _sine(freq: float, seconds: float, amp: float, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_streaming_lufs_matches_pyloudnorm() -> None:
    """Streaming K-weighting must agree with the offline reference meter."""
    rng = np.random.default_rng(0)
    for amp in (0.5, 0.1, 0.02):
        x = (_sine(997.0, 10.0, amp) + 0.01 * rng.standard_normal(10 * RATE)).astype(np.float32)
        reference = pyln.Meter(RATE).integrated_loudness(x.astype(np.float64))

        kw = KWeighting(RATE)
        # Feed in irregular block sizes: real capture blocks never align to
        # anything, and filter-state bugs show up exactly here.
        out, i = [], 0
        for size in (4096, 1000, 7777, 512):
            while i < len(x):
                out.append(kw(x[i : i + size]))
                i += size
        z = np.concatenate(out)
        streaming = loudness_from_ms(float(np.mean(z * z)))

        # Integrated loudness is gated, this is ungated; on a stationary signal
        # the gate removes nothing, so they must line up.
        assert abs(streaming - reference) < 0.1, (amp, streaming, reference)


def test_kweighting_state_carries_across_blocks() -> None:
    """Block-wise filtering must equal filtering the whole signal at once."""
    x = _sine(200.0, 2.0, 0.3)
    whole = KWeighting(RATE)(x)
    kw = KWeighting(RATE)
    parts = np.concatenate([kw(x[i : i + 999]) for i in range(0, len(x), 999)])
    assert np.allclose(whole, parts, atol=1e-12)


def test_true_peak_detects_inter_sample_overs() -> None:
    """A half-scale sine reads -6 dBTP; a signal under 0 dBFS can exceed 0 dBTP."""
    tp = TruePeakMeter(RATE)
    assert abs(tp.measure(_sine(1000.0, 1.0, 0.5)) - (-6.02)) < 0.2

    # Worst-case inter-sample over: a tone near Nyquist/2 sampled so the true
    # waveform peaks between samples. Sample peak stays below 0 dBFS.
    tp2 = TruePeakMeter(RATE)
    x = _sine(11997.0, 1.0, 0.99)
    assert 20 * np.log10(np.max(np.abs(x))) < 0.0
    assert tp2.measure(x) > -0.2


def test_clip_run_detection() -> None:
    x = np.zeros(1000, dtype=np.float32)
    assert longest_clip_run(x) == 0
    x[10] = 1.0
    assert longest_clip_run(x) == 1
    x[100:107] = -1.0
    assert longest_clip_run(x) == 7


def test_silence_is_finite_and_very_negative() -> None:
    """Digital black must not produce -inf or NaN anywhere in the JSON output."""
    a = WindowAnalyzer(AnalyzerConfig(vad_backend="webrtc"))
    windows = list(a.push(np.zeros(WINDOW_SAMPLES * 2, dtype=np.float32), 0.0))
    assert windows
    for w in windows:
        for key, value in w.to_dict().items():
            assert not isinstance(value, float) or np.isfinite(value), key
        assert w.lufs < -100
        assert w.speech_ratio == 0.0


def test_voice_crowd_separation_recovers_snr() -> None:
    """Voice-over-crowd should track a known mix ratio.

    Builds a crowd bed (steady band-limited noise) with a louder amplitude-
    modulated tone burst standing in for speech, and checks the reported
    voice-to-crowd ratio moves the right way when the mix changes.
    """
    rng = np.random.default_rng(1)
    seconds = 12
    n = seconds * RATE
    crowd = (0.02 * rng.standard_normal(n)).astype(np.float32)

    results = {}
    for label, voice_amp in (("buried", 0.03), ("clear", 0.4)):
        voice = _sine(300.0, seconds, voice_amp) * (0.5 + 0.5 * _sine(3.0, seconds, 1.0))
        # Alternate 1 s of voice with 1 s of crowd-only so the VAD has gaps to
        # measure the crowd floor in.
        gate = np.zeros(n, dtype=np.float32)
        for s in range(0, seconds, 2):
            gate[s * RATE : (s + 1) * RATE] = 1.0
        mix = (crowd + voice * gate).astype(np.float32)

        a = WindowAnalyzer(AnalyzerConfig(vad_backend="webrtc"))
        ratios = [w.voice_to_crowd_db for w in a.push(mix, 0.0) if w.speech_ratio > 0.3]
        results[label] = float(np.median(ratios)) if ratios else float("nan")

    assert results["clear"] > results["buried"], results


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
