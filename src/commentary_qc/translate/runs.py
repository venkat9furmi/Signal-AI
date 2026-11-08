"""Where shadow runs and their audio are kept.

Its own SQLite file, owned by the web process. That follows the rule this
project already runs on: one writer per database file. The recorder owns
``live.db``, the page owns ``claims.db``, and shadow runs are produced by the
page -- so they get a third file rather than a second writer on somebody
else's.

The audio lives in the row. Two reasons it is not a directory of files: the
database is on a named Docker volume precisely because a Windows bind mount
cannot carry SQLite's shared memory, and adding a second storage mechanism
beside it would need its own volume, its own pruning and its own way of going
stale. And the volume is small -- a run is a few hundred kilobytes, and only
the most recent handful are kept.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any

from .models import TranslationRun

log = logging.getLogger(__name__)

#: Statements applied one at a time. No SQL comments anywhere in here: the
#: schema is split on the semicolon, and a semicolon inside a comment silently
#: cuts a statement in half.
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS translate_runs (
        id              TEXT PRIMARY KEY,
        created         REAL NOT NULL,
        mode            TEXT NOT NULL,
        kind            TEXT,
        channel         TEXT,
        seconds         REAL,
        provider        TEXT,
        model           TEXT,
        voice           TEXT,
        source_language TEXT,
        source_text     TEXT,
        target_language TEXT,
        target_text     TEXT,
        status          TEXT,
        error           TEXT,
        transcribe_s    REAL,
        translate_s     REAL,
        speak_s         REAL,
        total_s         REAL,
        credits         TEXT,
        source_url      TEXT,
        heard_audio     BLOB,
        spoken_audio    BLOB,
        heard_type      TEXT,
        spoken_type     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_created ON translate_runs (created DESC)",
)

_COLUMNS = (
    "id", "created", "mode", "kind", "channel", "seconds", "provider", "model", "voice",
    "source_language", "source_text", "target_language", "target_text",
    "status", "error", "transcribe_s", "translate_s", "speak_s", "total_s",
    "credits", "source_url", "heard_audio", "spoken_audio", "heard_type",
    "spoken_type",
)


class RunStore:
    """Shadow runs, newest first, with the audio attached."""

    #: How many runs keep their audio. Older rows stay -- the text and the
    #: timings are the record of what was tried -- but their blobs are
    #: dropped, because the point of keeping audio is listening to it and
    #: nobody goes back to last week's clip.
    KEEP_AUDIO = 25
    #: And how many rows are kept at all.
    KEEP_ROWS = 400
    #: A ceiling on the audio held in total. The row count alone was enough
    #: when every run was a 15 second clip; a live run is minutes of audio on
    #: both sides, so 25 of those would be hundreds of megabytes in a file
    #: that is meant to stay small. Whichever limit bites first wins.
    KEEP_AUDIO_BYTES = 192 * 1024 * 1024

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        # check_same_thread=False with an explicit lock: FastAPI runs a sync
        # endpoint on a worker thread, so the connection is reached from a
        # different thread each time even though only one call uses it at once.
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            for statement in SCHEMA:
                self._conn.execute(statement)
            self._conn.commit()
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Bring an older file up to the current column set.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already
        exists, so a column added after a database was first written would be
        missing and every insert would fail. Additive only -- nothing here
        drops or rewrites anything.
        """
        with self._lock:
            have = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(translate_runs)")
            }
            for name, decl in (("kind", "TEXT"),):
                if name not in have:
                    log.info("adding column %s to translate_runs", name)
                    self._conn.execute(
                        f"ALTER TABLE translate_runs ADD COLUMN {name} {decl}"
                    )
            self._conn.commit()

    # -- writing ---------------------------------------------------------
    def put(self, run: TranslationRun) -> TranslationRun:
        values = [getattr(run, name) for name in _COLUMNS]
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO translate_runs ({', '.join(_COLUMNS)}) "
                f"VALUES ({placeholders})",
                values,
            )
            self._conn.commit()
        self.prune()
        return run

    def prune(self) -> dict[str, int]:
        """Drop old audio, then old rows."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE translate_runs SET heard_audio = NULL, spoken_audio = NULL "
                "WHERE id NOT IN ("
                "  SELECT id FROM translate_runs ORDER BY created DESC LIMIT ?"
                ") AND (heard_audio IS NOT NULL OR spoken_audio IS NOT NULL)",
                (self.KEEP_AUDIO,),
            )
            stripped = cur.rowcount or 0
            keep: list[str] = []
            total = 0
            for row in self._conn.execute(
                "SELECT id,"
                " COALESCE(LENGTH(heard_audio), 0) + COALESCE(LENGTH(spoken_audio), 0)"
                " AS bytes FROM translate_runs"
                " WHERE heard_audio IS NOT NULL OR spoken_audio IS NOT NULL"
                " ORDER BY created DESC"
            ):
                total += int(row["bytes"] or 0)
                if total > self.KEEP_AUDIO_BYTES:
                    break
                keep.append(row["id"])
            cur = self._conn.execute(
                "UPDATE translate_runs SET heard_audio = NULL, spoken_audio = NULL"
                " WHERE (heard_audio IS NOT NULL OR spoken_audio IS NOT NULL)"
                f" AND id NOT IN ({', '.join('?' for _ in keep) or 'NULL'})",
                keep,
            )
            stripped += cur.rowcount or 0
            cur = self._conn.execute(
                "DELETE FROM translate_runs WHERE id NOT IN ("
                "  SELECT id FROM translate_runs ORDER BY created DESC LIMIT ?"
                ")",
                (self.KEEP_ROWS,),
            )
            removed = cur.rowcount or 0
            self._conn.commit()
        return {"audio_stripped": stripped, "rows_removed": removed}

    # -- reading ---------------------------------------------------------
    def recent(
        self, limit: int = 20, mode: str | None = None, channel: str | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, created, mode, channel, seconds, provider, model, voice,"
            " source_language, source_text, target_language, target_text, status,"
            " error, transcribe_s, translate_s, speak_s, total_s, credits,"
            " source_url, heard_type, spoken_type, kind,"
            " heard_audio IS NOT NULL AS has_heard,"
            " spoken_audio IS NOT NULL AS has_spoken"
            " FROM translate_runs WHERE 1=1"
        )
        args: list[Any] = []
        if mode:
            sql += " AND mode = ?"
            args.append(mode)
        if channel:
            sql += " AND channel = ?"
            args.append(channel)
        sql += " ORDER BY created DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["has_heard"] = bool(item.get("has_heard"))
            item["has_spoken"] = bool(item.get("has_spoken"))
            for key in ("seconds", "transcribe_s", "translate_s", "speak_s", "total_s"):
                if item.get(key) is not None:
                    item[key] = round(float(item[key]), 2)
            out.append(item)
        return out

    def audio(self, run_id: str, which: str) -> tuple[bytes, str] | None:
        """One run's audio and its media type, or ``None`` if it has none."""
        column = "spoken_audio" if which == "spoken" else "heard_audio"
        type_column = "spoken_type" if which == "spoken" else "heard_type"
        with self._lock:
            row = self._conn.execute(
                f"SELECT {column} AS blob, {type_column} AS kind "
                "FROM translate_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None or not row["blob"]:
            return None
        return bytes(row["blob"]), (row["kind"] or "audio/wav")

    def spent_today(self, mode: str | None = None) -> dict[str, Any]:
        """Audio sent since the UTC day began, which is what the budget counts.

        Read from the database rather than from the Budget object so the
        figure survives a restart of the web container. A spending guard that
        forgets at every deploy is not a guard.
        """
        day_start = (int(time.time()) // 86400) * 86400
        sql = (
            "SELECT COUNT(*) AS runs, COALESCE(SUM(seconds), 0) AS seconds,"
            " SUM(status = 'ok') AS ok, SUM(status != 'ok') AS failed"
            " FROM translate_runs WHERE created >= ? AND status != 'skipped'"
        )
        args: list[Any] = [day_start]
        if mode:
            sql += " AND mode = ?"
            args.append(mode)
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        by_channel: dict[str, float] = {}
        with self._lock:
            for item in self._conn.execute(
                "SELECT channel, COALESCE(SUM(seconds), 0) AS seconds"
                " FROM translate_runs WHERE created >= ? AND status != 'skipped'"
                " GROUP BY channel",
                (day_start,),
            ).fetchall():
                if item["channel"]:
                    by_channel[item["channel"]] = float(item["seconds"] or 0.0)
        return {
            "runs": int(row["runs"] or 0),
            "seconds": round(float(row["seconds"] or 0.0), 1),
            "ok": int(row["ok"] or 0),
            "failed": int(row["failed"] or 0),
            "by_channel": by_channel,
            "since": day_start,
        }

    def last_credits(self, mode: str | None = None) -> str:
        """The credit figure the provider reported most recently.

        The only spend number that comes from the provider rather than from
        our own count of seconds. Camb publishes no balance endpoint, so this
        is as close to a bill as anything available.
        """
        sql = (
            "SELECT credits FROM translate_runs"
            " WHERE credits IS NOT NULL AND credits != ''"
        )
        args: list[Any] = []
        if mode:
            sql += " AND mode = ?"
            args.append(mode)
        sql += " ORDER BY created DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return (row["credits"] if row else "") or ""

    def close(self) -> None:
        with self._lock:
            self._conn.close()
