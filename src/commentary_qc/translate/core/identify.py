"""What is actually on this channel: which language, and which match.

Two questions the quality engine cannot answer because it measures levels
rather than words, and that transcription answers for free once a live session
is running. Both are pure functions over text, which is the point -- they are
the kind of thing that is easy to get subtly wrong and impossible to test when
it is tangled up with a socket.

**Which language.** ``channels.json`` carries a language per channel and that
column is hand-maintained on the vendor's side, so it is a statement of
intent, not an observation. A commentator who signs into the wrong channel
produces perfectly good audio in the wrong language, every level rule passes,
and nothing notices. Comparing what was heard against what was configured is
a one-line check that catches it.

**Which match.** The system currently cannot work this out at all: the
commentator picks their match when they claim a channel, and that claim is
also what turns Slack delivery on. Forget to claim and the channel is measured
in silence. The commentator says the team names out loud in the first minute
of every broadcast, so the transcript can say which fixture this is.

Both return a guess with its confidence and its runner-up, never a bare
answer. A wrong-language warning that fires on a goal celebration, or a match
guess that silently picks the wrong derby, would be worse than no feature:
the whole value here is that somebody can act on it without checking.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


#: Enough words to say anything at all. Below this the scores are noise --
#: a single exclamation is in no language in particular, and football
#: commentary is full of names that belong to every language.
MIN_TOKENS = 12

#: How far ahead the winner has to be before the guess is worth acting on.
#: A margin rather than an absolute score, because the absolute share depends
#: on how much of the passage is names and numbers, which varies wildly
#: between a goal and a lull.
MIN_MARGIN = 1.35

#: Share of the letters that have to be Greek before script alone decides it.
#: Not 100%: a Greek transcript carries Latin-script player names, club names
#: and competition names throughout.
GREEK_SHARE = 0.30


# -- the vocabularies ------------------------------------------------------
# Function words, not football words. Tempting as "fuorigioco" and "Abseits"
# are, they turn up a few times a match while "che" and "nicht" turn up in
# every sentence -- and the words that distinguish languages are precisely the
# ones with no meaning to carry.
#
# Chosen for *contrast*, not frequency. "la", "una", "del" and "con" are
# common in Italian, Spanish and Portuguese alike and so are worth nothing
# here; "che" against "que", "non" against "no" and "nao", "molto" against
# "muy" and "muito" are what actually separate them.
_WORDS: dict[str, tuple[str, ...]] = {
    "en-us": (
        "the", "and", "is", "of", "to", "in", "that", "with", "for", "from",
        "this", "has", "was", "they", "there", "but", "now", "just", "it",
        "his", "into", "out", "over", "been", "very", "down", "back", "on",
    ),
    "it-it": (
        "il", "che", "di", "non", "per", "sono", "gli", "nel", "molto",
        "anche", "questo", "della", "dei", "come", "più", "ancora",
        "qui", "ecco", "adesso", "sulla", "dalla", "lui", "suo", "noi",
        "però",
    ),
    "pt-pt": (
        "não", "está", "muito", "também", "então",
        "aqui", "já", "mas", "ele", "isso", "para", "mais", "bem",
        "sua", "pelo", "pela", "dois", "até", "foi", "vai",
        "nós", "agora", "ainda", "sobre",
    ),
    "es-es": (
        "el", "que", "no", "por", "los", "muy", "también", "esto",
        "pero", "aquí", "ya", "más", "está", "sus", "hasta",
        "ahora", "desde", "sobre", "donde", "este", "nosotros", "ella",
        "porque",
    ),
    "de-de": (
        "der", "die", "das", "und", "ist", "nicht", "ein", "eine", "mit",
        "auf", "für", "den", "dem", "sich", "aber", "jetzt", "hier",
        "wird", "noch", "auch", "sehr", "von", "zum", "hat", "wie",
        "schon", "ganz",
    ),
    "fr-fr": (
        "le", "les", "des", "et", "est", "pas", "une", "pour", "dans",
        "avec", "sur", "qui", "que", "il", "ce", "mais", "maintenant",
        "très", "plus", "cette", "son", "sont", "elle", "nous",
        "bien", "encore", "deux", "tout",
    ),
    "nl-nl": (
        "het", "een", "van", "is", "niet", "dat", "met", "voor", "maar",
        "naar", "ook", "nog", "deze", "zijn", "wordt", "heeft", "hier",
        "daar", "weer", "goed", "even", "heel",
    ),
}

#: Set form, built once. The tuples above are kept as tuples because they are
#: the readable, reviewable version -- somebody will want to add a word.
_SETS: dict[str, frozenset[str]] = {
    tag: frozenset(words) for tag, words in _WORDS.items()
}

#: A readable name per tag. Deliberately the names ``channels.json`` uses, so
#: a mismatch can be reported in the same words the channel list is written
#: in rather than in tags nobody reads.
NAMES: dict[str, str] = {
    "en-us": "English",
    "it-it": "Italian",
    "pt-pt": "Portuguese",
    "es-es": "Spanish",
    "de-de": "German",
    "fr-fr": "French",
    "nl-nl": "Dutch",
    "el-gr": "Greek",
}

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_GREEK_RE = re.compile(r"[Ͱ-Ͽἀ-῿]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def tokens(text: str) -> list[str]:
    """Words, lowercased, with the accents kept.

    Accents are the point: ``nao`` against ``no``, ``piu`` against ``plus``
    and ``fuer`` against ``fur`` are some of the strongest signals available,
    and stripping them to be forgiving would throw away exactly the
    characters that separate the languages this platform carries.
    """
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def primary(tag: str) -> str:
    """``pt-br`` -> ``pt``. Two tags name the same language or they do not."""
    return (tag or "").strip().lower().split("-", 1)[0]


@dataclass(frozen=True, slots=True)
class LanguageGuess:
    """What the words looked like, and how sure that is.

    ``tag`` is empty when there is not enough to go on, which is a different
    thing from a confident "not what you configured" -- and the caller has to
    be able to tell them apart, because one is a warning and the other is a
    shrug.
    """

    tag: str = ""
    name: str = ""
    confidence: float = 0.0
    runner_up: str = ""
    words: int = 0
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return bool(self.tag)

    def agrees_with(self, tag: str) -> bool:
        """Whether this is the language ``tag`` names.

        Compared at the language, not the locale: ``pt-pt`` and ``pt-br`` are
        the same commentary, and a channel configured ``en-us`` carrying
        ``en-gb`` is not a fault anybody needs telling about.

        ``auto`` and a blank tag both mean *nobody said what this channel
        carries*, which cannot be disagreed with. That is not a nicety: the
        racing channels have no language in ``channels.json`` at all, so
        treating ``auto`` as a claim reported a wrong-language fault on every
        one of them, every run.
        """
        if not self.known or not tag or tag.strip().lower() in ("auto", "unknown"):
            return True
        return primary(self.tag) == primary(tag)

    def to_dict(self) -> dict:
        return {
            "tag": self.tag,
            "name": self.name,
            "confidence": round(self.confidence, 2),
            "runner_up": self.runner_up,
            "words": self.words,
            "scores": {k: round(v, 3) for k, v in sorted(self.scores.items())},
        }


def identify_language(text: str) -> LanguageGuess:
    """Which of MMC's languages this passage is in.

    Script first, because it is not a guess: Greek is written in an alphabet
    none of the others use, so counting characters settles it outright and no
    word list is needed. Everything else shares the Latin alphabet and is
    scored on function words.
    """
    words = tokens(text)
    letters = _LETTER_RE.findall(text or "")
    if letters:
        greek = len(_GREEK_RE.findall(text or "")) / len(letters)
        if greek >= GREEK_SHARE:
            return LanguageGuess(
                tag="el-gr",
                name=NAMES["el-gr"],
                # Script is evidence of a different kind from word frequency:
                # the alphabet is not shared, so there is nothing to be
                # confused with. Reported as the share of Greek letters so a
                # mostly-Latin passage with a few Greek words does not read as
                # certainty.
                confidence=round(min(1.0, greek + 0.4), 2),
                words=len(words),
                scores={"el-gr": round(greek, 3)},
            )

    if len(words) < MIN_TOKENS:
        return LanguageGuess(words=len(words))

    seen = set(words)
    total = len(words)
    scores: dict[str, float] = {}
    for tag, vocab in _SETS.items():
        # Hits are counted over *distinct* words, not occurrences. One
        # repeated filler word -- and a nervous commentator has one -- would
        # otherwise carry a whole verdict on its own.
        scores[tag] = len(seen & vocab) / max(1, len(seen))

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    second, second_score = ranked[1] if len(ranked) > 1 else ("", 0.0)

    if best_score <= 0.0:
        return LanguageGuess(words=total, scores=scores)
    margin = best_score / second_score if second_score > 0 else float("inf")
    if margin < MIN_MARGIN:
        # Two languages fit about as well, which on this platform means
        # Italian against Spanish or Portuguese against Spanish. Naming
        # either one would be a coin toss dressed up as a measurement.
        return LanguageGuess(
            words=total, runner_up=NAMES.get(second, second), scores=scores
        )
    return LanguageGuess(
        tag=best,
        name=NAMES.get(best, best),
        confidence=round(min(1.0, best_score * 3.0), 2),
        runner_up=NAMES.get(second, second),
        words=total,
        scores=scores,
    )


# -- which match -----------------------------------------------------------
#: Club-type abbreviations and words that appear in dozens of club names.
#: Removed before matching, because a transcript containing "city" tells you
#: nothing when four of the fixtures are City sides -- and a fixture matched
#: on nothing but "fc" would score as well as the real one.
GENERIC = frozenset({
    "fc", "cf", "ac", "sc", "ss", "as", "us", "sv", "tsg", "vfl", "vfb",
    "bsc", "afc", "cd", "ud", "sd", "ca", "club", "city", "united", "town",
    "county", "athletic", "atletico", "real", "sporting", "racing", "rovers",
    "albion", "wanderers", "borough", "de", "do", "da", "of", "the", "and",
    "calcio", "futbol", "football", "futebol", "sport", "sports", "team",
    "ii", "b", "u21", "u23", "women", "ladies",
})

#: Shortest token worth matching on. "as", "us" and "ii" are noise; "ajax",
#: "roma" and "lyon" are whole clubs.
MIN_NAME_TOKEN = 4


@dataclass(frozen=True, slots=True)
class FixtureRef:
    """A fixture, reduced to what matching needs.

    A plain value rather than the project's ``Match``: this module is in the
    domain and may not import the fixture feed, and the feed's type carries a
    kick-off time, a status and two scores that have nothing to do with
    recognising a name in a sentence.
    """

    id: str
    home: str
    away: str
    competition: str = ""
    state: str = ""

    @property
    def name(self) -> str:
        return f"{self.home} v {self.away}"


@dataclass(frozen=True, slots=True)
class MatchGuess:
    """Which fixture the commentary is about, and how firmly.

    ``both_sides`` is the field that matters. A transcript that mentions one
    team names a *club*; one that mentions both names a *fixture*. Everything
    else being equal, that distinction is the difference between a guess worth
    acting on and one worth showing.
    """

    fixture: FixtureRef | None = None
    home_hits: int = 0
    away_hits: int = 0
    confidence: float = 0.0
    runner_up: str = ""
    considered: int = 0

    @property
    def known(self) -> bool:
        return self.fixture is not None

    @property
    def both_sides(self) -> bool:
        return self.home_hits > 0 and self.away_hits > 0

    def to_dict(self) -> dict:
        out: dict = {
            "confidence": round(self.confidence, 2),
            "both_sides": self.both_sides,
            "home_hits": self.home_hits,
            "away_hits": self.away_hits,
            "runner_up": self.runner_up,
            "considered": self.considered,
        }
        if self.fixture is not None:
            out.update({
                "id": self.fixture.id,
                "home": self.fixture.home,
                "away": self.fixture.away,
                "name": self.fixture.name,
                "competition": self.fixture.competition,
                "state": self.fixture.state,
            })
        return out


def name_tokens(name: str) -> frozenset[str]:
    """The words in a club name worth recognising.

    Accents are stripped here, unlike in :func:`tokens`, and for the opposite
    reason: a transcriber writes "Atletico" as often as "Atletico" with the
    accent and "Besiktas" as often as the Turkish spelling, so insisting on
    the diacritic would miss the club. Language detection needs the accent;
    name matching needs it gone.
    """
    flat = unicodedata.normalize("NFKD", name or "")
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    out = {
        t for t in tokens(flat)
        if len(t) >= MIN_NAME_TOKEN and t not in GENERIC
    }
    if not out:
        # Every token was generic or short -- "AS Roma" with a shorter
        # minimum, or a one-word club in the generic list. Fall back to
        # whatever is there rather than matching nothing at all.
        out = {t for t in tokens(flat) if t not in GENERIC} or set(tokens(flat))
    return frozenset(out)


def identify_match(text: str, fixtures: list[FixtureRef]) -> MatchGuess:
    """Which of ``fixtures`` the commentary is about.

    Scored on both sides separately and deliberately: the winner is the
    fixture with the most distinct team-name words present, and a fixture
    whose two sides are both mentioned beats one with more hits on a single
    side. That ordering is what stops a derby being decided by whichever club
    happens to be named more often.
    """
    heard = set(name_tokens(text))
    if not heard or not fixtures:
        return MatchGuess(considered=len(fixtures))

    scored: list[tuple[tuple[int, int], FixtureRef, int, int]] = []
    for fixture in fixtures:
        home = len(heard & name_tokens(fixture.home))
        away = len(heard & name_tokens(fixture.away))
        if not home and not away:
            continue
        # Sort key: both sides first, then total hits. A tuple rather than a
        # weighted sum, so "mentioned both teams" can never be outvoted by
        # enough mentions of one.
        scored.append((
            (1 if home and away else 0, home + away), fixture, home, away
        ))

    if not scored:
        return MatchGuess(considered=len(fixtures))
    scored.sort(key=lambda row: row[0], reverse=True)
    _, best, home_hits, away_hits = scored[0]
    runner_up = scored[1][1].name if len(scored) > 1 else ""

    if home_hits and away_hits:
        # Both named. The remaining doubt is whether another fixture also has
        # both named, which happens when two matches share a club name.
        rival = scored[1][0][0] if len(scored) > 1 else 0
        confidence = 0.95 if not rival else 0.6
    else:
        # One side only: this is the right club, but possibly the wrong
        # fixture for it. Says "probably" and means it.
        confidence = 0.45

    return MatchGuess(
        fixture=best,
        home_hits=home_hits,
        away_hits=away_hits,
        confidence=confidence,
        runner_up=runner_up,
        considered=len(fixtures),
    )


__all__ = [
    "GENERIC", "GREEK_SHARE", "MIN_MARGIN", "MIN_NAME_TOKEN", "MIN_TOKENS",
    "NAMES", "FixtureRef", "LanguageGuess", "MatchGuess",
    "identify_language", "identify_match", "name_tokens", "primary", "tokens",
]
