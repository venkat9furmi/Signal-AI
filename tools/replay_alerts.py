"""Replay a recorded night through today's rules and count the alerts.

    python tools/replay_alerts.py wed.db
    python tools/replay_alerts.py wed.db --channel football-102 --rules too_quiet --list
    python tools/replay_alerts.py wed.db --no-gap        # thermostat off, for comparison

Stored metrics keep the per-second measurements; the slow features the rules
judge (minute loudness, balance median, repeat score...) are rebuilt with the
analyzer's own RollingFeatures, so an old recording is judged exactly as the
live system would judge it today. This is how a threshold change gets checked
against real audio before it goes live.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.config import AnalyzerConfig, Thresholds  # noqa: E402
from commentary_qc.engine import AlertEngine  # noqa: E402
from commentary_qc.metrics import MetricsWindow, RollingFeatures  # noqa: E402

BASE = ("wall", "t", "lufs", "lufs_short", "true_peak_dbtp", "sample_peak_dbfs", "clip_run",
        "speech_ratio", "voice_lufs", "crowd_lufs", "voice_to_crowd_db", "crowd_measured")


def replay(conn: sqlite3.Connection, channel: str, th: Thresholds, cfg: AnalyzerConfig):
    rows = conn.execute(
        f"SELECT {','.join(BASE)} FROM metrics WHERE channel=? ORDER BY wall", (channel,)
    ).fetchall()
    roll = RollingFeatures(cfg)
    engine = AlertEngine(th, channel)
    events = []
    for i, r in enumerate(rows):
        m = dict(zip(BASE, r))
        crowd_measured = bool(m["crowd_measured"])
        f = roll.update(m["lufs_short"], m["speech_ratio"], m["crowd_lufs"],
                        m["voice_to_crowd_db"], crowd_measured and m["speech_ratio"] > 0.15)
        w = MetricsWindow(seq=i, crowd_measured=crowd_measured,
                          **{k: m[k] for k in BASE if k != "crowd_measured"}, **f)
        events.extend(engine.push(w))
    return events, len(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("db")
    p.add_argument("--channel", action="append", help="Only these channels (repeatable).")
    p.add_argument("--rules", help="Comma-separated rule ids to report.")
    p.add_argument("--no-gap", action="store_true",
                   help="Disable the thermostat gap (clear at the fire level).")
    p.add_argument("--list", action="store_true", help="Print every FIRE/CLEAR.")
    args = p.parse_args(argv)

    th = Thresholds()
    if args.no_gap:
        th = dataclasses.replace(th, too_quiet_clear_lufs=th.too_quiet_lufs,
                                 too_loud_clear_lufs=th.too_loud_lufs)
    wanted = set(args.rules.split(",")) if args.rules else None
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    chans = args.channel or [r[0] for r in conn.execute(
        "SELECT DISTINCT channel FROM metrics ORDER BY channel")]

    total = Counter()
    for ch in chans:
        events, n = replay(conn, ch, th, AnalyzerConfig())
        fires = Counter(a.rule for a in events if a.event == "FIRE"
                        and (not wanted or a.rule in wanted))
        total.update(fires)
        summary = ", ".join(f"{k} {v}" for k, v in sorted(fires.items())) or "no alerts"
        print(f"{ch:<14} {n / 3600:4.1f} h   {summary}")
        if args.list:
            for a in events:
                if not wanted or a.rule in wanted:
                    when = dt.datetime.fromtimestamp(a.wall).strftime("%H:%M:%S")
                    print(f"    {when}  {a.event:<5} {a.rule:<16} {a.message}")
    print("\nTOTAL  " + (", ".join(f"{k} {v}" for k, v in sorted(total.items())) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
