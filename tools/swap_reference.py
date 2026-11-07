"""Replace the reference in a round-2 sheet, keeping every test clip the same.

The first reference was chosen from an assumption about the target level
(-19 LUFS) rather than from the listener's ear, and it sounded loud. In a
reference-anchored test that is not a cosmetic problem: the reference *defines*
"normal", so everything quieter than a loud reference reads as "quiet" and the
boundary the test finds is just the reference's own level.

The fix is to anchor on a clip the listener has already judged "ok". This cuts
the steadiest 20 seconds of such a clip from round one, makes it the new
reference and the new hidden reference copy, and clears the answer columns so
the sheet can be done again. The test clips and hidden repeats are untouched,
so results from the two references stay directly comparable.

    python tools/swap_reference.py assessment2 --round1-clip clip_06
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from make_ab_assessment import cut, steady_window  # noqa: E402
from make_assessment import find_ffmpeg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?", default="assessment2")
    ap.add_argument("--round1-clip", required=True, help="e.g. clip_06 from round one.")
    ap.add_argument("--round1", default="assessment", help="Round-one folder.")
    ap.add_argument("--db", default="wed.db")
    args = ap.parse_args()

    root = Path(args.dir)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ffmpeg not found - it is needed to cut the new reference.")
        return 1

    name = args.round1_clip.removesuffix(".aac")
    r1 = {k["clip"].removesuffix(".aac"): k
          for k in json.loads((Path(args.round1) / "answer_key.json").read_text(encoding="utf-8"))}
    if name not in r1:
        print(f"{name} is not in {args.round1}/answer_key.json")
        return 1
    src = r1[name]

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    w = steady_window(conn, src["channel"], src["file"])
    if not w:
        print(f"{name} has no steady 20-second stretch with speech in it.")
        return 1

    # Keep whatever has been answered so far before the columns are cleared.
    sheet = root / "assessment.md"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(sheet, root / f"assessment.before-swap-{stamp}.md")

    key_path = root / "answer_key.json"
    doc = json.loads(key_path.read_text(encoding="utf-8"))
    new_ref = {
        "file": src["file"], "channel": src["channel"], "at": src["at"],
        "from_round1": name, **w,
    }
    cut(ffmpeg, src["file"], w["offset"], root / "reference.mp3")
    for k in doc["clips"]:
        if k["role"] == "hidden_reference":
            cut(ffmpeg, src["file"], w["offset"], root / "clips" / f"{k['clip']}.mp3")
            k.update({kk: vv for kk, vv in new_ref.items()})
    doc.setdefault("previous_references", []).append(doc["reference"])
    doc["reference"] = new_ref
    key_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    lines = []
    for line in sheet.read_text(encoding="utf-8").splitlines():
        m = re.search(r"\[(clip_\d+)\]", line)
        if m and line.lstrip().startswith("|"):
            c = m.group(1)
            line = f"| [{c}](clips/{c}.mp3) |  |  |  |"
        lines.append(line)
    sheet.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"new reference: {name} from round one ({src['channel']}), "
          f"{w['lufs']:.1f} LUFS, voice over crowd {w['balance']:.1f} dB")
    print(f"previous answers kept in {root}/assessment.before-swap-{stamp}.md")
    print("answer columns cleared - the test clips are unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
