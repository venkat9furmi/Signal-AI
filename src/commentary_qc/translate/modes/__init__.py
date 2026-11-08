"""The execution-mode registry.

One place that knows which modes exist, so the endpoints can be namespaced by
mode without a conditional anywhere: ``/api/translate/{mode}/run`` looks the
mode up here and either gets a wiring or a clean refusal.

Built once per process, because a mode holds the thing that has to outlive a
request -- its budget. A registry rebuilt per call would hand back a fresh
daily allowance every time, which is the one bug a spending guard cannot
afford.
"""

from __future__ import annotations

from typing import Any

from ..models import ModeNotBuilt
from ..settings import TranslateSettings
from .base import ExecutionMode, ModeInfo, NotBuiltMode, Wiring
from .shadow import ShadowMode
from .standard import StandardMode

#: Declaration order is the order the page shows them: test, then production.
ORDER = ("shadow", "standard")


class ModeRegistry:
    """The modes this deployment has, already wired to its settings."""

    def __init__(self, settings: TranslateSettings) -> None:
        self.settings = settings
        self._modes: dict[str, Any] = {
            "shadow": ShadowMode(settings),
            "standard": StandardMode(),
        }

    def __contains__(self, name: object) -> bool:
        return str(name).strip().lower() in self._modes

    def get(self, name: str) -> Any:
        """The mode, or a refusal naming the ones that exist.

        A mode that is declared but unbuilt is returned, not refused: it is
        its ``wire`` that raises, so an endpoint can still report what the
        mode is for and why it is unavailable.
        """
        key = (name or "").strip().lower()
        if key not in self._modes:
            raise ModeNotBuilt(
                f"no such execution mode: {name!r} (have {list(ORDER)})"
            )
        return self._modes[key]

    def wire(self, name: str, provider: str = "") -> Wiring:
        """Build the infrastructure for one call in one mode."""
        return self.get(name).wire(self.settings, provider)

    def describe(self) -> list[dict]:
        """Every mode, built or not, in the words the page shows."""
        out = []
        for name in ORDER:
            mode = self._modes[name]
            info: ModeInfo = mode.info
            row = info.to_dict()
            row["default"] = name == self.default
            out.append(row)
        return out

    @property
    def default(self) -> str:
        """The mode a caller gets when it does not say.

        Shadow, and it stays shadow while production is unbuilt: defaulting to
        the mode that spends money and publishes would be the wrong way round.
        """
        return "shadow"


__all__ = [
    "ModeRegistry",
    "ExecutionMode",
    "ModeInfo",
    "NotBuiltMode",
    "ShadowMode",
    "StandardMode",
    "Wiring",
    "ORDER",
]
