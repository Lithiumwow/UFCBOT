"""ESPN UFC scoreboard client for fight results / autograding."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional


ESPN_SCOREBOARD = (
    "https://site.web.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
)


@dataclass
class EspnFightResult:
    event_id: str
    event_name: str
    event_date: str  # ISO-ish
    competition_id: str
    fighter1: str
    fighter2: str
    winner: str | None
    method: str | None  # decision | ko | submission | unknown
    round: int | None
    time: str | None
    completed: bool
    weight_class: str | None = None

    def ends_inside_distance(self) -> bool | None:
        if not self.completed or not self.method:
            return None
        return self.method in {"ko", "submission"}

    def goes_distance(self) -> bool | None:
        itd = self.ends_inside_distance()
        return None if itd is None else (not itd)


class EspnMmaClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._board_cache: dict[str, list[EspnFightResult]] = {}

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.espn.com/",
                "Origin": "https://www.espn.com",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def scoreboard(self, on_date: date | None = None) -> list[EspnFightResult]:
        key = on_date.strftime("%Y%m%d") if on_date else "_live"
        if key in self._board_cache:
            return self._board_cache[key]
        if on_date is None:
            url = ESPN_SCOREBOARD
        else:
            url = f"{ESPN_SCOREBOARD}?dates={on_date.strftime('%Y%m%d')}"
        try:
            data = self._get(url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            self._board_cache[key] = []
            return []
        fights = self._parse_board(data)
        self._board_cache[key] = fights
        return fights

    def results_window(self, days_back: int = 10, days_forward: int = 1) -> list[EspnFightResult]:
        """Pull recent cards (past + slight future). Denser near today."""
        today = datetime.now(timezone.utc).date()
        out: list[EspnFightResult] = []
        seen: set[str] = set()

        def _add(fights: list[EspnFightResult]) -> None:
            for fight in fights:
                if fight.competition_id in seen:
                    continue
                seen.add(fight.competition_id)
                out.append(fight)

        # live/default board (current card window)
        _add(self.scoreboard(None))

        # delta: negative = past, positive = future
        for delta in range(-days_back, days_forward + 1):
            # denser near today; sparse farther back
            if abs(delta) > 4 and abs(delta) % 2 != 0:
                continue
            _add(self.scoreboard(today + timedelta(days=delta)))
        return out

    def find_fight(
        self, fighter_a: str, fighter_b: str | None = None, *, days_back: int = 21
    ) -> EspnFightResult | None:
        a = fighter_a.lower()
        b = (fighter_b or "").lower()
        best = None
        best_score = 0
        for fight in self.results_window(days_back=days_back, days_forward=1):
            n1 = fight.fighter1.lower()
            n2 = fight.fighter2.lower()
            score = 0
            if _name_hit(a, n1) or _name_hit(a, n2):
                score += 2
            if b and (_name_hit(b, n1) or _name_hit(b, n2)):
                score += 2
            if score > best_score:
                best_score = score
                best = fight
        return best if best_score >= 2 else None

    def _parse_board(self, data: dict) -> list[EspnFightResult]:
        out: list[EspnFightResult] = []
        for event in data.get("events") or []:
            event_id = str(event.get("id") or "")
            event_name = event.get("name") or event.get("shortName") or ""
            event_date = event.get("date") or ""
            for comp in event.get("competitions") or []:
                comps = comp.get("competitors") or []
                if len(comps) < 2:
                    continue
                # order 1 is often corner A
                comps_sorted = sorted(comps, key=lambda x: x.get("order") or 0)
                f1 = _athlete_name(comps_sorted[0])
                f2 = _athlete_name(comps_sorted[1])
                winner = None
                for c in comps_sorted:
                    if c.get("winner"):
                        winner = _athlete_name(c)
                        break
                status = (comp.get("status") or {}).get("type") or {}
                completed = bool(status.get("completed"))
                period = (comp.get("status") or {}).get("period")
                clock = (comp.get("status") or {}).get("displayClock")
                method = _method_from_details(comp.get("details") or [])
                weight = (comp.get("type") or {}).get("abbreviation")
                out.append(
                    EspnFightResult(
                        event_id=event_id,
                        event_name=event_name,
                        event_date=event_date,
                        competition_id=str(comp.get("id") or ""),
                        fighter1=f1,
                        fighter2=f2,
                        winner=winner,
                        method=method,
                        round=int(period) if period else None,
                        time=str(clock) if clock else None,
                        completed=completed,
                        weight_class=weight,
                    )
                )
        return out


def _athlete_name(c: dict) -> str:
    ath = c.get("athlete") or {}
    return (
        ath.get("displayName")
        or ath.get("fullName")
        or ath.get("shortName")
        or c.get("id")
        or "Unknown"
    )


def _method_from_details(details: list) -> str | None:
    texts = []
    for d in details:
        t = ((d.get("type") or {}).get("text") or "").lower()
        texts.append(t)
    blob = " | ".join(texts)
    if "submission" in blob:
        return "submission"
    if "kotko" in blob or "ko/tko" in blob or re.search(r"\bko\b", blob):
        return "ko"
    if "decision" in blob:
        return "decision"
    return None


def _name_hit(query: str, full: str) -> bool:
    q = query.strip().lower()
    f = full.strip().lower()
    if not q or not f:
        return False
    if q == f or q in f or f in q:
        return True
    qparts = q.split()
    fparts = f.split()
    if qparts and qparts[-1] in fparts:
        if len(qparts) == 1:
            return True
        if any(p[0] == fparts[0][0] for p in qparts[:-1] if fparts):
            return True
        if qparts[0] in fparts:
            return True
    return False
