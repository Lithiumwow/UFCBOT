"""Resolve a fighter + market selection into priced legs."""

from __future__ import annotations

from typing import Optional

from .client import FightOddsClient
from .markets import METHOD_MARKETS, MarketDef, normalize_market
from .models import Fight, Leg, Selection
from .odds_math import format_american


def describe_selection(fight: Fight, market: MarketDef, corner: int) -> str:
    if corner in (1, 2):
        name = fight.fighter_name(corner)
        return f"{name} — {market.label} ({fight.label()})"
    return f"{market.label} ({fight.label()})"


def price_from_summary(fight: Fight, market: MarketDef, corner: int) -> Optional[int]:
    if market.fight_field is None:
        return None
    return market.fight_field(fight, corner)


def price_from_props(
    client: FightOddsClient,
    fight: Fight,
    market: MarketDef,
    corner: int,
) -> tuple[Optional[int], Optional[str]]:
    """
    Fall back to prop table: match offerTypeId + fighter name.
    Returns (american, sportsbook_or_None).
    """
    if not market.offer_type_id:
        return None, None
    target = fight.fighter_name(corner).lower()
    try:
        props = client.prop_offers(fight.slug)
    except Exception:
        return None, None

    best: Optional[int] = None
    best_book: Optional[str] = None

    for prop in props:
        ot = (prop.get("offerType") or {}).get("offerTypeId")
        if ot != market.offer_type_id:
            continue
        # Prefer summary bestOdds if outcomes map to this fighter
        for side, best_key, name_key in (
            (1, "bestOdds1", "propName1"),
            (2, "bestOdds2", "propName2"),
        ):
            pname = (prop.get(name_key) or "").lower()
            if target.split()[-1] in pname or target in pname:
                odd = prop.get(best_key)
                if odd and int(odd) != 0:
                    if best is None or _is_better(int(odd), best):
                        best = int(odd)
                        best_book = "best"

        for edge in (prop.get("offers") or {}).get("edges") or []:
            node = edge["node"]
            book = (node.get("sportsbook") or {}).get("shortName")
            for key in ("outcome1", "outcome2"):
                out = node.get(key)
                if not out or out.get("odds") is None:
                    continue
                fname = ""
                if out.get("fighter"):
                    f = out["fighter"]
                    fname = f"{f.get('firstName', '')} {f.get('lastName', '')}".strip().lower()
                oname = (out.get("name") or "").lower()
                if target.split()[-1] not in fname and target not in fname and target.split()[-1] not in oname:
                    continue
                odd = int(out["odds"])
                if best is None or _is_better(odd, best):
                    best = odd
                    best_book = book
    return best, best_book


def _is_better(candidate: int, current: int) -> bool:
    """Higher American = better price for the bettor (underdog or less juice)."""
    return candidate > current


def resolve_leg(
    client: FightOddsClient,
    fight: Fight,
    fighter_query: str,
    market_raw: str,
    *,
    prefer_props: bool = False,
    refresh: bool = True,
) -> Leg:
    """
    Build a priced Leg for fighter_query + market on the given fight.
    """
    market_key = normalize_market(market_raw)
    market = METHOD_MARKETS[market_key]
    corner = fight.corner(fighter_query)

    if refresh:
        fight = client.fight_by_slug(fight.slug)

    american: Optional[int] = None
    source = "fight_summary"
    sportsbook: Optional[str] = None

    if not prefer_props:
        american = price_from_summary(fight, market, corner)

    if american is None:
        american, sportsbook = price_from_props(client, fight, market, corner)
        if american is not None:
            source = "prop_table"

    if american is None:
        raise LookupError(
            f"No odds for {fight.fighter_name(corner)} / {market.label} "
            f"on {fight.label()}"
        )

    selection = Selection(
        fight_slug=fight.slug,
        fight_label=fight.label(),
        market_key=market_key,
        corner=corner,
        description=describe_selection(fight, market, corner),
    )
    return Leg(
        selection=selection,
        american=int(american),
        source=source,
        sportsbook=sportsbook,
    )


def list_available_markets(fight: Fight, corner: int) -> list[tuple[str, str, Optional[int]]]:
    """Return (key, label, odds) for menus."""
    rows = []
    for key, market in METHOD_MARKETS.items():
        odd = price_from_summary(fight, market, corner)
        rows.append((key, market.label, odd))
    return rows


def format_fight_board(fight: Fight) -> str:
    """Pretty board for both corners."""
    lines = [
        fight.event_name,
        fight.label(),
        f"slug: {fight.slug}",
        "",
        f"{'Market':<22} {fight.fighter1_name:<22} {fight.fighter2_name}",
        "-" * 68,
    ]
    rows = [
        ("Moneyline", fight.fighter1_odds, fight.fighter2_odds),
        ("By submission", fight.fighter1_sub, fight.fighter2_sub),
        ("By KO/TKO", fight.fighter1_ko, fight.fighter2_ko),
        ("By decision", fight.fighter1_dec, fight.fighter2_dec),
        ("Round 1", fight.fighter1_r1, fight.fighter2_r1),
        ("Round 2", fight.fighter1_r2, fight.fighter2_r2),
        ("Round 3", fight.fighter1_r3, fight.fighter2_r3),
        ("Inside distance", fight.fighter1_itd, fight.fighter2_itd),
    ]
    for label, a, b in rows:
        lines.append(f"{label:<22} {format_american(a):<22} {format_american(b)}")
    return "\n".join(lines)
