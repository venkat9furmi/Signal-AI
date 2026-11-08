"""What an execution mode is.

A mode is not a feature flag. It is a named *wiring*: given the settings, it
decides which provider is built, which model that provider is pointed at, what
it is allowed to spend, where the result is stored and who is allowed to see
it. The endpoints are namespaced by mode -- ``/api/translate/shadow/run`` and
``/api/translate/standard/run`` -- so calling one is choosing an execution
profile, not passing an argument that something downstream may ignore.

Two modes are declared:

``shadow``
    For testing. Runs against real commentary, keeps every byte of audio so
    the output can be listened to, spends from a small deliberate budget, and
    shows the result to nobody but whoever pressed the button.

``standard``
    For production. Declared here and deliberately not implemented yet.

The reason both are declared while only one is built is that the shape has to
exist before the second one can be a change of wiring rather than a rewrite.
A mode that is missing raises :class:`~..models.ModeNotBuilt`; it never
quietly falls back to the other one, because asking for production and
silently getting the test wiring is the single failure nobody would notice
from the outside.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..budget import Budget
from ..models import ModeNotBuilt
from ..settings import TranslateSettings


@dataclass(frozen=True, slots=True)
class ModeInfo:
    """What a mode is, in the words the page shows."""

    name: str
    title: str
    #: One sentence: what this mode is for.
    purpose: str
    #: Who sees the output. The honest difference between the modes.
    audience: str
    #: Is there an implementation behind it?
    built: bool = False
    #: May the output be shown to commentators or sent to Slack?
    publishes: bool = False
    #: Is the audio kept so somebody can listen to it afterwards?
    retains_audio: bool = False
    #: Why it is not built, when it is not.
    pending: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class Wiring:
    """Everything one mode's endpoints execute with.

    Built fresh per request rather than held as process state. A provider is a
    thin object over ``urllib``, so constructing one costs nothing, and a
    request that builds its own wiring cannot be affected by a setting another
    request changed halfway through -- which matters most for the one thing
    that is shared on purpose: the budget.
    """

    mode: str
    settings: TranslateSettings
    provider: Any
    budget: Budget
    publishes: bool = False
    retains_audio: bool = False
    #: Applied to the settings by the mode, so the page can show what the mode
    #: changed rather than leaving it implied in the code.
    overrides: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        try:
            self.provider.close()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass


@runtime_checkable
class ExecutionMode(Protocol):
    """A named wiring of infrastructure, model and audience."""

    info: ModeInfo

    def wire(self, settings: TranslateSettings, provider: str = "") -> Wiring:
        """Build the infrastructure this mode runs on.

        ``provider`` overrides the configured adapter for this one call, which
        is how a model is switched on and off from the page without changing
        what the deployment defaults to.
        """
        ...


class NotBuiltMode:
    """A mode that is declared but has no implementation.

    Exists so the registry can list it, the page can show it greyed out with
    the reason, and its endpoints can answer with a straight "not built yet"
    instead of a 404 that looks like a typo in the URL.
    """

    def __init__(self, info: ModeInfo) -> None:
        self.info = info

    def wire(self, settings: TranslateSettings, provider: str = "") -> Wiring:
        raise ModeNotBuilt(
            f"{self.info.name} mode is not built yet"
            + (f": {self.info.pending}" if self.info.pending else "")
        )
