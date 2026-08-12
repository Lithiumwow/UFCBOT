"""
Map a FightIQ/FightOdds Play (label + offer_type_id) into a bot leg dict.

Odds are intentionally ignored — only description / structure for logging
and auto-grading.

Two things this file is careful about, since both were previously broken:

1. Fighter attribution (`fighter_pick`) for props whose FightOdds label
   text doesn't literally mention a fighter's name (structural attribution
   via `play.side` instead of text) -- previously only the ML branch had
   this fallback, so e.g. "Wins Inside Distance" (ID) silently defaulted
   to fighter_a regardless of which fighter the play actually belonged to.

2. The leg description text: fighter-specific picks (ML, KO/TKO, Sub,
   Dec, ID, etc) should read as just "<Fighter> <outcome>" with no fight
   matchup prefix -- e.g. "Myktybek Orolbai wins inside distance". Only
   genuinely fight-level picks (Over/Under rounds, Fight Ends by KO
   either fighter, etc) should include the matchup, e.g. "Jeremiah Wells
   vs Myktybek Orolbai under 2.5 rounds". This used to be decided by
   scanning the raw label text for a fighter's name, which is unreliable
   for the same structural-attribution reason as (1) -- now it's decided
   by an explicit fighter-specific/fight-level classification of the
   outcome_type itself.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from leg_parser import _OUTCOME_LABELS

# FightOdds offer_type_id → bot outcome_type (when a stable mapping exists)
_DIRECT_MAP = {
    "STRAIGHT": "ML",
    "KO": "KO_TKO",
    "SUB": "SUB",
    "DEC": "DEC",
    "UD": "UD",
    "SD": "SD",
    "ID": "ID",
    "END_KO": "FIGHT_KO",
    "END_SUB": "FIGHT_SUB",
    "KO_SUB": "KO_OR_SUB",
    "KO_DEC": "KO_DEC",
    "SUB_DEC": "SUB_DEC",
    "DRAW": "DRAW",
}

# Fighter-specific direct-mapped types -- always tied to one named fighter,
# never a "vs" matchup prefix in the description.
_FIGHTER_SPECIFIC_DIRECT = {
    "ML", "KO_TKO", "SUB", "DEC", "UD", "SD", "ID", "KO_DEC", "SUB_DEC", "KO_OR_SUB",
}

_OVERUNDER_RE = re.compile(r"^OVERUNDER_(\d+(?:\.\d+)?)$", re.I)
_ROUND_WIN_RE = re.compile(r"^R_(\d)$", re.I)
_METHOD_ROUND_RE = re.compile(r"^(KO|SUB)_(\d)$", re.I)
_END_ROUND_RE = re.compile(r"^END_(\d)$", re.I)
_START_ROUND_RE = re.compile(r"^START_(\d)$", re.I)

_NEG_DISTANCE = re.compile(
    r"does\s*n['’]?t|does\s+not|not\s+go|inside\s+the\s+distance|ends?\s+inside",
    re.I,
)

_FIGHT_LEVEL_LABELS = {
    "FIGHT_KO": "ends by KO/TKO (either fighter)",
    "FIGHT_SUB": "ends by Submission (either fighter)",
    "DISTANCE": "goes the distance",
    "NOT_DISTANCE": "does NOT go the distance",
    "DRAW": "ends in a draw",
}


def _match_fighter(label: str, fighter_a: str, fighter_b: str) -> Optional[str]:
    low = label.lower()
    best: Optional[str] = None
    best_len = 0
    for name in (fighter_a, fighter_b):
        n = name.lower()
        if n and n in low and len(n) > best_len:
            best, best_len = name, len(n)
            continue
        last = n.split()[-1] if n.split() else ""
        if len(last) >= 3 and re.search(rf"\b{re.escape(last)}\b", low):
            if len(last) > best_len:
                best, best_len = name, len(last)
    return best


def _is_under_side(label: str) -> bool:
    return bool(re.search(r"\bunder\b", label, re.I))


def _fighter_side_fallback(play: Any, fighter_a: str, fighter_b: str) -> str:
    """Structural fallback (play.side) for when the label text doesn't
    literally name a fighter -- common for FightOdds props where fighter
    attribution is conveyed by a side field, not the label string."""
    return fighter_a if getattr(play, "side", 1) == 1 else fighter_b


def _is_fighter_specific(outcome_type: str) -> bool:
    if outcome_type in _FIGHTER_SPECIFIC_DIRECT:
        return True
    if re.match(r"^(KO|SUB)_\d$", outcome_type):  # method+round, e.g. KO_2
        return True
    if re.match(r"^R_\d$", outcome_type):  # round-win, e.g. R_3
        return True
    return False


def map_play_to_leg(
    play: Any,
    *,
    fighter_a: str,
    fighter_b: str,
) -> dict[str, Any]:
    """Build {description, fighter_pick, outcome_type, outcome_round} from a Play."""
    label = (getattr(play, "label", None) or "").strip()
    ot = (getattr(play, "offer_type_id", None) or "").strip().upper()
    category = (getattr(play, "category", None) or "").lower()

    outcome_type = ot
    outcome_round: Optional[int] = None
    fighter_pick: Optional[str] = _match_fighter(label, fighter_a, fighter_b)

    if ot in _DIRECT_MAP:
        outcome_type = _DIRECT_MAP[ot]
        if _is_fighter_specific(outcome_type) and not fighter_pick:
            fighter_pick = _fighter_side_fallback(play, fighter_a, fighter_b)

    elif ot == "DISTANCE":
        # Fight-level distance market (not the per-fighter "ID" one above).
        outcome_type = "NOT_DISTANCE" if _NEG_DISTANCE.search(label) else "DISTANCE"
        if not fighter_pick:
            fighter_pick = fighter_a  # anchor only, fight-level pick

    elif m := _OVERUNDER_RE.match(ot):
        line = m.group(1)
        whole = line.split(".")[0]
        direction = "UNDER" if _is_under_side(label) else "OVER"
        outcome_type = f"{direction}_{whole}_5"
        if not fighter_pick:
            fighter_pick = fighter_a  # anchor only, fight-level pick

    elif m := _ROUND_WIN_RE.match(ot):
        outcome_type = f"R_{m.group(1)}"
        outcome_round = int(m.group(1))
        if not fighter_pick:
            fighter_pick = _fighter_side_fallback(play, fighter_a, fighter_b)

    elif m := _METHOD_ROUND_RE.match(ot):
        method, rnd = m.group(1).upper(), int(m.group(2))
        outcome_type = f"{method}_{rnd}"  # KO_2 / SUB_3 -- grader understands these
        outcome_round = rnd
        if not fighter_pick:
            fighter_pick = _fighter_side_fallback(play, fighter_a, fighter_b)

    elif m := _END_ROUND_RE.match(ot):
        outcome_type = f"END_{m.group(1)}"
        outcome_round = int(m.group(1))
        if not fighter_pick:
            fighter_pick = fighter_a  # anchor only, fight-level pick

    elif m := _START_ROUND_RE.match(ot):
        outcome_type = f"START_{m.group(1)}"
        outcome_round = int(m.group(1))
        if not fighter_pick:
            fighter_pick = fighter_a  # anchor only, fight-level pick

    else:
        outcome_type = ot or "PROP"
        if not fighter_pick and category in {"totals", "distance", "method_fight", "other"}:
            fighter_pick = fighter_a  # anchor only, fight-level pick

    if not fighter_pick:
        fighter_pick = fighter_a

    description = _build_description(
        label=label, fighter_a=fighter_a, fighter_b=fighter_b,
        fighter_pick=fighter_pick, outcome_type=outcome_type, outcome_round=outcome_round,
    )

    return {
        "description": description,
        "fighter_pick": fighter_pick,
        "outcome_type": outcome_type,
        "outcome_round": outcome_round,
    }


def rebuild_description_from_stored(
    *, fighter_pick: Optional[str], outcome_type: Optional[str], outcome_round: Optional[int],
    fighter_a: str, fighter_b: str, current_description: str,
) -> str:
    """Rebuilds a leg's description using the current (fixed) formatting
    rules, from data already stored in bet_legs -- no live FightOdds Play
    object needed. Used to fix bets that were logged before this file's
    fighter-specific/fight-level classification existed, whose stale
    description text is otherwise stuck forever (fixing the builder logic
    doesn't retroactively rewrite what's already in the database).

    Returns the current_description unchanged if outcome_type/fighter_pick
    aren't set (free-text legs) or the type isn't recognized -- only
    rebuilds when we can confidently do so."""
    if not outcome_type or not fighter_pick:
        return current_description

    if _is_fighter_specific(outcome_type):
        if outcome_type in _OUTCOME_LABELS:
            return f"{fighter_pick} {_OUTCOME_LABELS[outcome_type]}"
        if m := re.match(r"^(KO|SUB)_(\d)$", outcome_type):
            method_text = "KO/TKO" if m.group(1) == "KO" else "Submission"
            return f"{fighter_pick} by {method_text} (Round {m.group(2)})"
        if m := re.match(r"^R_(\d)$", outcome_type):
            return f"{fighter_pick} wins in Round {m.group(1)}"
        return current_description  # unrecognized -- leave it alone

    fight_label = f"{fighter_a} vs {fighter_b}"
    if outcome_type in _FIGHT_LEVEL_LABELS:
        return f"{fight_label} {_FIGHT_LEVEL_LABELS[outcome_type]}"
    if m := re.match(r"^(OVER|UNDER)_(\d)_5$", outcome_type):
        direction = "over" if m.group(1) == "OVER" else "under"
        return f"{fight_label} {direction} {m.group(2)}.5 rounds"
    if m := re.match(r"^END_(\d)$", outcome_type):
        return f"{fight_label} ends in round {m.group(1)}"
    if m := re.match(r"^START_(\d)$", outcome_type):
        return f"{fight_label} reaches round {m.group(1)}"
    return current_description  # unrecognized -- leave it alone


def _build_description(
    *, label: str, fighter_a: str, fighter_b: str, fighter_pick: str,
    outcome_type: str, outcome_round: Optional[int],
) -> str:
    fight_label = f"{fighter_a} vs {fighter_b}"

    if _is_fighter_specific(outcome_type):
        # Never a "vs" prefix -- just "<Fighter> <outcome>", built from a
        # known label so it's always correctly attributed regardless of
        # whether the raw FightOdds label text happens to name them.
        if outcome_type in _OUTCOME_LABELS:
            return f"{fighter_pick} {_OUTCOME_LABELS[outcome_type]}"
        if m := re.match(r"^(KO|SUB)_(\d)$", outcome_type):
            method_text = "KO/TKO" if m.group(1) == "KO" else "Submission"
            return f"{fighter_pick} by {method_text} (Round {m.group(2)})"
        if m := re.match(r"^R_(\d)$", outcome_type):
            return f"{fighter_pick} wins in Round {m.group(1)}"
        # Unrecognized fighter-specific type -- fall back to the raw label,
        # prefixed with the fighter's name if it isn't already there.
        if label and fighter_pick.lower() in label.lower():
            return label
        return f"{fighter_pick} {label}".strip() if label else f"{fighter_pick} ({outcome_type})"

    # Fight-level: always include the matchup, no fighter singled out.
    if outcome_type in _FIGHT_LEVEL_LABELS:
        return f"{fight_label} {_FIGHT_LEVEL_LABELS[outcome_type]}"
    if m := re.match(r"^(OVER|UNDER)_(\d)_5$", outcome_type):
        direction = "over" if m.group(1) == "OVER" else "under"
        return f"{fight_label} {direction} {m.group(2)}.5 rounds"
    if m := re.match(r"^END_(\d)$", outcome_type):
        return f"{fight_label} ends in round {m.group(1)}"
    if m := re.match(r"^START_(\d)$", outcome_type):
        return f"{fight_label} reaches round {m.group(1)}"
    # Unrecognized fight-level type -- fall back to fight_label + raw label.
    return f"{fight_label} {label}" if label else f"{fight_label} ({outcome_type})"