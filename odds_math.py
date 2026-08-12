"""American odds helpers used by FightOdds domain models."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence


def american_to_decimal(american: int | float) -> Decimal:
    a = Decimal(str(int(american)))
    if a == 0:
        raise ValueError("American odds cannot be 0")
    if a > 0:
        return (a / Decimal(100)) + Decimal(1)
    return (Decimal(100) / abs(a)) + Decimal(1)


def decimal_to_american(decimal_odds: Decimal | float) -> int:
    d = Decimal(str(decimal_odds))
    if d <= 1:
        raise ValueError(f"Decimal odds must be > 1, got {d}")
    if d >= 2:
        american = (d - 1) * 100
    else:
        american = Decimal(-100) / (d - 1)
    return int(american.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def combine_parlay(american_legs: Sequence[int]) -> dict:
    if not american_legs:
        raise ValueError("Need at least one leg")
    decimals = [american_to_decimal(a) for a in american_legs]
    combined = Decimal(1)
    for d in decimals:
        combined *= d
    return {
        "legs_american": list(american_legs),
        "legs_decimal": [float(d) for d in decimals],
        "combined_decimal": float(combined.quantize(Decimal("0.0001"))),
        "combined_american": decimal_to_american(combined),
        "implied_prob": float((Decimal(1) / combined).quantize(Decimal("0.0001"))),
    }


def format_american(american: int | None) -> str:
    if american is None:
        return "n/a"
    a = int(american)
    return f"+{a}" if a > 0 else str(a)
