#!/usr/bin/env python3
"""
Interactive BetMMA-style CLI.

Flow: mode → event/fighter → fight board → pick market → (parlay: repeat) → done
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fightiq import TicketBuilder
from fightiq.odds_math import format_american
from fightiq.selectors import format_fight_board


def prompt(msg: str) -> str:
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)


def main() -> int:
    b = TicketBuilder()
    print("FightIQ — FightOdds straight / prop / parlay builder\n")

    while True:
        mode = prompt("Mode [straight / prop / parlay]: ")
        try:
            b.set_mode(mode)
            break
        except ValueError as e:
            print(e)

    print(f"Mode set to {b.mode.value}. Allowed markets: {', '.join(b.allowed_markets())}")
    print("Commands: events | card <name> | fighter <name> | board | pick <fighter> <market> | legs | done | quit\n")

    while True:
        cmd = prompt("fightiq> ")
        if not cmd:
            continue
        low = cmd.lower()
        if low in {"q", "quit", "exit"}:
            return 0

        try:
            if low == "events":
                for i, e in enumerate(b.list_events(limit=15), 1):
                    print(f"  {i}. {e.label()}  pk={e.pk}")

            elif low.startswith("card "):
                q = cmd[5:].strip()
                fights = b.load_event_card(q)
                print(f"{len(fights)} fight(s) on card:")
                for i, f in enumerate(fights, 1):
                    print(
                        f"  {i}. {f.label()}  "
                        f"ML {format_american(f.fighter1_odds)} / {format_american(f.fighter2_odds)}"
                    )
                    print(f"      slug={f.slug}")

            elif low.startswith("fighter "):
                q = cmd[8:].strip()
                fights = b.find_fighter_fights(q)
                print(f"{len(fights)} fight(s):")
                for i, f in enumerate(fights[:10], 1):
                    print(f"  {i}. [{f.event_date}] {f.event_name} — {f.label()}")
                    print(f"      slug={f.slug}")
                if fights:
                    b.current_fight = b.client.fight_by_slug(fights[0].slug)
                    print(f"\nCurrent fight set to latest: {b.current_fight.label()}")

            elif low.startswith("board"):
                parts = cmd.split(maxsplit=1)
                slug = parts[1] if len(parts) > 1 else (
                    b.current_fight.slug if b.current_fight else ""
                )
                if not slug:
                    print("Usage: board <slug>  or select a fight first")
                    continue
                print(b.show_fight(slug))

            elif low.startswith("pick "):
                rest = cmd[5:].strip()
                # pick Turner sub   OR   pick Turner submission
                bits = rest.split()
                if len(bits) < 2:
                    print("Usage: pick <fighter words...> <market>")
                    continue
                market = bits[-1]
                fighter = " ".join(bits[:-1])
                leg = b.add_pick(fighter, market)
                print(f"Added: {leg.label()}")
                if b.mode and b.mode.value != "parlay":
                    print(b.done()["text"])

            elif low == "legs":
                print(b.ticket.summary())

            elif low == "done":
                result = b.done()
                print(result["text"])
                print(
                    f"Combined American: {format_american(result['combined_american'])}"
                )
                return 0

            elif low == "clear":
                b.clear_ticket()
                print("Ticket cleared")

            else:
                print("Unknown command")
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
