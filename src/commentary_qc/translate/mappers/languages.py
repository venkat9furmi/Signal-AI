"""Our language names on one side, a provider's tags on the other.

``channels.json`` says "Portuguese". Camb AI wants a BCP-47 tag. The docs
deprecate their numeric ids in favour of tags but never print the tags, and
warn against assuming them -- so the table below is a *starting guess* that is
resolved against whatever the provider actually lists before it is used.

The resolution is deliberately forgiving in one direction only: ``pt-pt``
falling back to ``pt-br`` is fine, because a Portuguese transcriber handles
Portuguese. Falling back across languages is never fine, so it raises.
"""

from __future__ import annotations

import logging

from ..models import NotConfigured

log = logging.getLogger(__name__)


#: What ``channels.json`` calls a language -> the tag we ask for first.
#: MMC's six: English, Italian, Portuguese, German, Greek, French.
CHANNEL_LANGUAGE_TAGS: dict[str, str] = {
    "English": "en-us",
    "Italian": "it-it",
    "Portuguese": "pt-pt",
    "German": "de-de",
    "Greek": "el-gr",
    "French": "fr-fr",
    "Spanish": "es-es",
    "Dutch": "nl-nl",
    "Polish": "pl-pl",
    "Turkish": "tr-tr",
    "Arabic": "ar-sa",
}

#: Reverse direction, for printing a tag on the page.
TAG_NAMES: dict[str, str] = {tag: name for name, tag in CHANNEL_LANGUAGE_TAGS.items()}

#: Asking the provider to detect the language itself. Worth having: a channel
#: whose configured language is wrong -- which happens, the panel's language
#: column is hand-maintained -- transcribes as gibberish rather than failing,
#: and gibberish is hard to spot in a language you do not read.
AUTO = "auto"


def tag_for_channel(language: str | None) -> str:
    """The tag to ask for, given a channel's configured language name.

    An unknown or missing language becomes ``auto`` rather than an error: a
    channel with a blank language column should still be transcribed.
    """
    if not language:
        return AUTO
    return CHANNEL_LANGUAGE_TAGS.get(language.strip(), AUTO)


def primary(tag: str) -> str:
    """``pt-br`` -> ``pt``. The part that decides whether two tags are the
    same language at all."""
    return (tag or "").strip().lower().split("-", 1)[0]


class LanguageMap:
    """A provider's language catalogue, and how our tags land in it.

    Constructed from whatever the provider lists. Until that happens it is
    empty, and an empty map permits everything -- the alternative would be
    refusing to work when the catalogue endpoint is unreachable, which turns a
    cosmetic problem into an outage.
    """

    def __init__(self, catalogue: dict[str, str] | None = None) -> None:
        #: tag -> human name, exactly as the provider gave it.
        self.catalogue: dict[str, str] = {}
        if catalogue:
            self.update(catalogue)

    def update(self, catalogue: dict[str, str]) -> "LanguageMap":
        self.catalogue = {
            str(tag).strip().lower(): str(name) for tag, name in catalogue.items() if tag
        }
        return self

    @property
    def known(self) -> bool:
        return bool(self.catalogue)

    def resolve(self, tag: str) -> str:
        """The tag to actually send.

        ``auto`` passes straight through. An exact match wins. Otherwise any
        tag for the same language is accepted, with a note in the log so the
        guess in :data:`CHANNEL_LANGUAGE_TAGS` can be corrected. A language
        the provider does not carry at all raises, because sending it would
        produce confident nonsense rather than an error.
        """
        want = (tag or "").strip().lower()
        if not want or want == AUTO:
            return AUTO
        if not self.known:
            return want
        if want in self.catalogue:
            return want
        root = primary(want)
        for candidate in sorted(self.catalogue):
            if primary(candidate) == root:
                log.info(
                    "language %s is not offered; using %s instead", want, candidate
                )
                return candidate
        raise NotConfigured(
            f"the provider does not offer {want!r} "
            f"(it lists {len(self.catalogue)} languages)"
        )

    def name(self, tag: str) -> str:
        """A readable name for a tag, for the page."""
        want = (tag or "").strip().lower()
        if want == AUTO:
            return "detect"
        return self.catalogue.get(want) or TAG_NAMES.get(want) or want or "unknown"

    def to_dict(self) -> dict:
        return {"count": len(self.catalogue), "tags": sorted(self.catalogue)}
