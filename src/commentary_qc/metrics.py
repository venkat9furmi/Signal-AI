"""Per-window analysis: loudness, peaks, and VAD-gated voice/crowd separation.

The feed is confirmed MONO (44.1 kHz AAC, 1 channel), so the mid/side trick is
unavailable and separation has to come from the VAD: speech-labelled samples
carry voice+crowd, gap-labelled samples carry crowd alone, and voice power is
the difference. Every level is measured on the K-weighted signal so voice,
crowd and programme loudness are all in the same unit (LUFS) and directly
comparable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Iterator

import numpy as np
from scipy import signal

from .config import (
    ANALYSIS_RATE,
    DECIM,
    VAD_CHUNK_16K,
    WINDOW_SAMPLES,
    WINDOW_SECONDS,
    AnalyzerConfig,
)
from .loudness import (
    KWeighting,
    TruePeakMeter,
    loudness_from_ms,
    longest_clip_run,
    power_to_db,
)
from .vad import make_vad


@dataclass(slots=True)
class MetricsWindow:
    """One analysis window. Every field is logged for later model training."""

    seq: int
    #: Media time of the window start, in seconds since connect.
    t: float
    #: Wall-clock epoch seconds, for correlating with the match timeline.
    wall: float

    #: Programme loudness over this window (1.024 s), LUFS.
    lufs: float
    #: EBU R128 short-term loudness (~3 s trailing), LUFS.
    lufs_short: float
    #: Median short-term loudness over the last minute. The loudness rules
    #: judge this, not the 3-second figure, which moves with every shout and
    #: pause.
    lufs_minute: float

    true_peak_dbtp: float
    sample_peak_dbfs: float
    clip_run: int

    #: Fraction of the window the VAD labelled speech.
    speech_ratio: float
    #: Level of the estimated commentator voice, LUFS.
    voice_lufs: float
    #: Level of the crowd/atmosphere bed, LUFS.
    crowd_lufs: float
    #: voice power - crowd power, dB. The headroom the commentator has over
    #: the crowd; small or negative means buried.
    voice_to_crowd_db: float
    #: Rolling median of the above over recent judgeable windows. This is what
    #: the balance rules use -- the single-window value swings too much to hold
    #: a multi-second attack timer.
    voice_to_crowd_med: float
    #: Rolling median speech ratio (~60 s). Near 1.0 means continuous talking
    #: with no breathing pauses, which is a recording rather than a person.
    speech_ratio_med: float
    #: Standard deviation of short-term loudness over ~60 s. Live audio moves;
    #: a looping file is unnaturally steady.
    level_sd: float
    #: Rolling median crowd level (~60 s). The per-window figure wanders above
    #: the filler threshold often enough that a continuous-hold rule could
    #: never fire on it; the median is what the state judgement needs.
    crowd_lufs_med: float
    #: False when this window had no speech gaps and the crowd figure is
    #: carried forward from earlier windows.
    crowd_measured: bool
    #: How strongly the last ~3 minutes of loudness repeat themselves, 0-1.
    #: Filler loops score 0.8-1.0, live commentary 0.2-0.5. See loop_score().
    loop_score: float = 0.0
    #: The cycle length the repeat was found at, seconds (0 = none yet).
    loop_lag_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class _Decimator:
    """Streaming 48 kHz -> 16 kHz anti-aliased decimator.

    Filter state carries across windows so the VAD sees one continuous signal
    rather than 1-second segments with independent edge transients.
    """

    def __init__(self, factor: int = DECIM, taps: int = 96) -> None:
        self._factor = factor
        self._b = signal.firwin(taps, 1.0 / factor).astype(np.float64)
        self._zi = np.zeros(taps - 1, dtype=np.float64)
        self._phase = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = signal.lfilter(self._b, [1.0], np.asarray(x, dtype=np.float64), zi=self._zi)
        out = y[self._phase :: self._factor]
        # Preserve subsampling phase across blocks of arbitrary length.
        consumed = len(y) - self._phase
        self._phase = (-consumed) % self._factor
        return out.astype(np.float32)

    def reset(self) -> None:
        self._zi[:] = 0.0
        self._phase = 0


def loop_score(envelope, min_lag: int, max_lag: int) -> tuple[float, int]:
    """How strongly a loudness envelope repeats itself, and at what lag.

    Returns the highest Pearson correlation between the envelope and a copy of
    itself shifted by ``min_lag``..``max_lag`` windows, with the lag it occurred
    at. A playout loop replays the same pauses and swells on a fixed cycle, so
    its envelope lines up with itself almost perfectly (0.8-1.0 measured); a
    live commentator never repeats their own rhythm (0.2-0.5).

    This is the loudness *envelope*, not the waveform: the loops on this
    platform change content from cycle to cycle (waveform correlation 0.015 at
    the 36.7 s cycle on football-01) while the pattern of speech and gaps stays
    locked.
    """
    e = np.asarray(envelope, dtype=np.float64)
    best, best_lag = 0.0, 0
    for k in range(min_lag, min(max_lag, len(e) // 2) + 1):
        a, b = e[:-k], e[k:]
        sa, sb = a.std(), b.std()
        # A flat stretch has no pattern to match; silence is the silence rule's job.
        if sa < 1e-3 or sb < 1e-3:
            continue
        r = float(np.mean((a - a.mean()) * (b - b.mean())) / (sa * sb))
        if r > best:
            best, best_lag = r, k
    return best, best_lag


#: Floor for the loop envelope. Digital silence measures far below anything
#: audible; letting that into a correlation makes every pause the dominant
#: feature of the pattern.
_ENVELOPE_FLOOR = -70.0


class RollingFeatures:
    """The slow, multi-window features the rules judge.

    Kept apart from the per-window DSP so ``tools/replay_alerts.py`` can rebuild
    them from stored metrics and judge an old recording exactly as the live
    analyzer would today.
    """

    def __init__(self, cfg: AnalyzerConfig) -> None:
        self.cfg = cfg
        self._minute = deque(maxlen=max(1, cfg.loudness_minute_windows))
        self._balance = deque(maxlen=max(1, cfg.balance_median_windows))
        self._level = deque(maxlen=max(2, cfg.commentary_window))
        self._speech = deque(maxlen=max(2, cfg.commentary_window))
        self._crowd = deque(maxlen=max(2, cfg.commentary_window))
        self._envelope = deque(maxlen=max(2, cfg.loop_history_windows))
        self._loop: tuple[float, int] = (0.0, 0)
        self._n = 0

    def reset(self) -> None:
        for d in (self._minute, self._balance, self._level, self._speech,
                  self._crowd, self._envelope):
            d.clear()
        self._loop = (0.0, 0)
        self._n = 0

    def update(
        self, lufs_short: float, speech_ratio: float, crowd_lufs: float,
        vc_db: float, judgeable: bool,
    ) -> dict:
        cfg = self.cfg
        self._n += 1
        self._minute.append(lufs_short)
        # Only windows that can actually be judged feed the balance median: a
        # window with no speech, or none of the gaps needed to see the crowd,
        # says nothing about the balance and would drag the median around.
        if judgeable:
            self._balance.append(vc_db)
        self._level.append(lufs_short)
        self._speech.append(speech_ratio)
        self._crowd.append(crowd_lufs)
        self._envelope.append(max(lufs_short, _ENVELOPE_FLOOR))

        # Needs a full window before it means anything; until then report a
        # variability high enough that the filler rule cannot fire on it.
        full = len(self._level) == self._level.maxlen
        # The repeat test needs its whole history too.
        if len(self._envelope) < self._envelope.maxlen:
            self._loop = (0.0, 0)
        elif self._n % max(1, cfg.loop_every_windows) == 0:
            self._loop = loop_score(
                self._envelope, cfg.loop_min_lag_windows, cfg.loop_max_lag_windows
            )
        return {
            "lufs_minute": float(np.median(self._minute)),
            "voice_to_crowd_med": float(np.median(self._balance)) if self._balance else vc_db,
            "speech_ratio_med": float(np.median(self._speech)),
            "level_sd": float(np.std(self._level)) if full else 99.0,
            "crowd_lufs_med": float(np.median(self._crowd)),
            "loop_score": self._loop[0],
            "loop_lag_s": self._loop[1] * WINDOW_SECONDS,
        }


class WindowAnalyzer:
    """Turns a stream of audio blocks into :class:`MetricsWindow` records."""

    def __init__(self, cfg: AnalyzerConfig, rate: int = ANALYSIS_RATE) -> None:
        self.cfg = cfg
        self.rate = rate
        self.vad = make_vad(
            cfg.vad_backend, cfg.vad_model_path, cfg.vad_threshold, cfg.webrtc_aggressiveness
        )
        self.vad_name = type(self.vad).__name__
        self._kw = KWeighting(rate)
        self._tp = TruePeakMeter(rate)
        self._decim = _Decimator()

        self._buf = np.zeros(0, dtype=np.float32)
        self._kbuf = np.zeros(0, dtype=np.float64)
        self._vbuf = np.zeros(0, dtype=np.float32)

        self._short = deque(maxlen=max(1, cfg.short_term_windows))
        self._roll = RollingFeatures(cfg)
        self._crowd_ms: float | None = None
        self._seq = 0
        self._samples_seen = 0

    def reset(self) -> None:
        """Clear all stream state. Called after a reconnect."""
        self.vad.reset()
        self._kw.reset()
        self._tp.reset()
        self._decim.reset()
        self._buf = np.zeros(0, dtype=np.float32)
        self._kbuf = np.zeros(0, dtype=np.float64)
        self._vbuf = np.zeros(0, dtype=np.float32)
        self._short.clear()
        self._roll.reset()
        self._crowd_ms = None
        self._samples_seen = 0

    def push(self, block: np.ndarray, wall: float) -> Iterator[MetricsWindow]:
        """Feed one block of float32 mono audio; yield any completed windows."""
        self._buf = np.concatenate([self._buf, np.asarray(block, dtype=np.float32)])
        # K-weighting and decimation are continuous filters, so they run on the
        # arriving block, not on window-aligned slices.
        self._kbuf = np.concatenate([self._kbuf, self._kw(block)])
        self._vbuf = np.concatenate([self._vbuf, self._decim(block)])

        vad_needed = WINDOW_SAMPLES // DECIM
        while len(self._buf) >= WINDOW_SAMPLES and len(self._vbuf) >= vad_needed:
            raw = self._buf[:WINDOW_SAMPLES]
            kw = self._kbuf[:WINDOW_SAMPLES]
            vad_pcm = self._vbuf[:vad_needed]
            self._buf = self._buf[WINDOW_SAMPLES:]
            self._kbuf = self._kbuf[WINDOW_SAMPLES:]
            self._vbuf = self._vbuf[vad_needed:]
            yield self._analyze(raw, kw, vad_pcm, wall)

    def _analyze(
        self, raw: np.ndarray, kw: np.ndarray, vad_pcm: np.ndarray, wall: float
    ) -> MetricsWindow:
        t = self._samples_seen / self.rate
        self._samples_seen += WINDOW_SAMPLES

        # --- programme loudness and peaks --------------------------------
        ms = float(np.mean(kw * kw))
        self._short.append(ms)
        lufs = loudness_from_ms(ms)
        lufs_short = loudness_from_ms(float(np.mean(self._short)))

        true_peak = self._tp.measure(raw)
        sample_peak = 20.0 * np.log10(max(float(np.max(np.abs(raw))), 1e-20))
        clip_run = longest_clip_run(raw)

        # --- VAD gating ---------------------------------------------------
        n_chunks = len(vad_pcm) // VAD_CHUNK_16K
        flags = np.zeros(n_chunks, dtype=bool)
        for i in range(n_chunks):
            chunk = vad_pcm[i * VAD_CHUNK_16K : (i + 1) * VAD_CHUNK_16K]
            flags[i] = self.vad.speech_prob(chunk) >= self.cfg.vad_threshold
        speech_ratio = float(flags.mean()) if n_chunks else 0.0

        # Expand the 16 kHz chunk labels onto the 48 kHz K-weighted signal.
        mask = np.repeat(flags, VAD_CHUNK_16K * DECIM)
        if mask.size < kw.size:
            mask = np.concatenate([mask, np.zeros(kw.size - mask.size, dtype=bool)])
        mask = mask[: kw.size]

        power = kw * kw
        speech_ms = float(power[mask].mean()) if mask.any() else 0.0
        gap_ms = float(power[~mask].mean()) if (~mask).any() else 0.0

        # --- crowd floor ---------------------------------------------------
        # Crowd is only observable in the gaps. In a window with no gaps we
        # carry the previous estimate forward rather than inventing one, and
        # flag the window so the rules can discount it.
        crowd_measured = bool((~mask).any())
        if crowd_measured:
            if self._crowd_ms is None:
                self._crowd_ms = gap_ms
            else:
                a = self.cfg.crowd_ema
                self._crowd_ms = a * self._crowd_ms + (1.0 - a) * gap_ms
        crowd_ms = self._crowd_ms if self._crowd_ms is not None else 0.0

        # Speech-labelled samples carry voice *on top of* the crowd bed, so the
        # crowd power is subtracted out. Clamped at zero: when the VAD labels a
        # loud crowd swell as speech the difference can go negative, which means
        # "no voice detectable above the crowd", not negative energy.
        voice_ms = max(speech_ms - crowd_ms, 0.0) if mask.any() else 0.0

        # Clamp to a readable range. When no voice is detectable above the
        # crowd the raw ratio collapses toward the -200 dB log floor, which is
        # meaningless to report and, worse, drags the rolling median with it.
        # Both rule thresholds sit well inside these bounds, so clamping cannot
        # change a decision -- only what gets displayed and averaged.
        vc_db = max(-30.0, min(60.0, power_to_db(voice_ms) - power_to_db(crowd_ms)))
        crowd_lufs = loudness_from_ms(crowd_ms)

        roll = self._roll.update(
            lufs_short, speech_ratio, crowd_lufs, vc_db,
            judgeable=crowd_measured and speech_ratio > 0.15,
        )

        self._seq += 1
        return MetricsWindow(
            seq=self._seq,
            t=round(t, 3),
            wall=round(wall, 3),
            lufs=round(lufs, 2),
            lufs_short=round(lufs_short, 2),
            lufs_minute=round(roll["lufs_minute"], 2),
            true_peak_dbtp=round(true_peak, 2),
            sample_peak_dbfs=round(sample_peak, 2),
            clip_run=clip_run,
            speech_ratio=round(speech_ratio, 3),
            voice_lufs=round(loudness_from_ms(voice_ms), 2),
            crowd_lufs=round(crowd_lufs, 2),
            voice_to_crowd_db=round(vc_db, 2),
            voice_to_crowd_med=round(roll["voice_to_crowd_med"], 2),
            speech_ratio_med=round(roll["speech_ratio_med"], 3),
            level_sd=round(roll["level_sd"], 2),
            crowd_lufs_med=round(roll["crowd_lufs_med"], 2),
            crowd_measured=crowd_measured,
            loop_score=round(roll["loop_score"], 3),
            loop_lag_s=round(roll["loop_lag_s"], 1),
        )
