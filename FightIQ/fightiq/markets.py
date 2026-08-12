"""Market catalog: maps user-facing method keys → FightOdds fields / offer types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .models import Fight


@dataclass(frozen=True)
class MarketDef:
    key: str
    label: str
    offer_type_id: str | None
    # Summarized best-odds field resolver on Fight (if available)
    fight_field: Callable[[Fight, int], Optional[int]] | None
    category: str  # straight | prop | fight_prop
    needs_corner: bool = True


def _field(name: str) -> Callable[[Fight, int], Optional[int]]:
    def getter(fight: Fight, corner: int) -> Optional[int]:
        attr = name.format(n=corner)
        return getattr(fight, attr, None)

    return getter


# User-facing markets used by TicketBuilder / selectors
METHOD_MARKETS: dict[str, MarketDef] = {
    "ml": MarketDef(
        key="ml",
        label="Moneyline (to win)",
        offer_type_id=None,
        fight_field=_field("fighter{n}_odds"),
        category="straight",
        needs_corner=True,
    ),
    "sub": MarketDef(
        key="sub",
        label="Wins by submission",
        offer_type_id="SUB",
        fight_field=_field("fighter{n}_sub"),
        category="prop",
        needs_corner=True,
    ),
    "ko": MarketDef(
        key="ko",
        label="Wins by KO/TKO",
        offer_type_id="KO",
        fight_field=_field("fighter{n}_ko"),
        category="prop",
        needs_corner=True,
    ),
    "dec": MarketDef(
        key="dec",
        label="Wins by decision",
        offer_type_id="DEC",
        fight_field=_field("fighter{n}_dec"),
        category="prop",
        needs_corner=True,
    ),
    "itd": MarketDef(
        key="itd",
        label="Wins inside the distance",
        offer_type_id="ID",
        fight_field=_field("fighter{n}_itd"),
        category="prop",
        needs_corner=True,
    ),
    "r1": MarketDef(
        key="r1",
        label="Wins in round 1",
        offer_type_id="R_1",
        fight_field=_field("fighter{n}_r1"),
        category="prop",
        needs_corner=True,
    ),
    "r2": MarketDef(
        key="r2",
        label="Wins in round 2",
        offer_type_id="R_2",
        fight_field=_field("fighter{n}_r2"),
        category="prop",
        needs_corner=True,
    ),
    "r3": MarketDef(
        key="r3",
        label="Wins in round 3",
        offer_type_id="R_3",
        fight_field=_field("fighter{n}_r3"),
        category="prop",
        needs_corner=True,
    ),
}

# Aliases users / bots may type
ALIASES: dict[str, str] = {
    "moneyline": "ml",
    "straight": "ml",
    "win": "ml",
    "to win": "ml",
    "submission": "sub",
    "by sub": "sub",
    "by submission": "sub",
    "submit": "sub",
    "knockout": "ko",
    "tko": "ko",
    "by ko": "ko",
    "by tko": "ko",
    "decision": "dec",
    "by decision": "dec",
    "inside distance": "itd",
    "finish": "itd",
    "round 1": "r1",
    "round1": "r1",
    "r1 win": "r1",
    "round 2": "r2",
    "round 3": "r3",
}


def normalize_market(raw: str) -> str:
    k = raw.strip().lower()
    if k in METHOD_MARKETS:
        return k
    if k in ALIASES:
        return ALIASES[k]
    # bare number for rounds
    if k in {"1", "2", "3", "4", "5"}:
        return f"r{k}"
    raise KeyError(
        f"Unknown market '{raw}'. Try: {', '.join(METHOD_MARKETS)} "
        f"or aliases like submission / ko / decision"
    )


def markets_for_mode(mode: str) -> list[MarketDef]:
    mode = mode.lower()
    if mode == "straight":
        return [METHOD_MARKETS["ml"]]
    if mode == "prop":
        return [m for m in METHOD_MARKETS.values() if m.category == "prop"]
    # parlay can use any
    return list(METHOD_MARKETS.values())
