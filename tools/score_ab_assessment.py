"""Score round two of the listening test (reference-anchored).

Reads the two answer columns -- level relative to the reference, and whether
the commentator could be followed -- joins them to the hidden measurements, and
reports:

* **Consistency.** Hidden repeats and the hidden copy of the reference say how
  reliable one listener is. A threshold is only worth as much as the ear it
  came from; if repeats disagree, the boundaries below carry that uncertainty.
* **Loudness boundaries** from `level`: where "same" turns into "quiet", and
  into "loud".
* **Voice/crowd boundary** from `voice`: where "clear" turns into "buried",
  measured as voice over crowd -- kept separate from loudness, because round
  one showed listeners saying "low" when they meant "the crowd covers him".

    python tools/score_ab_assessment.py assessment2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from score_assessment import best_split  # noqa: E402

LEVEL = {"same", "quiet", "loud", "skip"}
VOICE = {"clear", "buried", "skip"}
SYNONYMS = {
    "ok": "same", "okay": "same", "fine": "same", "good": "same", "equal": "same",
    "low": "quiet", "lower": "quiet", "quieter": "quiet",
    "high": "loud", "louder": "loud", "harsh": "loud",
    "covered": "buried", "crowd": "buried",
}


def word(cell: str, allowed: set[str]) -> str | None:
    first = (cell.split() or [""])[0].lower().strip(".,;:!`")
    first = SYNONYMS.get(first, first)
    return first if first in allowed else None


def read_sheet(path: Path) -> dict[str, dict]:
    got = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        m = re.search(r"(clip_\d+)", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        got[m.group(1)] = {
            "level": word(cells[1], LEVEL), "voice": word(cells[2], VOICE),
            "raw_level": cells[1], "raw_voice": cells[2],
            "notes": cells[3] if len(cells) > 3 else "",
        }
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?", default="assessment2")
    args = ap.parse_args()
    root = Path(args.dir)

    key_doc = json.loads((root / "answer_key.json").read_text(encoding="utf-8"))
    ref = key_doc["reference"]
    key = {k["clip"]: k for k in key_doc["clips"]}

    labels = root / "labels.json"
    if labels.exists():
        ans = json.loads(labels.read_text(encoding="utf-8"))["labels"]
        print(f"answers from {labels}")
    else:
        ans = read_sheet(root / "assessment.md")

    unread = [c for c in key if not ans.get(c) or (ans[c].get("level") is None
                                                   and ans[c].get("voice") is None)]
    if unread:
        print(f"{len(unread)} rows without a readable answer: {', '.join(unread[:10])}")
    # A row can be half-readable: a valid voice word beside a level written as a
    # sentence. Checking only whole rows let that level vanish from the result
    # without a word, so every unrecognised cell is reported on its own.
    for c in key:
        a = ans.get(c)
        if not a:
            continue
        for col in ("level", "voice"):
            raw = a.get(f"raw_{col}", "")
            if raw and a.get(col) is None:
                print(f"   {c} {col}: '{raw}' is not one of the allowed words - ignored")
    if len(unread) == len(key):
        return 1

    print(f"\nreference: {ref['lufs']:.1f} LUFS, voice over crowd {ref['balance']:.1f} dB")

    # ---- consistency ---------------------------------------------------
    print("\n--- how consistent is the ear? ---")
    checks = 0
    agree = 0
    for c, k in key.items():
        a = ans.get(c) or {}
        if k["role"] == "repeat" and k.get("repeat_of") in ans:
            o = ans[k["repeat_of"]]
            for q in ("level", "voice"):
                if a.get(q) and o.get(q) and "skip" not in (a[q], o[q]):
                    checks += 1
                    agree += a[q] == o[q]
                    mark = "same answer" if a[q] == o[q] else "DIFFERENT"
                    print(f"  {c} replays {k['repeat_of']}  {q:<5}: {o[q]} then {a[q]}   {mark}")
        if k["role"] == "hidden_reference":
            lv = a.get("level")
            print(f"  {c} was the reference itself  -> you said '{lv}'"
                  f"{'   (correct)' if lv == 'same' else '   (expected same)'}")
    if checks:
        print(f"  repeats agreed on {agree} of {checks} answers")

    # ---- loudness ------------------------------------------------------
    tests = [(c, k) for c, k in key.items() if k["role"] == "test"]
    grp = {"same": [], "quiet": [], "loud": []}
    for c, k in tests:
        lv = (ans.get(c) or {}).get("level")
        if lv in grp:
            grp[lv].append(k["lufs"])
    print("\n--- loudness (level column) ---")
    for v in ("quiet", "same", "loud"):
        if grp[v]:
            print(f"  {v:<6}{len(grp[v]):>3} clips   {min(grp[v]):6.1f} .. {max(grp[v]):6.1f} LUFS")
    lo = best_split(grp["quiet"], grp["same"])
    hi = best_split(grp["same"], grp["loud"])
    if lo:
        print(f"  -> too_quiet below {lo[0]:.1f} LUFS  ({lo[1]} clip(s) on the wrong side)"
              f"   engine today: -23.0")
    if hi and len(grp["loud"]) >= 2:
        print(f"  -> too_loud above  {hi[0]:.1f} LUFS  ({hi[1]} clip(s) on the wrong side)"
              f"   engine today: -17.0")
    elif grp["loud"]:
        print(f"  -> too_loud: {len(grp['loud'])} loud clip - not enough to place a line")

    # ---- voice / crowd ---------------------------------------------------
    clear, buried = [], []
    for c, k in tests:
        vo = (ans.get(c) or {}).get("voice")
        if vo == "clear":
            clear.append(k["balance"])
        elif vo == "buried":
            buried.append(k["balance"])
    print("\n--- voice over crowd (voice column) ---")
    if clear:
        print(f"  clear  {len(clear):>3} clips   {min(clear):5.1f} .. {max(clear):5.1f} dB")
    if buried:
        print(f"  buried {len(buried):>3} clips   {min(buried):5.1f} .. {max(buried):5.1f} dB")
    bd = best_split(buried, clear)
    if bd:
        print(f"  -> crowd_dominating below {bd[0]:.1f} dB  ({bd[1]} clip(s) on the wrong side)"
              f"   engine today: 1.8")
    elif not buried:
        print("  -> no clip was judged buried, so there is no crowd line to place")

    notes = [(c, a["notes"]) for c, a in ans.items() if a.get("notes")]
    if notes:
        print("\n--- your notes ---")
        for c, n in sorted(notes):
            k = key.get(c, {})
            print(f"  {c}  ({k.get('lufs', 0):.1f} LUFS, {k.get('balance', 0):.1f} dB)  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
