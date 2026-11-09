"""Event store: the single source of truth both Slack and the app read from.

Deliberately not "Slack is the destination". If alerts went straight to Slack we
would have no history, no current state, and swapping or adding a delivery
channel later would mean rewriting the engine. Everything lands here first;
notifiers and the web view are readers.

SQLite for the MVP -- one file, no server. All writes funnel through a single
background thread, so the monitor threads never block on disk and SQLite never
sees concurrent writers.

Several *processes* do share the file though -- the recorder writes while the
commentator page reads -- so every connection sets a busy timeout. Without one,
the web app fails outright with "database is locked" the moment it starts while
a match is being recorded, which is exactly when it is needed.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from commentary_qc.translate.infra.captions import SCHEMA as _TRANSLATE_SCHEMA

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    wall      REAL    NOT NULL,
    channel   TEXT    NOT NULL,
    session   TEXT,
    event     TEXT    NOT NULL,          -- FIRE | CLEAR
    rule      TEXT    NOT NULL,
    severity  TEXT    NOT NULL,
    message   TEXT    NOT NULL,
    t         REAL,
    held_s    REAL,
    evidence  TEXT                        -- JSON
);
CREATE INDEX IF NOT EXISTS ix_alerts_channel_wall ON alerts(channel, wall DESC);

-- One row per analysis window. This is the training set for the classifier
-- that eventually replaces the hand-tuned rules, so it keeps every field.
CREATE TABLE IF NOT EXISTS metrics (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    wall              REAL NOT NULL,
    channel           TEXT NOT NULL,
    session           TEXT,
    t                 REAL,
    lufs              REAL,
    lufs_short        REAL,
    lufs_minute       REAL,
    true_peak_dbtp    REAL,
    sample_peak_dbfs  REAL,
    clip_run          INTEGER,
    speech_ratio      REAL,
    voice_lufs        REAL,
    crowd_lufs        REAL,
    voice_to_crowd_db REAL,
    voice_to_crowd_med REAL,
    speech_ratio_med  REAL,
    level_sd          REAL,
    crowd_lufs_med    REAL,
    crowd_measured    INTEGER,
    loop_score        REAL,
    loop_lag_s        REAL
);
CREATE INDEX IF NOT EXISTS ix_metrics_channel_wall ON metrics(channel, wall DESC);

-- A commentator claiming a channel. This is what scopes the system: MMC's
-- channels cannot be identified by name, number or language, so a claimed
-- channel is an MMC channel by definition. It also supplies the fixture name,
-- which probing the audio can never provide, and tells the engine that
-- commentary is expected -- which is what turns "a holding loop is playing"
-- from a state into a fault.
CREATE TABLE IF NOT EXISTS claims (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT NOT NULL,
    commentator  TEXT,
    fixture      TEXT,
    event_id     TEXT,                    -- the picked match in the fixture feed
    kickoff      REAL,
    claimed_at   REAL NOT NULL,
    released_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_claims_open ON claims(channel, released_at);

-- Current state per channel, so the app can answer "what is happening right
-- now" with one cheap read instead of scanning the alert history.
CREATE TABLE IF NOT EXISTS channel_state (
    channel     TEXT PRIMARY KEY,
    live        INTEGER NOT NULL,
    session     TEXT,
    since       REAL,
    last_seen   REAL,
    active      TEXT,                     -- JSON list of firing rule ids
    last_metric TEXT                      -- JSON of the most recent window
);

-- One row per long-running process, refreshed every few seconds. Lets the
-- page say "the recorder is not running" instead of showing the last
-- measurements it happened to write before it stopped as if they were live.
CREATE TABLE IF NOT EXISTS service (
    name       TEXT PRIMARY KEY,
    pid        INTEGER,
    started    REAL,
    last_seen  REAL,
    info       TEXT                        -- JSON
);
""" + _TRANSLATE_SCHEMA

#: A claim older than this is treated as released. A match is at most about
#: 3.5 hours; a claim still open after 6 was simply never released (someone
#: closed the tab), and must not keep a channel monitored for ever.
CLAIM_TTL_S = 6 * 3600


def read_open_claims(path: str, max_age_s: float = CLAIM_TTL_S) -> dict[str, dict]:
    """Open claims, read-only, keyed by channel.

    For the recorder, which must never write to the claims file -- the web app
    owns it. A missing file or table means nobody has claimed anything yet.
    """
    from pathlib import Path

    f = Path(path)
    if not f.exists():
        return {}
    try:
        conn = sqlite3.connect(f"{f.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, channel, commentator, fixture, claimed_at FROM claims"
            " WHERE released_at IS NULL AND claimed_at > ? ORDER BY claimed_at",
            (time.time() - max_age_s,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {r["channel"]: dict(r) for r in rows}

_METRIC_COLS = (
    "t", "lufs", "lufs_short", "lufs_minute", "true_peak_dbtp", "sample_peak_dbfs", "clip_run",
    "speech_ratio", "voice_lufs", "crowd_lufs", "voice_to_crowd_db", "voice_to_crowd_med",
    "speech_ratio_med", "level_sd", "crowd_lufs_med", "crowd_measured",
    "loop_score", "loop_lag_s",
)


@dataclass(slots=True)
class _Write:
    sql: str
    args: tuple


class EventStore:
    """Append-only alert/metric store with a live per-channel state table."""

    #: Sentinel pushed on close to drain the writer cleanly.
    _STOP = object()

    def __init__(
        self, path: str = "commentary_qc.db", queue_size: int = 10_000, schema: bool = True
    ) -> None:
        self.path = path
        #: Whether this process owns the schema. Only the writer should create
        #: tables: a running recorder writes several rows a second, so a reader
        #: attempting DDL can wait forever for a lock it will never get.
        self.schema = schema
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._dropped = 0
        # Reads happen on caller threads, so they get their own connection.
        self._read_lock = threading.Lock()
        self._reader: sqlite3.Connection | None = None

    # -- lifecycle --------------------------------------------------------
    #: Seconds a connection waits for another process to finish writing.
    BUSY_TIMEOUT = 30.0

    def start(self) -> "EventStore":
        conn = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT)
        if self.schema:
            # One statement at a time rather than executescript: executescript
            # opens its own transaction, which loses the busy timeout.
            for stmt in filter(None, (x.strip() for x in _SCHEMA.split(";"))):
                conn.execute(stmt)
            # Older files predate some metric columns. Add whatever is missing,
            # so a run against an existing database keeps writing instead of
            # failing every insert.
            have = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
            for col in _METRIC_COLS:
                if col not in have:
                    conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} REAL")
            have = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
            for col, kind in (("event_id", "TEXT"), ("kickoff", "REAL")):
                if col not in have:
                    conn.execute(f"ALTER TABLE claims ADD COLUMN {col} {kind}")
        # WAL lets the web view read while the writer is committing.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={int(self.BUSY_TIMEOUT * 1000)}")
        conn.commit()
        conn.close()

        self._thread = threading.Thread(target=self._writer, name="store-writer", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if self._thread:
            self._q.put(self._STOP)
            self._thread.join(timeout=10)
            self._thread = None
        if self._reader:
            self._reader.close()
            self._reader = None
        if self._dropped:
            log.warning("store dropped %d writes under backpressure", self._dropped)

    def _writer(self) -> None:
        conn = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={int(self.BUSY_TIMEOUT * 1000)}")
        pending = 0
        last_commit = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item is self._STOP:
                break
            if item is not None:
                try:
                    conn.execute(item.sql, item.args)
                    pending += 1
                except sqlite3.Error:
                    log.exception("store write failed")
            # Batch commits: one fsync per second rather than per window keeps
            # 11 channels writing a row a second from thrashing the disk.
            if pending and (pending >= 200 or time.monotonic() - last_commit > 1.0):
                conn.commit()
                pending, last_commit = 0, time.monotonic()
        conn.commit()
        conn.close()

    def _put(self, sql: str, args: tuple) -> None:
        try:
            self._q.put_nowait(_Write(sql, args))
        except queue.Full:
            # Monitoring must never stall because the disk is slow. Losing a
            # metrics row is acceptable; blocking a capture thread is not.
            self._dropped += 1

    # -- writes -----------------------------------------------------------
    def record_alert(self, channel: str, session: str | None, alert: dict) -> None:
        self._put(
            "INSERT INTO alerts (wall, channel, session, event, rule, severity, message,"
            " t, held_s, evidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                alert.get("wall", time.time()), channel, session, alert["event"],
                alert["rule"], alert["severity"], alert["message"],
                alert.get("t"), alert.get("held_s"),
                json.dumps(alert.get("evidence", {})),
            ),
        )

    def record_metrics(self, channel: str, session: str | None, window: dict) -> None:
        self._put(
            f"INSERT INTO metrics (wall, channel, session, {','.join(_METRIC_COLS)}) "
            f"VALUES ({','.join('?' * (3 + len(_METRIC_COLS)))})",
            (window.get("wall", time.time()), channel, session)
            + tuple(window.get(c) for c in _METRIC_COLS),
        )

    def set_channel_state(
        self,
        channel: str,
        live: bool,
        session: str | None = None,
        since: float | None = None,
        active: list[str] | None = None,
        last_metric: dict | None = None,
    ) -> None:
        self._put(
            "INSERT INTO channel_state (channel, live, session, since, last_seen, active,"
            " last_metric) VALUES (?,?,?,?,?,?,?) ON CONFLICT(channel) DO UPDATE SET"
            " live=excluded.live, session=excluded.session,"
            " since=COALESCE(channel_state.since, excluded.since),"
            " last_seen=excluded.last_seen, active=excluded.active,"
            " last_metric=excluded.last_metric",
            (
                channel, int(live), session, since or time.time(), time.time(),
                json.dumps(active or []),
                json.dumps(last_metric) if last_metric else None,
            ),
        )

    # -- claims -----------------------------------------------------------
    def claim(
        self, channel: str, commentator: str, fixture: str,
        event_id: str = "", kickoff: float | None = None,
    ) -> None:
        """Put a commentator on air for a channel, releasing any stale claim."""
        self._put(
            "UPDATE claims SET released_at=? WHERE channel=? AND released_at IS NULL",
            (time.time(), channel),
        )
        self._put(
            "INSERT INTO claims (channel, commentator, fixture, event_id, kickoff, claimed_at)"
            " VALUES (?,?,?,?,?,?)",
            (channel, commentator, fixture, event_id or None, kickoff, time.time()),
        )

    def release(self, channel: str) -> None:
        self._put(
            "UPDATE claims SET released_at=? WHERE channel=? AND released_at IS NULL",
            (time.time(), channel),
        )

    def active_claims(self, max_age_s: float = CLAIM_TTL_S) -> dict[str, dict]:
        rows = self.query(
            "SELECT * FROM claims WHERE released_at IS NULL AND claimed_at > ?"
            " ORDER BY claimed_at",
            (time.time() - max_age_s,),
        )
        return {r["channel"]: r for r in rows}

    def prune(self, days: float) -> int:
        """Delete measurements older than `days`; return how many go.

        The per-second measurements are the whole size of this file (about
        105 MB a day for 13 channels); alerts are a few hundred rows and are
        never pruned, because "what happened last month" is a question people
        actually ask. Deleting rows does not shrink the file, it reuses the
        space, so the size settles at roughly `days` worth instead of growing
        for ever.
        """
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        rows = self.query("SELECT COUNT(*) AS n FROM metrics WHERE wall < ?", (cutoff,))
        n = int(rows[0]["n"]) if rows else 0
        if n:
            self._put("DELETE FROM metrics WHERE wall < ?", (cutoff,))
        return n

    def mark_all_down(self) -> None:
        """Forget live state left behind by a previous run.

        A recorder that was killed never got to mark its channels off air, so
        on start every channel is down until a monitor says otherwise.
        """
        self._put("UPDATE channel_state SET live=0, active='[]', since=NULL, last_metric=NULL", ())

    def heartbeat(self, name: str, started: float, info: dict) -> None:
        import os

        self._put(
            "INSERT INTO service (name, pid, started, last_seen, info) VALUES (?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET pid=excluded.pid, started=excluded.started,"
            " last_seen=excluded.last_seen, info=excluded.info",
            (name, os.getpid(), started, time.time(), json.dumps(info)),
        )

    def clear_since(self, channel: str) -> None:
        """Reset the 'live since' stamp when a channel goes down."""
        self._put("UPDATE channel_state SET since=NULL WHERE channel=?", (channel,))

    # -- reads ------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        if self._reader is None:
            self._reader = sqlite3.connect(
                self.path, check_same_thread=False, timeout=self.BUSY_TIMEOUT
            )
            self._reader.execute(f"PRAGMA busy_timeout={int(self.BUSY_TIMEOUT * 1000)}")
            self._reader.row_factory = sqlite3.Row
        return self._reader

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._read_lock:
            try:
                return [dict(r) for r in self._conn().execute(sql, args).fetchall()]
            except sqlite3.OperationalError as exc:
                # The page can start before the recorder has ever created its
                # tables. Nothing recorded yet is an empty answer, not a crash.
                if "no such table" in str(exc):
                    return []
                raise

    def service(self, name: str) -> dict | None:
        rows = self.query("SELECT * FROM service WHERE name=?", (name,))
        if not rows:
            return None
        row = rows[0]
        row["info"] = json.loads(row["info"] or "{}")
        return row

    def history(self, channel: str, since: float, max_points: int = 300) -> list[list]:
        """Loudness over time for one channel: [wall, lufs_short, lufs_minute]."""
        rows = self.query(
            "SELECT wall, lufs_short, lufs_minute FROM metrics WHERE channel=? AND wall>=?"
            " ORDER BY wall",
            (channel, since),
        )
        step = max(1, len(rows) // max_points)
        return [[r["wall"], r["lufs_short"], r["lufs_minute"]] for r in rows[::step]]

    def current_state(self) -> list[dict]:
        rows = self.query("SELECT * FROM channel_state ORDER BY channel")
        for r in rows:
            r["active"] = json.loads(r["active"] or "[]")
            r["last_metric"] = json.loads(r["last_metric"]) if r["last_metric"] else None
        return rows

    def recent_alerts(
        self, limit: int = 100, channel: str | None = None, session: str | None = None
    ) -> list[dict]:
        if channel and session:
            rows = self.query(
                "SELECT * FROM alerts WHERE channel=? AND session=? ORDER BY wall DESC LIMIT ?",
                (channel, session, limit),
            )
        elif channel:
            rows = self.query(
                "SELECT * FROM alerts WHERE channel=? ORDER BY wall DESC LIMIT ?",
                (channel, limit),
            )
        else:
            rows = self.query("SELECT * FROM alerts ORDER BY wall DESC LIMIT ?", (limit,))
        for r in rows:
            r["evidence"] = json.loads(r["evidence"] or "{}")
        return rows
