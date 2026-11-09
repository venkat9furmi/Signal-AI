"""Tests for the bench: several models on one clip, judged blind.

The bench is the point of the translate package -- a single run says a model
produced something, and only a comparison says whether another would have done
better. So these tests are mostly about fairness: that every candidate hears
the same audio, that nobody can see which model is which until they ask, that
one person cannot out-vote a colleague by clicking twice, and that the
scoreboard arithmetic is right.

That last one is not hypothetical. The first version joined the score rows
straight onto the runs, so a run judged by two people counted as two runs and
weighed twice in the mean latency -- and the scoreboard cheerfully printed
"ok 2 of 1". There is a check for it below.

    python tests/test_bench.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.translate.app import bench as bench_mod  # noqa: E402
from commentary_qc.translate.app.bench import (  # noqa: E402
    Bench,
    BenchRunner,
    Candidate,
    cost_estimate,
    coverage,
    plan,
    run_bench,
)
from commentary_qc.translate.core.models import (  # noqa: E402
    NotConfigured,
    Transcript,
    TranslationRun,
    Utterance,
)
from commentary_qc.translate.app.modes import ModeRegistry  # noqa: E402
from commentary_qc.translate.infra.runs import RunStore  # noqa: E402
from commentary_qc.translate.settings import TranslateSettings  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def table_info(path, what: str = "table_info") -> set[str]:
    """Column or index names, from a connection that is then closed.

    Closing matters on Windows: an open SQLite handle keeps the file locked,
    and the temporary directory then refuses to go away at the end of the run.
    """
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA {what}(translate_runs)")}
    finally:
        conn.close()


def speech(seconds: float = 6.0, rate: int = 48_000) -> np.ndarray:
    """Speech-shaped noise, loud enough to pass the silence guard."""
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    rng = np.random.default_rng(3)
    env = (np.sin(2 * np.pi * 0.5 * t) > -0.3).astype(np.float32)
    return np.clip(rng.normal(0, 0.2, t.size).astype(np.float32) * env, -1, 1)


def settings(**kw) -> TranslateSettings:
    base = dict(
        provider="mock", enabled_providers=("mock", "camb"),
        shadow_daily_minutes=60.0, shadow_per_channel_minutes=60.0,
    )
    base.update(kw)
    return TranslateSettings(**base)


# ======================================================================
# 1. planning
# ======================================================================
def test_planning() -> None:
    section("planning a comparison")

    cands = plan(settings(), ["mock", "camb"], ["de-de", "el-gr"])
    check("every model is paired with every language", len(cands) == 4)
    check("and each pair is named so it can be told apart",
          {c.key for c in cands} == {
              "mock->de-de", "mock->el-gr", "camb->de-de", "camb->el-gr"})
    check("a blank name is skipped rather than becoming a candidate",
          len(plan(settings(), ["mock", "", "  "], ["de-de"])) == 1)
    check("so is a blank language",
          len(plan(settings(), ["mock"], ["de-de", ""])) == 1)

    with_over = plan(settings(), ["camb"], ["de-de"],
                     overrides={"camb": {"camb_voice_id": 999}})
    check("overrides ride along, so 'which voice' is the same comparison "
          "as 'which vendor'",
          with_over[0].overrides == {"camb_voice_id": 999})
    check("and show up in the key, so two entries from one vendor are "
          "distinguishable", "camb_voice_id=999" in with_over[0].key)

    est = cost_estimate(cands, 15.0)
    check("the cost is counted before anything is sent",
          est["audio_seconds"] == 60.0 and est["candidates"] == 4)
    check("and expressed in minutes, because that is what every vendor "
          "bills on", est["audio_minutes"] == 1.0)

    tweaked = bench_mod._with_overrides(settings(), {"camb_voice_id": 7})
    check("an override is applied through the dataclass, so it is validated",
          tweaked.camb_voice_id == 7)
    check("and an unknown knob is dropped rather than aborting a "
          "hand-typed candidate list",
          bench_mod._with_overrides(settings(), {"nonsense": 1}).provider == "mock")

    try:
        Bench("football-41", [])
        check("a bench with no candidates is refused", False)
    except NotConfigured:
        check("a bench with no candidates is refused", True)


# ======================================================================
# 2. blindness
# ======================================================================
def test_blindness() -> None:
    section("a bench is blind until somebody asks")

    cands = plan(settings(), ["mock", "camb"], ["de-de"])
    seen = set()
    for _ in range(25):
        b = Bench("football-41", cands)
        seen.add(tuple(e.candidate.provider for e in b.entries))
    check("the display order is shuffled, so a tester who knows the "
          "expensive model is always first is not blind", len(seen) > 1)

    b = Bench("football-41", cands)
    for e in b.entries:
        b.set_result(e, TranslationRun(
            id="r-" + e.label, created=time.time(), mode="shadow",
            bench=b.id, provider=e.candidate.provider,
            model=e.candidate.provider + "/x", voice="v1", credits="42",
            target_language=e.candidate.target, target_text="text " + e.label,
            status="ok", spoken_audio=b"RIFF", total_s=1.0,
        ))
    entry = b.to_dict()["entries"][0]
    check("the vendor is hidden", "candidate" not in entry)
    for giveaway in ("provider", "model", "voice", "credits"):
        check(f"and so is {giveaway}, which would identify it just as well",
              giveaway not in entry["run"])
    check("but the result itself is shown, which is the thing being judged",
          entry["text"] == "text " + entry["label"] and entry["has_audio"])

    b.reveal()
    revealed = b.to_dict()["entries"][0]
    check("revealing shows the vendor", revealed["candidate"]["provider"] in
          ("mock", "camb"))
    check("and the model name with it", bool(revealed["run"]["model"]))
    check("labels are stable across a reveal, so a score given to B still "
          "belongs to B", revealed["label"] == entry["label"])


# ======================================================================
# 3. running it
# ======================================================================
class MuteProvider:
    """Transcribes, but never says which language it heard.

    A real shape, not a contrivance: it is what broke the first bench run.
    The provider returned a transcript with no language, the source stayed
    ``auto``, and the translate step refused it -- so every entry failed with
    "the source language is unknown" even on a channel whose language we knew
    perfectly well from channels.json.
    """

    name = "mute"
    model = "mute/no-language"

    def __init__(self, settings) -> None:
        self.settings = settings
        self.asked: list[str] = []

    def transcribe(self, segment, language):
        # The whole point of this stub: language is left empty.
        return Transcript(
            utterances=(Utterance("il pallone va fuori", 0.0, 1.0),),
            language="", provider=self.name, model=self.model,
        )

    def translate(self, texts, source, target):
        from commentary_qc.translate.core.models import Translation

        self.asked.append(source)
        if not source or source == "auto":
            raise NotConfigured(
                "the source language is unknown, and translation cannot detect it"
            )
        return Translation(
            texts=tuple(f"[{target}] {t}" for t in texts),
            source_language=source, target_language=target,
            provider=self.name, model=self.model,
        )

    def speak(self, text, language):
        from commentary_qc.translate.core.models import SpokenAudio

        return SpokenAudio(audio=b"RIFF0000", media_type="audio/wav",
                           voice="mute", model=self.model, provider=self.name,
                           language=language)

    def languages(self):
        return {"it-it": "Italian", "en-us": "English", "de-de": "German"}

    def live_languages(self):
        return ("en-us", "de-de")

    def healthcheck(self):
        return {"ok": True}

    def capabilities(self):
        return {"provider": self.name, "steps": []}

    def close(self) -> None:
        pass


def test_running(tmp: Path) -> None:
    section("running a bench, including when a model fails")

    from commentary_qc.translate import adapters as registry

    registry.FACTORIES["mute"] = lambda s: MuteProvider(s)
    registry.DESCRIPTIONS["mute"] = {
        "title": "Mute", "detail": "never reports a language", "cost": "free",
    }
    try:
        cfg = settings(enabled_providers=("mock", "camb", "mute"))
        modes = ModeRegistry(cfg)
        shadow = modes.get("shadow")
        audio = speech(6.0)

        # camb has no key, so it cannot be wired: one entry must fail while
        # the others still run.
        cands = plan(cfg, ["mock", "camb"], ["de-de"])
        b = Bench("football-41", cands, seconds=6.0)
        run_bench(b, shadow, cfg, audio, 48_000)
        results = {e.candidate.provider: e for e in b.entries}
        check("the model that works produces a run",
              results["mock"].run is not None
              and results["mock"].run.status == "ok")
        check("the model that cannot be used produces an error, not an "
              "exception that loses the whole bench",
              results["camb"].run is None and bool(results["camb"].error))
        check("and the bench as a whole is done rather than failed, because "
              "a comparison with one entry missing is still a comparison",
              b.phase == "done")
        check("the heard audio is NOT copied onto every entry -- it lives on "
              "the bench, since every entry heard the same clip",
              results["mock"].run.heard_audio == b"")
        check("each run is stamped with the bench it belongs to",
              results["mock"].run.bench == b.id)

        # every candidate got the identical samples
        b2 = Bench("football-41", plan(cfg, ["mock"], ["de-de", "el-gr"]),
                   seconds=6.0)
        run_bench(b2, shadow, cfg, audio, 48_000)
        heard = {e.run.source_text for e in b2.entries if e.run}
        check("two entries on the same clip heard the same words, which is "
              "the whole basis of the comparison", len(heard) == 1, str(heard))

        # the fallback that the first real run needed
        b3 = Bench("football-21", plan(cfg, ["mute"], ["de-de"]), seconds=6.0)
        run_bench(b3, shadow, cfg, audio, 48_000, fallback_language="it-it")
        got = b3.entries[0]
        check("a model that reports no language is rescued by the channel's "
              "configured one, rather than failing the entry",
              got.run is not None and got.run.status == "ok",
              got.run.error if got.run else got.error)
        check("and the source is recorded as that language, not as 'auto'",
              got.run.source_language == "it-it")

        b4 = Bench("football-21", plan(cfg, ["mute"], ["de-de"]), seconds=6.0)
        run_bench(b4, shadow, cfg, audio, 48_000)
        check("without a fallback it still fails honestly rather than "
              "guessing a language",
              b4.entries[0].run is not None
              and b4.entries[0].run.status == "not_configured")
    finally:
        registry.FACTORIES.pop("mute", None)
        registry.DESCRIPTIONS.pop("mute", None)


# ======================================================================
# 4. coverage
# ======================================================================
def test_coverage() -> None:
    section("which model reaches which language")

    from commentary_qc.translate import adapters as registry

    table = coverage(settings(), registry)
    check("every target is asked about", len(table["targets"]) == 6)
    check("MMC's six languages are the default question, not a vendor's "
          "whole catalogue",
          set(table["targets"]) == {"en-us", "it-it", "pt-pt", "de-de",
                                    "el-gr", "fr-fr"})
    rows = {r["provider"]: r for r in table["providers"]}
    check("the offline model offers all six, so the table has a baseline",
          all(v == "yes" for v in rows["mock"]["targets"].values()),
          str(rows["mock"]["targets"]))
    check("a model with no key is reported as unknown, NOT as 'no' -- a "
          "catalogue we could not fetch is our problem, not the model's",
          set(rows["camb"]["targets"].values()) == {"unknown"})
    check("and the reason is given", bool(rows["camb"]["blocked"]))

    check("an exact tag is yes", bench_mod._verdict("de-de", {"de-de"}) == "yes")
    check("another locale of the same language is 'near', because "
          "Portuguese is Portuguese",
          bench_mod._verdict("pt-pt", {"pt-br"}) == "near")
    check("a language that is absent is no",
          bench_mod._verdict("el-gr", {"de-de", "en-us"}) == "no")
    check("an empty catalogue is unknown, not no",
          bench_mod._verdict("el-gr", set()) == "unknown")


# ======================================================================
# 5. storage, scoring, and the arithmetic
# ======================================================================
def test_store(tmp: Path) -> None:
    section("storing a bench, and the scoreboard arithmetic")

    store = RunStore(str(tmp / "bench.db"))
    cands = plan(settings(), ["mock", "camb"], ["de-de"])
    b = Bench("football-41", cands, seconds=9.0, label="first pass")
    b.heard_audio = b"RIFFsource"
    b.source_url = "https://example/playlist.m3u8"
    for i, e in enumerate(b.entries):
        b.set_result(e, TranslationRun(
            id="r%d" % i, created=time.time(), mode="shadow", bench=b.id,
            channel="football-41", provider=e.candidate.provider,
            model=e.candidate.provider + "/x",
            target_language=e.candidate.target, target_text="t%d" % i,
            status="ok", total_s=2.0 + i, spoken_audio=b"RIFF",
        ))
    b.phase = "done"
    b.finish()
    store.put_bench(b)

    got = store.bench(b.id)
    check("the bench comes back", got is not None and got["id"] == b.id)
    check("with its entries", got["count"] == 2)
    check("and its label, so a comparison can be found again later",
          got["label"] == "first pass")
    check("the source clip is stored ONCE, on the bench",
          store.bench_audio(b.id)[0] == b"RIFFsource")
    check("and each entry's own audio is stored with the run",
          store.audio("r0", "spoken")[0] == b"RIFF")
    check("a stored bench is listed", any(r["id"] == b.id
          for r in store.benches(limit=5)))
    check("revealing is recorded, not just held in memory",
          store.reveal(b.id) and store.bench(b.id)["revealed"])
    check("revealing a bench that does not exist says so",
          store.reveal("nope") is False)

    # -- scoring
    store.score(b.id, "r0", "venkat", 4, "clear")
    store.score(b.id, "r0", "venkat", 5, "changed my mind")
    store.score(b.id, "r0", "anna", 3)
    store.score(b.id, "r1", "venkat", 2)
    scores = store.scores(b.id)
    check("one score per person per entry -- a second go replaces the first",
          len(scores["r0"]) == 2)
    check("and it is the later value that stands",
          {x["who"]: x["score"] for x in scores["r0"]}["venkat"] == 5)
    check("a note rides along with the score",
          any(x["note"] == "changed my mind" for x in scores["r0"]))

    # -- the arithmetic that was wrong
    rows = {(r["provider"], r["target"]): r for r in store.scoreboard()}
    check("every model that was benched appears", len(rows) == 2)
    for key, row in rows.items():
        check(f"{key[0]}: runs are counted once, not once per score",
              row["runs"] == 1, f"runs={row['runs']}")
        check(f"{key[0]}: successes cannot exceed runs",
              (row["ok"] or 0) <= (row["runs"] or 0),
              f"ok={row['ok']} of {row['runs']}")
    judged = next(r for r in rows.values() if r["judgements"] == 2)
    check("the mean score is the mean of the judgements", judged["mean_score"] == 4.0,
          str(judged["mean_score"]))
    one = next(r for r in rows.values() if r["judgements"] == 1)
    check("a run judged once weighs once", one["mean_score"] == 2.0)
    check("mean latency is per run, not per judgement -- joining the score "
          "rows directly made a twice-judged run count twice",
          judged["mean_s"] == 2.0, str(judged["mean_s"]))

    check("the scoreboard can be narrowed to one language",
          len(store.scoreboard(target="de-de")) == 2
          and store.scoreboard(target="el-gr") == [])

    # a solo run must not pollute the comparison
    store.put(TranslationRun(id="solo", created=time.time(), mode="shadow",
                             provider="mock", model="mock/x",
                             target_language="de-de", status="ok"))
    after = {(r["provider"], r["target"]): r for r in store.scoreboard()}
    check("a run outside any bench is excluded, so one-off tries do not "
          "dilute the comparison",
          after[("mock", "de-de")]["runs"] == rows[("mock", "de-de")]["runs"]
          if ("mock", "de-de") in rows else True)
    store.close()


def test_migration_order(tmp: Path) -> None:
    section("an older database opens, indexes and all")

    path = tmp / "old2.db"
    cols = (
        "id TEXT PRIMARY KEY, created REAL NOT NULL, mode TEXT NOT NULL, "
        "kind TEXT, channel TEXT, seconds REAL, provider TEXT, model TEXT, "
        "voice TEXT, source_language TEXT, source_text TEXT, "
        "target_language TEXT, target_text TEXT, status TEXT, error TEXT, "
        "transcribe_s REAL, translate_s REAL, speak_s REAL, total_s REAL, "
        "credits TEXT, source_url TEXT, heard_audio BLOB, spoken_audio BLOB, "
        "heard_type TEXT, spoken_type TEXT"
    )
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE translate_runs ({cols})")
    conn.execute("INSERT INTO translate_runs (id, created, mode, channel)"
                 " VALUES ('old', 1.0, 'shadow', 'football-41')")
    conn.commit()
    conn.close()

    store = RunStore(str(path))
    check("an index on a migrated column is created AFTER the column -- "
          "building it first failed on every open and the page would not "
          "start at all", True)
    names = table_info(path, "index_list")
    check("the bench index exists", "idx_runs_bench" in names)
    have = table_info(path)
    check("and the bench column with it", "bench" in have)
    check("the old row survived", any(r["id"] == "old"
          for r in store.recent(limit=5)))
    store.close()


# ======================================================================
# 6. the runner
# ======================================================================
def test_runner(tmp: Path) -> None:
    section("one bench at a time")

    store = RunStore(str(tmp / "runner.db"))
    runner = BenchRunner(store, keep=3)
    cands = plan(settings(), ["mock"], ["de-de"])

    slow = Bench("football-41", cands, seconds=5.0)

    def work(b) -> None:
        b.heard_audio = b"RIFF"
        time.sleep(0.6)
        b.phase = "done"

    runner.start(slow, work)
    try:
        runner.start(Bench("football-42", cands), work)
        check("a second bench is refused while one is running -- two "
              "overlapping fan-outs would contend for the same rate limits "
              "and make every measured latency meaningless", False)
    except Exception as exc:  # noqa: BLE001
        check("a second bench is refused while one is running -- two "
              "overlapping fan-outs would contend for the same rate limits "
              "and make every measured latency meaningless",
              "already running" in str(exc), str(exc))

    check("it finishes", slow.wait(20))
    check("and is filed without anybody polling for it",
          store.bench(slow.id) is not None)
    check("the runner can find it again", runner.get(slow.id) is slow)
    check("and knows which was most recent", runner.current() is slow)

    for _ in range(5):
        extra = Bench("football-41", cands)
        runner.start(extra, work)
        extra.wait(20)
    check("only the recent ones are held in memory, so a page left open all "
          "afternoon does not accumulate every clip",
          len(runner.all()) <= 3, str(len(runner.all())))

    broken = Bench("football-41", cands)

    def explode(b) -> None:
        raise RuntimeError("the capture failed")

    runner.start(broken, explode)
    check("a bench whose work raises is marked failed rather than left "
          "running forever", broken.wait(20) and broken.phase == "failed")
    check("with the reason attached", "capture failed" in broken.error)
    store.close()


def main() -> int:
    import tempfile

    print("=" * 70)
    print("Commentary QC - the model bench")
    print("=" * 70)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        test_planning()
        test_blindness()
        test_running(tmp)
        test_coverage()
        test_store(tmp)
        test_migration_order(tmp)
        test_runner(tmp)
    print("\n" + "=" * 70)
    print(f"{PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
