"""Turn a filled-in listening sheet into thresholds.

Reads the verdicts, joins them to the hidden measurements, and reports where a
human's judgement actually changes. That boundary is the threshold -- derived
from someone who knows what good commentary sounds like, rather than inherited
from an old prototype.

Verdicts come from `labels.json` in the assessment folder when it exists
(listeners write notes, not single words, and those get transcribed there), and
otherwise from the first word of each row's verdict cell in `assessment.md`.

    python tools/score_assessment.py assessment
"""

from __future__ import annotations

import argparse
import json
import re
import statistics as st
from pathlib import Path

VERDICTS = {"ok", "quiet", "loud", "skip"}
#: Accepted spellings. "quite" is a real answer from a real sheet -- a typo for
#: quiet that otherwise drops the clip out of the result without a word.
SYNONYMS = {"okay": "ok", "good": "ok", "fine": "ok", "low": "quiet", "high": "loud",
            "quite": "quiet", "quit": "quiet", "qiet": "quiet", "lowd": "loud"}


def read_sheet(path: Path) -> dict[str, str]:
    """Pull verdicts out of the markdown table, whichever layout it uses."""
    got = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.search(r"(clip_\d+)", line)
        if not m or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        first = (cells[1].split() or [""])[0].lower().strip(".,")
        got[f"{m.group(1)}.aac"] = SYNONYMS.get(first, first)
    return got


def read_labels(root: Path) -> dict[str, dict]:
    p = root / "labels.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("labels", {})


def best_split(low: list[float], high: list[float]) -> tuple[float, int] | None:
    """The boundary between two groups that misplaces the fewest clips.

    Ears are not step functions, so the groups normally overlap near the edge.
    Every midpoint between neighbouring values is tried; when several tie, the
    centre of the tied range is returned rather than an arbitrary end of it.
    """
    if not low or not high:
        return None
    vals = sorted(set(low + high))
    cands = [(a + b) / 2 for a, b in zip(vals, vals[1:])] or vals
    err = lambda t: sum(x >= t for x in low) + sum(x < t for x in high)
    best = min(err(t) for t in cands)
    tied = [t for t in cands if err(t) == best]
    return round((min(tied) + max(tied)) / 2, 1), best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?", default="assessment")
    ap.add_argument("--quiet-now", type=float, default=-23.0)
    ap.add_argument("--loud-now", type=float, default=-17.0)
    args = ap.parse_args()
    root = Path(args.dir)

    key = {k["clip"]: k for k in json.loads((root / "answer_key.json").read_text(encoding="utf-8"))}
    labels = read_labels(root)
    if labels:
        verdicts = {c: l["verdict"] for c, l in labels.items()}
        tags = {c: l.get("tags", []) for c, l in labels.items()}
        print(f"verdicts from {root / 'labels.json'}")
    else:
        verdicts = read_sheet(root / "assessment.md")
        tags = {}

    judged = {c: v for c, v in verdicts.items() if v in VERDICTS and c in key}
    unread = [c for c in key if c not in judged]
    if unread:
        print(f"{len(unread)} clips without a readable verdict: {', '.join(unread[:8])}")
    if not judged:
        print("Nothing to score. If the sheet has free-text notes, transcribe them into labels.json.")
        return 1

    groups: dict[str, list[float]] = {"ok": [], "quiet": [], "loud": []}
    for c, v in judged.items():
        if v != "skip":
            groups[v].append(key[c]["lufs"])

    print(f"\n{sum(map(len, groups.values()))} clips judged\n")
    print(f"{'verdict':<9}{'n':>4}{'quietest':>11}{'median':>9}{'loudest':>9}   LUFS")
    print("-" * 52)
    for v in ("quiet", "ok", "loud"):
        g = groups[v]
        if g:
            print(f"{v:<9}{len(g):>4}{min(g):>11.1f}{st.median(g):>9.1f}{max(g):>9.1f}")

    print("\n--- thresholds your ear implies ---")
    lo = best_split(groups["quiet"], groups["ok"])
    hi = best_split(groups["ok"], groups["loud"])
    if lo:
        print(f"  too_quiet below  {lo[0]:>7.1f} LUFS   ({lo[1]} clip(s) on the wrong side)"
              f"   currently {args.quiet_now}")
    else:
        print("  too_quiet: need at least one 'quiet' and one 'ok' clip")
    if hi and len(groups["loud"]) >= 3:
        print(f"  too_loud above   {hi[0]:>7.1f} LUFS   ({hi[1]} clip(s) on the wrong side)"
              f"   currently {args.loud_now}")
    elif groups["loud"]:
        print(f"  too_loud: only {len(groups['loud'])} loud clip(s) -- not enough to place a"
              f" boundary; the ear says the loud end starts somewhere above"
              f" {max(groups['ok']):.1f}")
    else:
        print("  too_loud: no clip judged loud")

    q_now = lo[0] if lo else args.quiet_now
    print("\n--- where the engine and your ear disagree ---")
    n = cur = new = 0
    for c, v in sorted(judged.items(), key=lambda kv: key[kv[0]]["lufs"]):
        if v == "skip":
            continue
        lu = key[c]["lufs"]
        was = "quiet" if lu < args.quiet_now else "loud" if lu > args.loud_now else "ok"
        now = "quiet" if lu < q_now else "loud" if lu > args.loud_now else "ok"
        n += 1
        cur += was != v
        new += now != v
        if was != v:
            print(f"  {c}  {lu:>7.1f} LUFS  you: {v:<6} engine today: {was}")
    print(f"\n  current thresholds disagree on {cur} of {n}")
    print(f"  your boundary would disagree on {new} of {n}")

    if tags:
        print("\n--- what you heard besides loudness ---")
        for c in sorted(tags, key=lambda x: key.get(x, {}).get("lufs", 0)):
            extra = [t for t in tags[c] if t != "borderline"]
            if extra and c in key:
                k = key[c]
                print(f"  {c}  {', '.join(extra):<28} crowd {k['crowd']:>6.1f}"
                      f"  voice/crowd {k['balance']:>5.1f} dB  peak {k['peak']:>5.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
