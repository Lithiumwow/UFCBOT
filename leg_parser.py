"""
Parses a natural-language leg line into a structured pick for auto-grading.

Supports builder-style lines:
    "Islam Makhachev - Submission" -> fighter=…, SUB
    "Islam Makhachev - Submission, R2"

And free-text phrasing used in logs / old slips:
    "Tofiq Musayev ML"
    "Uros Medic by KO/TKO"
    "Jovan Leka Ko/Sub"
    "Mansur vs Stolzfus Fight Ends by KO"
    "Under 2.5 rounds"
"""
from __future__ import annotations

import re

_OUTCOME_LABELS = {
    "ML": "ML",
    "KO_TKO": "by KO/TKO",
    "SUB": "by Submission",
    "DEC": "by Decision",
    "KO_OR_SUB": "by KO/TKO or Submission",
    "FIGHT_KO": "ends by KO/TKO",
    "FIGHT_SUB": "ends by Submission",
    "DISTANCE": "goes the distance",
    "NOT_DISTANCE": "does NOT go the distance",
    "ID": "wins inside the distance",
    "UD": "by Unanimous Decision",
    "SD": "by Split Decision",
    "KO_DEC": "by KO/TKO or Decision",
    "SUB_DEC": "by Submission or Decision",
}

# Longest/most-specific phrases first
_OUTCOME_KEYWORDS = [
    ("not to go the distance", "NOT_DISTANCE"),
    ("not going the distance", "NOT_DISTANCE"),
    ("not go the distance", "NOT_DISTANCE"),
    ("not distance", "NOT_DISTANCE"),
    ("inside the distance", "NOT_DISTANCE"),
    ("doesn't go the distance", "NOT_DISTANCE"),
    ("does not go the distance", "NOT_DISTANCE"),
    ("doesn’t go the distance", "NOT_DISTANCE"),
    ("doesn't go distance", "NOT_DISTANCE"),
    ("go distance = no", "NOT_DISTANCE"),
    ("distance = no", "NOT_DISTANCE"),
    ("goes the distance", "DISTANCE"),
    ("go the distance", "DISTANCE"),
    ("go distance = yes", "DISTANCE"),
    ("distance = yes", "DISTANCE"),
    ("ko or sub", "KO_OR_SUB"),
    ("ko/sub", "KO_OR_SUB"),
    ("ko/tko or submission", "KO_OR_SUB"),
    ("ko/tko or sub", "KO_OR_SUB"),
    # FanDuel Method of Victory Double Chance
    ("ko, tko, dq or submission", "KO_OR_SUB"),
    ("ko/tko/dq or submission", "KO_OR_SUB"),
    ("ko tko dq or submission", "KO_OR_SUB"),
    ("by ko, tko, dq or submission", "KO_OR_SUB"),
    ("by ko/tko/dq or submission", "KO_OR_SUB"),
    ("moneyline", "ML"),
    ("money line", "ML"),
    ("to win fight", "ML"),
    ("to win", "ML"),
    ("ko/tko", "KO_TKO"),
    ("knockout", "KO_TKO"),
    ("tko", "KO_TKO"),
    (" ko", "KO_TKO"),
    ("ko ", "KO_TKO"),
    ("by ko", "KO_TKO"),
    ("submission", "SUB"),
    (" sub", "SUB"),
    ("sub ", "SUB"),
    ("by sub", "SUB"),
    ("decision", "DEC"),
    ("points", "DEC"),
    (" pts", "DEC"),
    ("pts ", "DEC"),
    ("by pts", "DEC"),
    (" dec", "DEC"),
    ("dec ", "DEC"),
    (" ml", "ML"),
    ("ml ", "ML"),
]

_ROUND_RE = re.compile(r"round\s*(\d)|\br\s?(\d)\b|\brd\.?\s*(\d)\b", re.IGNORECASE)

_TOTALS_RE = re.compile(
    r"\b(over|under)\s*(\d+(?:\.\d+)?)\s*rounds?\b",
    re.IGNORECASE,
)

_FIGHT_METHOD_RE = re.compile(
    r"(?:fight\s+)?ends?\s+(?:by|in)\s+(ko|tko|knockout|sub(?:mission)?)",
    re.IGNORECASE,
)

_FIGHT_KO_RE = re.compile(
    r"\bend(?:s)?\s+in\s+ko\b|\bfight\s+ends?\s+by\s+ko\b|\bends?\s+by\s+ko\b",
    re.IGNORECASE,
)


def describe_outcome(fighter: str, outcome_type: str, round_num: int | None) -> str:
    if outcome_type == "DISTANCE":
        return f"{fighter}'s fight goes the distance"
    if outcome_type == "NOT_DISTANCE":
        return f"{fighter}'s fight does NOT go the distance"
    if outcome_type == "FIGHT_KO":
        return f"{fighter} fight ends by KO/TKO"
    if outcome_type == "FIGHT_SUB":
        return f"{fighter} fight ends by Submission"
    if outcome_type.startswith("OVER_") or outcome_type.startswith("UNDER_"):
        direction, rest = outcome_type.split("_", 1)
        whole = rest.split("_")[0]
        return f"{fighter} - {direction.title()} {whole}.5 Rounds"
    desc = f"{fighter} {_OUTCOME_LABELS.get(outcome_type, outcome_type)}"
    if round_num and outcome_type not in ("DEC", "DISTANCE", "NOT_DISTANCE"):
        desc += f" (Round {round_num})"
    return desc


def _scan_outcome(text: str) -> tuple[str | None, int | None]:
    low = f" {text.strip().lower()} "
    round_num = None
    m = _ROUND_RE.search(low)
    if m:
        round_num = int(next(g for g in m.groups() if g))

    # Totals first
    tm = _TOTALS_RE.search(text)
    if tm:
        direction = tm.group(1).upper()
        line = float(tm.group(2))
        whole = int(line)  # 2.5 -> 2 for UNDER_2_5
        return f"{direction}_{whole}_5", None

    if _FIGHT_KO_RE.search(text) or re.search(
        r"\bfight ends by ko\b|\bends by ko/tko\b", text, re.I
    ):
        return "FIGHT_KO", round_num

    fm = _FIGHT_METHOD_RE.search(text)
    if fm:
        token = fm.group(1).lower()
        if token.startswith("sub"):
            return "FIGHT_SUB", round_num
        return "FIGHT_KO", round_num

    outcome_type = None
    for phrase, label in _OUTCOME_KEYWORDS:
        if phrase in low:
            outcome_type = label
            break

    # Bare trailing "ML" / "KO" / "SUB"
    if outcome_type is None:
        if re.search(r"\bml\b", low):
            outcome_type = "ML"
        elif re.search(r"\bko/?tko\b|\bko\b|\btko\b", low):
            outcome_type = "KO_TKO"
        elif re.search(r"\bsub(?:mission)?\b", low):
            outcome_type = "SUB"
        elif re.search(r"\bdec(?:ision)?\b|\bpts\b|\bpoints\b", low):
            outcome_type = "DEC"

    return outcome_type, round_num


def _strip_outcome_noise(text: str) -> str:
    """Remove market words so the remainder is closer to a fighter name."""
    cleaned = text
    cleaned = _TOTALS_RE.sub(" ", cleaned)
    cleaned = _FIGHT_METHOD_RE.sub(" ", cleaned)
    cleaned = _FIGHT_KO_RE.sub(" ", cleaned)
    cleaned = re.sub(
        r"\b("
        r"by|via|to\s+win(?:\s+fight)?|ml|moneyline|money\s*line|"
        r"ko/?tko|ko/?sub|knockout|tko|ko|dq|"
        r"sub(?:mission)?|decision|dec|pts|points|"
        r"round|rounds?|rd\.?|r\d|"
        r"over|under|distance|either\s+fighter|"
        r"fight|ends?|in|or"
        r")\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[,\-/+|]+", " ", cleaned)
    return " ".join(cleaned.split())


def parse_leg_line(line: str) -> dict:
    """
    Returns {"description": str, "fighter_pick": str|None,
    "outcome_type": str|None, "outcome_round": int|None}.
    """
    line = line.strip()
    if not line:
        return {
            "description": line,
            "fighter_pick": None,
            "outcome_type": None,
            "outcome_round": None,
        }

    # Classic builder shape: "Fighter - Outcome"
    if " - " in line:
        fighter_part, outcome_part = line.split(" - ", 1)
        fighter_part = fighter_part.strip()
        outcome_type, round_num = _scan_outcome(outcome_part)
        if fighter_part and outcome_type:
            return {
                "description": line,  # keep original free-text for display
                "fighter_pick": fighter_part,
                "outcome_type": outcome_type,
                "outcome_round": round_num,
            }

    outcome_type, round_num = _scan_outcome(line)
    if not outcome_type:
        return {
            "description": line,
            "fighter_pick": None,
            "outcome_type": None,
            "outcome_round": None,
        }

    fighter_part = _strip_outcome_noise(line)
    # "A vs B" for fight-level props — keep first fighter as anchor
    if re.search(r"\bvs\.?\b", fighter_part, re.I):
        fighter_part = re.split(r"\bvs\.?\b", fighter_part, maxsplit=1, flags=re.I)[0].strip()

    if not fighter_part or len(fighter_part) < 2:
        # Totals / fight-level with no name — still gradeable if anchored later
        if outcome_type in ("FIGHT_KO", "FIGHT_SUB", "DISTANCE", "NOT_DISTANCE") or outcome_type.startswith(
            ("OVER_", "UNDER_")
        ):
            return {
                "description": line,
                "fighter_pick": None,  # card match may fill later
                "outcome_type": outcome_type,
                "outcome_round": round_num,
            }
        return {
            "description": line,
            "fighter_pick": None,
            "outcome_type": None,
            "outcome_round": None,
        }

    return {
        "description": line,
        "fighter_pick": fighter_part,
        "outcome_type": outcome_type,
        "outcome_round": round_num,
    }


def parse_legs(text: str) -> list[dict]:
    """Parses a multi-line legs textbox into a list of leg dicts, skipping blank lines."""
    return [parse_leg_line(ln) for ln in text.splitlines() if ln.strip()]
