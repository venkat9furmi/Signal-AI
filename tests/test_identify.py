"""Which language is on the channel, and which match is being played.

Both features exist to catch a mistake nobody currently notices: a
commentator signed into the wrong channel, and a channel nobody claimed. So
the tests that matter are the ones about being *wrong*: a guess made on too
little text, two languages that genuinely look alike, and a derby where one
club name appears in two fixtures.

The passages below are deliberately **not** built from the word lists in
``core/identify.py``. Scoring a vocabulary against itself proves nothing --
it would pass with the lists empty of everything except the words quoted
here. They are written as football commentary and then checked, which is the
only version of this test that can fail for a real reason.

    python tests/test_identify.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from commentary_qc.translate.core.identify import (  # noqa: E402
    FixtureRef,
    identify_language,
    identify_match,
    name_tokens,
)

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}\n          {detail}")


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


# -- real commentary, written for this test -------------------------------
# Each is a plausible twenty seconds from the languages MMC actually carries.
# Player and club names are left in, because they are what a real transcript
# is full of and they belong to no language in particular -- a detector that
# only works on passages scrubbed of names would not work on this platform.
PASSAGES = {
    "German": (
        "Und da kommt der Pass von Kimmich nach vorne, Mueller laeuft in den "
        "Strafraum hinein, aber der Verteidiger klaert zur Seite. Jetzt wieder "
        "Bayern, sie spielen heute Abend sehr geduldig, und die Zuschauer "
        "hier werden schon etwas unruhig."
    ),
    "Italian": (
        "Palla a Barella che cerca subito Lautaro in mezzo, ma il difensore "
        "anticipa tutto. Rimessa laterale per i nerazzurri, siamo al "
        "ventesimo minuto del primo tempo e non si e ancora visto un tiro "
        "verso la porta."
    ),
    "Portuguese": (
        "Bola para o Otavio, que procura logo o companheiro na direita, mas o "
        "defesa corta bem. Canto para os encarnados, estamos aos vinte "
        "minutos da primeira parte e ainda nao houve nada de perigoso aqui "
        "no estadio."
    ),
    "French": (
        "Le ballon arrive pour Dembele qui cherche tout de suite son "
        "coequipier sur la droite, mais le defenseur degage en touche. "
        "Corner pour les Parisiens, nous sommes a la vingtieme minute et ce "
        "n est pas encore tres ouvert."
    ),
    "English": (
        "The ball goes out wide to Saka on the right, he looks for a runner "
        "in the box, but the defender gets there ahead of him. Corner to "
        "Arsenal, twenty minutes gone here and it has been a cagey start "
        "from both sides."
    ),
    "Greek": (
        "Η μπάλα πηγαίνει στα δεξιά, ο παίκτης ψάχνει τη σέντρα μέσα στην "
        "περιοχή, αλλά ο αμυντικός προλαβαίνει. Κόρνερ για τον Ολυμπιακό, "
        "είκοσι λεπτά έχουν περάσει."
    ),
}


section("Each of MMC's six languages, from commentary not built from the lists")
for want, passage in PASSAGES.items():
    guess = identify_language(passage)
    check(
        f"{want.lower()} commentary reads as {want.lower()}",
        guess.name == want,
        f"got {guess.name or '(unsure)'} at {guess.confidence}, "
        f"runner-up {guess.runner_up or '(none)'}",
    )


section("Saying nothing, when there is nothing to say")
check(
    "a single shout is not a language",
    not identify_language("Goal!").known,
    "a two-word exclamation produced a verdict",
)
check(
    "silence is not a language",
    not identify_language("").known,
)
check(
    "a line of names is not a language",
    not identify_language(
        "Kimmich, Mueller, Sane, Davies, Upamecano, Kane, Olise, Pavlovic"
    ).known,
    "a team sheet produced a verdict",
)


section("The comparison that actually matters: configured against heard")
german = identify_language(PASSAGES["German"])
check(
    "German commentary agrees with a channel configured de-de",
    german.agrees_with("de-de"),
)
check(
    "German commentary disagrees with a channel configured it-it",
    not german.agrees_with("it-it"),
    "the wrong-channel case did not register as a mismatch",
)
portuguese = identify_language(PASSAGES["Portuguese"])
check(
    "pt-pt commentary does not complain about a pt-br channel",
    portuguese.agrees_with("pt-br"),
    "a locale difference was reported as a fault",
)
check(
    "an unsure guess never contradicts the configuration",
    identify_language("Goal!").agrees_with("el-gr"),
    "too little text produced a mismatch warning",
)
check(
    "a channel whose language nobody recorded is never a mismatch",
    german.agrees_with("auto") and german.agrees_with(""),
    "the racing channels have no language in channels.json, so 'auto' must "
    "not read as a claim -- it reported a fault on every one of them",
)


section("Greek is settled by its alphabet, not by a word list")
mixed = identify_language(
    "Η μπάλα στον Ολυμπιακό, pass from the right, ο τερματοφύλακας "
    "προλαβαίνει και σφυρίζει ο διαιτητής εδώ στο γήπεδο"
)
check(
    "Greek with English words in it is still Greek",
    mixed.name == "Greek",
    f"got {mixed.name or '(unsure)'}",
)
latin_with_a_greek_name = identify_language(
    "The ball goes wide to the right and Olympiacos have a corner here, "
    "twenty minutes gone in this match and it is still nothing each"
)
check(
    "English with a Greek club in it is not Greek",
    latin_with_a_greek_name.name == "English",
    f"got {latin_with_a_greek_name.name or '(unsure)'}",
)


# -- which match ----------------------------------------------------------
FIXTURES = [
    FixtureRef("401", "Inter Milan", "AC Milan", "Serie A", "live"),
    FixtureRef("402", "Benfica", "Sporting CP", "Primeira Liga", "live"),
    FixtureRef("403", "Bayern Munich", "Borussia Dortmund", "Bundesliga", "live"),
    FixtureRef("404", "Arsenal", "Manchester City", "Premier League", "live"),
    FixtureRef("405", "Norwich City", "Leicester City", "Championship", "pre"),
    FixtureRef("406", "Paris Saint-Germain", "Olympique Lyonnais", "Ligue 1", "live"),
]

section("Which match, from the names the commentator says out loud")
both = identify_match(
    "Good evening and welcome to the Allianz Arena for Bayern Munich against "
    "Borussia Dortmund, the biggest fixture in the German calendar.",
    FIXTURES,
)
check(
    "both teams named identifies the fixture",
    both.known and both.fixture.id == "403",
    f"got {both.fixture.name if both.known else '(none)'}",
)
check(
    "and says both sides were heard",
    both.both_sides and both.confidence >= 0.9,
    f"both_sides={both.both_sides} confidence={both.confidence}",
)

one_side = identify_match(
    "Arsenal come forward again, and the crowd here is getting behind them.",
    FIXTURES,
)
check(
    "one team named finds the fixture but is not confident",
    one_side.known and one_side.fixture.id == "404" and one_side.confidence < 0.6,
    f"got {one_side.fixture.name if one_side.known else '(none)'} "
    f"at {one_side.confidence}",
)
check(
    "and says only one side was heard",
    not one_side.both_sides,
)

check(
    "commentary naming nobody identifies nothing",
    not identify_match(
        "The ball is played out to the right and the cross is cleared.",
        FIXTURES,
    ).known,
    "a passage with no club in it still matched a fixture",
)
check(
    "an empty fixture list identifies nothing",
    not identify_match("Bayern Munich against Borussia Dortmund", []).known,
)


section("The traps: shared names, and club words that mean nothing")
check(
    "'City' alone does not pick a fixture",
    not identify_match(
        "City are pressing high up the pitch and have the ball back again.",
        FIXTURES,
    ).known,
    "the generic word 'city' matched a fixture on its own",
)
derby = identify_match(
    "Inter Milan against AC Milan, the Derby della Madonnina, and what an "
    "atmosphere at San Siro tonight.",
    FIXTURES,
)
check(
    "a derby where both clubs share a city word still resolves",
    derby.known and derby.fixture.id == "401",
    f"got {derby.fixture.name if derby.known else '(none)'}",
)
check(
    "'Manchester City' beats 'Norwich City' when Arsenal is also named",
    identify_match(
        "Arsenal versus Manchester City here at the Emirates.", FIXTURES
    ).fixture.id == "404",
)
check(
    "a club name with an accent is recognised without it",
    "lyonnais" in name_tokens("Olympique Lyonnais"),
)
check(
    "club-type words are dropped from a name",
    name_tokens("Sporting CP") and "sporting" not in name_tokens("Sporting CP"),
    f"got {sorted(name_tokens('Sporting CP'))}",
)
check(
    "a club whose every word is generic still matches on something",
    bool(name_tokens("Real Sporting Club")),
    "a name made entirely of generic words reduced to nothing",
)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
