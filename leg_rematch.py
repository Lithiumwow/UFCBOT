"""
Rematch free-text / partially-structured UFC legs against a fight card.

Legacy slips often store only `bet_title` (no bet_legs / fighter_pick). This
module invents missing leg rows from the title and fills fighter_pick + market
outcome (for auto-grading) so recap sheets and ESPN grading both work.
"""
from __future__ import annotations

import logging
from typing import Any

from bet_types import effective_legs
from card_data import resolve_fighter_on_card
from leg_parser import parse_leg_line

log = logging.getLogger("ufc-bet-bot.leg_rematch")


def _title_lines(bet: dict[str, Any]) -> list[str]:
    title = (bet.get("bet_title") or "").strip()
    if not title:
        return []
    return [ln.strip() for ln in title.split("\n") if ln.strip()]


def _structure_leg_fields(
    description: str,
    fights: list[tuple[str, str]],
    existing_fighter: str | None = None,
) -> tuple[str | None, str | None, int | None]:
    """Return (fighter_pick, outcome_type, outcome_round) for auto-grading."""
    parsed = parse_leg_line(description or "")
    outcome = parsed.get("outcome_type")
    round_num = parsed.get("outcome_round")
    candidate = existing_fighter or parsed.get("fighter_pick")

    hit = resolve_fighter_on_card(
        fighter_pick=candidate,
        description=description,
        fights=fights,
    )
    fighter = hit[0] if hit else (candidate or None)

    # Totals / fight-level markets still need a card corner for ESPN fight lookup
    if fighter is None and outcome and fights:
        hit2 = resolve_fighter_on_card(
            fighter_pick=None, description=description or "", fights=fights
        )
        if hit2:
            fighter = hit2[0]

    return fighter, outcome, round_num


async def rematch_bets_to_card(
    db: Any,
    bets: list[dict[str, Any]],
    fights: list[tuple[str, str]],
) -> dict[str, int]:
    """
    Persist card matches + parseable markets onto legs.

    Returns counts: created_legs, updated_legs, updated_bets, matched_fighters, structured.
    """
    stats = {
        "created_legs": 0,
        "updated_legs": 0,
        "updated_bets": 0,
        "matched_fighters": 0,
        "structured": 0,
    }
    if not bets:
        return stats

    for bet in bets:
        bet_id = bet["id"]
        legs = await db.get_legs_for_bet(bet_id)

        if not legs:
            lines = _title_lines(bet)
            if not lines:
                continue
            slip_status = bet.get("status") or "pending"
            for idx, line in enumerate(lines):
                fighter, outcome, rnd = _structure_leg_fields(line, fights)
                await db.add_bet_leg(
                    bet_id,
                    idx,
                    line,
                    fighter_pick=fighter,
                    outcome_type=outcome,
                    outcome_round=rnd,
                )
                stats["created_legs"] += 1
                if fighter:
                    stats["matched_fighters"] += 1
                if fighter and outcome:
                    stats["structured"] += 1
            if slip_status in ("won", "loss", "void"):
                await db.update_all_legs_status(bet_id, slip_status)
            legs = await db.get_legs_for_bet(bet_id)

        for leg in legs:
            if leg.get("status") not in (None, "pending") and leg.get("status") != "pending":
                # still allow structure fill if unset even on settled? skip settled
                if leg.get("status") in ("won", "loss", "void"):
                    continue

            fighter, outcome, rnd = _structure_leg_fields(
                leg.get("description") or "",
                fights,
                existing_fighter=leg.get("fighter_pick"),
            )
            if fighter:
                stats["matched_fighters"] += 1

            need_structure = bool(outcome) and (
                (leg.get("fighter_pick") or "") != (fighter or "")
                or (leg.get("outcome_type") or "") != (outcome or "")
                or leg.get("outcome_round") != rnd
            )
            if need_structure and (fighter or outcome):
                await db.update_leg_structure(
                    leg["id"],
                    fighter_pick=fighter or leg.get("fighter_pick"),
                    outcome_type=outcome or leg.get("outcome_type"),
                    outcome_round=rnd if rnd is not None else leg.get("outcome_round"),
                )
                leg["fighter_pick"] = fighter or leg.get("fighter_pick")
                leg["outcome_type"] = outcome or leg.get("outcome_type")
                leg["outcome_round"] = rnd if rnd is not None else leg.get("outcome_round")
                stats["updated_legs"] += 1
                if fighter and outcome:
                    stats["structured"] += 1
            elif fighter and (leg.get("fighter_pick") or "") != fighter:
                await db.update_leg_pick(leg["id"], fighter_pick=fighter)
                leg["fighter_pick"] = fighter
                stats["updated_legs"] += 1

        effective = effective_legs(bet, legs)
        if len(effective) == 1 and fights:
            leg0 = effective[0]
            hit = resolve_fighter_on_card(
                fighter_pick=leg0.get("fighter_pick"),
                description=leg0.get("description") or bet.get("bet_title"),
                fights=fights,
            )
            if hit:
                fighter, opponent, _ = hit
                if (
                    (bet.get("fighter_pick") or "") != fighter
                    or (bet.get("opponent_pick") or "") != opponent
                ):
                    await db.update_bet_picks(
                        bet_id, fighter_pick=fighter, opponent_pick=opponent
                    )
                    if outcome := (leg0.get("outcome_type") or parse_leg_line(
                        leg0.get("description") or ""
                    ).get("outcome_type")):
                        # also denormalize outcome onto bet row for legacy grade paths
                        pass
                    bet["fighter_pick"] = fighter
                    bet["opponent_pick"] = opponent
                    stats["updated_bets"] += 1

    log.info(
        "Rematch: created=%s updated_legs=%s bets=%s fighters=%s structured=%s",
        stats["created_legs"],
        stats["updated_legs"],
        stats["updated_bets"],
        stats["matched_fighters"],
        stats["structured"],
    )
    return stats
