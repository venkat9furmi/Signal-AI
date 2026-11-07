"""Build a blind listening test from a night's recording.

Picks clips spanning the whole measured loudness range, copies them out under
neutral shuffled names, and writes a sheet with the verdict column left empty.

**The listener must not see the measurements.** If the sheet said "-38 LUFS"
next to a clip, the answer would be anchored to what the machine already thinks
and the exercise would prove nothing. The measured values go into a separate
answer key that is only opened at scoring time.

Clips are drawn evenly across the loudness range rather than at random, because
a random sample of a night's audio is mostly ordinary and says little about
where the boundaries are. What matters is having examples on both sides of every
candidate threshold.

    python tools/make_assessment.py wed.db --clips clips --out assessment
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import statistics as st
import subprocess
from datetime import datetime
from pathlib import Path

SEGMENT_SECONDS = 60

#: Where winget puts ffmpeg on this machine; used when it is not on PATH.
_WINGET_FFMPEG = (
    Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-9.0.1-full_build/bin/ffmpeg.exe"
)


def find_ffmpeg() -> str | None:
    found = shutil.which(os.environ.get("FFMPEG_BIN", "ffmpeg"))
    if found:
        return found
    return str(_WINGET_FFMPEG) if _WINGET_FFMPEG.exists() else None


def clip_time(path: Path) -> datetime | None:
    """Segments are named by wall clock: 20260909-201530.aac"""
    try:
        return datetime.strptime(path.stem, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def collect(db: str, clips_dir: str) -> list[dict]:
    """Every clip, with the audio measurements taken while it was recorded."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = []
    for chan_dir in sorted(Path(clips_dir).iterdir()):
        if not chan_dir.is_dir():
            continue
        for f in sorted(chan_dir.glob("*.aac")):
            t = clip_time(f)
            if t is None or f.stat().st_size < 50_000:
                continue  # partial or truncated segment
            start = t.timestamp()
            rows = [
                dict(r) for r in conn.execute(
                    "SELECT lufs_short, speech_ratio, crowd_lufs, true_peak_dbtp,"
                    " voice_to_crowd_med FROM metrics"
                    " WHERE channel=? AND wall>=? AND wall<?",
                    (chan_dir.name, start, start + SEGMENT_SECONDS),
                )
            ]
            if len(rows) < 20:
                continue  # not enough measurement to characterise it
            med = lambda k: st.median([r[k] for r in rows if r[k] is not None])
            speech, crowd = med("speech_ratio"), med("crowd_lufs")
            levels = [r["lufs_short"] for r in rows if r["lufs_short"] is not None]
            steady = st.pstdev(levels) if len(levels) > 2 else 99.0

            # Someone must actually be talking, with pauses. Below 0.25 is a
            # crowd-only passage; above 0.85 nobody is breathing, which means a
            # recording rather than a person.
            if not 0.25 <= speech <= 0.85:
                continue
            # Reject holding loops outright. Filler talks continuously with no
            # crowd behind it and an unnaturally steady level -- and asking
            # someone whether a holding loop is "too quiet" answers nothing
            # about live commentary. The speech test alone lets filler straight
            # through, because filler is 100% speech.
            if speech > 0.88 and crowd < -40 and steady < 1.6:
                continue
            out.append({
                "file": str(f), "channel": chan_dir.name, "at": t.strftime("%H:%M:%S"),
                "lufs": round(med("lufs_short"), 1),
                "speech": round(speech, 2),
                "crowd": round(crowd, 1),
                "steady": round(steady, 2),
                "peak": round(med("true_peak_dbtp"), 1),
                "balance": round(med("voice_to_crowd_med"), 1),
            })
    return out


def spread(items: list[dict], n: int, key: str = "lufs") -> list[dict]:
    """Pick n items spread evenly across the range of `key`."""
    if len(items) <= n:
        return list(items)
    ordered = sorted(items, key=lambda x: x[key])
    lo, hi = ordered[0][key], ordered[-1][key]
    picked, used = [], set()
    for i in range(n):
        target = lo + (hi - lo) * i / max(1, n - 1)
        best = min(
            (x for j, x in enumerate(ordered) if j not in used),
            key=lambda x: abs(x[key] - target),
            default=None,
        )
        if best is None:
            break
        used.add(ordered.index(best))
        picked.append(best)
    return picked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--clips", default="clips")
    ap.add_argument("--out", default="assessment")
    ap.add_argument("-n", type=int, default=24, help="How many clips to judge.")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    all_clips = collect(args.db, args.clips)
    if not all_clips:
        print("No usable clips found. Was the audio recorder running?")
        return 1
    lufs = [c["lufs"] for c in all_clips]
    chans = sorted({c["channel"] for c in all_clips})
    print(f"{len(all_clips)} usable clips (live commentary, holding loops excluded)")
    print(f"  loudness range {min(lufs):.1f} to {max(lufs):.1f} LUFS across {len(chans)} channels")
    if max(lufs) - min(lufs) < 8:
        print("  WARNING: narrow range - the test may not find the boundaries")

    chosen = spread(all_clips, args.n)
    rng = random.Random(args.seed)
    rng.shuffle(chosen)  # so the order carries no hint of level

    out = Path(args.out)
    (out / "clips").mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    key = []
    for i, c in enumerate(chosen, 1):
        name = f"clip_{i:02d}.aac"
        shutil.copy2(c["file"], out / "clips" / name)
        # AAC will not play on double-click on a stock Windows machine -- the
        # first listener had to convert all 24 by hand. Ship an MP3 beside it.
        if ffmpeg:
            subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-i", str(out / "clips" / name),
                 "-codec:a", "libmp3lame", "-q:a", "2",
                 str(out / "clips" / name.replace(".aac", ".mp3"))],
                check=False,
            )
        key.append({"clip": name, **c})
    if not ffmpeg:
        print("  note: ffmpeg not found, so no MP3 copies were made")

    (out / "answer_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")

    sheet = out / "assessment.md"
    lines = [
        "# Listening assessment",
        "",
        "Play each clip (the `.mp3` opens in any player) and write **one word** in the",
        "verdict column. Notes are welcome — say anything else you hear: the crowd",
        "taking over, distortion, the level changing partway through.",
        "",
        "| verdict | means |",
        "|---|---|",
        "| `ok` | sounds right — you would send this to a bookmaker |",
        "| `quiet` | too quiet — you would ask the commentator to come up |",
        "| `loud` | too loud, or distorting |",
        "| `skip` | cannot judge (level changes too much, no speech, etc.) |",
        "",
        "Keep your playback volume the same for every clip. The clips are in random",
        "order and the measurements are hidden, so that your ear decides the",
        "thresholds rather than agreeing with the machine.",
        "",
        "---",
        "",
        "| clip | verdict | notes |",
        "|---|---|---|",
    ]
    lines += [f"| [{k['clip'][:-4]}](clips/{k['clip'][:-4]}.mp3) |  |  |" for k in key]
    lines += [
        "",
        "---",
        "",
        "When every row is filled in:",
        "",
        "```",
        f"python tools/score_assessment.py {args.out}",
        "```",
    ]
    sheet.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nwrote {out}/assessment.md  ({len(key)} clips)")
    print(f"      {out}/clips/           the audio to play (.mp3 and .aac)")
    print(f"      {out}/answer_key.json  measurements - do not open before judging")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
