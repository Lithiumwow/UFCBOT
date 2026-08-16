"""
Fetches UFC event data from ESPN's public (unofficial, undocumented)
scoreboard endpoint:

    https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard

This is the same JSON endpoint ESPN's own site/app uses. It's not an
official/documented API, has no auth, and can change or rate-limit without
notice -- treat it as best-effort. Passing a `dates=YYYYMMDD-YYYYMMDD` query
param widens the window beyond "this week only", which is what the plain
endpoint tends to return.
"""
from __future__ import annotations

import datetime
import re
from typing import Any, Optional

import aiohttp

from config import ESPN_SCOREBOARD_URLS


async def _fetch_raw_scoreboard(
    sport: str,
    window_days: int = 120,
    *,
    past_days: int = 0,
) -> dict[str, Any]:
    url = ESPN_SCOREBOARD_URLS[sport]
    today = datetime.date.today()
    start = today - datetime.timedelta(days=past_days)
    end = today + datetime.timedelta(days=window_days)
    dates_param = f"{start:%Y%m%d}-{end:%Y%m%d}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"dates": dates_param}, timeout=15) as resp:
            resp.raise_for_status()
            return await resp.json()


async def fetch_upcoming_events(
    sport: str, limit: int = 3, window_days: int = 120
) -> list[dict[str, Any]]:
    """
    Returns up to `limit` upcoming/live events for the given sport
    ("ufc" or "nba") as a list of dicts:
        {"id": str, "name": str, "date": datetime.datetime, "short_name": str,
         "is_live": bool}
    Sorted soonest-first. Only *completed* events are filtered out -- an
    event that has started but hasn't finished ("live") is kept, so it still
    shows up for autocomplete while it's happening.
    """
    # past_days=1: a card can start last night (server-local calendar day)
    # and still be live into today. ESPN's `dates` query param is applied
    # server-side, so without looking back at least one day, a currently-
    # live event that started "yesterday" never even comes back in the
    # response -- it's not a filtering bug on our end, ESPN just never
    # sends it.
    data = await _fetch_raw_scoreboard(sport, window_days, past_days=1)

    events = []
    for ev in data.get("events", []):
        raw_date = ev.get("date")
        if not raw_date:
            continue
        try:
            # ESPN dates look like "2026-08-01T22:00Z"
            event_dt = datetime.datetime.strptime(raw_date, "%Y-%m-%dT%H:%MZ").replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            continue

        status_type = ((ev.get("status") or {}).get("type") or {})
        state = status_type.get("state", "pre")  # "pre" | "in" | "post"
        if state == "post":
            continue  # event's over -- don't offer it for new bets

        # For UFC, ESPN's "name" field includes the main-event matchup
        # (e.g. "UFC Fight Night: Ankalaev vs. Guskov") while "shortName" is
        # generic ("UFC Fight Night"). For NBA "name" is already a full
        # matchup (e.g. "San Antonio Spurs at New York Knicks"). Either way,
        # prefer "name".
        full_name = ev.get("name") or ev.get("shortName") or "Event"

        events.append(
            {
                "id": ev.get("id"),
                "name": full_name,
                "short_name": full_name,
                "date": event_dt,
                "is_live": state == "in",
            }
        )

    events.sort(key=lambda e: e["date"])
    return events[:limit]


async def fetch_fights_for_event(event_name: str, window_days: int = 120) -> list[tuple[str, str]]:
    """
    UFC only. Returns the fight card for one event as a list of
    (fighter_a, fighter_b) tuples, main event first. Matches by exact event
    name. Returns an empty list if the event can't be found or has no
    fight data yet.
    """
    # Same reasoning as fetch_upcoming_events: a card can start last night
    # and still be live today, and ESPN's own date-range query would
    # otherwise exclude it entirely.
    data = await _fetch_raw_scoreboard("ufc", window_days, past_days=1)

    matching = None
    for ev in data.get("events", []):
        name = ev.get("name") or ev.get("shortName")
        if name == event_name:
            matching = ev
            break

    if matching is None:
        return []

    fights: list[tuple[str, str]] = []
    for comp in matching.get("competitions", []):
        competitors = comp.get("competitors", [])
        if len(competitors) != 2:
            continue
        ordered = sorted(competitors, key=lambda c: c.get("order", 0))
        name_a = (ordered[0].get("athlete") or {}).get("displayName") or "Fighter A"
        name_b = (ordered[1].get("athlete") or {}).get("displayName") or "Fighter B"
        fights.append((name_a, name_b))

    fights.reverse()  # ESPN lists main event last -- flip so it's first
    return fights


_METHOD_PATTERNS = [
    # Check the unambiguous, explicit terms first -- official results
    # almost always literally say "Submission" or "Decision" for those
    # methods, so these are safe to match on their own.
    (re.compile(r"\bsub(?:mission)?\b", re.IGNORECASE), "SUB"),
    (re.compile(r"\bdecision\b|\bdec\b", re.IGNORECASE), "DEC"),
    # KO/TKO -- Contender Series uses "Kotko" in details ("Unofficial Winner Kotko").
    (re.compile(
        r"\b(ko/?tko|knockout|technical knockout|kotko|\btko\b|\bko\b)\b",
        re.IGNORECASE,
    ), "KO_TKO"),
]
_ROUND_PATTERNS = [
    # Safest first: explicit "round" word, or ordinal + "round".
    re.compile(r"\bround\s*(\d)\b", re.IGNORECASE),
    re.compile(r"\b(\d)(?:st|nd|rd|th)\s*round\b", re.IGNORECASE),
    # Standard result notation "R2, 3:45" / "R1 0:45" -- requires a
    # following timestamp, so it can't accidentally grab an unrelated
    # "R" + digit elsewhere in the combined text.
    re.compile(r"\bR(\d)\s*[,.]?\s*\d{1,2}:\d{2}\b"),
    # Loose last resort: bare "R2" with nothing else to go on. Checked
    # last and only reached if nothing safer matched, since this is the
    # one most likely to grab an unrelated number from a joined-together
    # blob of several different ESPN text fields.
    re.compile(r"\bR(\d)\b"),
]


def _parse_method_and_round(text_fields: list[str]) -> tuple[Optional[str], Optional[int]]:
    """
    Best-effort text scan across whatever result-description fields ESPN
    gives us for a completed fight. Returns (None, None) if nothing matched.
    """
    combined = " | ".join(t for t in text_fields if t)
    method = None
    for pattern, label in _METHOD_PATTERNS:
        if pattern.search(combined):
            method = label
            break

    round_num = None
    for pattern in _ROUND_PATTERNS:
        m = pattern.search(combined)
        if m:
            try:
                round_num = int(m.group(1))
            except (ValueError, IndexError):
                pass
            break

    return method, round_num


def _method_from_espn_details(details: list | None) -> Optional[str]:
    """ESPN Contender feeds often put method in details as 'Unofficial Winner Kotko'."""
    if not details:
        return None
    texts: list[str] = []
    for d in details:
        if not isinstance(d, dict):
            continue
        t = d.get("type") or {}
        if isinstance(t, dict) and t.get("text"):
            texts.append(str(t["text"]))
        if d.get("text"):
            texts.append(str(d["text"]))
    method, _ = _parse_method_and_round(texts)
    return method



def _elapsed_seconds_in_round(status: dict) -> Optional[float]:
    """Seconds elapsed in the current/final round from ESPN clock fields.

    Contender/UFC finishes typically expose `clock` as elapsed seconds (e.g. 34.0
    with displayClock "0:34" for a R1 finish at 0:34).
    """
    raw = status.get("clock")
    if raw is not None:
        try:
            val = float(raw)
            if val >= 0:
                return val
        except (TypeError, ValueError):
            pass
    disp = status.get("displayClock")
    if isinstance(disp, str) and ":" in disp:
        try:
            mins, secs = disp.strip().split(":", 1)
            return int(mins) * 60 + float(secs)
        except (TypeError, ValueError):
            pass
    return None


async def fetch_fight_results(
    event_name: str, window_days: int = 14, past_days: int = 90
) -> list[dict[str, Any]]:
    """
    UFC only. Best-effort auto-grading data for every fight on a card:
        {"fighter_a": str, "fighter_b": str, "completed": bool,
         "winner": Optional[str], "method": Optional[str] ("KO_TKO"|"SUB"|"DEC"|None),
         "round": Optional[int]}

    Looks back `past_days` and forward `window_days` so completed cards still
    grade. Matches event names fuzzily (Contender naming differences, etc.).
    """
    data = await _fetch_raw_scoreboard("ufc", window_days, past_days=past_days)

    candidates: list[tuple[str, dict]] = []
    for ev in data.get("events", []):
        name = ev.get("name") or ev.get("shortName") or ""
        if name:
            candidates.append((name, ev))

    matching = None
    # Exact first
    for name, ev in candidates:
        if name == event_name:
            matching = ev
            break

    if matching is None:
        try:
            from card_data import _best_event_name

            hit = _best_event_name(event_name, [n for n, _ in candidates], min_score=40.0)
            if hit:
                matching = next(ev for n, ev in candidates if n == hit)
        except Exception:
            matching = None

    if matching is None:
        return []

    results: list[dict[str, Any]] = []
    for comp in matching.get("competitions", []):
        competitors = comp.get("competitors", [])
        if len(competitors) != 2:
            continue
        ordered = sorted(competitors, key=lambda c: c.get("order", 0))
        name_a = (ordered[0].get("athlete") or {}).get("displayName") or "Fighter A"
        name_b = (ordered[1].get("athlete") or {}).get("displayName") or "Fighter B"

        status = comp.get("status") or {}
        status_type = status.get("type") or {}
        completed = bool(status_type.get("completed", False))

        winner_name = None
        for c, name in ((ordered[0], name_a), (ordered[1], name_b)):
            if c.get("winner") is True:
                winner_name = name
                break

        method = None
        round_num = None
        raw_text = None
        if completed:
            text_fields = [
                status_type.get("description"),
                status_type.get("detail"),
                status_type.get("shortDetail"),
            ]
            for note in comp.get("notes", []) or []:
                if isinstance(note, dict):
                    text_fields.append(note.get("headline"))
            method, round_num = _parse_method_and_round(text_fields)
            raw_text = " | ".join(t for t in text_fields if t) or None

            # Contender Series often leaves description as "Final" only —
            # method lives in details ("Unofficial Winner Kotko"), round in period.
            if method is None:
                method = _method_from_espn_details(comp.get("details"))

            period = status.get("period")
            if round_num is None and period is not None:
                try:
                    period_i = int(period)
                    if period_i > 0:
                        round_num = period_i
                except (TypeError, ValueError):
                    pass

        scheduled_rounds = (comp.get("format") or {}).get("regulation", {}).get("periods")
        elapsed = _elapsed_seconds_in_round(status) if completed else None

        # After a decision ESPN often only says "Final". A last-round
        # *stoppage* has the same period as a decision, so only treat it
        # as DEC when the clock looks like a full round (or is missing).
        if (
            completed
            and method is None
            and winner_name
            and scheduled_rounds
            and round_num is not None
            and int(round_num) >= int(scheduled_rounds)
            and (elapsed is None or float(elapsed) >= 290)
        ):
            method = "DEC"
            round_num = int(scheduled_rounds)

        results.append(
            {
                "fighter_a": name_a,
                "fighter_b": name_b,
                "completed": completed,
                "winner": winner_name,
                "method": method,
                "round": round_num,
                "scheduled_rounds": scheduled_rounds,
                "raw_text": raw_text,
                "elapsed_seconds": elapsed,
            }
        )

    return results