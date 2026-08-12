# FightIQ

MMA betting-flow toolkit powered by [FightOdds.io](https://fightodds.io/odds).

Mirrors the BetMMA-style path:

1. **Pick bet type** — Straight · Prop · Parlay  
2. **Pick event / fight** (from live card data)  
3. **Pick fighter + method** (or other prop market)  
4. **Fetch odds** (best American price, optional per-book)  
5. **Combine** legs into a parlay (American → decimal → product → American)

## Layout

```
FightIQ/
├── README.md
├── docs/
│   ├── ARCHITECTURE.md      # Bet flow + bot design
│   └── FIGHTODDS_API.md     # GraphQL map, offer types, queries
├── fightiq/
│   ├── __init__.py
│   ├── client.py            # GraphQL client
│   ├── models.py            # Event / Fight / Leg / Ticket
│   ├── odds_math.py         # American ↔ decimal, parlay combo
│   ├── markets.py           # Offer-type catalog (SUB, KO, DEC, …)
│   ├── selectors.py         # Resolve fighter / method → odds
│   └── bot_flow.py          # Step machine (straight / prop / parlay)
├── examples/
│   ├── lookup_turner_sub.py
│   └── interactive_cli.py
└── requirements.txt         # none (stdlib only)
```

## Full prop catalog (sportsbook plays)

`fightiq/props_catalog.py` loads **every** FightOdds prop for a fight (O/U rounds,
methods, round winners, double chances, etc.) and flattens both sides into playables.

```python
from fightiq import get_prop_service

svc = get_prop_service()
cat = svc.get_catalog("islam-makhachev-vs-ian-garry-79253")

# Popular shortlist (web/Discord default)
popular = cat.filter(popular_only=True, limit=40)

# Search (Discord-friendly when list is huge)
subs = cat.filter(query="submission", limit=25)

# Single book
fd = cat.filter(sportsbook="FanDuel", query="distance", limit=30)
```

Web: pick **Bookie** → event → fight → **Popular / All / Search**.

## Web sandbox

```bash
cd /root/.local/FightIQ
python3 webapp/app.py
# http://HOST:9999
```

## Quick start (CLI)

```bash
cd /root/.local/FightIQ
python3 examples/lookup_turner_sub.py
python3 examples/interactive_cli.py
```

## Core idea for a Discord / Telegram bot

```
User: /bet
Bot:  [Straight] [Prop] [Parlay]

User: Parlay
Bot:  Upcoming events… pick one
User: UFC 330
Bot:  Fights on the card…
User: Turner vs Fernandes
Bot:  Side + method: [Turner ML] [Turner by SUB] [Turner by KO] …
User: Turner by SUB  →  +400  (leg 1)
Bot:  Add another leg or /done
User: (another fight / method)
Bot:  Combined parlay odds = …
```

Odds always come from FightOdds GraphQL (`api.fightodds.io/gql`). See `docs/FIGHTODDS_API.md`.
