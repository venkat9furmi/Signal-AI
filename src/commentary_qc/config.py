"""Configuration for the commentary QC engine.

Every tunable lives in this module. The control plane (FastAPI + Postgres, later)
retunes a running deployment by rewriting these dataclasses, so nothing here may
be hardcoded at a call site.

Loudness knobs are in LUFS (ITU-R BS.1770-4 / EBU R128). Relative balance knobs
are in energy dB. Timing knobs are in seconds.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Audio pipeline geometry
# ---------------------------------------------------------------------------

#: Analysis rate. We deliberately do NOT analyse at 16 kHz: true-peak and
#: clipping are meaningless once the signal has been downsampled below the
#: source rate (44.1 kHz here), and BS.1770 K-weighting is specified for
#: broadcast rates. VAD gets its own 16 kHz decimation downstream.
ANALYSIS_RATE = 48_000

#: VAD rate. Silero v5 and webrtcvad both operate at 16 kHz.
VAD_RATE = 16_000

#: Silero v5 requires exactly 512 samples per call at 16 kHz (32 ms).
VAD_CHUNK_16K = 512

#: Decimation factor ANALYSIS_RATE -> VAD_RATE.
DECIM = ANALYSIS_RATE // VAD_RATE  # 3

#: One analysis window = 32 VAD chunks = 1.024 s. Chosen so that the window
#: divides exactly into VAD chunks at both rates, which keeps partial-frame
#: handling out of the hot loop.
VAD_CHUNKS_PER_WINDOW = 32
WINDOW_SAMPLES = VAD_CHUNK_16K * DECIM * VAD_CHUNKS_PER_WINDOW  # 49152 @ 48 kHz
WINDOW_SECONDS = WINDOW_SAMPLES / ANALYSIS_RATE  # 1.024 s


@dataclass(slots=True)
class SourceConfig:
    """How to reach one stream."""

    #: Channel name as it appears in the vendor control panel (e.g. "football-01").
    channel: str = "football-01"

    #: Explicit stream URL. When set, discovery is skipped entirely.
    #: Never holds a hardcoded broadcast UUID in committed config.
    url: str | None = None

    #: CDN root used by discovery when ``url`` is not set.
    hls_base: str = "https://hls.mediaondemand.net"

    #: Extra HTTP headers for the ffmpeg pull. Empty by default: the CDN was
    #: measured to serve without Referer/Origin, but the vendor is actively
    #: tightening access, so this stays configurable.
    headers: dict[str, str] = field(default_factory=dict)

    user_agent: str = "commentary-qc/0.1 (+MMC Sport stream QC)"

    #: Seconds without a single audio sample before the source is declared
    #: stalled. HLS segments are ~1 s, so 10 s is ~10 missed segments.
    stall_timeout_s: float = 10.0

    #: Reconnect backoff bounds for the capture supervisor.
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0


@dataclass(slots=True)
class Thresholds:
    """Detection thresholds and hysteresis.

    Set from MMC's own listening tests (2026-09-10) checked against published
    standards; each knob's comment says where its number came from. Scope is
    football commentary; studio racing feeds are out of scope and have no crowd
    bed anyway.
    """

    # -- loudness band -----------------------------------------------------
    # Set 2026-09-10 from a blind listening test (round 3: 24 clips, one
    # listener, fixed playback volume), checked against published standards.
    #  - loud: the ear placed "too loud" at -18.2 LUFS; AES TD1008 puts speech
    #    streaming at -18 with about +2 LU tolerance. Both land near -17.
    #  - quiet: the ear only called clips quiet below about -41 LUFS. A line at
    #    -40 disagreed with the listener on 4 of 24 clips, against 14 of 24 for
    #    the old -23. The commentator is interrupted only when genuinely hard to
    #    hear; compliance with the -18 streaming target belongs in a post-match
    #    report, not a live alert.
    # Both rules judge `lufs_minute` (median over about a minute), not 3-second
    # loudness: the standards define loudness over a programme, and the
    # 3-second figure swung with every shout and pause, which is where most of
    # the alert noise came from.
    #: Streaming speech target (AES TD1008). A reference point, not an alert line.
    target_lufs: float = -18.0
    too_loud_lufs: float = -17.0
    too_quiet_lufs: float = -40.0

    # Thermostat gap. An alert fires at the line above but only clears once the
    # level is back past these, a few dB inside the band. Without it a channel
    # sitting right on the line fires and clears over and over: Football Ch.102
    # sat at about -40 for the whole of 2026-09-09. With the gap that is one
    # alert which stays up until the level has genuinely recovered.
    too_quiet_clear_lufs: float = -37.0
    too_loud_clear_lufs: float = -19.0

    #: Below this the feed counts as silent rather than merely quiet, so
    #: dead-air fires instead of too-quiet.
    dead_air_lufs: float = -50.0

    # -- clipping ----------------------------------------------------------
    #: EBU R128 permits a maximum of -1 dBTP.
    true_peak_dbtp: float = -1.0
    #: Consecutive samples at full scale within one window before we call it
    #: hard clipping rather than an inter-sample over.
    clip_run_samples: int = 3

    # -- voice / crowd balance --------------------------------------------
    # MMC's prototype specifies a band of 4-14 dB with a fault on BOTH sides:
    # too little voice (buried) and too much (atmosphere too thin).
    #
    # Its SNR is measured as speech-segment level minus ambient-segment level,
    # i.e. (voice+crowd)/crowd. We subtract the crowd out first and measure
    # voice/crowd, so the band has to be converted rather than copied:
    #     ours = 10*log10(10**(theirs/10) - 1)
    #     4 dB -> 1.8 dB      14 dB -> 13.8 dB
    # The ends differ most at the bottom, which is exactly where "commentator
    # is buried" lives, so pasting 4 dB straight in would be ~2 dB too lax on
    # the fault that matters most.

    #: Below this the commentator is buried in the crowd bed. Was 1.8 (the old
    #: prototype's 4 dB, converted). Moved to 6 dB on 2026-09-10: listening
    #: research prefers about 9 dB of speech over a steady crowd (EBU / BBC /
    #: Fraunhofer dialogue-enhancement tests), and in our listening tests the
    #: crowd was heard taking over at 3.9 and 7.4 dB while 8.8-9.0 dB was fine.
    crowd_dominating_db: float = 6.0
    #: Above this the voice is too far forward and the atmosphere is too thin.
    #: Was 13.8 (converted from the prototype's 14 dB). Measured against 19,137
    #: windows of live Champions League commentary that flagged 44.7% of normal
    #: output -- the real median is 12.3 dB, sitting right against the limit.
    #: 20 dB is the 85th percentile of genuine commentary, so it flags the
    #: outliers rather than the middle.
    voice_dominates_db: float = 20.0
    #: Absolute crowd level below which there is no atmosphere at all.
    no_crowd_lufs: float = -45.0
    #: A window needs at least this much speech before the voice/crowd ratio
    #: is trustworthy enough to judge.
    min_speech_ratio_for_balance: float = 0.30

    # -- commentary present at all -----------------------------------------
    # Measured 2026-09-07 across 3 channels and 2 live matches. Filler and live
    # commentary separated cleanly on every one of these, with no overlap:
    #
    #                    filler            live commentary
    #   speech ratio     1.00 .. 1.00      0.62 .. 0.72
    #   crowd bed      -70.2 .. -49.6 dB  -32.1 LUFS
    #   level std dev    0.58 .. 1.30      2.01 .. 4.56
    #
    # Each threshold sits mid-gap, and all three must agree, so no single odd
    # channel can trigger it. The giveaway is the speech ratio: a recorded
    # announcement talks continuously, a real commentator breathes.
    #: Above this, speech is too continuous to be a person talking.
    filler_speech_ratio: float = 0.88
    #: Below this there is no stadium atmosphere behind the voice.
    filler_crowd_lufs: float = -40.0
    #: Below this the loudness is too steady to be live audio.
    filler_level_sd: float = 1.6

    #: Repeat test. The three signals above only describe one kind of filler
    #: (a dry announcement) and miss loops with crowd in them -- Ch.31 on
    #: 2026-09-09 played a crowd-heavy loop for three hours, Ch.35 did the same
    #: before kick-off on 2026-09-08. What every loop shares is that it repeats:
    #: its loudness pattern lines up with itself a fixed interval later.
    #: `loop_score` measures that (see metrics.loop_score). Replayed across
    #: three recorded nights (24 channels): every labelled filler stretch scored
    #: 0.77-1.00, live commentary 0.2-0.5, and the rule never fired during a
    #: match half. Either this OR the three signals above calls it filler.
    filler_loop_score: float = 0.6

    #: Never call it a clip when the audio is below the quiet line AND carries
    #: essentially no speech. Near-silence correlates with itself, so the
    #: repeat test drifts over its threshold on a dead-ish input: German Ch.44
    #: on 2026-09-18 sat at -41.4 LUFS with no speech and scored 0.67, while
    #: real clips that day scored 0.79-1.00. Calling that a clip would also
    #: mask its "too quiet" alert, which is the fault someone needs to hear.
    filler_quiet_speech_floor: float = 0.10

    # -- speech presence ---------------------------------------------------
    #: Fraction of a window the VAD must call speech for the window to count
    #: as "commentator talking".
    speech_ratio_present: float = 0.15

    #: A fault that is already firing has to get this much worse before it
    #: is worth saying again (dB). Ch.22 on 2026-09-07 went from quiet to
    #: nearly dead and nobody was told, because the alert was already up.
    escalate_step_db: float = 6.0

    #: Per-rule override of the step above, for measures that do not live on
    #: the same scale as loudness.
    #:
    #: The balance rules needed this. `crowd_dominating` escalated **153 times
    #: on 212 fires** in the two months to 2026-11-12 -- 72%, so nearly every
    #: instance also sent a "getting worse" message. The cause is that
    #: voice-over-crowd spans 1.4 dB to 33.4 dB across real commentary (p05 to
    #: p95), where loudness spans a few dB, so a 6 dB step is ordinary
    #: variation on one scale and a real deterioration on the other.
    #:
    #: 12 dB is roughly a third of the observed range rather than a twentieth,
    #: which is the same proportion 6 dB is of a loudness swing.
    escalate_step_by_rule: dict[str, float] = field(
        default_factory=lambda: {
            "crowd_dominating": 12.0,
            "voice_dominates": 12.0,
        }
    )

    #: Rules that do not apply to this channel. Not every detector is
    #: meaningful on every feed: studio racing commentary has no crowd bed by
    #: design, so `no_crowd` and `crowd_dominating` fire constantly and measure
    #: room tone between words rather than a crowd. Set per channel.
    disabled_rules: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # A clear level on the wrong side of its fire level would clear an
        # alert the moment it fired. Refuse it at load time instead.
        if self.too_quiet_clear_lufs < self.too_quiet_lufs:
            raise ValueError("too_quiet_clear_lufs must be at or above too_quiet_lufs")
        if self.too_loud_clear_lufs > self.too_loud_lufs:
            raise ValueError("too_loud_clear_lufs must be at or below too_loud_lufs")

    # -- hysteresis (attack = hold before FIRE, release = hold before CLEAR) --
    attack_s: dict[str, float] = field(
        default_factory=lambda: {
            # Measured on football-01: the feed's playout cycle contains a
            # structural 8.8 s silence every 36.7 s. A 5 s attack fires on
            # every one of them (~98 false alerts/hour). This must stay above
            # the longest silence that is normal for the feed -- confirm the
            # intended cadence with the vendor before treating it as final.
            # Renamed from dead_air: the flag should say what was observed, not use
            # broadcast jargon. The knob below keeps its old name.
            "silence": 12.0,
            # A capture gap has to last before anybody is told. Measured
            # over 2,010 of them to 2026-11-12: median 9 s, 53% over
            # inside 15 s, and 556 were our own container failing to
            # resolve the vendor hostname. Those are not broadcast faults
            # and they are fixed before a commentator could read the
            # message. 45 s keeps the 24% that lasted minutes.
            "stream_down": 45.0,
            # Loudness is now judged over a minute, so the alert waits for the
            # minute figure to stay out of range, not for a 3-second spike.
            "too_loud": 30.0,
            "too_quiet": 30.0,
            "clipping": 3.0,
            # Overs are a warning, not distortion: wait for them to persist.
            "peak_over": 10.0,
            "crowd_dominating": 30.0,
            # Thin atmosphere is an advisory nobody fixes in seconds, and a
            # 12 s attack made it the noisiest rule on the board.
            "voice_dominates": 60.0,
            # Filler vs commentary is a sustained state, not a momentary blip,
            # so this deliberately waits minutes rather than seconds.
            "filler_playing": 180.0,
            # Nobody re-mixes atmosphere in seconds, and it is not the
            # commentator's to fix. This belongs in a post-match summary;
            # until that exists, it waits minutes rather than seconds.
            "no_crowd": 180.0,
        }
    )
    release_s: dict[str, float] = field(
        default_factory=lambda: {
            "silence": 5.0,
            "too_loud": 30.0,
            "too_quiet": 30.0,
            "clipping": 15.0,
            "peak_over": 30.0,
            "crowd_dominating": 30.0,
            "voice_dominates": 20.0,
            "filler_playing": 120.0,
            "no_crowd": 120.0,
        }
    )


@dataclass(slots=True)
class AnalyzerConfig:
    """Analyzer behaviour that is not itself a detection threshold."""

    #: "silero" (neural, default) or "webrtc" (fallback). See vad.py.
    vad_backend: str = "silero"
    vad_model_path: str = "models/silero_vad.onnx"
    #: Speech probability above which a 32 ms chunk counts as speech.
    vad_threshold: float = 0.5
    #: webrtcvad aggressiveness 0-3, used only by the fallback backend.
    webrtc_aggressiveness: int = 2

    #: Short-term loudness integration, in windows. 3 windows ~= 3.07 s, which
    #: is the EBU R128 short-term period.
    short_term_windows: int = 3

    #: Windows in the rolling median of voice-to-crowd. The per-window ratio is
    #: noisy -- it depends on how much speech and how much gap happened to fall
    #: in that particular second -- so the balance rules judge a median instead.
    #: MMC's own prototype does the same thing for the same reason.
    balance_median_windows: int = 5

    #: Windows used for the loudness-stability and speech-continuity measures
    #: that separate filler from live commentary. 60 windows is ~61 s -- long
    #: enough that a commentator's pause does not look like a loop.
    commentary_window: int = 60

    #: Windows in the rolling median behind `lufs_minute`, which the loudness
    #: rules judge. 60 windows is about 61 s.
    loudness_minute_windows: int = 60

    #: Repeat test (filler detection). History of the loudness envelope that is
    #: searched for a repeat, and the lags searched, all in windows. 180 windows
    #: is about 3 minutes; lags of 15-90 cover cycles of 15 s to 1.5 minutes
    #: (the loop measured on football-01 cycled every 36.7 s).
    loop_history_windows: int = 180
    loop_min_lag_windows: int = 15
    loop_max_lag_windows: int = 90
    #: The score moves slowly, so it is recomputed every few windows.
    loop_every_windows: int = 3

    #: Smoothing for the carried-forward crowd floor, applied only in windows
    #: that actually contain non-speech samples. 0 = no memory, 1 = frozen.
    crowd_ema: float = 0.7


@dataclass(slots=True)
class Settings:
    source: SourceConfig = field(default_factory=SourceConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    analyzer: AnalyzerConfig = field(default_factory=AnalyzerConfig)

    #: Emit one JSON metrics record per window. This is the BigQuery feed and
    #: the training set for the classifier that replaces these rules later.
    emit_metrics: bool = False

    #: Store only every Nth window for channels nobody has claimed. Measured
    #: 2026-10-20: 47 channels writing every window put ~94 rows/s through
    #: SQLite's single writer, which sustained about 56/s - the database fell
    #: a minute behind and the page reported a false outage. A claimed channel
    #: still stores every window, because that is the one whose trend chart and
    #: match report somebody will actually read. 1 = store everything.
    metrics_sample_unclaimed: int = 10

    #: Seconds between "what is this channel doing now" updates per channel.
    #: The page polls every 5 s, so writing this every 1.024 s window was
    #: four times more often than anyone could see.
    state_write_every_s: float = 2.0

    ffmpeg_bin: str = "ffmpeg"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Settings":
        """Build settings from a nested mapping, rejecting unknown keys.

        Used by the config file loader and, later, by the control plane. A typo
        in a threshold name must fail loudly rather than silently keep the
        default -- a silently-ignored threshold is an outage we would not see.
        """
        return cls(
            source=_build(SourceConfig, data.get("source", {})),
            thresholds=_build(Thresholds, data.get("thresholds", {})),
            analyzer=_build(AnalyzerConfig, data.get("analyzer", {})),
            emit_metrics=bool(data.get("emit_metrics", False)),
            ffmpeg_bin=str(data.get("ffmpeg_bin", os.environ.get("FFMPEG_BIN", "ffmpeg"))),
        )

    @classmethod
    def load(cls, path: str | None) -> "Settings":
        if not path:
            return cls(ffmpeg_bin=os.environ.get("FFMPEG_BIN", "ffmpeg"))
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_mapping(json.load(fh))


def _build(cls: type, data: Mapping[str, Any]):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**dict(data))
