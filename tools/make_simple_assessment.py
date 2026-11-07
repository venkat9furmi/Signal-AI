"""Round-one style listening sheet (one clip, one verdict), with fresh clips.

Same format as the first test -- ok / quiet / loud / skip plus notes -- but
each clip is the steadiest 20 seconds of its minute, so the level does not
change mid-clip, and clips already heard in earlier rounds are left out.

    python tools/make_simple_assessment.py wed.db --out assessment3
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from make_ab_assessment import cut, steady_window  # noqa: E402
from make_assessment import collect, find_ffmpeg, spread  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--clips", default="clips")
    ap.add_argument("--out", default="assessment3")
    ap.add_argument("-n", type=int, default=24)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--exclude", nargs="*",
                    default=["assessment/answer_key.json", "assessment2/answer_key.json"])
    args = ap.parse_args()

    used: set[str] = set()
    for p in args.exclude:
        if Path(p).exists():
            doc = json.loads(Path(p).read_text(encoding="utf-8"))
            items = doc["clips"] if isinstance(doc, dict) else doc
            used |= {k["file"] for k in items}

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    pool = []
    for c in collect(args.db, args.clips):
        if c["file"] in used:
            continue
        w = steady_window(conn, c["channel"], c["file"])
        if w:
            pool.append({**c, "minute_lufs": c["lufs"], **w})

    chosen = spread(pool, args.n)
    random.Random(args.seed).shuffle(chosen)

    out = Path(args.out)
    (out / "clips").mkdir(parents=True, exist_ok=True)
    ff = find_ffmpeg()
    key = []
    for i, c in enumerate(chosen, 1):
        name = f"clip_{i:02d}"
        cut(ff, c["file"], c["offset"], out / "clips" / f"{name}.mp3")
        key.append({"clip": f"{name}.aac", **c})
    (out / "answer_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")

    rows = "\n".join(f"| [{k['clip'][:-4]}](clips/{k['clip'][:-4]}.mp3) |  |  |" for k in key)
    (out / "assessment.md").write_text(f"""# Listening test — round 3

Same as the first test. Volume: set it once to a comfortable level, write it
here **____**, and don't change it.

For each clip write **one word** in *verdict*:

| word | means |
|---|---|
| `ok` | fine to send to a bookmaker |
| `quiet` | too quiet — ask the commentator to come up |
| `loud` | too loud or crackly |
| `skip` | can't judge |

Anything else (crowd too loud, no crowd, noise...) goes in *notes*.
Don't open `answer_key.json` until you finish.

| clip | verdict | notes |
|---|---|---|
{rows}
""", encoding="utf-8")

    lv = [c["lufs"] for c in chosen]
    print(f"{len(pool)} fresh clips available; picked {len(chosen)}, "
          f"{min(lv):.1f} to {max(lv):.1f} LUFS -> {out}/assessment.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
