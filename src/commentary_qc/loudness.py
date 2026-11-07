"""Streaming loudness and peak measurement (ITU-R BS.1770-4).

Why this exists instead of a direct ``pyloudnorm.Meter.integrated_loudness``
call per window: that API is built for offline blocks and resets the
K-weighting filters on every call. Resetting a highpass every second injects a
transient at each window boundary and corrupts exactly the low-level
measurements dead-air detection depends on. Here the filter state carries
across windows, so the measurement is continuous for the life of the stream.

pyloudnorm is still used for what it is good at: designing the rate-correct
K-weighting coefficients.
"""

from __future__ import annotations

import numpy as np
import pyloudnorm as pyln
from scipy import signal

#: BS.1770 mean-square to LKFS offset for a single (mono) channel at unity
#: channel weight.
_ABS_OFFSET = -0.691

#: Floor for log conversions, ~-200 LUFS. Digital-black input must produce a
#: very negative number rather than -inf, which would poison JSON output.
_EPS = 1e-20


class KWeighting:
    """Continuous BS.1770 K-weighting filter (high shelf + highpass)."""

    def __init__(self, rate: int) -> None:
        meter = pyln.Meter(rate)
        self._stages = []
        for name in ("high_shelf", "high_pass"):
            filt = meter._filters[name]
            b = np.asarray(filt.b, dtype=np.float64)
            a = np.asarray(filt.a, dtype=np.float64)
            self._stages.append([b, a, signal.lfilter_zi(b, a) * 0.0])

    def __call__(self, x: np.ndarray) -> np.ndarray:
        """K-weight a block, carrying filter state from the previous block."""
        y = np.asarray(x, dtype=np.float64)
        for stage in self._stages:
            b, a, zi = stage
            y, stage[2] = signal.lfilter(b, a, y, zi=zi)
        return y

    def reset(self) -> None:
        for stage in self._stages:
            stage[2] = stage[2] * 0.0


def loudness_from_ms(mean_square: float) -> float:
    """Convert a K-weighted mean square to LUFS (mono, unity channel weight)."""
    return _ABS_OFFSET + 10.0 * np.log10(max(float(mean_square), _EPS))


def power_to_db(power: float) -> float:
    return 10.0 * np.log10(max(float(power), _EPS))


class TruePeakMeter:
    """4x-oversampling true-peak meter per BS.1770-4 Annex 2.

    Sample peak alone misses inter-sample overs, which is the failure mode that
    matters here: the measured feed peaks at +1.0 dBTP while never exceeding
    0 dBFS at the sample level. A downstream encoder clips that; a sample-peak
    meter would report the feed as clean.
    """

    #: BS.1770 requires at least 4x for rates up to 48 kHz.
    OVERSAMPLE = 4

    def __init__(self, rate: int) -> None:
        # 32 taps per phase is comfortably above the BS.1770 reference filter
        # and cheap at these block sizes.
        self._up = signal.firwin(
            32 * self.OVERSAMPLE, 1.0 / self.OVERSAMPLE, window="blackmanharris"
        ).astype(np.float64)
        self._up *= self.OVERSAMPLE
        self._tail = np.zeros(0, dtype=np.float64)

    def measure(self, x: np.ndarray) -> float:
        """Return the block's true peak in dBTP.

        The previous block's tail is prepended so peaks that straddle a block
        boundary are not lost, then discarded from the result.
        """
        xs = np.asarray(x, dtype=np.float64)
        pad = len(self._tail)
        joined = np.concatenate([self._tail, xs]) if pad else xs
        up = signal.upfirdn(self._up, joined, up=self.OVERSAMPLE, down=1)
        # Drop the region already covered by the previous call.
        skip = pad * self.OVERSAMPLE
        peak = float(np.max(np.abs(up[skip:]))) if up[skip:].size else 0.0
        self._tail = xs[-32:].copy()
        return 20.0 * np.log10(max(peak, _EPS))

    def reset(self) -> None:
        self._tail = np.zeros(0, dtype=np.float64)


def longest_clip_run(x: np.ndarray, level: float = 0.9995) -> int:
    """Longest run of consecutive samples pinned at full scale.

    Distinguishes real hard clipping (a flat-topped waveform) from an isolated
    full-scale sample, which is normal.
    """
    at_rail = np.abs(np.asarray(x)) >= level
    if not at_rail.any():
        return 0
    # Run-length encode the boolean array via change points.
    idx = np.flatnonzero(np.diff(at_rail.astype(np.int8)))
    bounds = np.concatenate([[0], idx + 1, [at_rail.size]])
    runs = np.diff(bounds)
    return int(runs[at_rail[bounds[:-1]]].max())
