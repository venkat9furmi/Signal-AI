"""Where captions are kept.

The table is defined here but written through the existing
:class:`~commentary_qc.store.EventStore`, which owns the only writer thread on
the recorder's database. That is not an incidental detail: this project already
spent an outage of visibility on a second writer, and translation -- a
passenger feature -- must not be the thing that puts measurement rows at risk.

So :class:`CaptionStore` holds a reference to the event store and borrows its
queue. It knows the SQL; the event store knows the thread.

Failures are rows too. A caption with ``status='error'`` or ``'skipped'`` is
what lets the page say "the provider timed out" instead of showing a gap,
which is indistinguishable from the commentator having said nothing.
"""

from __future__ import annotations

import time
from typing import Any

from commentary_qc.translate.core.models import Caption

SCHEMA = """
-- One row per segment of commentary that went through the translation
-- pipeline, successful or not. `mode` records whether this was a shadow run
-- or production, so a day of shadow output can be read back and judged
-- without being confused for something a commentator saw.
CREATE TABLE IF NOT EXISTS captions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wall            REAL NOT NULL,
    channel         TEXT NOT NULL,
    session         TEXT,
    seq             INTEGER,
    t0              REAL,
    t1              REAL,
    duration_s      REAL,
    mode            TEXT NOT NULL,        -- shadow | prod
    status          TEXT NOT NULL,        -- ok | error | skipped
    provider        TEXT,
    model           TEXT,
    source_language TEXT,
    source_text     TEXT,
    target_language TEXT,
    target_text     TEXT,
    latency_s       REAL,
    cost_s          REAL,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS ix_captions_channel_wall ON captions(channel, wall DESC);
CREATE INDEX IF NOT EXISTS ix_captions_wall ON captions(wall DESC);
"""

_COLS = (
    "wall", "channel", "session", "seq", "t0", "t1", "duration_s", "mode",
    "status", "provider", "model", "source_language", "source_text",
    "target_language", "target_text", "latency_s", "cost_s", "error",
)

_INSERT = (
    f"INSERT INTO captions ({','.join(_COLS)}) "
    f"VALUES ({','.join('?' * len(_COLS))})"
)


class CaptionStore:
    """Reads and writes captions, on top of the event store's single writer."""

    def __init__(self, store: Any) -> None:
        self.store = store

    # -- writes ---------------------------------------------------------
    def record(self, caption: Caption) -> None:
        row = caption.to_dict()
        self.store._put(  # noqa: SLF001 - sharing the one writer is the point
            _INSERT, tuple(row.get(col) for col in _COLS)
        )

    # -- reads ----------------------------------------------------------
    def recent(
        self,
        channel: str | None = None,
        limit: int = 50,
        mode: str | None = None,
        since: float | None = None,
        statuses: tuple[str, ...] = (),
    ) -> list[dict]:
        """Latest captions first, which is the order the page shows them in."""
        where, args = ["1=1"], []
        if channel:
            where.append("channel = ?")
            args.append(channel)
        if mode:
            where.append("mode = ?")
            args.append(mode)
        if since is not None:
            where.append("wall >= ?")
            args.append(since)
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        return self.store.query(
            f"SELECT * FROM captions WHERE {' AND '.join(where)}"
            " ORDER BY wall DESC LIMIT ?",
            tuple(args) + (max(1, min(limit, 500)),),
        )

    def spent_today(self, now: float | None = None) -> tuple[float, dict[str, float]]:
        """Audio seconds already charged today, total and per channel.

        Read at startup so the daily cap survives a restart. Without it,
        Docker's restart policy would hand the pipeline a fresh allowance
        every time a container came back.
        """
        now = time.time() if now is None else now
        midnight = (now // 86400) * 86400
        rows = self.store.query(
            "SELECT channel, SUM(cost_s) AS spent FROM captions"
            " WHERE wall >= ? GROUP BY channel",
            (midnight,),
        )
        by_channel = {r["channel"]: float(r["spent"] or 0.0) for r in rows}
        return sum(by_channel.values()), by_channel

    def summary(self, since: float, mode: str | None = None) -> dict:
        """How the pipeline has been doing, for the page's header.

        Median latency rather than mean: one 90 s timeout drags a mean far
        enough to hide that everything else was quick.
        """
        where, args = ["wall >= ?"], [since]
        if mode:
            where.append("mode = ?")
            args.append(mode)
        clause = " AND ".join(where)
        rows = self.store.query(
            f"SELECT status, COUNT(*) AS n, SUM(cost_s) AS cost FROM captions"
            f" WHERE {clause} GROUP BY status",
            tuple(args),
        )
        counts = {r["status"]: int(r["n"]) for r in rows}
        cost = sum(float(r["cost"] or 0.0) for r in rows)
        lat = self.store.query(
            f"SELECT latency_s FROM captions WHERE {clause} AND status = 'ok'"
            " AND latency_s IS NOT NULL ORDER BY latency_s",
            tuple(args),
        )
        values = [float(r["latency_s"]) for r in lat]
        median = values[len(values) // 2] if values else None
        worst = values[-1] if values else None
        total = sum(counts.values())
        return {
            "captions": total,
            "ok": counts.get("ok", 0),
            "errors": counts.get("error", 0),
            "skipped": counts.get("skipped", 0),
            "success_rate": round(counts.get("ok", 0) / total, 3) if total else None,
            "audio_minutes": round(cost / 60, 2),
            "latency_median_s": round(median, 2) if median is not None else None,
            "latency_worst_s": round(worst, 2) if worst is not None else None,
        }

    def channels_seen(self, since: float) -> list[dict]:
        """Per-channel counts, so the page can list what is being translated."""
        return self.store.query(
            "SELECT channel, mode, COUNT(*) AS n,"
            " SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok,"
            " SUM(cost_s) AS cost, MAX(wall) AS last_wall"
            " FROM captions WHERE wall >= ? GROUP BY channel, mode"
            " ORDER BY last_wall DESC",
            (since,),
        )
