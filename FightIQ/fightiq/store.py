"""SQLite persistence for parsed slips + grades."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "fightiq.db"


class TicketStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS slips (
                  id TEXT PRIMARY KEY,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  status TEXT NOT NULL DEFAULT 'open',
                  source TEXT,
                  book TEXT,
                  stake REAL,
                  to_win REAL,
                  raw_text TEXT,
                  quickpick_link TEXT,
                  quickpick_json TEXT,
                  notes_json TEXT,
                  grade_json TEXT
                );
                CREATE TABLE IF NOT EXISTS slip_legs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  slip_id TEXT NOT NULL,
                  idx INTEGER NOT NULL,
                  raw TEXT,
                  label TEXT,
                  selection TEXT,
                  market TEXT,
                  fighter TEXT,
                  opponent TEXT,
                  american INTEGER,
                  line REAL,
                  side TEXT,
                  confidence REAL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  grade_note TEXT,
                  result_json TEXT,
                  FOREIGN KEY(slip_id) REFERENCES slips(id)
                );
                CREATE INDEX IF NOT EXISTS idx_slips_status ON slips(status);
                CREATE INDEX IF NOT EXISTS idx_legs_slip ON slip_legs(slip_id);
                """
            )

    def save_parsed(self, parsed: dict[str, Any], *, slip_id: str | None = None) -> str:
        sid = slip_id or uuid.uuid4().hex[:12]
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO slips(
                  id, created_at, updated_at, status, source, book, stake, to_win,
                  raw_text, quickpick_link, quickpick_json, notes_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  updated_at=excluded.updated_at,
                  source=excluded.source,
                  book=excluded.book,
                  stake=excluded.stake,
                  raw_text=excluded.raw_text,
                  quickpick_link=excluded.quickpick_link,
                  notes_json=excluded.notes_json
                """,
                (
                    sid,
                    now,
                    now,
                    "open",
                    parsed.get("source"),
                    parsed.get("book"),
                    parsed.get("stake"),
                    parsed.get("to_win"),
                    parsed.get("raw_text"),
                    parsed.get("quickpick_link"),
                    json.dumps(parsed.get("quickpick_raw")) if parsed.get("quickpick_raw") else None,
                    json.dumps(parsed.get("notes") or []),
                ),
            )
            conn.execute("DELETE FROM slip_legs WHERE slip_id=?", (sid,))
            for i, leg in enumerate(parsed.get("legs") or []):
                conn.execute(
                    """
                    INSERT INTO slip_legs(
                      slip_id, idx, raw, label, selection, market, fighter, opponent,
                      american, line, side, confidence, status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending')
                    """,
                    (
                        sid,
                        i,
                        leg.get("raw"),
                        leg.get("label"),
                        leg.get("selection"),
                        leg.get("market"),
                        leg.get("fighter"),
                        leg.get("opponent"),
                        leg.get("american"),
                        leg.get("line"),
                        leg.get("side"),
                        leg.get("confidence"),
                    ),
                )
        return sid

    def list_slips(self, limit: int = 50, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM slips"
        args: list[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(q, args).fetchall()
            return [self._slip_row(conn, r) for r in rows]

    def get_slip(self, slip_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM slips WHERE id=?", (slip_id,)).fetchone()
            if not row:
                return None
            return self._slip_row(conn, row)

    def open_slips(self) -> list[dict]:
        return self.list_slips(limit=200, status="open") + self.list_slips(
            limit=200, status="partial"
        )

    def update_leg_grade(
        self,
        slip_id: str,
        idx: int,
        status: str,
        note: str,
        result: dict | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE slip_legs SET status=?, grade_note=?, result_json=?
                WHERE slip_id=? AND idx=?
                """,
                (status, note, json.dumps(result) if result else None, slip_id, idx),
            )
            self._refresh_slip_status(conn, slip_id)

    def set_slip_grade(self, slip_id: str, status: str, grade: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE slips SET status=?, grade_json=?, updated_at=? WHERE id=?
                """,
                (status, json.dumps(grade), time.time(), slip_id),
            )

    def _refresh_slip_status(self, conn: sqlite3.Connection, slip_id: str) -> None:
        legs = conn.execute(
            "SELECT status FROM slip_legs WHERE slip_id=?", (slip_id,)
        ).fetchall()
        if not legs:
            return
        statuses = [r["status"] for r in legs]
        if all(s == "won" for s in statuses):
            overall = "won"
        elif any(s == "lost" for s in statuses):
            overall = "lost"
        elif all(s in {"won", "push", "void"} for s in statuses) and any(
            s == "won" for s in statuses
        ):
            # all settled, mix of push/won
            overall = "won" if any(s == "won" for s in statuses) else "push"
        elif all(s in {"won", "push", "void", "lost"} for s in statuses):
            overall = "lost" if "lost" in statuses else "push"
        elif any(s == "pending" for s in statuses) and any(
            s in {"won", "lost", "push"} for s in statuses
        ):
            overall = "partial"
        else:
            overall = "open"
        conn.execute(
            "UPDATE slips SET status=?, updated_at=? WHERE id=?",
            (overall, time.time(), slip_id),
        )

    def _slip_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        legs = conn.execute(
            "SELECT * FROM slip_legs WHERE slip_id=? ORDER BY idx", (row["id"],)
        ).fetchall()
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "source": row["source"],
            "book": row["book"],
            "stake": row["stake"],
            "to_win": row["to_win"],
            "raw_text": row["raw_text"],
            "quickpick_link": row["quickpick_link"],
            "notes": json.loads(row["notes_json"] or "[]"),
            "grade": json.loads(row["grade_json"] or "null"),
            "legs": [
                {
                    "idx": leg["idx"],
                    "raw": leg["raw"],
                    "label": leg["label"],
                    "selection": leg["selection"],
                    "market": leg["market"],
                    "fighter": leg["fighter"],
                    "opponent": leg["opponent"],
                    "american": leg["american"],
                    "line": leg["line"],
                    "side": leg["side"],
                    "confidence": leg["confidence"],
                    "status": leg["status"],
                    "grade_note": leg["grade_note"],
                    "result": json.loads(leg["result_json"] or "null"),
                }
                for leg in legs
            ],
        }
