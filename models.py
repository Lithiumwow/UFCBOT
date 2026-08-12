"""Domain models for FightOdds events and fights (used by the Discord bot)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
