"""Backtest the `filler_playing` rule against labelled recordings.

Labels:
- 2026-09-07 (tonight.db): kick-off times from the vendor's own schedule, plus a
  human listening to Ch.22 and confirming it carried a holding loop rather than
  the Serie A match it was scheduled for.
- 2026-09-08 (ucl.db): Porto v Manchester City on seven language channels at
  once, kick-off 21:00. Only the two halves are labelled (as live commentary),
  so this recording checks the rule stays quiet during a match; pre-match and
  half-time are left unlabelled because nobody listened to them.

Usage:  python tests/test_no_commentary.py [tonight.db] [ucl.db]

Replays the stored per-second metrics through the analyzer's own
RollingFeatures and the engine's real `filler_playing` predicate, so this tests
the rule the recorder actually runs rather than a copy of it.
"""

from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.config import AnalyzerConfig, Thresholds  # noqa: E402
from commentary_qc.engine import RULES  # noqa: E402
from commentary_qc.metrics import MetricsWindow, RollingFeatures  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FILLER = next(r for r in RULES if r.id == "filler_playing")
BASE = ("wall", "t", "lufs", "lufs_short", "true_peak_dbtp", "sample_peak_dbfs", "clip_run",
        "speech_ratio", "voice_lufs", "crowd_lufs", "voice_to_crowd_db", "crowd_measured")


def _at(day: datetime.date, hh: int, mm: int) -> float:
    return datetime.datetime.combine(day, datetime.time(hh, mm)).timestamp()


def labelled_periods() -> list[tuple[str, str, str, float | None, float | None, bool]]:
    """(db, label, channel, start, end, is_filler).

    Half-time is labelled as filler, not commentary. The first version of these
    labels marked whole matches as live; the rule then flagged a contiguous
    13-minute block in the middle of Ch.52 and looked wrong. It was not -- that
    block was half-time, and the labels were.
    """
    d7, d8 = datetime.date(2026, 9, 7), datetime.date(2026, 9, 8)
    out = [
        # Ch.52 -- Greek Super League, kick-off 17:00
        ("tonight.db", "Ch.52 before kick-off", "football-52", None, _at(d7, 16, 55), True),
        ("tonight.db", "Ch.52 first half", "football-52", _at(d7, 17, 10), _at(d7, 17, 48), False),
        ("tonight.db", "Ch.52 half-time", "football-52", _at(d7, 17, 53), _at(d7, 18, 3), True),
        ("tonight.db", "Ch.52 second half", "football-52", _at(d7, 18, 8), _at(d7, 18, 50), False),
        # Ch.12 -- La Liga, kick-off 19:00. Stops before its own half-time.
        ("tonight.db", "Ch.12 before kick-off", "football-12", None, _at(d7, 18, 55), True),
        ("tonight.db", "Ch.12 first half", "football-12", _at(d7, 19, 5), _at(d7, 19, 44), False),
        # Ch.22 -- scheduled Serie A 18:30, carried a holding loop instead
        # (confirmed by ear). It changed state again at ~19:33.
        ("tonight.db", "Ch.22 filler (early)", "football-22", None, _at(d7, 18, 25), True),
    ]
    for ch in ("football-05", "football-15", "football-25", "football-35",
               "football-45", "football-100", "football-108"):
        n = ch.split("-")[1].lstrip("0")
        out += [
            ("ucl.db", f"Ch.{n} first half", ch, _at(d8, 21, 5), _at(d8, 21, 44), False),
            ("ucl.db", f"Ch.{n} second half", ch, _at(d8, 22, 8), _at(d8, 22, 48), False),
        ]
    return out


def flags_for(conn: sqlite3.Connection, channel: str, th: Thresholds) -> list[tuple[float, bool]]:
    """(wall, rule says filler) for every stored window of a channel."""
    rows = conn.execute(
        f"SELECT {','.join(BASE)} FROM metrics WHERE channel=? ORDER BY wall", (channel,)
    ).fetchall()
    roll = RollingFeatures(AnalyzerConfig())
    out = []
    for i, r in enumerate(rows):
        m = dict(zip(BASE, r))
        cm = bool(m["crowd_measured"])
        f = roll.update(m["lufs_short"], m["speech_ratio"], m["crowd_lufs"],
                        m["voice_to_crowd_db"], cm and m["speech_ratio"] > 0.15)
        w = MetricsWindow(seq=i, crowd_measured=cm,
                          **{k: m[k] for k in BASE if k != "crowd_measured"}, **f)
        out.append((m["wall"], bool(FILLER.predicate(w, th))))
    return out


def main(dbs: list[str]) -> int:
    th = Thresholds()
    print(f"{'recording':<12}{'period':<24}{'expect':<8}{'flagged filler':>15}   verdict")
    print("-" * 72)
    ok, cache, conns = True, {}, {}
    for db, label, ch, start, end, is_filler in labelled_periods():
        path = next((d for d in dbs if Path(d).name == db), None)
        if not path or not Path(path).exists():
            continue
        conn = conns.setdefault(path, sqlite3.connect(f"file:{path}?mode=ro", uri=True))
        if (path, ch) not in cache:
            cache[(path, ch)] = flags_for(conn, ch, th)
        # Skip the warm-up: the rolling features need their history first.
        flags = [f for w, f in cache[(path, ch)][180:]
                 if (start is None or w >= start) and (end is None or w < end)]
        if len(flags) < 120:
            print(f"{db:<12}{label:<24}{'-':<8}{'too few rows':>15}   SKIP")
            continue
        pct = 100.0 * sum(flags) / len(flags)
        # A sustained state, so the rule should agree on the overwhelming
        # majority of windows, not merely on a bare majority.
        good = pct > 90 if is_filler else pct < 10
        ok &= good
        print(f"{db:<12}{label:<24}{'filler' if is_filler else 'live':<8}{pct:>14.1f}%   "
              f"{'PASS' if good else 'FAIL'}")

    print()
    print("ALL PASS" if ok else "FAILURES - the rule does not match the labels")
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:] or [str(ROOT / "tonight.db"), str(ROOT / "ucl.db")]
    raise SystemExit(main(args))
