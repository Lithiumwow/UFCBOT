# FightOdds.io GraphQL API

**Endpoint:** `POST https://api.fightodds.io/gql`  
**UI:** https://fightodds.io/odds  

Reads do not require an API key. Send JSON:

```http
POST /gql HTTP/1.1
Host: api.fightodds.io
Content-Type: application/json
Origin: https://fightodds.io
Referer: https://fightodds.io/odds
User-Agent: FightIQ/1.0

{"query":"{ ... }", "variables": {}}
```

Odds are **American integers** (`-170`, `+400`). `null` means no price listed.

---

## Key queries

### Upcoming / past events

```graphql
{
  allEvents(first: 20, upcoming: true) {
    edges {
      node { pk name date isCancelled promotion { shortName } }
    }
  }
}
```

Filters: `name_Icontains`, `date_Gte`, `promotion_ShortName`, `isCancelled`.

### Fights on an event

`allFights` accepts fighter name filters and `event` (global ID). Easier path for bots:

```graphql
{
  allFights(
    first: 30
    fighter1_LastName_Icontains: "Turner"
  ) {
    edges {
      node {
        id pk slug isCancelled fightType isFiveRounds
        event { pk name date }
        fighter1 { id firstName lastName slug }
        fighter2 { id firstName lastName slug }
        fighter1Odds fighter2Odds
        fighter1SubOdds fighter2SubOdds
        fighter1KoOdds fighter2KoOdds
        fighter1DecOdds fighter2DecOdds
        fighter1R1Odds fighter2R1Odds
        fighter1R2Odds fighter2R2Odds
        fighter1R3Odds fighter2R3Odds
        fighter1ItdOdds fighter2ItdOdds
        fightItdOdds
      }
    }
  }
}
```

### Single fight by slug

```graphql
{
  fightBySlug(slug: "jalin-turner-vs-kaue-fernandes-79619") {
    slug
    event { name date }
    fighter1 { firstName lastName }
    fighter2 { firstName lastName }
    fighter1Odds fighter2Odds
    fighter1SubOdds fighter2SubOdds
    # …
  }
}
```

### Fighter search

```graphql
{
  allFighters(first: 10, firstName_Icontains: "Jalin", lastName_Icontains: "Turner") {
    edges { node { id firstName lastName slug } }
  }
  fighterBySlug(slug: "jalin-turner-14302") {
    id firstName lastName subWins koWins decWins
  }
}
```

### Moneyline table (best + books)

```graphql
{
  fightOfferTable(slug: "jalin-turner-vs-kaue-fernandes-79619") {
    fighter1 { firstName lastName }
    fighter2 { firstName lastName }
    bestOdds1 bestOdds2
    straightOffers { /* connection of book lines */ }
  }
}
```

### Prop / method markets (SUB, KO, rounds, …)

```graphql
{
  fightPropOfferTable(slug: "jalin-turner-vs-kaue-fernandes-79619") {
    fight {
      fighter1 { firstName lastName }
      fighter2 { firstName lastName }
      fighter1SubOdds fighter2SubOdds
    }
    propOffers(first: 100) {
      edges {
        node {
          offerType { offerTypeId description category subCategory }
          propName1 propName2
          bestOdds1 bestOdds2
          offers {
            edges {
              node {
                sportsbook { shortName fullName }
                outcome1 { name odds fighter { firstName lastName } }
                outcome2 { name odds fighter { firstName lastName } }
              }
            }
          }
        }
      }
    }
  }
}
```

### Offer-type catalog

```graphql
{
  allOfferTypes(first: 200) {
    edges {
      node {
        offerTypeId category subCategory
        description notDescription value
      }
    }
  }
}
```

---

## Straight / method summary fields on `FightNode`

These are the fastest path for a bot (best-available American odds):

| Field | Meaning |
|--------|---------|
| `fighter1Odds` / `fighter2Odds` | Moneyline |
| `fighter1SubOdds` / `fighter2SubOdds` | Wins by submission |
| `fighter1KoOdds` / `fighter2KoOdds` | Wins by KO/TKO |
| `fighter1DecOdds` / `fighter2DecOdds` | Wins by decision |
| `fighter1R1Odds` … `fighter1R5Odds` | Wins in round N |
| `fighter1ItdOdds` / `fighter2ItdOdds` | Wins inside the distance |
| `fightItdOdds` | Fight ends inside distance (fight-level) |

When the fighter is **corner 1**, use `fighter1*`; when **corner 2**, use `fighter2*`.

---

## Important `offerTypeId` values (props)

### Method of victory (per fighter) — category fighter outcome

| ID | Description |
|----|-------------|
| `SUB` | wins by submission |
| `KO` | wins by TKO/KO |
| `DEC` | wins by decision |
| `UD` | wins by unanimous decision |
| `SD` | wins by split/majority decision |
| `ID` | wins inside distance |

### Round (per fighter)

| ID | Description |
|----|-------------|
| `R_1` … `R_5` | wins in round N |
| `SUB_1` … `SUB_5` | wins round N by submission |
| `KO_1` … `KO_5` | wins round N by KO/TKO |

### Fight-level result

| ID | Description |
|----|-------------|
| `DISTANCE` | fight goes the distance |
| `END_SUB` | fight ends in submission |
| `END_KO` | fight ends by KO/TKO/DQ |
| `END_1` … `END_5` | fight ends in round N |
| `OVERUNDER_2.5` etc. | total rounds O/U |

---

## Example: Jalin Turner submission (live snapshot when documented)

- Event: **UFC 330: Makhachev vs. Machado Garry** (`2026-08-15`)
- Fight slug: `jalin-turner-vs-kaue-fernandes-79619`
- Turner ML: `-170`
- **Turner by submission: `+400`**
- Fernandes by submission: `+900`

---

## Notes / limits

1. UI is a SPA (`app.bundle.js`); all real data is GraphQL — prefer API over HTML scrape.  
2. `allUpcomingEventOfferTables` may throw server errors; use `allEvents(upcoming: true)` + fights instead.  
3. Some prop books only post one side (e.g. yes-priced method, no “doesn't win by X”).  
4. Prices move; always re-fetch before posting a ticket.  
5. Rate-limit politely if polling (this is not an official public SDK).  
6. Global IDs are base64 Relay IDs (`FighterNode:14302` → `RmlnaHRlck5vZGU6MTQzMDI=`).
