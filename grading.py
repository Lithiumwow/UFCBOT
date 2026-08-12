"""
Auto-grading: matches a structured /bet-ufc pick (fighter, outcome type,
round) against ESPN's best-effort fight-result data and decides Won/Loss.

Grading rules, from most to least certain:
- Fight not completed yet -> no grade, still pending.
- Picked fighter didn't win the fight -> LOSS, regardless of outcome_type
  (if your fighter lost, any bet on them winning some particular way lost
  too).
- Picked fighter won, outcome_type == "ML" (moneyline / just-the-winner)
  -> WON. This is the reliable case (based on competitors[].winner).
- Picked fighter won, method/round props:
    - Need a parseable method (and round when the slip specifies one).
    - Never "fallback win" on winner-only when method or round is required —
      leave pending until ESPN gives enough detail, or grade LOSS when the
      known method/round clearly misses.
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Optional

_TOTAL_ROUNDS_RE = re.compile(r"^(OVER|UNDER)_(\d)_5$")


def _name_matches(pick: str, espn_name: str) -> bool:
    """Loose match: exact, one contains the other (handles cases like a bet
    logged as 'Ankalaev' matching ESPN's 'Magomed Ankalaev'), same last
    name (handles shortened/nickname first names like 'Jonathan' vs 'Jon'),
    or a close-spelling last name (handles transliteration variants like
    'Hasan' vs 'Hassan' -- real names genuinely get spelled differently
    across sources, and a single missed letter otherwise means the leg can
    never find its fight result and stays stuck forever)."""
    pick_l = pick.strip().lower()
    espn_l = espn_name.strip().lower()
    if not pick_l or not espn_l:
        return False
    if pick_l == espn_l or pick_l in espn_l or espn_l in pick_l:
        return True

    pick_tokens = pick_l.split()
    espn_tokens = espn_l.split()
    if not pick_tokens or not espn_tokens:
        return False

    pick_last, espn_last = pick_tokens[-1], espn_tokens[-1]
    if pick_last == espn_last and len(pick_last) >= 3:
        return True

    if len(pick_last) >= 4 and len(espn_last) >= 4:
        ratio = difflib.SequenceMatcher(None, pick_last, espn_last).ratio()
        if ratio >= 0.85:
            return True
    return False


def find_result_for_bet(bet: dict[str, Any], fight_results: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Finds the fight_results entry matching this bet's fighter_pick
    (checking either corner), or None if no match found on this card."""
    picked = bet.get("fighter_pick")
    if not picked:
        return None
    for result in fight_results:
        if _name_matches(picked, result["fighter_a"]) or _name_matches(picked, result["fighter_b"]):
            return result
    return None


def _actual_end_round(result: dict[str, Any]) -> Optional[int]:
    """Which round the fight actually ended in -- the finish round for a
    KO/TKO/Submission, or the last scheduled round for a decision (since
    going to a decision means it lasted the full fight). None if this
    can't be determined from what ESPN gave us."""
    method = result.get("method")
    if method == "DEC":
        return result.get("scheduled_rounds")
    if method in ("KO_TKO", "SUB"):
        return result.get("round")
    # Even without method, ESPN's period often has the end round
    return result.get("round")


# MMA rounds are 5 minutes; "0.5 rounds" = 2:30 into that round.
_ROUND_SECONDS = 300.0


def _fight_progress_rounds(result: dict[str, Any]) -> Optional[float]:
    """How far the fight went, in round units (e.g. R1 @ 0:34 → ~0.113).

    Under 0.5 = finished before 2:30 of round 1 (progress < 0.5).
    Under 1.5 = finished before 2:30 of round 2 (progress < 1.5).
    """
    method = result.get("method")
    if method == "DEC":
        scheduled = result.get("scheduled_rounds")
        return float(scheduled) if scheduled else None

    end_round = result.get("round")
    if end_round is None:
        end_round = _actual_end_round(result)
    if end_round is None:
        return None

    end_round = int(end_round)
    elapsed = result.get("elapsed_seconds")
    if elapsed is not None:
        try:
            elapsed_f = float(elapsed)
        except (TypeError, ValueError):
            elapsed_f = None
        if elapsed_f is not None and elapsed_f >= 0:
            # Clamp into one round length in case ESPN sends odd values
            elapsed_f = min(elapsed_f, _ROUND_SECONDS)
            return (end_round - 1) + (elapsed_f / _ROUND_SECONDS)

    # No clock: we only know the finish round. Safe bounds:
    # progress is in (end_round - 1, end_round] — enough for some .5 lines
    # (e.g. R1 finish is always under 1.5 / over 0.5) but NOT for lines
    # that split that round (under/over 0.5 on a R1 finish).
    return None


def _grade_total_rounds(
    direction: str, line: float, result: dict[str, Any]
) -> Optional[tuple[str, bool]]:
    progress = _fight_progress_rounds(result)
    if progress is not None:
        if direction == "OVER":
            return ("won" if progress > line else "loss", True)
        return ("won" if progress < line else "loss", True)

    # Fallback without clock: use whole-round bounds where unambiguous.
    end_round = _actual_end_round(result)
    if end_round is None:
        return None
    end_round = int(end_round)
    # Max progress if fight ended in this round without a clock: just under end_round
    # Min progress: just over (end_round - 1)
    # Under L wins only if even the max is still < L
    # Over L wins only if even the min is still > L
    max_progress = float(end_round)  # ended at end of round (worst for under)
    min_progress = float(end_round - 1) + 1e-6  # started the round (worst for over)

    if direction == "UNDER":
        if max_progress < line:
            return ("won", True)
        if min_progress >= line:
            return ("loss", True)
        return None  # e.g. Under 0.5 with R1 finish and no clock
    # OVER
    if min_progress > line:
        return ("won", True)
    if max_progress <= line:
        return ("loss", True)
    return None


def grade_bet(bet: dict[str, Any], result: dict[str, Any]) -> Optional[tuple[str, bool]]:
    """
    Returns (new_status, method_confirmed) or None if not gradeable yet.
    method_confirmed is True when method/round props were verified against
    ESPN detail (not just winner).
    """
    if not result.get("completed"):
        return None

    outcome_type = (bet.get("outcome_type") or "ML").upper()
    method = result.get("method")

    total_rounds_match = _TOTAL_ROUNDS_RE.match(outcome_type)
    if total_rounds_match:
        direction, whole = total_rounds_match.group(1), int(total_rounds_match.group(2))
        line = whole + 0.5
        return _grade_total_rounds(direction, line, result)

    if outcome_type in ("DISTANCE", "NOT_DISTANCE", "FIGHT_KO", "FIGHT_SUB"):
        # Winner-agnostic: if method can't be determined, leave pending.
        if method is None:
            return None
        went_the_distance = method == "DEC"
        if outcome_type == "DISTANCE":
            return ("won" if went_the_distance else "loss", True)
        if outcome_type == "NOT_DISTANCE":
            return ("won" if not went_the_distance else "loss", True)
        if outcome_type == "FIGHT_KO":
            return ("won" if method == "KO_TKO" else "loss", True)
        return ("won" if method == "SUB" else "loss", True)  # FIGHT_SUB

    picked = bet.get("fighter_pick") or ""
    winner = result.get("winner")

    if not winner or not _name_matches(picked, winner):
        return ("loss", True)  # picked fighter didn't win -- always a clean loss

    if outcome_type == "ML":
        return ("won", True)

    round_num = result.get("round")
    picked_round = bet.get("outcome_round")

    # Method / round props must be verified — never "winner-only" WON.
    if method is None:
        return None

    if outcome_type == "KO_OR_SUB":
        method_matches = method in ("KO_TKO", "SUB")
    else:
        method_matches = method == outcome_type

    if not method_matches:
        return ("loss", True)

    if picked_round is not None:
        if round_num is None:
            return None  # need end-round before settling a specific-round prop
        if int(picked_round) != int(round_num):
            return ("loss", True)

    return ("won", True)


def aggregate_bet_status(leg_statuses: list[str]) -> Optional[str]:
    """
    Combines every leg's status into the overall bet status (parlay logic):
    a parlay needs every leg to win. leg_statuses should include ALL legs
    on the bet, structured or not -- pass "pending" for any freeform leg
    that isn't auto-graded, since a parlay can't be called won/lost while
    any leg (graded or not) is still unresolved.

    Returns "won" only if every leg is "won", "loss" as soon as any leg is
    "loss" (a parlay is dead the moment one leg loses, regardless of the
    others), or None if it's not yet decidable (something's still pending).
    """
    if any(s == "loss" for s in leg_statuses):
        return "loss"
    if all(s == "won" for s in leg_statuses):
        return "won"
    return None
