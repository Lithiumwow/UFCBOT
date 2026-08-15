"""
SQLite persistence layer (via aiosqlite). One `bets` table, shared across
sports, distinguished by a `sport` column ("ufc" or "nba") so /bet-ufc and
/bet-nba (and their /results-ufc / /results-nba counterparts) never mix
each other's data.

Statuses: "pending", "won", "loss", "void"
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    guild_id      INTEGER,
    channel_id    INTEGER,
    message_id    INTEGER,
    sport         TEXT NOT NULL DEFAULT 'ufc',
    event         TEXT,
    bet_title     TEXT,
    units         REAL NOT NULL DEFAULT 1.0,
    odds          INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL,
    stake_gbp     REAL,
    returns_gbp   REAL,
    fighter_pick  TEXT,
    opponent_pick TEXT,
    outcome_type  TEXT,
    outcome_round INTEGER,
    co_user_id    INTEGER,
    is_collab     INTEGER NOT NULL DEFAULT 0,
    partner_units REAL,
    partner_odds  INTEGER
);

CREATE TABLE IF NOT EXISTS predictions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    event          TEXT NOT NULL,
    fight_index    INTEGER NOT NULL,
    fighter_a      TEXT NOT NULL,
    fighter_b      TEXT NOT NULL,
    picked_fighter TEXT NOT NULL,
    confidence     TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE(user_id, event, fight_index)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id    INTEGER PRIMARY KEY,
    unit_value REAL NOT NULL DEFAULT 100.0,
    currency   TEXT
);

CREATE TABLE IF NOT EXISTS monitored_events (
    event      TEXT PRIMARY KEY,
    started_by INTEGER NOT NULL,
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bet_legs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id        INTEGER NOT NULL,
    leg_index     INTEGER NOT NULL,
    description   TEXT NOT NULL,
    fighter_pick  TEXT,
    outcome_type  TEXT,
    outcome_round INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',
    added_by      INTEGER
);

CREATE TABLE IF NOT EXISTS collab_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    event           TEXT,
    host_user_id    INTEGER NOT NULL,
    partner_user_id INTEGER,
    guild_id        INTEGER,
    channel_id      INTEGER,
    message_id      INTEGER,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL,
    host_units      REAL,
    host_odds       INTEGER,
    partner_units   REAL,
    partner_odds    INTEGER
);

CREATE TABLE IF NOT EXISTS collab_legs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    leg_index     INTEGER NOT NULL,
    description   TEXT NOT NULL,
    fighter_pick  TEXT,
    outcome_type  TEXT,
    outcome_round INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS openai_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    source            TEXT,
    user_id           INTEGER
);
"""


class Database:
    """Thin async wrapper around a single SQLite connection."""

    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Add columns introduced after a database may already exist.
        SQLite has no 'ADD COLUMN IF NOT EXISTS', so just swallow the
        'duplicate column' error on databases that already have them."""
        for statement in (
            "ALTER TABLE bets ADD COLUMN stake_gbp REAL",
            "ALTER TABLE bets ADD COLUMN returns_gbp REAL",
            "ALTER TABLE bets ADD COLUMN sport TEXT NOT NULL DEFAULT 'ufc'",
            "ALTER TABLE predictions ADD COLUMN confidence TEXT",
            "ALTER TABLE bets ADD COLUMN fighter_pick TEXT",
            "ALTER TABLE bets ADD COLUMN opponent_pick TEXT",
            "ALTER TABLE bets ADD COLUMN outcome_type TEXT",
            "ALTER TABLE bets ADD COLUMN outcome_round INTEGER",
            "ALTER TABLE user_settings ADD COLUMN currency TEXT",
            "ALTER TABLE bets ADD COLUMN co_user_id INTEGER",
            "ALTER TABLE bets ADD COLUMN is_collab INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE bet_legs ADD COLUMN added_by INTEGER",
            "ALTER TABLE bets ADD COLUMN partner_units REAL",
            "ALTER TABLE bets ADD COLUMN partner_odds INTEGER",
            "ALTER TABLE collab_sessions ADD COLUMN host_units REAL",
            "ALTER TABLE collab_sessions ADD COLUMN host_odds INTEGER",
            "ALTER TABLE collab_sessions ADD COLUMN partner_units REAL",
            "ALTER TABLE collab_sessions ADD COLUMN partner_odds INTEGER",
        ):
            try:
                await self._conn.execute(statement)
            except aiosqlite.OperationalError:
                pass  # column already exists

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # ---------- writes ----------

    async def add_bet(
        self,
        user_id: int,
        guild_id: Optional[int],
        channel_id: Optional[int],
        event: Optional[str],
        bet_title: Optional[str],
        units: float,
        odds: Optional[int],
        sport: str = "ufc",
        stake_gbp: Optional[float] = None,
        returns_gbp: Optional[float] = None,
        fighter_pick: Optional[str] = None,
        opponent_pick: Optional[str] = None,
        outcome_type: Optional[str] = None,
        outcome_round: Optional[int] = None,
        co_user_id: Optional[int] = None,
        is_collab: bool = False,
        partner_units: Optional[float] = None,
        partner_odds: Optional[int] = None,
    ) -> int:
        cursor = await self._conn.execute(
            """
            INSERT INTO bets (user_id, guild_id, channel_id, sport, event, bet_title,
                               units, odds, status, created_at, stake_gbp, returns_gbp,
                               fighter_pick, opponent_pick, outcome_type, outcome_round,
                               co_user_id, is_collab, partner_units, partner_odds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                guild_id,
                channel_id,
                sport,
                event,
                bet_title,
                units,
                odds,
                datetime.datetime.utcnow().isoformat(),
                stake_gbp,
                returns_gbp,
                fighter_pick,
                opponent_pick,
                outcome_type,
                outcome_round,
                co_user_id,
                1 if is_collab else 0,
                partner_units,
                partner_odds,
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def set_message_id(self, bet_id: int, message_id: int) -> None:
        await self._conn.execute(
            "UPDATE bets SET message_id = ? WHERE id = ?", (message_id, bet_id)
        )
        await self._conn.commit()

    async def update_status(self, bet_id: int, status: str) -> None:
        await self._conn.execute(
            "UPDATE bets SET status = ? WHERE id = ?", (status, bet_id)
        )
        await self._conn.commit()

    async def delete_bet(self, bet_id: int) -> None:
        await self._conn.execute("DELETE FROM bet_legs WHERE bet_id = ?", (bet_id,))
        await self._conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
        await self._conn.commit()

    async def delete_bets_for_event(self, event: str, sport: str, user_id: int) -> None:
        cursor = await self._conn.execute(
            "SELECT id FROM bets WHERE event = ? AND sport = ? "
            "AND (user_id = ? OR co_user_id = ?)",
            (event, sport, user_id, user_id),
        )
        ids = [r["id"] for r in await cursor.fetchall()]
        for bet_id in ids:
            await self._conn.execute("DELETE FROM bet_legs WHERE bet_id = ?", (bet_id,))
        await self._conn.execute(
            "DELETE FROM bets WHERE event = ? AND sport = ? "
            "AND (user_id = ? OR co_user_id = ?)",
            (event, sport, user_id, user_id),
        )
        await self._conn.commit()

    async def update_bet_fields(
        self,
        bet_id: int,
        *,
        event: Optional[str],
        bet_title: Optional[str],
        units: Optional[float] = None,
        odds: Optional[int] = None,
        partner_units: Optional[float] = None,
        partner_odds: Optional[int] = None,
        edit_partner_side: bool = False,
    ) -> None:
        """By default updates the bet's own event/title/units/odds (the
        host's stake, for a collab bet). Pass edit_partner_side=True to
        instead update partner_units/partner_odds (leaving the host's
        stake untouched) -- used when the collab partner is the one
        editing, so they can never accidentally overwrite the host's
        numbers with their own."""
        if edit_partner_side:
            await self._conn.execute(
                "UPDATE bets SET event = ?, bet_title = ?, partner_units = ?, partner_odds = ? "
                "WHERE id = ?",
                (event, bet_title, partner_units, partner_odds, bet_id),
            )
        else:
            await self._conn.execute(
                "UPDATE bets SET event = ?, bet_title = ?, units = ?, odds = ?, "
                "stake_gbp = NULL, returns_gbp = NULL WHERE id = ?",
                (event, bet_title, units, odds, bet_id),
            )
        await self._conn.commit()

    # ---------- reads ----------

    async def get_bet(self, bet_id: int) -> Optional[dict[str, Any]]:
        cursor = await self._conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_bet_by_message_id(self, message_id: int) -> Optional[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM bets WHERE message_id = ?", (message_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_all_bets(self, sport: str, user_id: Optional[int] = None) -> list[dict[str, Any]]:
        """user_id=None returns every bet for that sport regardless of owner
        -- only used internally (e.g. re-registering persistent button
        views on startup), never to show one person another's data.
        When user_id is set, includes collab slips where they are co-author."""
        if user_id is None:
            cursor = await self._conn.execute(
                "SELECT * FROM bets WHERE sport = ? ORDER BY id DESC", (sport,)
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM bets WHERE sport = ? AND (user_id = ? OR co_user_id = ?) "
                "ORDER BY id DESC",
                (sport, user_id, user_id),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_bets_for_event(self, event: str, sport: str, user_id: int) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM bets WHERE event = ? AND sport = ? "
            "AND (user_id = ? OR co_user_id = ?) ORDER BY id DESC",
            (event, sport, user_id, user_id),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_bets_for_event_matching(
        self,
        event: str,
        sport: str,
        user_id: int,
        *,
        min_score: float = 70.0,
    ) -> list[dict[str, Any]]:
        """Exact event match, plus fuzzy aliases (e.g. Contender Week 1 name variants)."""
        from card_data import _event_match_score  # local import avoids cycles at module load

        exact = await self.get_bets_for_event(event, sport, user_id)
        if exact:
            # Still merge aliases so ESPN/FightOdds label variants sit on one sheet
            seen = {b["id"] for b in exact}
            all_bets = await self.get_all_bets(sport, user_id)
            for b in all_bets:
                if b["id"] in seen:
                    continue
                en = b.get("event") or ""
                if en and _event_match_score(event, en) >= min_score:
                    exact.append(b)
                    seen.add(b["id"])
            exact.sort(key=lambda r: r["id"], reverse=True)
            return exact

        all_bets = await self.get_all_bets(sport, user_id)
        out = [
            b
            for b in all_bets
            if (b.get("event") or "") and _event_match_score(event, b["event"]) >= min_score
        ]
        out.sort(key=lambda r: r["id"], reverse=True)
        return out

    async def get_distinct_events(self, sport: str, user_id: int) -> list[str]:
        cursor = await self._conn.execute(
            "SELECT DISTINCT event FROM bets WHERE event IS NOT NULL AND event != '' "
            "AND sport = ? AND (user_id = ? OR co_user_id = ?) ORDER BY id DESC",
            (sport, user_id, user_id),
        )
        rows = await cursor.fetchall()
        return [r["event"] for r in rows]

    async def update_leg_pick(
        self,
        leg_id: int,
        *,
        fighter_pick: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        if description is not None:
            await self._conn.execute(
                "UPDATE bet_legs SET fighter_pick = ?, description = ? WHERE id = ?",
                (fighter_pick, description, leg_id),
            )
        else:
            await self._conn.execute(
                "UPDATE bet_legs SET fighter_pick = ? WHERE id = ?",
                (fighter_pick, leg_id),
            )
        await self._conn.commit()

    async def update_leg_structure(
        self,
        leg_id: int,
        *,
        fighter_pick: Optional[str],
        outcome_type: Optional[str],
        outcome_round: Optional[int] = None,
    ) -> None:
        await self._conn.execute(
            "UPDATE bet_legs SET fighter_pick = ?, outcome_type = ?, outcome_round = ? WHERE id = ?",
            (fighter_pick, outcome_type, outcome_round, leg_id),
        )
        await self._conn.commit()

    async def get_events_with_pending_gradeable_legs(self) -> list[str]:
        """Distinct event names that still have pending UFC legs worth grading."""
        cursor = await self._conn.execute(
            """
            SELECT DISTINCT bets.event FROM bets
            JOIN bet_legs ON bet_legs.bet_id = bets.id
            WHERE bets.sport = 'ufc'
              AND bets.status = 'pending'
              AND bets.event IS NOT NULL AND bets.event != ''
              AND bet_legs.status = 'pending'
            """
        )
        rows = await cursor.fetchall()
        return [r["event"] for r in rows if r["event"]]

    async def update_bet_picks(
        self,
        bet_id: int,
        *,
        fighter_pick: Optional[str] = None,
        opponent_pick: Optional[str] = None,
    ) -> None:
        await self._conn.execute(
            "UPDATE bets SET fighter_pick = ?, opponent_pick = ? WHERE id = ?",
            (fighter_pick, opponent_pick, bet_id),
        )
        await self._conn.commit()

    # ---------- predictions (UFC-only feature, not sport-scoped) ----------

    async def upsert_prediction(
        self,
        *,
        user_id: int,
        event: str,
        fight_index: int,
        fighter_a: str,
        fighter_b: str,
        picked_fighter: str,
        confidence: Optional[str] = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO predictions
                (user_id, event, fight_index, fighter_a, fighter_b, picked_fighter, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, event, fight_index)
            DO UPDATE SET picked_fighter = excluded.picked_fighter,
                          confidence = excluded.confidence,
                          created_at = excluded.created_at
            """,
            (
                user_id,
                event,
                fight_index,
                fighter_a,
                fighter_b,
                picked_fighter,
                confidence,
                datetime.datetime.utcnow().isoformat(),
            ),
        )
        await self._conn.commit()

    async def get_predictions_for_event(self, event: str, user_id: int) -> dict[int, str]:
        """Returns {fight_index: picked_fighter} for this user's picks on this event."""
        cursor = await self._conn.execute(
            "SELECT fight_index, picked_fighter FROM predictions WHERE event = ? AND user_id = ?",
            (event, user_id),
        )
        rows = await cursor.fetchall()
        return {r["fight_index"]: r["picked_fighter"] for r in rows}

    # ---------- per-user settings ----------

    async def get_unit_value(self, user_id: int, default: float) -> float:
        cursor = await self._conn.execute(
            "SELECT unit_value FROM user_settings WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["unit_value"] if row else default

    async def get_currency(self, user_id: int) -> Optional[str]:
        cursor = await self._conn.execute(
            "SELECT currency FROM user_settings WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        value = row["currency"]
        return value if value else None

    async def set_unit_value(self, user_id: int, unit_value: float) -> None:
        await self._conn.execute(
            """
            INSERT INTO user_settings (user_id, unit_value) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET unit_value = excluded.unit_value
            """,
            (user_id, unit_value),
        )
        await self._conn.commit()

    async def set_user_settings(
        self,
        user_id: int,
        *,
        unit_value: Optional[float] = None,
        currency: Optional[str] = None,
    ) -> None:
        """Upsert unit size and/or currency for a user."""
        existing = await self._conn.execute(
            "SELECT unit_value, currency FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        row = await existing.fetchone()
        if row is None:
            await self._conn.execute(
                """
                INSERT INTO user_settings (user_id, unit_value, currency)
                VALUES (?, ?, ?)
                """,
                (
                    user_id,
                    unit_value if unit_value is not None else 100.0,
                    currency,
                ),
            )
        else:
            new_unit = unit_value if unit_value is not None else row["unit_value"]
            new_cur = currency if currency is not None else row["currency"]
            await self._conn.execute(
                """
                UPDATE user_settings
                SET unit_value = ?, currency = ?
                WHERE user_id = ?
                """,
                (new_unit, new_cur, user_id),
            )
        await self._conn.commit()

    # ---------- auto-grading ----------

    async def get_pending_graded_bets_for_event(self, event: str) -> list[dict[str, Any]]:
        """Pending UFC bets for this event that have a structured pick
        (fighter_pick/outcome_type set via /bet-ufc's fight+outcome
        options) -- these are the only ones auto-grading can act on.
        Free-text-only legs/parlays are never auto-graded."""
        cursor = await self._conn.execute(
            "SELECT * FROM bets WHERE event = ? AND sport = 'ufc' AND status = 'pending' "
            "AND fighter_pick IS NOT NULL AND outcome_type IS NOT NULL",
            (event,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ---------- OpenAI usage tracking (/bet-slip Vision) ----------

    async def record_openai_usage(
        self,
        *,
        model: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int | None = None,
        source: str = "bet-slip",
        user_id: int | None = None,
    ) -> None:
        prompt_tokens = int(prompt_tokens or 0)
        completion_tokens = int(completion_tokens or 0)
        if total_tokens is None:
            total_tokens = prompt_tokens + completion_tokens
        await self._conn.execute(
            """
            INSERT INTO openai_usage
                (created_at, model, prompt_tokens, completion_tokens, total_tokens, source, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.datetime.utcnow().isoformat(),
                model,
                prompt_tokens,
                completion_tokens,
                int(total_tokens or 0),
                source,
                user_id,
            ),
        )
        await self._conn.commit()

    async def get_openai_usage_summary(self) -> dict[str, Any]:
        """Return today / month / all-time token totals (UTC)."""
        now = datetime.datetime.utcnow()
        today_prefix = now.strftime("%Y-%m-%d")
        month_prefix = now.strftime("%Y-%m")

        async def _agg(where: str, arg: str | None = None) -> dict[str, Any]:
            sql = (
                "SELECT COUNT(*) AS calls, "
                "COALESCE(SUM(prompt_tokens),0) AS prompt, "
                "COALESCE(SUM(completion_tokens),0) AS completion, "
                "COALESCE(SUM(total_tokens),0) AS total "
                f"FROM openai_usage {where}"
            )
            cursor = await self._conn.execute(sql, (arg,) if arg is not None else ())
            row = await cursor.fetchone()
            return {
                "calls": int(row["calls"] or 0),
                "prompt": int(row["prompt"] or 0),
                "completion": int(row["completion"] or 0),
                "total": int(row["total"] or 0),
            }

        cursor = await self._conn.execute(
            "SELECT model, COUNT(*) AS n FROM openai_usage "
            "GROUP BY model ORDER BY n DESC LIMIT 5"
        )
        models = [dict(r) for r in await cursor.fetchall()]

        return {
            "today": await _agg("WHERE created_at LIKE ?", f"{today_prefix}%"),
            "month": await _agg("WHERE created_at LIKE ?", f"{month_prefix}%"),
            "all": await _agg(""),
            "models": models,
            "as_of": now.isoformat(timespec="seconds") + "Z",
        }

    # ---------- monitored events (for auto-grading polling) ----------

    async def add_monitored_event(self, event: str, started_by: int) -> None:
        await self._conn.execute(
            """
            INSERT INTO monitored_events (event, started_by, started_at) VALUES (?, ?, ?)
            ON CONFLICT(event) DO UPDATE SET started_by = excluded.started_by,
                                              started_at = excluded.started_at
            """,
            (event, started_by, datetime.datetime.utcnow().isoformat()),
        )
        await self._conn.commit()

    async def remove_monitored_event(self, event: str) -> None:
        await self._conn.execute("DELETE FROM monitored_events WHERE event = ?", (event,))
        await self._conn.commit()

    async def get_monitored_events(self) -> list[str]:
        cursor = await self._conn.execute("SELECT event FROM monitored_events")
        rows = await cursor.fetchall()
        return [r["event"] for r in rows]

    async def clear_monitored_events(self) -> None:
        await self._conn.execute("DELETE FROM monitored_events")
        await self._conn.commit()

    # ---------- bet legs (per-leg structured picks, for parlay auto-grading) ----------

    async def add_bet_leg(
        self,
        bet_id: int,
        leg_index: int,
        description: str,
        fighter_pick: Optional[str] = None,
        outcome_type: Optional[str] = None,
        outcome_round: Optional[int] = None,
        added_by: Optional[int] = None,
    ) -> int:
        cursor = await self._conn.execute(
            """
            INSERT INTO bet_legs (bet_id, leg_index, description, fighter_pick,
                                   outcome_type, outcome_round, status, added_by)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                bet_id,
                leg_index,
                description,
                fighter_pick,
                outcome_type,
                outcome_round,
                added_by,
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_legs_for_bet(self, bet_id: int) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM bet_legs WHERE bet_id = ? ORDER BY leg_index", (bet_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    def user_can_access_bet(self, bet: dict[str, Any], user_id: int) -> bool:
        return bet.get("user_id") == user_id or bet.get("co_user_id") == user_id

    # ---------- collab sessions (shared slip draft) ----------

    async def create_collab_session(
        self,
        *,
        code: str,
        host_user_id: int,
        event: Optional[str],
        guild_id: Optional[int],
        channel_id: Optional[int],
    ) -> int:
        cursor = await self._conn.execute(
            """
            INSERT INTO collab_sessions
                (code, event, host_user_id, partner_user_id, guild_id, channel_id,
                 message_id, status, created_at)
            VALUES (?, ?, ?, NULL, ?, ?, NULL, 'open', ?)
            """,
            (
                code.upper(),
                event,
                host_user_id,
                guild_id,
                channel_id,
                datetime.datetime.utcnow().isoformat(),
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_collab_session(self, session_id: int) -> Optional[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM collab_sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_collab_session_by_code(self, code: str) -> Optional[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM collab_sessions WHERE code = ?", (code.upper().strip(),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def set_collab_message_id(self, session_id: int, message_id: int) -> None:
        await self._conn.execute(
            "UPDATE collab_sessions SET message_id = ? WHERE id = ?",
            (message_id, session_id),
        )
        await self._conn.commit()

    async def join_collab_session(self, session_id: int, partner_user_id: int) -> bool:
        """Attach partner if session is open and empty. Returns False if unavailable."""
        session = await self.get_collab_session(session_id)
        if not session or session["status"] != "open":
            return False
        if session["partner_user_id"] is not None:
            return False
        if session["host_user_id"] == partner_user_id:
            return False
        await self._conn.execute(
            "UPDATE collab_sessions SET partner_user_id = ? WHERE id = ? "
            "AND partner_user_id IS NULL AND status = 'open'",
            (partner_user_id, session_id),
        )
        await self._conn.commit()
        refreshed = await self.get_collab_session(session_id)
        return bool(refreshed and refreshed.get("partner_user_id") == partner_user_id)

    async def set_collab_status(self, session_id: int, status: str) -> None:
        await self._conn.execute(
            "UPDATE collab_sessions SET status = ? WHERE id = ?",
            (status, session_id),
        )
        await self._conn.commit()

    async def set_collab_stake(
        self, session_id: int, user_id: int, units: float, odds: Optional[int]
    ) -> Optional[str]:
        """Records one collab member's own units/odds independently --
        host and partner each get their own pair, neither overwrites the
        other's. Returns 'host' or 'partner' (whichever side was just
        set), or None if user_id isn't part of this session at all."""
        session = await self.get_collab_session(session_id)
        if session is None:
            return None
        if user_id == session["host_user_id"]:
            await self._conn.execute(
                "UPDATE collab_sessions SET host_units = ?, host_odds = ? WHERE id = ?",
                (units, odds, session_id),
            )
            await self._conn.commit()
            return "host"
        if user_id == session.get("partner_user_id"):
            await self._conn.execute(
                "UPDATE collab_sessions SET partner_units = ?, partner_odds = ? WHERE id = ?",
                (units, odds, session_id),
            )
            await self._conn.commit()
            return "partner"
        return None

    async def add_collab_leg(
        self,
        session_id: int,
        user_id: int,
        description: str,
        *,
        fighter_pick: Optional[str] = None,
        outcome_type: Optional[str] = None,
        outcome_round: Optional[int] = None,
    ) -> int:
        cursor = await self._conn.execute(
            "SELECT COALESCE(MAX(leg_index), -1) AS m FROM collab_legs WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        next_index = int(row["m"]) + 1 if row else 0
        cursor = await self._conn.execute(
            """
            INSERT INTO collab_legs
                (session_id, user_id, leg_index, description, fighter_pick,
                 outcome_type, outcome_round, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                next_index,
                description,
                fighter_pick,
                outcome_type,
                outcome_round,
                datetime.datetime.utcnow().isoformat(),
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_collab_legs(self, session_id: int) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM collab_legs WHERE session_id = ? ORDER BY leg_index",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def clear_collab_legs_for_user(self, session_id: int, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM collab_legs WHERE session_id = ? AND user_id = ?",
            (session_id, user_id),
        )
        await self._conn.commit()

    async def update_leg_status(self, leg_id: int, status: str) -> None:
        await self._conn.execute(
            "UPDATE bet_legs SET status = ? WHERE id = ?", (status, leg_id)
        )
        await self._conn.commit()

    async def update_leg_description(self, leg_id: int, description: str) -> None:
        await self._conn.execute(
            "UPDATE bet_legs SET description = ? WHERE id = ?", (description, leg_id)
        )
        await self._conn.commit()

    async def update_bet_title(self, bet_id: int, bet_title: str) -> None:
        await self._conn.execute(
            "UPDATE bets SET bet_title = ? WHERE id = ?", (bet_title, bet_id)
        )
        await self._conn.commit()

    async def update_all_legs_status(self, bet_id: int, status: str) -> None:
        """Set every leg on a bet to the same status -- used when the whole
        slip is graded manually (buttons or /grade) so the recap image can
        show W/L per leg instead of blank results."""
        await self._conn.execute(
            "UPDATE bet_legs SET status = ? WHERE bet_id = ?", (status, bet_id)
        )
        await self._conn.commit()

    async def get_pending_bets(
        self, sport: str, user_id: int, event: Optional[str] = None, *, min_score: float = 70.0
    ) -> list[dict[str, Any]]:
        """Pending slips for one user, optionally filtered to one event.
        Uses the same fuzzy event-alias matching as get_bets_for_event_matching
        (ESPN/FightOdds can name the same event slightly differently), so
        /grade doesn't silently miss bets logged under a different alias
        for what's really the same card."""
        if event:
            cursor = await self._conn.execute(
                "SELECT * FROM bets WHERE sport = ? AND (user_id = ? OR co_user_id = ?) "
                "AND status = 'pending' AND event = ? ORDER BY id DESC",
                (sport, user_id, user_id, event),
            )
            exact = [dict(r) for r in await cursor.fetchall()]

            from card_data import _event_match_score  # local import avoids cycles at module load

            seen = {b["id"] for b in exact}
            cursor_all = await self._conn.execute(
                "SELECT * FROM bets WHERE sport = ? AND (user_id = ? OR co_user_id = ?) "
                "AND status = 'pending' ORDER BY id DESC",
                (sport, user_id, user_id),
            )
            for row in await cursor_all.fetchall():
                b = dict(row)
                if b["id"] in seen:
                    continue
                en = b.get("event") or ""
                if en and _event_match_score(event, en) >= min_score:
                    exact.append(b)
                    seen.add(b["id"])
            exact.sort(key=lambda r: r["id"], reverse=True)
            return exact
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM bets WHERE sport = ? AND (user_id = ? OR co_user_id = ?) "
                "AND status = 'pending' ORDER BY id DESC",
                (sport, user_id, user_id),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_pending_graded_legs_for_event(self, event: str) -> list[dict[str, Any]]:
        """Pending legs (with a structured fighter_pick+outcome_type) that
        belong to still-pending UFC bets on this event -- the only legs
        auto-grading can act on. Free-text-only legs never match this."""
        cursor = await self._conn.execute(
            """
            SELECT bet_legs.* FROM bet_legs
            JOIN bets ON bet_legs.bet_id = bets.id
            WHERE bets.event = ? AND bets.sport = 'ufc' AND bets.status = 'pending'
              AND bet_legs.status = 'pending'
              AND bet_legs.fighter_pick IS NOT NULL
              AND bet_legs.outcome_type IS NOT NULL
            """,
            (event,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]