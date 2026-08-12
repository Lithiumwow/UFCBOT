"""
Stateful BetMMA-style ticket builder.

Typical path:
  builder = TicketBuilder(client)
  builder.set_mode("parlay")
  events = builder.list_events()
  fights = builder.load_event_card("UFC 330")
  board = builder.show_fight(fights[0].slug)
  builder.add_pick("Turner", "sub")
  builder.add_pick("OtherFighter", "ml", fight_slug=...)
  print(builder.ticket.summary())
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .client import FightOddsClient
from .markets import markets_for_mode
from .models import Event, Fight, Ticket
from .selectors import format_fight_board, resolve_leg


class BetMode(str, Enum):
    STRAIGHT = "straight"
    PROP = "prop"
    PARLAY = "parlay"


class TicketBuilder:
    def __init__(self, client: FightOddsClient | None = None) -> None:
        self.client = client or FightOddsClient()
        self.mode: BetMode | None = None
        self.ticket = Ticket(mode="straight")
        self.current_event: Event | None = None
        self.current_fights: list[Fight] = []
        self.current_fight: Fight | None = None
        self._events_cache: list[Event] | None = None

    # ---- mode ---------------------------------------------------------------

    def set_mode(self, mode: str) -> BetMode:
        m = mode.strip().lower()
        if m in {"s", "straight", "ml", "moneyline"}:
            self.mode = BetMode.STRAIGHT
        elif m in {"p", "prop", "props"}:
            self.mode = BetMode.PROP
        elif m in {"pl", "parlay", "multi", "acca"}:
            self.mode = BetMode.PARLAY
        else:
            raise ValueError("Mode must be straight | prop | parlay")
        self.ticket = Ticket(mode=self.mode.value)
        return self.mode

    def allowed_markets(self) -> list[str]:
        if not self.mode:
            return []
        return [m.key for m in markets_for_mode(self.mode.value)]

    # ---- events / fights ----------------------------------------------------

    def list_events(self, *, ufc_only: bool = True, limit: int = 20) -> list[Event]:
        if ufc_only:
            events = self.client.ufc_upcoming(first=80)
        else:
            events = self.client.upcoming_events(first=50)
        # Drop clearly past cards (name search can include history)
        from datetime import date as date_cls

        today = date_cls.today().isoformat()
        events = [e for e in events if (e.date or "9999") >= today]
        self._events_cache = events
        return events[:limit]

    def load_event_card(self, event_query: str) -> list[Fight]:
        fights = self.client.event_fights_by_name(event_query)
        if not fights:
            # Fallback: user might pass fighter last name to seed a card search
            raise LookupError(
                f"No fights found for event '{event_query}'. "
                "Try an event name fragment like 'UFC 330' or use find_fighter()."
            )
        self.current_fights = fights
        if fights and fights[0].event_pk is not None:
            self.current_event = Event(
                pk=fights[0].event_pk,
                name=fights[0].event_name,
                date=fights[0].event_date,
            )
        return fights

    def find_fighter_fights(self, fighter_name: str) -> list[Fight]:
        parts = fighter_name.strip().split()
        last = parts[-1] if parts else fighter_name
        fights = self.client.fights_for_fighter_lastname(last)
        # Prefer exact first-name match when provided
        if len(parts) >= 2:
            first = parts[0].lower()
            filtered = [
                f
                for f in fights
                if f.fighter1_name.lower().startswith(first)
                or f.fighter2_name.lower().startswith(first)
            ]
            if filtered:
                fights = filtered
        self.current_fights = fights
        return fights

    def show_fight(self, slug_or_query: str) -> str:
        fight = self._resolve_fight(slug_or_query)
        self.current_fight = fight
        return format_fight_board(fight)

    def _resolve_fight(self, slug_or_query: str) -> Fight:
        q = slug_or_query.strip()
        if "vs" in q or "-" in q and " " not in q:
            try:
                return self.client.fight_by_slug(q)
            except Exception:
                pass
        # search current card
        qlow = q.lower()
        for f in self.current_fights:
            if q == f.slug or qlow in f.label().lower() or qlow in f.slug:
                return self.client.fight_by_slug(f.slug)
        # fighter last name → most recent upcoming-ish fight
        fights = self.find_fighter_fights(q)
        if not fights:
            raise LookupError(f"No fight for '{q}'")
        return self.client.fight_by_slug(fights[0].slug)

    # ---- pick legs ----------------------------------------------------------

    def add_pick(
        self,
        fighter: str,
        market: str = "ml",
        *,
        fight_slug: str | None = None,
    ):
        if not self.mode:
            raise RuntimeError("Call set_mode() first")

        if self.mode == BetMode.STRAIGHT and market.lower() not in {
            "ml",
            "moneyline",
            "straight",
            "win",
            "to win",
        }:
            # Force moneyline for pure straight mode
            market = "ml"

        if fight_slug:
            fight = self.client.fight_by_slug(fight_slug)
        elif self.current_fight:
            fight = self.current_fight
        else:
            fight = self._resolve_fight(fighter)

        self.current_fight = fight
        leg = resolve_leg(self.client, fight, fighter, market)

        if self.mode in (BetMode.STRAIGHT, BetMode.PROP):
            self.ticket.legs.clear()
        self.ticket.add(leg)

        if self.mode != BetMode.PARLAY and len(self.ticket.legs) > 1:
            # should not happen; guard
            self.ticket.legs = self.ticket.legs[-1:]

        return leg

    def remove_leg(self, index: int):
        return self.ticket.remove(index - 1)  # 1-based for humans

    def clear_ticket(self) -> None:
        self.ticket.clear()

    def done(self) -> dict:
        """Finalize and return combined odds payload."""
        if not self.ticket.legs:
            raise RuntimeError("Ticket has no legs")
        if self.mode == BetMode.PARLAY and len(self.ticket.legs) < 2:
            raise RuntimeError("Parlay needs at least 2 legs")
        combo = self.ticket.combined()
        assert combo is not None
        return {
            "mode": self.mode.value if self.mode else self.ticket.mode,
            "legs": [
                {
                    "description": leg.selection.description,
                    "market": leg.selection.market_key,
                    "american": leg.american,
                    "fight": leg.selection.fight_label,
                    "slug": leg.selection.fight_slug,
                    "source": leg.source,
                    "sportsbook": leg.sportsbook,
                }
                for leg in self.ticket.legs
            ],
            "combined_american": combo["combined_american"],
            "combined_decimal": combo["combined_decimal"],
            "implied_prob": combo["implied_prob"],
            "text": self.ticket.summary(),
        }
