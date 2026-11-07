"""Vendor JSON on one side, our domain types on the other.

Every mapper here is a pure function: a dict in, a dataclass out, no HTTP and
no clock. That is what lets the wire format of a provider be tested against
the JSON its documentation prints, with no key and no network -- and it keeps
the adapters in ``providers/`` down to transport.
"""

from . import camb, languages

__all__ = ["camb", "languages"]
