"""
Full prop market catalog from FightOdds (bookie-style playables).

Flattens fightPropOfferTable into selectable plays:
  e.g. "Over 2.5 rounds", "Makhachev wins by submission", "Fight ends by KO…"

Supports:
  - best price across all books
  - filter by sportsbook (FanDuel, DraftKings, BetMGM, …)
  - popular shortlist + text search over full catalog
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .client import FightOddsClient, FightOddsError
from .odds_math import format_american

# Prefer showing these first in UI / Discord (common sportsbook props).
POPULAR_OFFER_TYPES = [
    "STRAIGHT",
    "DRAW",
    "OVERUNDER_0.5",
    "OVERUNDER_1.5",
    "OVERUNDER_2.5",
    "OVERUNDER_3.5",
    "OVERUNDER_4.5",
    "DISTANCE",
    "END_KO",
    "END_SUB",
    "END_SD",
    "END_1",
    "END_2",
    "END_3",
    "END_4",
    "END_5",
    "START_2",
    "START_3",
    "ID",
    "KO",
    "SUB",
    "DEC",
    "UD",
    "SD",
    "R_1",
    "R_2",
    "R_3",
    "R_4",
    "R_5",
    "KO_1",
    "KO_2",
    "KO_3",
    "SUB_1",
    "SUB_2",
    "SUB_3",
    "KO_1_2",
    "KO_2_3",
    "KO_3_4",
    "KO_4_5",
    "KO_1_2_3",
    "KO_2_3_4",
    "KO_3_4_5",
    "SUB_1_2",
    "SUB_2_3",
    "SUB_3_4",
    "SUB_4_5",
    "SUB_1_2_3",
    "SUB_2_3_4",
    "SUB_3_4_5",
    "R_1_2",
    "R_2_3",
    "R_3_4",
    "R_4_5",
    "R_1_2_3",
    "R_3_4_5",
    "R_OVER_1.5",
    "R_OVER_2.5",
    "R_UNDER_1.5",
    "R_UNDER_2.5",
    "KO_DEC",
    "KO_SUB",
    "SUB_DEC",
]

CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("moneyline", re.compile(r"^STRAIGHT")),
    ("totals", re.compile(r"^OVERUNDER_|R_OVER_|R_UNDER_")),
    ("distance", re.compile(r"^DISTANCE|^START_")),
    ("method_fight", re.compile(r"^END_|^DRAW$")),
    ("method_fighter", re.compile(r"^(KO|SUB|DEC|UD|SD|ID|KO_DEC|KO_SUB|SUB_DEC)$")),
    # Method+round (SUB_2, KO_2_3, …) before bare round-winner (R_2).
    ("round_method", re.compile(r"^(KO|SUB)_\d")),
    ("round_fighter", re.compile(r"^R_\d")),
    ("other", re.compile(r".*")),
]

# Dropped from search queries so "submission in round 2 or 3" matches
# FightOdds labels like "wins in round 2-3 - Submission".
_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "by",
        "for",
        "in",
        "of",
        "or",
        "the",
        "to",
        "vs",
        "with",
    }
)


def _search_blob(text: str) -> str:
    """Normalize text for prop search (hyphen/slash/'or'/comma round ranges)."""
    s = (text or "").lower()
    # Collapse round lists: "3 or 4 or 5", "3,4,5", "3/4/5", "3_4_5" → "3-4-5"
    for _ in range(6):
        nxt = re.sub(r"(\d)\s*(?:or|/|,|–—-|_)\s*(\d)", r"\1-\2", s)
        if nxt == s:
            break
        s = nxt
    return s


def _search_tokens(query: str) -> list[str]:
    q = _search_blob(query.strip())
    tokens = [t for t in re.split(r"\s+", q) if t and t not in _SEARCH_STOPWORDS]
    # Common shorthand → label wording (FightOdds uses "round", users type "rd")
    out: list[str] = []
    for t in tokens:
        if t in {"rd", "rds", "rnd", "rnds", "rounds"}:
            out.append("round")
        else:
            out.append(t)
    return out


def category_for(offer_type_id: str) -> str:
    for name, pat in CATEGORY_RULES:
        if pat.search(offer_type_id or ""):
            return name
    return "other"


@dataclass
class Play:
    """A single selectable bet outcome (one side of a market)."""

    id: str
    label: str
    offer_type_id: str
    side: int  # 1 or 2
    american: int | None
    category: str
    fight_slug: str
    prop_name_pair: tuple[str, str]
    books: dict[str, int] = field(default_factory=dict)  # shortName -> american
    popular: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "offer_type_id": self.offer_type_id,
            "side": self.side,
            "american": self.american,
            "formatted": format_american(self.american),
            "category": self.category,
            "fight_slug": self.fight_slug,
            "books": self.books,
            "popular": self.popular,
            "book_count": len(self.books),
        }


@dataclass
class PropCatalog:
    fight_slug: str
    fight_label: str
    event_name: str
    plays: list[Play]
    sportsbooks: list[dict[str, str]]
    fetched_at: float
    with_books: bool = True  # False = best-odds only (fast path)

    def filter(
        self,
        *,
        sportsbook: str | None = None,
        query: str | None = None,
        popular_only: bool = False,
        category: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Play]:
        rows = self.plays

        if sportsbook:
            sb = sportsbook.strip().lower()
            filtered: list[Play] = []
            for p in rows:
                price = None
                for name, odd in p.books.items():
                    if name.lower() == sb:
                        price = odd
                        break
                if price is None:
                    # fuzzy match shortName contains
                    for name, odd in p.books.items():
                        if sb in name.lower() or name.lower() in sb:
                            price = odd
                            break
                if price is None:
                    continue
                # clone with book-specific price
                filtered.append(
                    Play(
                        id=p.id,
                        label=p.label,
                        offer_type_id=p.offer_type_id,
                        side=p.side,
                        american=price,
                        category=p.category,
                        fight_slug=p.fight_slug,
                        prop_name_pair=p.prop_name_pair,
                        books={k: v for k, v in p.books.items() if k.lower() == sb or sb in k.lower()},
                        popular=p.popular,
                    )
                )
            rows = filtered

        if popular_only:
            rows = [p for p in rows if p.popular]

        if category:
            cat = category.lower()
            rows = [p for p in rows if p.category == cat]

        if query:
            tokens = _search_tokens(query)
            def match(p: Play) -> bool:
                if not tokens:
                    return True
                blob = _search_blob(f"{p.label} {p.offer_type_id} {p.category}")
                return all(t in blob for t in tokens)
            rows = [p for p in rows if match(p)]

        # Moneyline first, then popular, then label
        cat_rank = {
            "moneyline": 0,
            "totals": 1,
            "distance": 2,
            "method_fight": 3,
            "method_fighter": 4,
            "round_fighter": 5,
            "round_method": 6,
            "other": 9,
        }

        def _rank(p: Play) -> tuple:
            ml = 0 if (
                p.category == "moneyline"
                or (p.offer_type_id or "").upper() == "STRAIGHT"
            ) else 1
            return (
                ml,
                0 if p.popular else 1,
                cat_rank.get(p.category, 8),
                p.side if p.category == "moneyline" else 0,
                p.label.lower(),
            )

        rows = sorted(rows, key=_rank)

        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def get(self, play_id: str, sportsbook: str | None = None) -> Play | None:
        for p in self.plays:
            if p.id == play_id:
                if not sportsbook:
                    return p
                hit = self.filter(sportsbook=sportsbook, limit=None)
                for h in hit:
                    if h.id == play_id:
                        return h
                return None
        return None


class PropCatalogService:
    """Fetch + cache full prop play catalogs per fight."""

    def __init__(
        self,
        client: FightOddsClient | None = None,
        ttl: float = 300.0,
        page_size: int = 100,
    ) -> None:
        self.client = client or FightOddsClient()
        self.ttl = ttl  # re-fetch props after this (seconds)
        self.page_size = page_size
        self._cache: dict[str, PropCatalog] = {}
        self._sb_cache: list[dict[str, str]] | None = None
        self._sb_at: float = 0.0
        self._fight_meta: dict[str, tuple[str, str]] = {}  # slug -> (label, event)
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def _lock_for(self, slug: str) -> threading.Lock:
        with self._global_lock:
            if slug not in self._locks:
                self._locks[slug] = threading.Lock()
            return self._locks[slug]

    def sportsbooks(self) -> list[dict[str, str]]:
        now = time.time()
        if self._sb_cache is not None and (now - self._sb_at) < 600:
            return self._sb_cache
        data = self.client.gql(
            """
            {
              allSportsbooks(first: 80, hasOdds: true) {
                edges {
                  node {
                    shortName fullName slug
                    isDisabled isHidden
                  }
                }
              }
            }
            """
        )
        rows = []
        for e in (data.get("allSportsbooks") or {}).get("edges") or []:
            n = e["node"]
            if n.get("isDisabled") or n.get("isHidden"):
                continue
            rows.append(
                {
                    "shortName": n["shortName"],
                    "fullName": n["fullName"],
                    "slug": n["slug"],
                }
            )
        # Prefer common US books first
        preferred = [
            "FanDuel",
            "DraftKings",
            "BetMGM",
            "Caesars",
            "BetRivers",
            "HardRockBet",
            "Pinnacle",
            "BetOnline",
            "Bovada",
            "Stake",
            "Betway",
        ]
        order = {name: i for i, name in enumerate(preferred)}
        rows.sort(key=lambda r: (order.get(r["shortName"], 100), r["shortName"]))
        self._sb_cache = rows
        self._sb_at = now
        return rows

    def get_catalog(
        self,
        fight_slug: str,
        *,
        force: bool = False,
        with_books: bool = True,
    ) -> PropCatalog:
        """
        with_books=False → bestOdds only (faster first paint).
        with_books=True  → per-book lines (needed for bookie filter).
        """
        now = time.time()
        cached = self._cache.get(fight_slug)
        if (
            cached
            and not force
            and (now - cached.fetched_at) < self.ttl
            and (cached.with_books or not with_books)
        ):
            return cached

        with self._lock_for(fight_slug):
            now = time.time()
            cached = self._cache.get(fight_slug)
            if (
                cached
                and not force
                and (now - cached.fetched_at) < self.ttl
                and (cached.with_books or not with_books)
            ):
                return cached
            # Prefer upgrading a best-only catalog when books requested
            return self._build_catalog(fight_slug, now, with_books=with_books)

    def prefetch(self, fight_slug: str) -> None:
        """Warm best-odds cache in background (keeps first click snappy)."""
        try:
            self.get_catalog(fight_slug, with_books=False)
        except Exception:
            pass

    def _build_catalog(
        self, fight_slug: str, now: float, *, with_books: bool
    ) -> PropCatalog:
        label = ""
        event_name = ""
        meta = self._fight_meta.get(fight_slug)
        if meta:
            label, event_name = meta
        else:
            try:
                fight = self.client.fight_by_slug(fight_slug)
                label = fight.label()
                event_name = fight.event_name
                self._fight_meta[fight_slug] = (label, event_name)
            except FightOddsError:
                label = fight_slug.replace("-", " ")

        raw_props = self._fetch_all_prop_nodes(fight_slug, with_books=with_books)
        plays = self._flatten(fight_slug, raw_props)
        books_on_card: dict[str, str] = {}
        for p in plays:
            for b in p.books:
                books_on_card[b] = b

        preferred_names = [
            "FanDuel",
            "DraftKings",
            "BetMGM",
            "Caesars",
            "BetRivers",
            "HardRockBet",
            "Pinnacle",
            "BetOnline",
            "Bovada",
            "Stake",
            "Betway",
        ]
        ordered: list[dict[str, str]] = []
        seen: set[str] = set()
        global_sbs = None

        def _full_name(short: str) -> str:
            nonlocal global_sbs
            if global_sbs is None:
                try:
                    global_sbs = {s["shortName"]: s for s in self.sportsbooks()}
                except Exception:
                    global_sbs = {}
            hit = global_sbs.get(short)
            return (hit or {}).get("fullName") or short

        if with_books and books_on_card:
            for name in preferred_names:
                if name in books_on_card:
                    ordered.append(
                        {
                            "shortName": name,
                            "fullName": _full_name(name),
                            "slug": name.lower(),
                        }
                    )
                    seen.add(name)
            for b in sorted(books_on_card):
                if b not in seen:
                    ordered.append(
                        {
                            "shortName": b,
                            "fullName": _full_name(b),
                            "slug": b.lower(),
                        }
                    )
        else:
            # Prefer static shortlist while best-only (no book scrape yet)
            try:
                for s in self.sportsbooks()[:20]:
                    ordered.append(s)
            except Exception:
                ordered = [
                    {"shortName": n, "fullName": n, "slug": n.lower()}
                    for n in preferred_names
                ]

        catalog = PropCatalog(
            fight_slug=fight_slug,
            fight_label=label,
            event_name=event_name,
            plays=plays,
            sportsbooks=ordered,
            fetched_at=now,
            with_books=with_books,
        )
        self._cache[fight_slug] = catalog
        return catalog

    def remember_fight(self, slug: str, label: str, event_name: str = "") -> None:
        self._fight_meta[slug] = (label, event_name)

    def _fetch_all_prop_nodes(
        self, slug: str, *, with_books: bool = True
    ) -> list[dict]:
        """
        Paginate fightPropOfferTable.
        best-only mode skips nested offers → ~2× faster.
        """
        nodes: list[dict] = []
        cursor: str | None = None
        n = max(40, min(int(self.page_size), 100))
        if with_books:
            node_fields = """
              offerType { offerTypeId description notDescription }
              propName1 propName2
              bestOdds1 bestOdds2
              offers {
                edges {
                  node {
                    sportsbook { shortName }
                    outcome1 { odds }
                    outcome2 { odds }
                  }
                }
              }
            """
        else:
            node_fields = """
              offerType { offerTypeId description notDescription }
              propName1 propName2
              bestOdds1 bestOdds2
            """

        for _ in range(20):
            if cursor:
                query = f"""
                query($slug: String!, $cursor: String, $n: Int!) {{
                  fightPropOfferTable(slug: $slug) {{
                    propOffers(first: $n, after: $cursor) {{
                      pageInfo {{ hasNextPage endCursor }}
                      edges {{ node {{ {node_fields} }} }}
                    }}
                  }}
                }}
                """
                variables: dict[str, Any] = {"slug": slug, "cursor": cursor, "n": n}
            else:
                query = f"""
                query($slug: String!, $n: Int!) {{
                  fightPropOfferTable(slug: $slug) {{
                    propOffers(first: $n) {{
                      pageInfo {{ hasNextPage endCursor }}
                      edges {{ node {{ {node_fields} }} }}
                    }}
                  }}
                }}
                """
                variables = {"slug": slug, "n": n}

            data = self.client.gql(query, variables)
            table = data.get("fightPropOfferTable") or {}
            conn = table.get("propOffers") or {}
            for e in conn.get("edges") or []:
                nodes.append(e["node"])
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:
                break
        return nodes

    def _flatten(self, fight_slug: str, raw_props: list[dict]) -> list[Play]:
        plays: list[Play] = []
        popular_set = set(POPULAR_OFFER_TYPES)

        for prop in raw_props:
            ot = prop.get("offerType") or {}
            ot_id = ot.get("offerTypeId") or "UNKNOWN"
            name1 = (prop.get("propName1") or ot.get("description") or ot_id).strip()
            name2 = (
                prop.get("propName2")
                or ot.get("notDescription")
                or f"Not: {name1}"
            ).strip()
            cat = category_for(ot_id)
            is_pop = ot_id in popular_set

            books1: dict[str, int] = {}
            books2: dict[str, int] = {}
            for edge in (prop.get("offers") or {}).get("edges") or []:
                node = edge.get("node") or {}
                sb = (node.get("sportsbook") or {}).get("shortName")
                if not sb:
                    continue
                o1 = node.get("outcome1") or {}
                o2 = node.get("outcome2") or {}
                if o1.get("odds") is not None:
                    books1[sb] = int(o1["odds"])
                if o2.get("odds") is not None:
                    books2[sb] = int(o2["odds"])

            best1 = prop.get("bestOdds1")
            best2 = prop.get("bestOdds2")
            # Treat 0 as "no price" (FightOdds uses 0 often)
            if best1 is not None and int(best1) == 0 and books1:
                best1 = max(books1.values())  # best for bettor ~ highest american
            if best2 is not None and int(best2) == 0 and books2:
                best2 = max(books2.values())
            if best1 is not None and int(best1) == 0:
                best1 = None
            if best2 is not None and int(best2) == 0:
                best2 = None
            if best1 is None and books1:
                best1 = max(books1.values())
            if best2 is None and books2:
                best2 = max(books2.values())

            def _is_yes_side(label: str) -> bool:
                low = label.lower()
                return not any(
                    x in low
                    for x in (
                        "doesn't",
                        "does not",
                        "any other",
                        "not win",
                        "or under",
                        "or over",
                    )
                )

            # Side 1
            if name1 and (best1 is not None or books1):
                plays.append(
                    Play(
                        id=f"{ot_id}:1:{_slugify(name1)}",
                        label=name1,
                        offer_type_id=ot_id,
                        side=1,
                        american=int(best1) if best1 is not None else None,
                        category=cat,
                        fight_slug=fight_slug,
                        prop_name_pair=(name1, name2),
                        books=books1,
                        popular=is_pop and _is_yes_side(name1),
                    )
                )
            # Side 2 — unders / "doesn't" when books post them
            if name2 and (best2 is not None or books2):
                # Unders & distance "ends inside" stay popular
                pop2 = is_pop and (
                    name2.lower().startswith("under ")
                    or "inside distance" in name2.lower()
                    or ot_id == "STRAIGHT"
                    or (_is_yes_side(name2) and ot_id.startswith("OVERUNDER"))
                )
                plays.append(
                    Play(
                        id=f"{ot_id}:2:{_slugify(name2)}",
                        label=name2,
                        offer_type_id=ot_id,
                        side=2,
                        american=int(best2) if best2 is not None else None,
                        category=cat,
                        fight_slug=fight_slug,
                        prop_name_pair=(name1, name2),
                        books=books2,
                        popular=pop2,
                    )
                )

        # Deduplicate by id keeping better american if clash
        by_id: dict[str, Play] = {}
        for p in plays:
            if p.american is None and not p.books:
                continue
            prev = by_id.get(p.id)
            if prev is None:
                by_id[p.id] = p
                continue
            # merge books
            merged_books = {**prev.books, **p.books}
            best = p.american if p.american is not None else prev.american
            if prev.american is not None and p.american is not None:
                best = max(prev.american, p.american)
            by_id[p.id] = Play(
                id=p.id,
                label=p.label,
                offer_type_id=p.offer_type_id,
                side=p.side,
                american=best,
                category=p.category,
                fight_slug=p.fight_slug,
                prop_name_pair=p.prop_name_pair,
                books=merged_books,
                popular=prev.popular or p.popular,
            )
        return list(by_id.values())


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return s[:80] or "play"


# process-wide default for web/bot
_default_service: PropCatalogService | None = None


def get_prop_service() -> PropCatalogService:
    global _default_service
    if _default_service is None:
        _default_service = PropCatalogService()
    return _default_service
