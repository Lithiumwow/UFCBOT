"""
How UFC slips are classified for recaps (/spread-sheet) and related UIs.

Rules (matches the usual sportsbook mental model):

  • Parlay        — 2 or more legs combined as one stake
  • Straight Pick — exactly 1 leg that is a pure fighter moneyline (ML only)
  • Prop          — everything else on a single leg:
                    win-by method (KO/TKO, SUB, DEC), round props,
                    "to win in Rd X", either-fighter outcomes, totals,
                    free-text non-ML

Structured legs from /bet-ufc set `outcome_type` on each leg (e.g. "ML",
"KO_TKO"). Free-text is only treated as Straight when it clearly is
moneyline — never when there's a round/method keyword.
"""
from __future__ import annotations

import re
from typing import Any

# Single-leg moneylines only
_STRAIGHT_OUTCOMES = frozenset({"ML"})

# Explicit prop markets from the builder / auto-grader
_PROP_OUTCOMES = frozenset(
    {
        "KO_TKO",
        "SUB",
        "DEC",
        "KO_OR_SUB",
        "FIGHT_KO",
        "FIGHT_SUB",
        "DISTANCE",
        "NOT_DISTANCE",
        "ID",
        "UD",
        "SD",
        "KO_DEC",
        "SUB_DEC",
        "DRAW",
    }
)

_TOTAL_ROUNDS_RE = re.compile(r"^(OVER|UNDER)_\d_5$")
_FIGHTIQ_PROP_RE = re.compile(
    r"^(R_\d|KO_\d|SUB_\d|END_\d|START_\d|OVERUNDER_)",
    re.IGNORECASE,
)

# Anything with method / round / totals / fight-ending detail → prop
_PROP_HINT = re.compile(
    r"""
    \b(
        ko|tko|knockout|technical\s*knockout|
        sub(mission)?|
        decision|dec\b|
        # round props: "Rd 3", "R2", "round 2", "win in rd 3"
        round|rounds?|
        \brd\.?\s*\d|
        \br\s*\d\b|
        over|under|
        distance|finish|
        # "to win in/by …" is a prop market, not a plain ML
        to\s+win\s+(in|by|via)|
        wins?\s+(in|by|via)|
        end(s)?\s+by|
        within
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Pure ML markers only (no bare "to win" — too often "to win in Rd 3")
_ML_EXPLICIT = re.compile(
    r"""
    \b(ml|moneyline|money\s*line)\b
    |
    # "Fighter to win" with nothing after that qualifies a prop
    \bto\s+win\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def effective_legs(bet: dict[str, Any], legs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Prefer bet_legs rows; fall back to newline-split bet_title for legacy slips."""
    if legs:
        return legs
    title = bet.get("bet_title") or ""
    lines = [ln.strip() for ln in title.split("\n") if ln.strip()]
    if not lines:
        lines = ["Bet"]
    return [{"description": ln, "fighter_pick": None, "outcome_type": None, "status": None} for ln in lines]


def _looks_like_prop(desc: str) -> bool:
    return bool(_PROP_HINT.search(desc or ""))


def _looks_like_ml_only(desc: str, fighter_pick: str | None) -> bool:
    """True only for pure moneyline text — never for round/method props.

    Free-text without an explicit ML marker defaults to Prop (safer than
    treating \"to win in Rd 3\" or random text as a straight).
    """
    text = (desc or "").strip()
    if not text:
        return False
    if _looks_like_prop(text):
        return False
    if _ML_EXPLICIT.search(text):
        return True
    # Structured bare-name display sometimes is just the fighter after an ML pick
    if fighter_pick:
        name = fighter_pick.strip().lower()
        if text.lower() == name or text.lower() == name.split()[-1]:
            return True
    return False


def categorize_legs(legs: list[dict[str, Any]]) -> str:
    """
    Returns one of: \"Straight Pick\", \"Prop\", \"Parlay\".

    Straight = pure ML only. Round wins / method / anything else → Prop.
    """
    if len(legs) > 1:
        return "Parlay"

    if not legs:
        return "Prop"

    leg = legs[0]
    outcome = (leg.get("outcome_type") or "").strip().upper()
    desc = leg.get("description") or ""
    fighter = leg.get("fighter_pick")

    # Structured market from /bet-ufc builder or free-text parser
    if outcome == "ML":
        # Structured ML is straight — but if description somehow carries
        # round/method language, trust the text and call it a prop.
        if _looks_like_prop(desc):
            return "Prop"
        return "Straight Pick"
    if outcome in _PROP_OUTCOMES or _TOTAL_ROUNDS_RE.match(outcome or "") or _FIGHTIQ_PROP_RE.match(
        outcome or ""
    ):
        return "Prop"
    if outcome:
        # Any other coded market (including with outcome_round set) → prop
        return "Prop"

    # Free-text / legacy with no outcome_type: prop wins ties
    if _looks_like_prop(desc):
        return "Prop"
    if _looks_like_ml_only(desc, fighter):
        return "Straight Pick"
    return "Prop"


def categorize_bet(bet: dict[str, Any], legs: list[dict[str, Any]] | None = None) -> str:
    return categorize_legs(effective_legs(bet, legs))
