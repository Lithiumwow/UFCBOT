"""Domain models for events, fights, and bet tickets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .odds_math import combine_parlay, format_american


@dataclass(frozen=True)
class Event:
    pk: int
    name: str
    date: str | None
    promotion: str | None = None
    is_cancelled: bool = False

    def label(self) -> str:
        promo = f"[{self.promotion}] " if self.promotion else ""
        return f"{promo}{self.name} ({self.date or 'TBD'})"


@dataclass
class Fight:
    id: str
    pk: int | None
    slug: str
    event_name: str
    event_date: str | None
    event_pk: int | None
    fighter1_id: str
    fighter1_name: str
    fighter2_id: str
    fighter2_name: str
    fighter1_odds: int | None = None
    fighter2_odds: int | None = None
    fighter1_sub: int | None = None
    fighter2_sub: int | None = None
    fighter1_ko: int | None = None
    fighter2_ko: int | None = None
    fighter1_dec: int | None = None
    fighter2_dec: int | None = None
    fighter1_r1: int | None = None
    fighter2_r1: int | None = None
    fighter1_r2: int | None = None
    fighter2_r2: int | None = None
    fighter1_r3: int | None = None
    fighter2_r3: int | None = None
    fighter1_itd: int | None = None
    fighter2_itd: int | None = None
    fight_itd: int | None = None
    is_cancelled: bool = False
    is_five_rounds: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def label(self) -> str:
        return f"{self.fighter1_name} vs {self.fighter2_name}"

    def corner(self, fighter_query: str) -> int:
        """Return 1 or 2 for which corner matches the name query."""
        q = fighter_query.strip().lower()
        f1 = self.fighter1_name.lower()
        f2 = self.fighter2_name.lower()
        if q == f1 or q in f1.split() or f1.startswith(q) or q in f1:
            # Prefer exact last-name / full-name hits
            score1 = _name_score(q, f1)
            score2 = _name_score(q, f2)
            if score1 == 0 and score2 == 0:
                raise ValueError(f"Fighter '{fighter_query}' not in {self.label()}")
            return 1 if score1 >= score2 else 2
        score1 = _name_score(q, f1)
        score2 = _name_score(q, f2)
        if score1 == 0 and score2 == 0:
            raise ValueError(f"Fighter '{fighter_query}' not in {self.label()}")
        return 1 if score1 >= score2 else 2

    def fighter_name(self, corner: int) -> str:
        return self.fighter1_name if corner == 1 else self.fighter2_name


def _name_score(query: str, full: str) -> int:
    if not query:
        return 0
    if query == full:
        return 100
    parts = full.split()
    if query in parts:
        return 80
    if any(p.startswith(query) for p in parts):
        return 60
    if query in full:
        return 40
    return 0


@dataclass(frozen=True)
class Selection:
    """What the user chose before odds are attached."""

    fight_slug: str
    fight_label: str
    market_key: str  # ml, sub, ko, dec, r1, ...
    corner: int  # 1 or 2 (0 = fight-level market)
    description: str


@dataclass
class Leg:
    selection: Selection
    american: int
    source: str = "fight_summary"  # or prop_table / sportsbook
    sportsbook: str | None = None

    def label(self) -> str:
        return f"{self.selection.description} @ {format_american(self.american)}"


@dataclass
class Ticket:
    mode: str  # straight | prop | parlay
    legs: list[Leg] = field(default_factory=list)

    def add(self, leg: Leg) -> None:
        self.legs.append(leg)

    def remove(self, index: int) -> Leg:
        return self.legs.pop(index)

    def clear(self) -> None:
        self.legs.clear()

    def combined(self) -> dict | None:
        if not self.legs:
            return None
        americans = [leg.american for leg in self.legs]
        if len(americans) == 1:
            from .odds_math import american_to_decimal, imply_prob

            a = americans[0]
            return {
                "legs_american": americans,
                "legs_decimal": [float(american_to_decimal(a))],
                "combined_decimal": float(american_to_decimal(a)),
                "combined_american": a,
                "implied_prob": float(imply_prob(a)),
            }
        return combine_parlay(americans)

    def summary(self) -> str:
        if not self.legs:
            return f"Empty {self.mode} ticket"
        lines = [f"{self.mode.upper()} ticket ({len(self.legs)} leg(s)):"]
        for i, leg in enumerate(self.legs, 1):
            lines.append(f"  {i}. {leg.label()}")
        combo = self.combined()
        if combo:
            lines.append(
                f"Combined: {format_american(combo['combined_american'])} "
                f"(decimal {combo['combined_decimal']})"
            )
        return "\n".join(lines)
