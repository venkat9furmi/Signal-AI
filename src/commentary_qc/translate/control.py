"""The runtime switch, shared between two processes.

The page runs in the web container and the pipeline runs in the recorder. When
someone moves the mode from shadow to standard, or swaps the provider, that has to
cross a process boundary.

It crosses the same way a claim does, and for the same reason: the web app owns
the claims database and writes to it, the recorder only ever reads it. One
writer per SQLite file is the rule this project keeps, and it is what made
claim-starts-monitoring work, so switching models rides on the mechanism that
is already proven rather than inventing a second one.

The API key is deliberately **not** here. It comes from the environment, so no
value the page can write can ever change which account is billed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .settings import Mode, TranslateSettings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS translate_control (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    mode       TEXT    NOT NULL,
    provider   TEXT    NOT NULL,
    target     TEXT,
    -- A JSON list. Empty means "all eligible". Keep statement terminators
    -- out of these comments: the schema is applied one statement at a time,
    -- split on the terminator, because executescript opens its own
    -- transaction and would lose the busy timeout.
    channels   TEXT,
    updated_at REAL,
    updated_by TEXT
);
"""

#: Fields the page is allowed to change. Anything else -- budgets, segment
#: lengths, the key -- is deployment configuration, not a runtime switch, and
#: lives in the environment or the config file where it is reviewable.
WRITABLE = ("mode", "provider", "target", "channels")


def ensure(path: str) -> None:
    """Create the table. Only the owner of the file calls this."""
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        for stmt in filter(None, (s.strip() for s in SCHEMA.split(";"))):
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def write(
    path: str,
    *,
    mode: str | None = None,
    provider: str | None = None,
    target: str | None = None,
    channels: list[str] | None = None,
    by: str = "",
) -> dict:
    """Change the switch. Returns the row as it now stands.

    Validated here, before it is stored, so an impossible combination is
    rejected while someone is looking at the page -- rather than read by the
    recorder a few seconds later, in another process, with nobody watching.
    """
    current = read(path)
    new = {
        "mode": Mode.normalise(mode if mode is not None else current["mode"]),
        "provider": (provider if provider is not None else current["provider"]).strip().lower(),
        "target": (target if target is not None else current["target"]).strip().lower(),
        "channels": list(channels) if channels is not None else list(current["channels"]),
    }
    if new["mode"] not in Mode.ALL:
        raise ValueError(f"mode must be one of {Mode.ALL}")
    from .providers import available

    if new["provider"] not in available():
        raise ValueError(f"provider must be one of {available()}")

    ensure(path)
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            "INSERT INTO translate_control (id, mode, provider, target, channels,"
            " updated_at, updated_by) VALUES (1,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,"
            " provider=excluded.provider, target=excluded.target,"
            " channels=excluded.channels, updated_at=excluded.updated_at,"
            " updated_by=excluded.updated_by",
            (
                new["mode"], new["provider"], new["target"],
                json.dumps(new["channels"]), time.time(), by[:60],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    log.info(
        "translation switched to mode=%s provider=%s target=%s channels=%s by %s",
        new["mode"], new["provider"], new["target"],
        new["channels"] or "all eligible", by or "someone",
    )
    return read(path)


def read(path: str | None) -> dict[str, Any]:
    """The switch as it stands, read-only and never raising.

    A missing file, a missing table or an empty table all mean the same thing:
    nobody has touched the switch, so the configured defaults apply. The
    recorder reads this every few seconds and must not die because the page
    has not started yet.
    """
    blank: dict[str, Any] = {
        "mode": Mode.OFF, "provider": "mock", "target": "en-us",
        "channels": [], "updated_at": None, "updated_by": "", "set": False,
    }
    if not path or not Path(path).exists():
        return blank
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return blank
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT mode, provider, target, channels, updated_at, updated_by"
            " FROM translate_control WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return blank
    finally:
        conn.close()
    if row is None:
        return blank
    try:
        channels = json.loads(row["channels"] or "[]")
    except json.JSONDecodeError:
        channels = []
    return {
        "mode": Mode.normalise(row["mode"]) or Mode.OFF,
        "provider": row["provider"] or "mock",
        "target": row["target"] or "en-us",
        "channels": list(channels),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"] or "",
        "set": True,
    }


def apply(settings: TranslateSettings, control: dict) -> TranslateSettings:
    """Fold the switch into the settings the pipeline runs on.

    Two guards, both deliberate. A provider the deployment has not enabled is
    ignored rather than obeyed -- the page cannot turn on a paid model that
    was left out of ``enabled_providers``. And a mode the page asks for is
    only honoured if the chosen provider can actually run: without a key, the
    answer is off, not a stream of failures.
    """
    import dataclasses

    mode = Mode.normalise(control.get("mode")) or settings.mode
    provider = control.get("provider") or settings.provider
    if provider not in settings.enabled_providers:
        log.warning(
            "ignoring request for provider %r: not enabled in this deployment", provider
        )
        provider = settings.provider
    if provider == "camb" and not settings.camb_api_key and mode != Mode.OFF:
        log.warning("translation asked for Camb AI with no API key; staying off")
        mode = Mode.OFF
    return dataclasses.replace(
        settings,
        mode=mode,
        provider=provider,
        target_language=control.get("target") or settings.target_language,
        channels=tuple(control.get("channels") or settings.channels),
    )
