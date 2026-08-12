"""
Unified UFC event/card fetch for the Discord bot.

Prefer FightOdds.io for full cards — works for upcoming *and* recent events.
Fall back to ESPN when FightOdds misses a matchup.

Week matching is careful: "Week 1" must never resolve to "Week 10"
(substring trap that used to return wrong Contender Series cards).
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
import unicodedata
from typing import Any, Optional

import espn

log = logging.getLogger("ufc-bet-bot.card_data")

# Fight card entry: (fighter_a, fighter_b, fightodds_slug|None)
FightCardEntry = tuple[str, str, Optional[str]]

# In-memory card cache: event_key -> (fetched_at, fights)
_fight_cache: dict[str, tuple[float, list[FightCardEntry]]] = {}
_CACHE_TTL_SEC = 30 * 60


def fight_corners(fight: tuple) -> tuple[str, str]:
    """Return (fighter_a, fighter_b) from a 2- or 3-tuple card entry."""
    return fight[0], fight[1]


def fight_slug(fight: tuple) -> Optional[str]:
    """FightOdds slug when present (3rd element)."""
    return fight[2] if len(fight) > 2 else None


def _fold(text: str) -> str:
    """Strip accents so 'Medić' matches FightOdds 'Medic'."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c))


def _norm(text: str) -> str:
    text = _fold(text).lower()
    text = text.replace("vs.", "vs").replace(" v ", " vs ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _token_set(text: str) -> set[str]:
    return {t for t in _norm(text).split() if len(t) > 1}


def _extract_week(name: str) -> Optional[int]:
    """Contender-style week number; word-boundary so Week 1 ≠ Week 10."""
    m = re.search(r"\bweek\s*(\d+)\b", _fold(name), flags=re.I)
    return int(m.group(1)) if m else None


def _extract_year_hint(name: str) -> Optional[int]:
    m = re.search(r"\b(20\d{2})\b", name or "")
    if m:
        return int(m.group(1))
    # ESPN: Season 10 Contender ≈ 2026 (S1 ≈ 2017)
    m = re.search(r"\bseason\s*(\d+)\b", _fold(name), flags=re.I)
    if m:
        season = int(m.group(1))
        if 1 <= season <= 20:
            return 2016 + season
    return None


def _event_match_score(query: str, candidate: str) -> float:
    """Higher is better. Mismatched Contender week numbers score 0."""
    q, c = _norm(query), _norm(candidate)
    if not q or not c:
        return 0.0

    qw, cw = _extract_week(query), _extract_week(candidate)
    if qw is not None and cw is not None and qw != cw:
        return 0.0  # Week 1 must not match Week 10

    # Avoid "week 1" in "week 10" substring false positives on raw names
    q_safe = re.sub(r"\bweek\s*\d+\b", f"weeknum{qw if qw is not None else 'x'}", q)
    c_safe = re.sub(r"\bweek\s*\d+\b", f"weeknum{cw if cw is not None else 'x'}", c)

    if q_safe == c_safe:
        score = 100.0
    elif q_safe in c_safe or c_safe in q_safe:
        score = 80.0 + min(len(q_safe), len(c_safe)) / max(len(q_safe), len(c_safe)) * 10
    else:
        qt, ct = _token_set(query), _token_set(candidate)
        noise = {
            "ufc", "fight", "night", "vs", "mma", "ppv", "espn",
            "dana", "white", "s", "series", "contender", "season",
        }
        qt2, ct2 = qt - noise, ct - noise
        if not qt2 or not ct2:
            qt2, ct2 = qt, ct
        inter = qt2 & ct2
        if not inter:
            return 0.0
        score = 40.0 * (len(inter) / max(len(qt2), 1)) + 20.0 * (len(inter) / max(len(ct2), 1))

    if qw is not None and cw is not None and qw == cw:
        score += 45.0

    qy, cy = _extract_year_hint(query), _extract_year_hint(candidate)
    if qy and cy:
        if qy == cy:
            score += 30.0
        else:
            score -= 50.0

    return score


def _best_event_name(query: str, candidates: list[str], min_score: float = 25.0) -> Optional[str]:
    best_name, best = None, 0.0
    for name in candidates:
        score = _event_match_score(query, name)
        if score > best:
            best, best_name = score, name
    return best_name if best >= min_score else None


def _fights_from_nodes(fights: list) -> list[FightCardEntry]:
    out: list[FightCardEntry] = []
    for f in fights:
        if getattr(f, "is_cancelled", False):
            continue
        a = (f.fighter1_name or "").strip()
        b = (f.fighter2_name or "").strip()
        slug = (getattr(f, "slug", None) or "").strip() or None
        if a and b:
            out.append((a, b, slug))
    return out


def _with_null_slugs(fights: list[tuple[str, str]]) -> list[FightCardEntry]:
    return [(a, b, None) for a, b in fights]


def _is_contender_query(name: str) -> bool:
    n = _norm(name)
    return "contender" in n or "dwcs" in n


def _fightodds_fights_by_pk_sync(pk: int) -> list[FightCardEntry]:
    try:
        from client import FightOddsClient
    except Exception:
        log.exception("FightOdds client unavailable")
        return []
    client = FightOddsClient(timeout=25.0)
    try:
        fights = client.event_fights_by_pk(int(pk))
    except Exception:
        log.exception("event_fights_by_pk(%s) failed", pk)
        return []
    return _fights_from_nodes(fights)


def _fightodds_fights_sync(event_name: str) -> list[FightCardEntry]:
    try:
        from client import FightOddsClient
    except Exception:
        log.exception("FightOdds client unavailable")
        return []

    client = FightOddsClient(timeout=25.0)
    candidates: list = []
    seen_pk: set[int] = set()

    def _add(events) -> None:
        for e in events:
            if e.pk in seen_pk:
                continue
            seen_pk.add(e.pk)
            candidates.append(e)

    week = _extract_week(event_name)
    year = _extract_year_hint(event_name) or datetime.date.today().year
    folded = _fold(event_name)
    contender = _is_contender_query(event_name)

    probes: list[str] = []
    if contender and week is not None:
        # Exact FightOdds title e.g. "Contender Series 2026: Week 1" (pk 9113)
        probes.append(f"Contender Series {year}: Week {week}")
        probes.append(f"Contender Series {year}")
    probes.extend(
        [
            folded,
            event_name,
            "Contender Series" if contender else "",
        ]
    )
    if ":" in folded and not contender:
        probes.append(folded.split(":", 1)[1].strip())
        m = re.split(r"\bvs\.?\b", folded.split(":", 1)[1], maxsplit=1, flags=re.I)
        if m and m[0].strip():
            probes.append(m[0].strip())

    for probe in probes:
        if not probe:
            continue
        try:
            _add(client.search_events(probe, first=25))
        except Exception:
            log.exception("FightOdds search_events failed for %r", probe)

    try:
        _add(client.ufc_upcoming(first=40))
    except Exception:
        pass

    # Contender Series: only keep same week / Contender candidates. Never
    # allow Bellator/PFL/etc. or "Week 10" to win on a "Week 1" query.
    if contender:
        filtered = []
        for e in candidates:
            n = _norm(e.name)
            if "contender" not in n and "dwcs" not in n:
                continue
            cw = _extract_week(e.name)
            if week is not None and cw is not None and cw != week:
                continue
            filtered.append(e)
        candidates = filtered

    best_e, best_score = None, 0.0
    for e in candidates:
        score = _event_match_score(event_name, e.name)
        # Exact Contender Series YYYY: Week N bonus
        if contender and week is not None:
            ideal = f"contender series {year} week {week}"
            if ideal in _norm(e.name):
                score += 80.0
        if e.date:
            try:
                d = datetime.datetime.strptime(e.date[:10], "%Y-%m-%d").date()
                age = abs((d - datetime.date.today()).days)
                if age <= 30:
                    score += 12.0
                elif age > 400:
                    score -= 40.0
            except ValueError:
                pass
        if score > best_score:
            best_score, best_e = score, e

    if best_e is None or best_score < 35:
        log.warning(
            "No good FightOdds match for %r (best=%r score=%.1f among %d)",
            event_name,
            getattr(best_e, "name", None),
            best_score,
            len(candidates),
        )
        return []

    log.debug(
        "FightOdds matched %r → pk=%s %r (score=%.1f)",
        event_name,
        best_e.pk,
        best_e.name,
        best_score,
    )
    try:
        fights = client.event_fights_by_pk(best_e.pk)
    except Exception:
        log.exception("event_fights_by_pk(%s) failed", best_e.pk)
        return []
    return _fights_from_nodes(fights)


async def _espn_fights_fuzzy(event_name: str) -> list[FightCardEntry]:
    """ESPN scoreboard — past + future window, fuzzy event name match."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=90)
    end = today + datetime.timedelta(days=120)
    dates_param = f"{start:%Y%m%d}-{end:%Y%m%d}"

    try:
        import aiohttp
        from config import ESPN_SCOREBOARD_URLS

        url = ESPN_SCOREBOARD_URLS["ufc"]
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"dates": dates_param}, timeout=20) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except Exception:
        try:
            return _with_null_slugs(await espn.fetch_fights_for_event(event_name))
        except Exception:
            return []

    candidates: list[tuple[str, dict]] = []
    for ev in data.get("events", []):
        name = ev.get("name") or ev.get("shortName") or ""
        if name:
            candidates.append((name, ev))

    match_name = _best_event_name(event_name, [n for n, _ in candidates])
    if not match_name:
        return []

    matching = next(ev for n, ev in candidates if n == match_name)
    fights: list[FightCardEntry] = []
    for comp in matching.get("competitions", []):
        competitors = comp.get("competitors", [])
        if len(competitors) != 2:
            continue
        ordered = sorted(competitors, key=lambda c: c.get("order", 0))
        name_a = (ordered[0].get("athlete") or {}).get("displayName") or "Fighter A"
        name_b = (ordered[1].get("athlete") or {}).get("displayName") or "Fighter B"
        # skip TBA placeholders
        if "tba" in name_a.lower() or "tba" in name_b.lower():
            continue
        fights.append((name_a, name_b, None))
    fights.reverse()
    return fights


async def fetch_fights_for_event(
    event_name: str,
    *,
    use_cache: bool = True,
    event_pk: int | str | None = None,
) -> list[FightCardEntry]:
    """Return [(fighter_a, fighter_b, fightodds_slug|None), ...] for an event.

    Prefer `event_pk` (FightOdds id) when known from the upcoming-events cache —
    that avoids name ambiguity (Contender Week 1 vs Week 10, Bellator, etc.).
    """
    if not event_name and not event_pk:
        return []

    key = f"pk:{event_pk}" if event_pk else _norm(event_name)
    now = datetime.datetime.utcnow().timestamp()
    if use_cache and key in _fight_cache:
        ts, cached = _fight_cache[key]
        if now - ts < _CACHE_TTL_SEC and cached:
            return list(cached)

    fights: list[FightCardEntry] = []
    if event_pk is not None and str(event_pk).isdigit():
        try:
            fights = await asyncio.to_thread(_fightodds_fights_by_pk_sync, int(event_pk))
        except Exception:
            log.exception("FightOdds pk fetch failed for pk=%s", event_pk)

    if not fights and event_name:
        try:
            fights = await asyncio.to_thread(_fightodds_fights_sync, event_name)
        except Exception:
            log.exception("FightOdds thread failed for %r", event_name)

    if not fights and event_name:
        try:
            fights = await _espn_fights_fuzzy(event_name)
        except Exception:
            log.exception("ESPN fuzzy fights failed for %r", event_name)

    if fights:
        _fight_cache[key] = (now, fights)
        if event_name:
            _fight_cache[_norm(event_name)] = (now, fights)
        log.debug("Card for %r → %d fights", event_name or f"pk={event_pk}", len(fights))
    else:
        log.warning("No fight card found for event %r", event_name or f"pk={event_pk}")
    return fights


def _fightodds_upcoming_sync(limit: int = 15) -> list[dict[str, Any]]:
    try:
        from client import FightOddsClient
    except Exception:
        return []

    client = FightOddsClient(timeout=25.0)
    events = []
    try:
        events = client.ufc_upcoming(first=max(limit, 20))
    except Exception:
        log.exception("FightOdds ufc_upcoming failed")
        return []

    # Also surface Contender Series weeks near-term (often missing from ufc_upcoming)
    try:
        for e in client.search_events("Contender Series 2026", first=15):
            if not any(x.pk == e.pk for x in events):
                events.append(e)
    except Exception:
        pass

    out: list[dict[str, Any]] = []
    for e in events:
        dt = datetime.datetime.utcnow()
        if e.date:
            raw = e.date
            try:
                if "T" in raw:
                    dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(
                        tzinfo=None
                    )
                else:
                    dt = datetime.datetime.strptime(raw[:10], "%Y-%m-%d")
            except ValueError:
                pass

        # Prefer FightOdds Contender name when both exist later in merge
        out.append(
            {
                "id": str(e.pk),
                "name": e.name,
                "short_name": e.name,
                "date": dt,
                "is_live": False,
                "source": "fightodds",
            }
        )

    out.sort(key=lambda e: e["date"] or datetime.datetime.max)
    # Drop finished cards (FightOdds Contender search includes older weeks)
    out = [e for e in out if is_event_upcoming(e)]
    return out[:limit]


def is_event_upcoming(ev: dict[str, Any], *, today: Optional[datetime.date] = None) -> bool:
    """True for live events and cards scheduled today or later (not yet finished)."""
    if ev.get("is_live"):
        return True
    d = ev.get("date")
    if not isinstance(d, datetime.datetime):
        # Unknown date — keep so we don't hide a brand-new card
        return True
    day = d.date() if d.tzinfo is None else d.astimezone(datetime.timezone.utc).date()
    return day >= (today or datetime.date.today())


def filter_upcoming_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only not-yet-finished events, soonest first."""
    kept = [e for e in events if is_event_upcoming(e)]

    def _sort_key(e: dict[str, Any]) -> datetime.datetime:
        d = e.get("date")
        if isinstance(d, datetime.datetime):
            return d.replace(tzinfo=None) if d.tzinfo else d
        return datetime.datetime.max

    kept.sort(key=_sort_key)
    return kept


def match_event_in_list(
    query: str, events: list[dict[str, Any]], *, min_score: float = 70.0
) -> Optional[dict[str, Any]]:
    """Best matching upcoming event for a typed name, or None."""
    if not query or not events:
        return None
    best: Optional[dict[str, Any]] = None
    best_score = 0.0
    for ev in events:
        for key in ("short_name", "name"):
            name = ev.get(key) or ""
            if not name:
                continue
            if name == query:
                return ev
            score = _event_match_score(query, name)
            if score > best_score:
                best_score, best = score, ev
    return best if best is not None and best_score >= min_score else None


async def fetch_upcoming_events(limit: int = 12) -> list[dict[str, Any]]:
    """Merge FightOdds + ESPN upcoming, de-dupe by fuzzy name, soonest first.

    Past/completed events are never returned — used by /bet-ufc autocomplete.
    """
    fo: list[dict[str, Any]] = []
    es: list[dict[str, Any]] = []
    try:
        fo = await asyncio.to_thread(_fightodds_upcoming_sync, max(limit, 20))
    except Exception:
        log.exception("FightOdds upcoming merge failed")
    try:
        es = await espn.fetch_upcoming_events("ufc", limit=max(limit, 15))
        for e in es:
            e.setdefault("source", "espn")
            d = e.get("date")
            if isinstance(d, datetime.datetime) and d.tzinfo is not None:
                e["date"] = d.replace(tzinfo=None)
    except Exception:
        log.exception("ESPN upcoming merge failed")

    merged: list[dict[str, Any]] = []
    for src in (fo, es):  # FightOdds first so Contender uses official FO names/pks
        for ev in src:
            if not is_event_upcoming(ev):
                continue
            name = ev.get("name") or ""
            dup = next(
                (m for m in merged if _event_match_score(name, m["name"]) >= 70), None
            )
            if dup is not None:
                # Never lose a true is_live flag just because of merge
                # order -- FightOdds entries always hardcode is_live=False,
                # so if it's processed first and "wins" the dedup, ESPN's
                # correct is_live=True must still carry over, or an
                # actively-live event can silently drop off the upcoming
                # list once its calendar date rolls past "today".
                if ev.get("is_live") and not dup.get("is_live"):
                    dup["is_live"] = True
                continue
            merged.append(ev)

    return filter_upcoming_events(merged)[:limit]


def match_fighter_on_card(
    name: str, fights: list
) -> Optional[tuple[str, str, str]]:
    """If `name` matches a corner on the card, return (fighter, opponent, fight_label)."""
    if not name or not fights:
        return None
    q = name.strip().lower()
    q_tokens = [t for t in re.split(r"[^a-z0-9]+", q) if t]
    best = None
    best_score = 0
    for fight in fights:
        a, b = fight_corners(fight)
        for me, them in ((a, b), (b, a)):
            ml = me.lower()
            m_tokens = [t for t in re.split(r"[^a-z0-9]+", ml) if t]
            score = 0
            if q == ml:
                score = 100
            elif q in ml or ml in q:
                score = 70
            else:
                inter = set(q_tokens) & set(m_tokens)
                if inter:
                    score = 30 + 10 * len(inter)
                # Single-token last-name picks: "Wint", "Hasan", "Sola"
                if len(q_tokens) == 1 and m_tokens and q_tokens[0] == m_tokens[-1] and len(q_tokens[0]) >= 3:
                    score = max(score, 55)
            if score > best_score:
                best_score = score
                best = (me, them, f"{me} vs {them}")
    return best if best_score >= 30 else None


def resolve_fighter_on_card(
    *,
    fighter_pick: Optional[str],
    description: Optional[str],
    fights: list,
) -> Optional[tuple[str, str, str]]:
    """
    Canonical card match for a leg: prefer fighter_pick if it lands on the card,
    else scrape description free-text. Returns (fighter, opponent, fight_label).
    """
    if not fights:
        return None
    if fighter_pick:
        hit = match_fighter_on_card(fighter_pick, fights)
        if hit:
            return hit
    inferred = infer_fighter_from_text(description or "", fights)
    if inferred:
        return match_fighter_on_card(inferred, fights)
    return None


def infer_fighter_from_text(text: str, fights: list) -> Optional[str]:
    """Pull a card fighter name mentioned in free-text pick text."""
    if not text or not fights:
        return None
    lowered = _fold(text).lower()
    best_name, best_len = None, 0
    for fight in fights:
        a, b = fight_corners(fight)
        for name in (a, b):
            n = _fold(name).lower()
            if n and n in lowered and len(n) > best_len:
                best_name, best_len = name, len(n)
                continue
            last = n.split()[-1] if n.split() else ""
            if len(last) >= 3 and re.search(rf"\b{re.escape(last)}\b", lowered):
                # Prefer longer last-name / full-name hits; full name still wins above.
                if len(last) > best_len:
                    best_name, best_len = name, len(last)
    return best_name