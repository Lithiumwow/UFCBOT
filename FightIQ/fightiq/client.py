"""FightOdds.io GraphQL client."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .models import Event, Fight

log = logging.getLogger(__name__)

GQL_URL = "https://api.fightodds.io/gql"
DEFAULT_UA = "FightIQ/0.1 (+https://fightodds.io/odds)"


class FightOddsError(RuntimeError):
    pass


class FightOddsClient:
    def __init__(
        self,
        url: str = GQL_URL,
        user_agent: str = DEFAULT_UA,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.timeout = timeout

    def gql(self, query: str, variables: dict | None = None) -> dict[str, Any]:
        body = json.dumps(
            {"query": query, "variables": variables or {}},
            separators=(",", ":"),
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
                "Origin": "https://fightodds.io",
                "Referer": "https://fightodds.io/odds",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise FightOddsError(f"HTTP {e.code}: {detail[:500]}") from e
        except urllib.error.URLError as e:
            raise FightOddsError(f"Network error: {e}") from e

        if payload.get("errors"):
            msgs = "; ".join(
                err.get("message", str(err)) for err in payload["errors"]
            )
            # Some endpoints return partial data — only hard-fail if data missing
            if payload.get("data") is None:
                raise FightOddsError(f"GraphQL error: {msgs}")
            log.warning("GraphQL soft errors: %s", msgs)
        return payload.get("data") or {}

    # ---- high-level fetchers -------------------------------------------------

    def _parse_event_node(self, n: dict) -> Event:
        promo = (n.get("promotion") or {}).get("shortName")
        return Event(
            pk=int(n["pk"]),
            name=n["name"],
            date=n.get("date"),
            promotion=promo,
            is_cancelled=bool(n.get("isCancelled")),
        )

    def upcoming_events(self, first: int = 25) -> list[Event]:
        data = self.gql(
            """
            query($n: Int) {
              allEvents(first: $n, upcoming: true) {
                edges {
                  node {
                    pk name date isCancelled
                    promotion { shortName }
                  }
                }
              }
            }
            """,
            {"n": first},
        )
        events = [
            self._parse_event_node(edge["node"])
            for edge in (data.get("allEvents") or {}).get("edges") or []
        ]
        # Soonest first (API often returns far future first)
        events.sort(key=lambda e: e.date or "9999")
        return events

    def search_events(self, name_fragment: str, first: int = 20) -> list[Event]:
        data = self.gql(
            """
            query($q: String, $n: Int) {
              allEvents(first: $n, name_Icontains: $q) {
                edges {
                  node {
                    pk name date isCancelled
                    promotion { shortName }
                  }
                }
              }
            }
            """,
            {"q": name_fragment, "n": first},
        )
        events = [
            self._parse_event_node(edge["node"])
            for edge in (data.get("allEvents") or {}).get("edges") or []
        ]
        events.sort(key=lambda e: e.date or "9999", reverse=True)
        return events

    def ufc_upcoming(self, first: int = 40) -> list[Event]:
        """
        Upcoming-flagged events plus near-term UFC cards found by name search
        (FightOdds sometimes omits imminent cards from upcoming:true).
        """
        by_pk: dict[int, Event] = {}
        for e in self.upcoming_events(first=first):
            if (e.promotion or "").upper() == "UFC" or "UFC" in e.name.upper():
                by_pk[e.pk] = e
        for e in self.search_events("UFC", first=first):
            if (e.promotion or "").upper() != "UFC" and "UFC" not in e.name.upper():
                continue
            if e.is_cancelled:
                continue
            by_pk[e.pk] = e
        events = list(by_pk.values())
        events.sort(key=lambda e: e.date or "9999")
        return events

    def search_fighters(self, query: str, first: int = 15) -> list[dict]:
        parts = query.strip().split()
        if len(parts) >= 2:
            first_name, last_name = parts[0], parts[-1]
            q = """
            query($f: String, $l: String, $n: Int) {
              allFighters(first: $n, firstName_Icontains: $f, lastName_Icontains: $l) {
                edges { node { id firstName lastName slug } }
              }
            }
            """
            variables = {"f": first_name, "l": last_name, "n": first}
        else:
            q = """
            query($l: String, $n: Int) {
              allFighters(first: $n, lastName_Icontains: $l) {
                edges { node { id firstName lastName slug } }
              }
            }
            """
            variables = {"l": query.strip(), "n": first}
        data = self.gql(q, variables)
        return [e["node"] for e in (data.get("allFighters") or {}).get("edges") or []]

    def fights_for_fighter_lastname(self, last_name: str, first: int = 40) -> list[Fight]:
        data = self.gql(
            """
            query($ln: String, $n: Int) {
              f1: allFights(first: $n, fighter1_LastName: $ln) {
                edges { node { ...FightFields } }
              }
              f2: allFights(first: $n, fighter2_LastName: $ln) {
                edges { node { ...FightFields } }
              }
            }
            fragment FightFields on FightNode {
              id pk slug isCancelled isFiveRounds
              event { pk name date }
              fighter1 { id firstName lastName }
              fighter2 { id firstName lastName }
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
            """,
            {"ln": last_name, "n": first},
        )
        fights: list[Fight] = []
        seen: set[str] = set()
        for key in ("f1", "f2"):
            for edge in (data.get(key) or {}).get("edges") or []:
                fight = self._parse_fight(edge["node"])
                if fight.slug in seen:
                    continue
                seen.add(fight.slug)
                fights.append(fight)
        fights.sort(key=lambda f: f.event_date or "", reverse=True)
        return fights

    def fight_by_slug(self, slug: str) -> Fight:
        data = self.gql(
            """
            query($slug: String!) {
              fightBySlug(slug: $slug) {
                id pk slug isCancelled isFiveRounds
                event { pk name date }
                fighter1 { id firstName lastName }
                fighter2 { id firstName lastName }
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
            """,
            {"slug": slug},
        )
        node = data.get("fightBySlug")
        if not node:
            raise FightOddsError(f"Fight not found: {slug}")
        return self._parse_fight(node)

    def prop_offers(self, slug: str, first: int = 100) -> list[dict]:
        """Raw prop market list for a fight (book-level detail)."""
        data = self.gql(
            """
            query($slug: String!, $n: Int) {
              fightPropOfferTable(slug: $slug) {
                propOffers(first: $n) {
                  edges {
                    node {
                      offerType { offerTypeId description category subCategory }
                      propName1 propName2
                      bestOdds1 bestOdds2
                      offers {
                        edges {
                          node {
                            sportsbook { shortName fullName }
                            outcome1 {
                              name odds
                              fighter { firstName lastName }
                            }
                            outcome2 {
                              name odds
                              fighter { firstName lastName }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
            {"slug": slug, "n": first},
        )
        table = data.get("fightPropOfferTable") or {}
        return [
            e["node"]
            for e in (table.get("propOffers") or {}).get("edges") or []
        ]

    def event_fights_by_pk(self, pk: int) -> list[Fight]:
        """Load a fight card directly by FightOdds event pk."""
        return self._fetch_event_fights(pk)

    def event_fights_by_name(self, event_name_fragment: str, first: int = 50) -> list[Fight]:
        """Resolve an event by name/pk fragment, return full fight board with odds."""
        frag = event_name_fragment.strip()
        match: Event | None = None

        if frag.isdigit():
            return self.event_fights_by_pk(int(frag))
        else:
            # Prefer name search (covers imminent cards missing from upcoming:true)
            hits = self.search_events(frag, first=20)
            fl = frag.lower()
            for e in hits:
                if fl in e.name.lower():
                    match = e
                    break
            if match is None:
                for e in self.upcoming_events(first=60):
                    if fl in e.name.lower():
                        match = e
                        break

        if not match:
            return []

        return self._fetch_event_fights(match.pk)

    def _fetch_event_fights(self, pk: int) -> list[Fight]:
        # Lean query: card UI only needs names + ML (method odds come from props).
        data = self.gql(
            """
            query($pk: Int!) {
              eventOfferTable(pk: $pk, allFights: true) {
                name date pk
                fightOffers {
                  edges {
                    node {
                      slug
                      bestOdds1 bestOdds2
                      fighter1 { id firstName lastName }
                      fighter2 { id firstName lastName }
                      fight {
                        id pk slug isCancelled isFiveRounds
                        event { pk name date }
                        fighter1 { id firstName lastName }
                        fighter2 { id firstName lastName }
                        fighter1Odds fighter2Odds
                      }
                    }
                  }
                }
              }
            }
            """,
            {"pk": pk},
        )
        table = data.get("eventOfferTable")
        if not table:
            return []
        fights: list[Fight] = []
        for edge in (table.get("fightOffers") or {}).get("edges") or []:
            node = edge["node"]
            fight_node = node.get("fight")
            if fight_node:
                f = self._parse_fight(fight_node)
                # Prefer board-level best ML when nested fight odds empty
                if f.fighter1_odds is None and node.get("bestOdds1") is not None:
                    f.fighter1_odds = int(node["bestOdds1"]) or None
                if f.fighter2_odds is None and node.get("bestOdds2") is not None:
                    f.fighter2_odds = int(node["bestOdds2"]) or None
                fights.append(f)
            elif node.get("slug"):
                try:
                    fights.append(self.fight_by_slug(node["slug"]))
                except FightOddsError:
                    continue
        return fights

    def _parse_fight(self, n: dict) -> Fight:
        f1 = n["fighter1"] or {}
        f2 = n["fighter2"] or {}
        ev = n.get("event") or {}

        def nm(f: dict) -> str:
            return f"{f.get('firstName', '')} {f.get('lastName', '')}".strip()

        return Fight(
            id=n["id"],
            pk=n.get("pk"),
            slug=n["slug"],
            event_name=ev.get("name") or "",
            event_date=ev.get("date"),
            event_pk=ev.get("pk"),
            fighter1_id=f1.get("id") or "",
            fighter1_name=nm(f1),
            fighter2_id=f2.get("id") or "",
            fighter2_name=nm(f2),
            fighter1_odds=n.get("fighter1Odds"),
            fighter2_odds=n.get("fighter2Odds"),
            fighter1_sub=n.get("fighter1SubOdds"),
            fighter2_sub=n.get("fighter2SubOdds"),
            fighter1_ko=n.get("fighter1KoOdds"),
            fighter2_ko=n.get("fighter2KoOdds"),
            fighter1_dec=n.get("fighter1DecOdds"),
            fighter2_dec=n.get("fighter2DecOdds"),
            fighter1_r1=n.get("fighter1R1Odds"),
            fighter2_r1=n.get("fighter2R1Odds"),
            fighter1_r2=n.get("fighter1R2Odds"),
            fighter2_r2=n.get("fighter2R2Odds"),
            fighter1_r3=n.get("fighter1R3Odds"),
            fighter2_r3=n.get("fighter2R3Odds"),
            fighter1_itd=n.get("fighter1ItdOdds"),
            fighter2_itd=n.get("fighter2ItdOdds"),
            fight_itd=n.get("fightItdOdds"),
            is_cancelled=bool(n.get("isCancelled")),
            is_five_rounds=bool(n.get("isFiveRounds")),
            raw=n,
        )
