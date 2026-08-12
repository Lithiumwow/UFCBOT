"""Parse free-text / OCR betting slips into structured MMA legs."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .odds_math import format_american


AMERICAN_RE = re.compile(r"(?<![\w/])([+-]\d{2,5})(?![\w.])")
# Stake patterns
STAKE_RE = re.compile(
    r"(?:stake|risk|wager|to win|bet)\s*[:=]?\s*\$?\s*([\d,]+(?:\.\d{1,2})?)",
    re.I,
)
# Odds: -150 / +400 / 1.50 (decimal rare)
ODDS_LINE_RE = re.compile(
    r"^(?P<label>.+?)\s+(?P<odds>[+-]\d{2,5})\s*$",
    re.I,
)
VS_RE = re.compile(r"\bvs\.?\b|\bv\b", re.I)


@dataclass
class ParsedLeg:
    raw: str
    label: str
    selection: str
    market: str  # ml | sub | ko | dec | over | under | method_fight | round | other
    fighter: str | None = None
    opponent: str | None = None
    american: int | None = None
    line: float | None = None  # e.g. 2.5 for totals
    side: str | None = None  # over/under/yes/no
    confidence: float = 0.5
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["formatted"] = format_american(self.american)
        return d


@dataclass
class ParsedSlip:
    raw_text: str
    legs: list[ParsedLeg]
    stake: float | None = None
    to_win: float | None = None
    book: str | None = None
    source: str = "local"  # local | quickpick
    quickpick_link: str | None = None
    quickpick_raw: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "raw_text": self.raw_text,
            "legs": [leg.to_dict() for leg in self.legs],
            "stake": self.stake,
            "to_win": self.to_win,
            "book": self.book,
            "source": self.source,
            "quickpick_link": self.quickpick_link,
            "notes": self.notes,
            "leg_count": len(self.legs),
        }


def parse_slip_text(text: str) -> ParsedSlip:
    """Heuristic MMA slip parser (works without QuickPick)."""
    cleaned = _clean_text(text)
    notes: list[str] = []
    stake = None
    to_win = None
    book = None

    m = STAKE_RE.search(cleaned)
    if m:
        stake = float(m.group(1).replace(",", ""))

    # book hints
    for b in (
        "FanDuel",
        "DraftKings",
        "BetMGM",
        "Caesars",
        "BetRivers",
        "Hard Rock",
        "Pinnacle",
        "BetOnline",
        "Bovada",
        "Stake",
        "bet365",
    ):
        if re.search(rf"\b{re.escape(b)}\b", cleaned, re.I):
            book = b
            break

    legs: list[ParsedLeg] = []
    last_fighter: str | None = None
    last_opponent: str | None = None
    for line in cleaned.splitlines():
        line = line.strip(" -•\t")
        if len(line) < 3:
            continue
        if re.search(r"^(stake|risk|to win|payout|total|parlay|same game)", line, re.I):
            continue
        leg = _parse_line(line)
        if not leg:
            continue
        # Propagate last named fighter onto fight-level props (over/under, go distance)
        if not leg.fighter and last_fighter and leg.market in {
            "totals",
            "over",
            "under",
            "distance",
            "method_fight",
            "round",
        }:
            leg.fighter = last_fighter
            leg.opponent = last_opponent
            leg.confidence = max(leg.confidence, 0.45)
            leg.meta["inferred_fighter"] = True
        if leg.fighter:
            last_fighter = leg.fighter
            last_opponent = leg.opponent
        legs.append(leg)

    # Fallback: whole-text multi picks without clear lines
    if not legs:
        for chunk in re.split(r"[;\n]| \+ ", cleaned):
            chunk = chunk.strip()
            if len(chunk) < 5:
                continue
            leg = _parse_line(chunk)
            if leg:
                legs.append(leg)

    if not legs:
        notes.append("No legs detected — paste clearer slip text or enable QuickPick")

    return ParsedSlip(
        raw_text=text,
        legs=legs,
        stake=stake,
        to_win=to_win,
        book=book,
        source="local",
        notes=notes,
    )


def parse_quickpick_payload(payload: dict, original_text: str) -> ParsedSlip:
    """Map QuickPick complete payload into ParsedSlip (best-effort)."""
    legs: list[ParsedLeg] = []
    link = payload.get("link") or payload.get("betslip_link") or payload.get("url")

    # Common shapes: legs / selections / bets / picks
    raw_legs = (
        payload.get("legs")
        or payload.get("selections")
        or payload.get("bets")
        or payload.get("picks")
        or []
    )
    if isinstance(raw_legs, dict):
        raw_legs = raw_legs.get("items") or list(raw_legs.values())

    for item in raw_legs or []:
        if not isinstance(item, dict):
            continue
        label = (
            item.get("description")
            or item.get("label")
            or item.get("selection")
            or item.get("name")
            or item.get("text")
            or ""
        )
        odds = item.get("odds") or item.get("price") or item.get("american")
        american = None
        if odds is not None:
            try:
                american = int(float(str(odds).replace("+", ""))) if str(odds).lstrip("+-").isdigit() else int(odds)
                if isinstance(odds, str) and odds.startswith("+"):
                    american = abs(american)
                elif isinstance(odds, str) and odds.startswith("-"):
                    american = -abs(int(odds[1:]))
            except Exception:
                m = AMERICAN_RE.search(str(odds))
                american = int(m.group(1)) if m else None
        base = _parse_line(f"{label} {american if american is not None else ''}".strip())
        if base:
            if american is not None:
                base.american = american
            base.meta["quickpick"] = item
            legs.append(base)
        elif label:
            legs.append(
                ParsedLeg(
                    raw=label,
                    label=label,
                    selection=label,
                    market="other",
                    american=american,
                    confidence=0.4,
                    meta={"quickpick": item},
                )
            )

    # If QuickPick only returned a link, still parse original text
    if not legs:
        local = parse_slip_text(original_text)
        local.source = "quickpick+local"
        local.quickpick_link = link
        local.quickpick_raw = payload
        local.notes.append("QuickPick returned link but no structured legs; used local parse")
        return local

    return ParsedSlip(
        raw_text=original_text,
        legs=legs,
        source="quickpick",
        quickpick_link=link,
        quickpick_raw=payload,
    )


def _clean_text(text: str) -> str:
    t = text.replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _parse_line(line: str) -> ParsedLeg | None:
    raw = line.strip()
    if not raw:
        return None

    odds = None
    m_odds = AMERICAN_RE.search(raw)
    if m_odds:
        odds = int(m_odds.group(1))
        label = AMERICAN_RE.sub("", raw).strip(" -|:")
    else:
        label = raw

    low = label.lower()
    if len(label) < 3:
        return None

    # Totals
    m_tot = re.search(
        r"\b(over|under)\s*(\d+(?:\.\d+)?)\s*(?:rounds?|rds?)?\b", low
    )
    if m_tot:
        side = m_tot.group(1)
        line_v = float(m_tot.group(2))
        return ParsedLeg(
            raw=raw,
            label=label,
            selection=f"{side.title()} {line_v} rounds",
            market="totals",
            american=odds,
            line=line_v,
            side=side,
            confidence=0.85,
        )

    # Distance
    if "goes the distance" in low or "goes distance" in low:
        return ParsedLeg(
            raw=raw,
            label=label,
            selection="Fight goes the distance",
            market="distance",
            side="yes",
            american=odds,
            confidence=0.8,
        )
    if "inside distance" in low or "doesn't go the distance" in low or "does not go the distance" in low:
        return ParsedLeg(
            raw=raw,
            label=label,
            selection="Fight ends inside distance",
            market="distance",
            side="no",
            american=odds,
            confidence=0.8,
        )

    # Fighter method props
    method = None
    if re.search(r"\b(submission|by sub|submits)\b", low):
        method = "sub"
    elif re.search(r"\b(ko/tko|tko|by ko|knockout)\b", low):
        method = "ko"
    elif re.search(r"\b(decision|by dec|unanimous|split)\b", low):
        method = "dec"

    round_n = None
    m_r = re.search(r"\bround\s*([1-5])\b|\br([1-5])\b", low)
    if m_r:
        round_n = int(m_r.group(1) or m_r.group(2))

    fighter = _extract_fighter_name(label)
    opponent = None
    if VS_RE.search(label):
        parts = VS_RE.split(label)
        if len(parts) >= 2:
            fighter = _extract_fighter_name(parts[0]) or fighter
            opponent = _extract_fighter_name(parts[1])

    market = "ml"
    selection = label
    conf = 0.55
    if method and round_n:
        market = f"{method}_r{round_n}"
        selection = f"{fighter or 'Fighter'} wins round {round_n} by {method.upper()}"
        conf = 0.75
    elif method:
        market = method
        selection = f"{fighter or 'Fighter'} wins by {method.upper()}"
        conf = 0.8
    elif round_n:
        market = f"r{round_n}"
        selection = f"{fighter or 'Fighter'} wins in round {round_n}"
        conf = 0.75
    elif fighter and (
        re.search(r"\b(ml|moneyline|to win|winner)\b", low) or odds is not None
    ):
        market = "ml"
        selection = f"{fighter} ML"
        conf = 0.7
    elif "fight ends" in low or "ends by" in low or "ends in" in low:
        market = "method_fight"
        selection = label
        conf = 0.7
    else:
        if not fighter and odds is None:
            return None
        market = "other"
        conf = 0.4

    return ParsedLeg(
        raw=raw,
        label=label,
        selection=selection,
        market=market,
        fighter=fighter,
        opponent=opponent,
        american=odds,
        confidence=conf,
        meta={"round": round_n, "method": method},
    )


def _extract_fighter_name(text: str) -> str | None:
    t = text.strip()
    t = re.sub(
        r"\b(wins?|by|ml|moneyline|to win|winner|submission|ko|tko|decision|"
        r"round\s*[1-5]|over|under|\d+\.?\d*\s*rounds?|inside distance|"
        r"goes the distance|fight ends.*)\b",
        " ",
        t,
        flags=re.I,
    )
    t = re.sub(r"[^\w\s'.-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -")
    # Prefer 2-word names
    parts = [p for p in t.split() if p]
    if not parts:
        return None
    if len(parts) >= 2:
        # drop trailing noise words
        return " ".join(parts[:3])
    return parts[0]
