"""Build round two of the listening test: reference-anchored, 20-second clips.

Round one asked for an absolute judgement ("is this too quiet?") one clip at a
time, and it could not place a boundary: clips 1-2 dB apart on the same channel
got opposite verdicts, because a listener's ear adapts to whatever volume they
happen to be at. This version fixes the three things that caused that:

* **A fixed reference.** The listener plays the same known-good clip before
  every test clip and judges relative to it. People are far more precise at
  "louder or quieter than that?" than at "is this loud?".
* **Two separate questions.** Round one's "low" sometimes meant the channel was
  quiet and sometimes meant the crowd was covering the voice. Level and voice
  clarity are now asked separately, so the two can be told apart.
* **Steady 20-second cuts.** Each clip is the steadiest 20 seconds of its
  minute, with someone talking, so the level does not change mid-clip.

It also hides a few repeats and one copy of the reference among the test
clips. Those answer "how consistent is one listener?", which decides how much
any threshold derived from the sheet can be trusted.

    python tools/make_ab_assessment.py wed.db --clips clips --out assessment2
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from make_assessment import collect, find_ffmpeg, spread  # noqa: E402

#: Seconds of audio per clip. Long enough to judge, short enough that 24 clips
#: plus reference replays fit in one sitting.
CLIP_SECONDS = 20


def steady_window(conn: sqlite3.Connection, channel: str, file: str) -> dict | None:
    """The steadiest CLIP_SECONDS of a one-minute segment, with speech in it."""
    t0 = datetime.strptime(Path(file).stem, "%Y%m%d-%H%M%S").timestamp()
    rows = conn.execute(
        "SELECT wall, lufs_short, speech_ratio, crowd_lufs, voice_to_crowd_med FROM metrics"
        " WHERE channel=? AND wall>=? AND wall<? ORDER BY wall",
        (channel, t0, t0 + 60),
    ).fetchall()
    n = CLIP_SECONDS  # metrics rows are ~1.024 s apart
    best = None
    for i in range(0, len(rows) - n + 1):
        w = rows[i:i + n]
        speech = st.mean(r[2] for r in w)
        if not 0.35 <= speech <= 0.85:
            continue  # need a person talking, with pauses
        sd = st.pstdev(r[1] for r in w)
        if best is None or sd < best[0]:
            best = (sd, w)
    if best is None:
        return None
    sd, w = best
    return {
        "offset": round(max(0.0, w[0][0] - t0), 1),
        "lufs": round(st.median(r[1] for r in w), 1),
        "swing": round(sd, 2),
        "speech": round(st.mean(r[2] for r in w), 2),
        "crowd": round(st.median(r[3] for r in w), 1),
        "balance": round(st.median(r[4] for r in w), 1),
    }


def cut(ffmpeg: str, src: str, offset: float, dst: Path) -> None:
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-ss", str(offset), "-i", src,
         "-t", str(CLIP_SECONDS), "-codec:a", "libmp3lame", "-q:a", "2", str(dst)],
        check=True,
    )


def pick_reference(pool: list[dict]) -> dict:
    """A clip that should sound unremarkably right: near target, real crowd,
    commentator comfortably above it, and a steady level."""
    cands = [c for c in pool if c["crowd"] > -36 and 8 <= c["balance"] <= 16] or pool
    return min(cands, key=lambda c: abs(c["lufs"] + 19) + c["swing"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--clips", default="clips")
    ap.add_argument("--out", default="assessment2")
    ap.add_argument("--exclude", default="assessment/answer_key.json",
                    help="Answer key of an earlier round; its clips are not reused.")
    ap.add_argument("-n", type=int, default=20, help="Unique test clips.")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ffmpeg not found - it is needed to cut the clips.")
        return 1

    used = set()
    if Path(args.exclude).exists():
        used = {k["file"] for k in json.loads(Path(args.exclude).read_text(encoding="utf-8"))}

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    pool = []
    for c in collect(args.db, args.clips):
        if c["file"] in used:
            continue
        w = steady_window(conn, c["channel"], c["file"])
        if w:
            pool.append({**c, "minute_lufs": c["lufs"], **w})
    if len(pool) < args.n + 1:
        print(f"Only {len(pool)} usable fresh clips - not enough for {args.n}.")
        return 1

    ref = pick_reference(pool)
    pool = [c for c in pool if c is not ref]
    chosen = spread(pool, args.n)

    for i, c in enumerate(chosen):
        c["id"], c["role"] = i, "test"
    by_level = sorted(chosen, key=lambda c: c["lufs"])
    picks = [by_level[int(len(by_level) * q)] for q in (0.25, 0.5, 0.75)][: args.repeats]
    entries = list(chosen)
    entries += [{**p, "role": "repeat"} for p in picks]
    entries.append({**ref, "id": "ref", "role": "hidden_reference"})

    rng = random.Random(args.seed)
    for _ in range(200):  # keep a repeat away from its original
        rng.shuffle(entries)
        pos = {}
        clash = False
        for i, e in enumerate(entries):
            if e["id"] in pos and abs(pos[e["id"]] - i) < 4:
                clash = True
                break
            pos.setdefault(e["id"], i)
        if not clash:
            break

    out = Path(args.out)
    (out / "clips").mkdir(parents=True, exist_ok=True)
    cut(ffmpeg, ref["file"], ref["offset"], out / "reference.mp3")

    names: dict[object, str] = {}
    key = []
    for i, e in enumerate(entries, 1):
        name = f"clip_{i:02d}"
        cut(ffmpeg, e["file"], e["offset"], out / "clips" / f"{name}.mp3")
        if e["role"] == "test":
            names[e["id"]] = name
        key.append({"clip": name, **{k: v for k, v in e.items() if k != "id"}, "_id": e["id"]})
    for k in key:
        if k["role"] == "repeat":
            k["repeat_of"] = names.get(k["_id"])
        k.pop("_id")

    (out / "answer_key.json").write_text(
        json.dumps({"reference": {k: v for k, v in ref.items() if k != "id"}, "clips": key}, indent=2),
        encoding="utf-8")

    rows = "\n".join(f"| [{k['clip']}](clips/{k['clip']}.mp3) |  |  |  |" for k in key)
    sheet = f"""# Listening test — round 2

## Before you start (do this once)

1. **Use headphones**, and the same ones the whole time.
2. Play **[reference.mp3](reference.mp3)**. Turn your volume until the commentator
   sounds comfortable, like watching normal TV.
3. **Now leave the volume alone until you finish.** Write your volume number here: **____**
4. Quiet room, one sitting. It takes about 20 minutes.

## For each clip

1. Play **reference.mp3** for a few seconds, to reset your ear.
2. Play the clip (20 seconds).
3. Write **one word** in each of the two columns.

**level** — compared to the reference:

| word | means |
|---|---|
| `same` | about as loud as the reference |
| `quiet` | quieter — you would ask the commentator to come up |
| `loud` | louder than the reference, or harsh / crackly |

**voice** — can you follow the commentator?

| word | means |
|---|---|
| `clear` | easy to follow |
| `buried` | the crowd or background covers the voice |

Can't decide? Write `skip`. Anything else you notice goes in **notes**.

## Rules

- Only the words above in **level** and **voice**. Everything else goes in **notes**.
- Don't change the volume.
- Don't open `answer_key.json` until you have finished.
- Answer every clip on its own, even if it reminds you of an earlier one.

---

| clip | level | voice | notes |
|---|---|---|---|
{rows}

---

When you have finished, tell Claude and it will score the sheet.
"""
    (out / "assessment.md").write_text(sheet, encoding="utf-8")

    lv = [c["lufs"] for c in chosen]
    print(f"{len(pool) + 1} fresh usable clips; picked {len(chosen)} spanning "
          f"{min(lv):.1f} to {max(lv):.1f} LUFS")
    print(f"reference: {ref['channel']} {ref['at']}  {ref['lufs']:.1f} LUFS, "
          f"crowd {ref['crowd']:.1f}, voice over crowd {ref['balance']:.1f} dB")
    print(f"{len(key)} rows: {len(chosen)} test + {len(picks)} hidden repeats + 1 hidden reference")
    print(f"\nwrote {out}/assessment.md, {out}/reference.mp3, {out}/clips/*.mp3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
