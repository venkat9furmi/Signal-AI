"""Which adapters exist, and how one gets built.

A registry rather than an ``if`` ladder, so adding a model is one entry here
and a new file beside it -- nothing in the pipeline, the API or the page knows
a provider's name. ``describe`` is what the page renders, which is why the
descriptions read as sentences and not as identifiers.
"""

from __future__ import annotations

from typing import Any, Callable

from commentary_qc.translate.core.models import NotConfigured
from commentary_qc.translate.settings import TranslateSettings
from commentary_qc.translate.adapters.camb.provider import CambProvider
from commentary_qc.translate.adapters.mock.provider import MockProvider

#: name -> factory. A factory takes the settings and returns something
#: satisfying :class:`~..ports.Provider`.
FACTORIES: dict[str, Callable[[TranslateSettings], Any]] = {
    "mock": lambda s: MockProvider(s),
    "camb": lambda s: CambProvider(s),
}

#: What each one is, in words the page can show.
DESCRIPTIONS: dict[str, dict[str, str]] = {
    "mock": {
        "title": "Offline stand-in",
        "detail": "Runs the whole pipeline without a network call. Free, and the "
                  "words are deliberately marked so nobody mistakes them for a "
                  "translation. Use it to measure segmenting, latency and volume.",
        "cost": "free",
    },
    "camb": {
        "title": "Camb AI",
        "detail": "Speech recognition then translation, both as asynchronous jobs. "
                  "Needs CAMB_API_KEY. Spends credit on every segment sent.",
        "cost": "paid",
    },
}


def available() -> list[str]:
    return sorted(FACTORIES)


def blocked_reason(name: str, settings: TranslateSettings) -> str:
    """Why this adapter cannot be used right now, or an empty string.

    One function so the page and the builder can never disagree. They did:
    ``describe`` told the page Camb was unselectable without a key while
    ``build`` happily returned one, which meant a request naming it got as far
    as capturing fifteen seconds of audio before failing on the first call.
    """
    key = (name or "").strip().lower()
    if key not in FACTORIES:
        return f"no such provider (have {available()})"
    if key not in settings.enabled_providers:
        return (f"not enabled; enabled_providers is "
                f"{list(settings.enabled_providers)}")
    if key == "camb" and not settings.camb_api_key:
        return "CAMB_API_KEY is not set"
    return ""


def build(name: str, settings: TranslateSettings) -> Any:
    """Construct a provider by name, refusing one that cannot work.

    Checked here rather than at the call site so there is one gate: a paid
    model cannot be reached by any path -- the page, the config file or a
    stray environment variable -- unless it was enabled deliberately and has
    what it needs to run.
    """
    key = (name or "").strip().lower()
    reason = blocked_reason(key, settings)
    if reason:
        raise NotConfigured(f"provider {key!r} cannot be used: {reason}")
    return FACTORIES[key](settings)


def describe(settings: TranslateSettings) -> list[dict]:
    """Every adapter, with whether it may be selected right now and why not."""
    out = []
    for name in available():
        info = dict(DESCRIPTIONS.get(name, {}))
        enabled = name in settings.enabled_providers
        blocked = blocked_reason(name, settings)
        out.append({
            "name": name,
            "title": info.get("title", name),
            "detail": info.get("detail", ""),
            "cost": info.get("cost", "unknown"),
            "enabled": enabled,
            "selectable": enabled and not blocked,
            "blocked": blocked,
            "selected": name == settings.provider,
        })
    return out


__all__ = ["FACTORIES", "available", "blocked_reason", "build", "describe",
           "CambProvider", "MockProvider"]
