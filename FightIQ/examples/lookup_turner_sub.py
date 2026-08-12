#!/usr/bin/env python3
"""Example: Jalin Turner wins by submission odds."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fightiq import FightOddsClient, TicketBuilder
from fightiq.odds_math import format_american
from fightiq.selectors import format_fight_board


def main() -> int:
    client = FightOddsClient()
    builder = TicketBuilder(client)

    print("=== Bet mode: PROP ===")
    builder.set_mode("prop")

    print("\nLooking up Jalin Turner fights…")
    fights = builder.find_fighter_fights("Jalin Turner")
    if not fights:
        print("No fights found")
        return 1

    upcoming = fights[0]
    print(f"Using: {upcoming.event_name} — {upcoming.label()}")
    print()
    print(format_fight_board(client.fight_by_slug(upcoming.slug)))

    leg = builder.add_pick("Jalin Turner", "submission", fight_slug=upcoming.slug)
    print("\n=== Selected prop ===")
    print(leg.label())

    result = builder.done()
    print("\n=== Ticket ===")
    print(result["text"])
    print(
        f"\nAmerican: {format_american(result['combined_american'])} | "
        f"Decimal: {result['combined_decimal']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
