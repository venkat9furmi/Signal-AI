"""What a channel name is allowed to be.

A domain rule, not a transport detail: a channel is an identifier MMC uses
(``football-41``, ``horses-03``, ``greyhounds-01``), and everything downstream
treats it as one. The problem is that it arrives from an HTTP request and then
becomes part of a URL that ffmpeg is told to fetch.

Measured before this existed: ``{"channel": "../../x"}`` and
``{"channel": "x\\r\\ny"}`` were both accepted and started a run. Nothing bad
came of it on a localhost-only page, but a value that reaches a URL builder
and a subprocess should have a shape, and this is the cheapest possible place
to give it one.

Kept deliberately narrow. Every channel the platform has ever shown fits
``family-NN``; a name that does not fit is far likelier to be a mistake or a
probe than a channel nobody told us about.
"""

from __future__ import annotations

import re

from commentary_qc.translate.core.models import NotConfigured

#: Lowercase letters, digits and single hyphens. No slashes, no dots, no
#: whitespace, no control characters -- which is what rules out traversal,
#: header injection and URL trickery in one line.
CHANNEL_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

#: Long enough for anything real, short enough that a pathological value is
#: refused before it reaches a URL.
MAX_LENGTH = 64


def clean_channel(name: str) -> str:
    """The channel name, or raise :class:`NotConfigured` saying why.

    Case and surrounding space are forgiven, because a human typing
    ``Football-41`` means the channel. Nothing else is: a name is either an
    identifier or it is a mistake.
    """
    want = (name or "").strip().lower()
    if not want:
        raise NotConfigured("say which channel")
    if len(want) > MAX_LENGTH:
        raise NotConfigured(
            f"that channel name is {len(want)} characters; "
            f"the longest real one is well under {MAX_LENGTH}"
        )
    if not CHANNEL_RE.match(want):
        raise NotConfigured(
            f"{name!r} is not a channel name. They look like 'football-41' "
            f"or 'greyhounds-01': lowercase letters, digits and hyphens."
        )
    return want


def is_channel(name: str) -> bool:
    """Whether ``name`` is usable, without raising."""
    try:
        clean_channel(name)
    except NotConfigured:
        return False
    return True


__all__ = ["CHANNEL_RE", "MAX_LENGTH", "clean_channel", "is_channel"]
