# FightIQ bot architecture (BetMMA-style)

## User journey

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│ Select mode │ ──► │ Select event │ ──► │ Select fight    │
│ S / P / PL  │     │ (upcoming)   │     │ (from card)     │
└─────────────┘     └──────────────┘     └────────┬────────┘
                                                  │
                    ┌─────────────────────────────▼──────────┐
                    │ Select market:                          │
                    │  • Straight → side (F1 / F2)            │
                    │  • Prop → fighter + method (SUB/KO/DEC)  │
                    │          or fight prop (rounds, O/U)    │
                    └─────────────────────────────┬──────────┘
                                                  │
                    ┌─────────────────────────────▼──────────┐
                    │ Fetch odds from FightOdds GraphQL       │
                    │ Attach Leg(american=…, market=…)         │
                    └─────────────────────────────┬──────────┘
                                                  │
              ┌───────────────────┬───────────────┴────────────┐
              │ STRAIGHT / PROP   │ PARLAY                     │
              │ show single price │ add more legs → combine    │
              └───────────────────┴────────────────────────────┘
```

## Mode definitions

| Mode | Legs | Markets typically used |
|------|------|-------------------------|
| **Straight** | 1 | Moneyline only |
| **Prop** | 1 | Method, round, distance, O/U |
| **Parlay** | 2+ | Any mix of ML + props; combined via decimal product |

Parlay combo uses independent American odds (same math as sportsbook multiplications; does **not** reprice correlated-risk books).

## Modules

| Module | Role |
|--------|------|
| `client.FightOddsClient` | HTTP + GraphQL, retries |
| `models.Event / Fight / Selection / Leg / Ticket` | Structured bet objects |
| `markets.METHOD_MARKETS` | Maps `sub` / `ko` / `dec` → offerTypeId + fight field |
| `selectors.resolve_leg(...)` | Fighter + method → American odds |
| `odds_math` | American ↔ decimal; parlay product |
| `bot_flow.TicketBuilder` | Stateful step machine for chat bots |

## Suggested Discord commands

```
/fightiq start                 # open session
/fightiq mode straight|prop|parlay
/fightiq events                # list upcoming UFC
/fightiq card <event_pk|name>
/fightiq fight <slug|search>
/fightiq pick <fighter> [ml|sub|ko|dec|r1|…]
/fightiq legs                  # show ticket
/fightiq remove <n>
/fightiq done                  # lock ticket + combined odds
/fightiq cancel
```

## Data cache

Per session, cache:

- `events` — refreshed every 15–30 minutes  
- `fights_by_event` — when user opens a card  
- `fight_detail[slug]` — short TTL (30–60s) while building legs so prices stay fresh  

## Parlay combination

```
decimal_i = american_to_decimal(odds_i)
combined_decimal = ∏ decimal_i
combined_american = decimal_to_american(combined_decimal)
```

Example:

- Leg 1: Turner by sub **+400** → 5.00  
- Leg 2: Other ML **+150** → 2.50  
- Combined: 5.00 × 2.50 = **12.50** → **+1150**

## Extending markets

1. Add `offerTypeId` to `markets.py`  
2. Prefer summary field on `FightNode` if present (`fighter1SubOdds`, …)  
3. Else fall back to `fightPropOfferTable` → `propOffers` filtered by `offerTypeId` + fighter  
4. Optionally scan `offers.edges` for best book

## Source of truth

Live site uses the same GraphQL schema discovered in `docs/FIGHTODDS_API.md`.  
If field names change, re-introspect:

```graphql
{ __type(name: "FightNode") { fields { name } } }
```
