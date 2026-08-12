"""
Map a FightIQ/FightOdds Play (label + offer_type_id) into a bot leg dict.

Odds are intentionally ignored — only description / structure for logging
and auto-grading.
"""

from __future__ import annotations

import re
from typing import Any, Optional

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

_OVERUNDER_RE = re.compile(r"^OVERUNDER_(\d+(?:\.\d+)?)$", re.I)
_ROUND_WIN_RE = re.compile(r"^R_(\d)$", re.I)
_METHOD_ROUND_RE = re.compile(r"^(KO|SUB)_(\d)$", re.I)
_END_ROUND_RE = re.compile(r"^END_(\d)$", re.I)
_START_ROUND_RE = re.compile(r"^START_(\d)$", re.I)

_NEG_DISTANCE = re.compile(
    r"does\s*n['’]?t|does\s+not|not\s+go|inside\s+the\s+distance|ends?\s+inside",
    re.I,
)


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
        if outcome_type == "ML" and not fighter_pick:
            # Moneyline sides should always name a fighter
            fighter_pick = fighter_a if getattr(play, "side", 1) == 1 else fighter_b

    elif ot == "DISTANCE":
        if _NEG_DISTANCE.search(label):
            outcome_type = "NOT_DISTANCE"
        else:
            outcome_type = "DISTANCE"
        if not fighter_pick:
            fighter_pick = fighter_a

    elif m := _OVERUNDER_RE.match(ot):
        line = m.group(1)
        # "2.5" → OVER_2_5
        whole = line.split(".")[0]
        direction = "UNDER" if _is_under_side(label) else "OVER"
        outcome_type = f"{direction}_{whole}_5"
        if not fighter_pick:
            fighter_pick = fighter_a

    elif m := _ROUND_WIN_RE.match(ot):
        outcome_type = f"R_{m.group(1)}"
        outcome_round = int(m.group(1))

    elif m := _METHOD_ROUND_RE.match(ot):
        method, rnd = m.group(1).upper(), int(m.group(2))
        # Store as KO_1 / SUB_2 (grader understands these)
        outcome_type = f"{method}_{rnd}"
        outcome_round = rnd
        if method == "KO":
            # Keep KO_N form for round+method; grader maps KO → KO_TKO
            pass

    elif m := _END_ROUND_RE.match(ot):
        outcome_type = f"END_{m.group(1)}"
        outcome_round = int(m.group(1))
        if not fighter_pick:
            fighter_pick = fighter_a

    elif m := _START_ROUND_RE.match(ot):
        outcome_type = f"START_{m.group(1)}"
        outcome_round = int(m.group(1))
        if not fighter_pick:
            fighter_pick = fighter_a

    else:
        # Keep raw offer type; leave pending on unknown exotic props
        outcome_type = ot or "PROP"
        if not fighter_pick and category in {
            "totals",
            "distance",
            "method_fight",
            "other",
        }:
            fighter_pick = fighter_a

    if not fighter_pick:
        fighter_pick = fighter_a

    return {
        "description": label or f"{fighter_a} vs {fighter_b} — {outcome_type}",
        "fighter_pick": fighter_pick,
        "outcome_type": outcome_type,
        "outcome_round": outcome_round,
    }
