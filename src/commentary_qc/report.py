"""Post-match report: what a whole broadcast looked like, for the producer.

Live alerts are for the commentator, and they are deliberately few --
interrupting someone mid-sentence is expensive, so a feed that sat 4 dB under
the streaming target all evening is never worth a message at the time. A
producer wants exactly that: the whole broadcast on one page afterwards,
including the things nobody was told about while they happened.

Everything is counted in **measured audio**, not wall clock: one analysis
window is 1.024 s, and a stretch where the stream was down contributes no
windows at all rather than counting as good.

Holding clips are excluded from every figure about the commentator -- loudness,
peaks and distortion alike. A pre-match clip plays at its own level (and MMC's
football clip clips hard), so counting it would blame a recorded file on the
person. The clip's own worst peak is reported separately.
"""

from __future__ import annotations

import statistics
import time

from .config import WINDOW_SECONDS, Thresholds

_METRIC_SQL = (
    "SELECT wall, lufs_minute, lufs_short, true_peak_dbtp, clip_run, speech_ratio,"
    " loop_score, voice_to_crowd_med, crowd_lufs, crowd_measured"
    " FROM metrics WHERE channel=? AND wall>=? AND wall<? ORDER BY wall"
)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def alert_spans(store, channel: str, since: float, until: float) -> list[dict]:
    """Every alert in the window, paired into spans with a duration.

    A FIRE still open when the report is taken is reported as open rather than
    dropped: a fault that never cleared is the most important line in the list.
    """
    rows = store.query(
        "SELECT wall, event, rule, severity, message FROM alerts"
        " WHERE channel=? AND wall>=? AND wall<? ORDER BY wall",
        (channel, since, until),
    )
    open_at: dict[str, dict] = {}
    spans: list[dict] = []
    for r in rows:
        rule = r["rule"]
        if r["event"] in ("FIRE", "ESCALATE"):
            if rule in open_at:
                if r["event"] == "ESCALATE":
                    open_at[rule]["worsened"] += 1
                    open_at[rule]["message"] = r["message"]
                continue
            open_at[rule] = {"rule": rule, "severity": r["severity"], "from": r["wall"],
                             "message": r["message"], "worsened": 0}
        elif rule in open_at:
            span = open_at.pop(rule)
            span["to"] = r["wall"]
            span["seconds"] = round(r["wall"] - span["from"], 1)
            spans.append(span)
    for span in open_at.values():
        span["to"] = None
        span["seconds"] = round(until - span["from"], 1)
        span["open"] = True
        spans.append(span)
    spans.sort(key=lambda s: s["from"])
    return spans


def build(store, channel: str, since: float, until: float | None = None,
          th: Thresholds | None = None) -> dict:
    """Summarise one channel over a time window."""
    th = th or Thresholds()
    until = until or time.time()
    rows = store.query(_METRIC_SQL, (channel, since, until))
    w = WINDOW_SECONDS
    total = len(rows) * w

    if not rows:
        return {"channel": channel, "from": since, "to": until, "measured_s": 0.0,
                "empty": True, "alerts": alert_spans(store, channel, since, until)}

    clip = [r for r in rows if (r["loop_score"] or 0) >= th.filler_loop_score]
    silent = [r for r in rows if (r["lufs_short"] or -99) < th.dead_air_lufs]
    # What the commentator was actually responsible for: neither a holding
    # clip nor silence.
    live = [
        r for r in rows
        if (r["loop_score"] or 0) < th.filler_loop_score
        and (r["lufs_short"] or -99) >= th.dead_air_lufs
    ]
    live_lufs = [r["lufs_minute"] for r in live if r["lufs_minute"] is not None]
    live_s = len(live) * w

    quiet = [v for v in live_lufs if v < th.too_quiet_lufs]
    loud = [v for v in live_lufs if v > th.too_loud_lufs]
    # The streaming target is a quality bar, not an alert line: AES TD1008 puts
    # speech at -18 LUFS with about +/- 2 LU of tolerance.
    on_target = [v for v in live_lufs if abs(v - th.target_lufs) <= 2.0]
    # Peaks and distortion are judged over commentary too. A holding clip has
    # its own peaks -- the football-english clip clips hard -- and counting
    # those would tell a commentator they distorted for an hour while a
    # recorded file was playing.
    peaks = [r["true_peak_dbtp"] for r in live if r["true_peak_dbtp"] is not None]
    overs = [p for p in peaks if p > th.true_peak_dbtp]
    clipping = [r for r in live if (r["clip_run"] or 0) >= th.clip_run_samples]
    clip_peaks = [r["true_peak_dbtp"] for r in clip if r["true_peak_dbtp"] is not None]
    balance = [r["voice_to_crowd_med"] for r in live
               if r["crowd_measured"] and (r["speech_ratio"] or 0) > 0.15
               and r["voice_to_crowd_med"] is not None]

    return {
        "channel": channel,
        "from": rows[0]["wall"],
        "to": rows[-1]["wall"],
        "measured_s": round(total, 1),
        "live_s": round(live_s, 1),
        "clip_s": round(len(clip) * w, 1),
        "silent_s": round(len(silent) * w, 1),
        "clip_pct": _pct(len(clip), len(rows)),
        # -- loudness, over live commentary only --------------------------
        "target": th.target_lufs,
        "median_lufs": _median(live_lufs),
        "offset_from_target": (round(_median(live_lufs) - th.target_lufs, 2)
                               if live_lufs else None),
        "on_target_pct": _pct(len(on_target), len(live_lufs)),
        "quiet_s": round(len(quiet) * w, 1),
        "quiet_pct": _pct(len(quiet), len(live_lufs)),
        "loud_s": round(len(loud) * w, 1),
        "loud_pct": _pct(len(loud), len(live_lufs)),
        "quiet_line": th.too_quiet_lufs,
        "loud_line": th.too_loud_lufs,
        # -- peaks ---------------------------------------------------------
        "peak_max": round(max(peaks), 2) if peaks else None,
        #: The holding clip's own worst peak, so a clip that clips is visible
        #: as the vendor's problem rather than the commentator's.
        "clip_peak_max": round(max(clip_peaks), 2) if clip_peaks else None,
        "peak_limit": th.true_peak_dbtp,
        "over_s": round(len(overs) * w, 1),
        "clipping_s": round(len(clipping) * w, 1),
        # -- balance and speech --------------------------------------------
        "balance_median": _median(balance),
        "balance_low": th.crowd_dominating_db,
        "speech_pct": _pct(sum((r["speech_ratio"] or 0) for r in live), len(live) or 1) if live else 0.0,
        "crowd_median": _median([r["crowd_lufs"] for r in live if r["crowd_lufs"] is not None]),
        "alerts": alert_spans(store, channel, since, until),
    }
