"""
Match OCR / pasted slip legs against FightOdds prop catalogs for that card.

For each leg we resolve the fight on the event card, load that fight's plays,
score them against the slip text, and (when confident) replace the free-text
leg with a structured map_play_to_leg result so auto-grade can work.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

import card_data
from prop_play_map import map_play_to_leg, rebuild_description_from_stored
from props_loader import try_load_prop_catalog

log = logging.getLogger("ufc-bet-bot.slip_match")

# Minimum score to accept a catalog play (0–100-ish).
_MIN_SCORE = 42

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ROUND_NUMS_RE = re.compile(
    r"\b(?:rounds?|r)\s*(\d)\s*(?:or|/|-|–|—|to)\s*(\d)\b|\b(?:rounds?|r)\s*(\d)\b",
    re.I,
)
_OU_RE = re.compile(
    r"\b(?P<dir>over|under)\s*(?P<line>\d+(?:\.\d+)?)\s*(?:rounds?)?\b",
    re.I,
)
_METHOD_IN_ROUND_RE = re.compile(
    r"(?:by\s+)?(?P<method>ko/?tko|ko|tko|sub(?:mission)?)\s+"
    r"(?:in\s+)?(?:rounds?\s*)?(?P<r>\d)\b",
    re.I,
)
_WINS_ROUND_RE = re.compile(
    r"(?:wins?\s+(?:the\s+fight\s+)?in\s+)?rounds?\s*(?P<r>\d)\b|"
    r"\bin\s+round\s*(?P<r2>\d)\b",
    re.I,
)
_END_ROUND_RE = re.compile(
    r"(?:fight\s+)?ends?\s+in\s+rounds?\s*(?P<r>\d)|"
    r"fight\s+ends?\s+rounds?\s*(?P<r2>\d)",
    re.I,
)
_START_ROUND_RE = re.compile(
    r"(?:reaches?|starts?|goes?\s+to)\s+rounds?\s*(?P<r>\d)|"
    r"rounds?\s*(?P<r2>\d)\s+(?:or\s+)?(?:more|later)",
    re.I,
)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) > 1}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _extract_rounds(text: str) -> set[int]:
    rounds: set[int] = set()
    for m in _ROUND_NUMS_RE.finditer(text or ""):
        if m.group(1) and m.group(2):
            rounds.add(int(m.group(1)))
            rounds.add(int(m.group(2)))
        elif m.group(3):
            rounds.add(int(m.group(3)))
    return rounds


def _method_hints(text: str) -> set[str]:
    t = _norm(text)
    hints: set[str] = set()
    if re.search(r"\bko\b|\btko\b|knockout|dq\b", t):
        hints.add("ko")
    if re.search(r"\bsub\b|submission", t):
        hints.add("sub")
    if re.search(r"\bunanimous\b|\bud\b", t):
        hints.add("ud")
    if re.search(r"\bsplit\b|\bsd\b", t):
        hints.add("sd")
    if re.search(r"\bdec(?:ision)?\b|\bpoints\b|\bpts\b", t):
        hints.add("dec")
    if re.search(r"\bml\b|moneyline|money\s*line|\bto win\b(?!\s+by)", t):
        hints.add("ml")
    if re.search(r"inside\s+(?:the\s+)?distance|\bitd\b", t):
        hints.add("id")
    if re.search(r"does\s*n['’]?t\s+go|not\s+go\s+(?:the\s+)?distance|ends?\s+inside", t):
        hints.add("not_distance")
    elif re.search(r"goes?\s+the\s+distance|go\s+distance|distance\s*=\s*yes", t):
        hints.add("distance")
    if re.search(r"\bover\b|\bunder\b", t):
        hints.add("totals")
    if re.search(r"fight\s+ends?\s+by|ends?\s+by\s+(?:ko|sub)", t):
        hints.add("fight_method")
    if re.search(r"ends?\s+in\s+rounds?", t):
        hints.add("end_round")
    if re.search(r"reaches?\s+rounds?|starts?\s+rounds?|goes?\s+to\s+rounds?", t):
        hints.add("start_round")
    return hints


def _leg_query(leg: dict[str, Any]) -> str:
    return " ".join(
        filter(
            None,
            [
                leg.get("selection_raw"),
                leg.get("description"),
                leg.get("market_name"),
            ],
        )
    )


def _structured(
    *,
    description: str,
    fighter_pick: Optional[str],
    outcome_type: str,
    outcome_round: Optional[int] = None,
    selection_raw: Optional[str] = None,
    note: str = "structured",
) -> dict[str, Any]:
    return {
        "description": description,
        "fighter_pick": fighter_pick,
        "outcome_type": outcome_type,
        "outcome_round": outcome_round,
        "matched": True,
        "match_score": 100.0,
        "match_play_label": description,
        "match_offer_type": outcome_type,
        "selection_raw": selection_raw,
        "match_note": note,
    }


def try_structure_leg(
    leg: dict[str, Any],
    *,
    fighter_pick: str,
    fighter_a: str,
    fighter_b: str,
) -> Optional[dict[str, Any]]:
    """
    Map common bookmaker phrasings to structured outcome types.

    Covers: ML, KO/Sub/Dec/UD/SD, round wins, method+round, round combos,
    O/U totals, distance / ID, fight ends by method, end/start (time) rounds.
    """
    query = _leg_query(leg)
    q = _norm(query)
    raw = leg.get("selection_raw") or leg.get("description")
    hints = _method_hints(query)
    rounds = sorted(_extract_rounds(query))
    fight_label = f"{fighter_a} vs {fighter_b}"

    # ---- O/U totals (fight-level) ----
    ou = _OU_RE.search(query)
    if ou and ("totals" in hints or "round" in q):
        direction = ou.group("dir").upper()
        line = float(ou.group("line"))
        whole = int(line)
        ot = f"{direction}_{whole}_5"
        return _structured(
            description=f"{fight_label} {direction.lower()} {whole}.5 rounds",
            fighter_pick=fighter_a,
            outcome_type=ot,
            selection_raw=raw,
            note="totals",
        )

    # ---- Round N[, M] or by Decision (DraftKings alt round betting) ----
    if rounds and "dec" in hints and re.search(
        r"\bround|\br\s*\d", q
    ):
        rs = "_".join(str(r) for r in rounds)
        label = ", ".join(str(r) for r in rounds)
        return _structured(
            description=f"{fighter_pick} Round {label} or by Decision",
            fighter_pick=fighter_pick,
            outcome_type=f"R_{rs}_DEC",
            selection_raw=raw,
            note="round or decision",
        )

    # ---- Multi-round combos ----
    if len(rounds) >= 2:
        r1, r2 = rounds[0], rounds[1]
        if "ko" in hints:
            ot = f"KO_{r1}_{r2}"
            desc = f"{fighter_pick} by KO/TKO in Rounds {r1} or {r2}"
        elif "sub" in hints:
            ot = f"SUB_{r1}_{r2}"
            desc = f"{fighter_pick} by Submission in Rounds {r1} or {r2}"
        else:
            ot = f"R_{r1}_{r2}"
            desc = f"{fighter_pick} wins in Round {r1} or {r2}"
        return _structured(
            description=desc,
            fighter_pick=fighter_pick,
            outcome_type=ot,
            selection_raw=raw,
            note="round combo",
        )

    # ---- Fight ends in round N / reaches round N ----
    if "end_round" in hints or _END_ROUND_RE.search(query):
        m = _END_ROUND_RE.search(query)
        r = 0
        if m:
            r = int(m.group("r") or m.group("r2") or 0)
        elif rounds:
            r = rounds[0]
        if r:
            return _structured(
                description=f"{fight_label} ends in round {r}",
                fighter_pick=fighter_a,
                outcome_type=f"END_{r}",
                outcome_round=r,
                selection_raw=raw,
                note="end round",
            )

    if "start_round" in hints or _START_ROUND_RE.search(query):
        m = _START_ROUND_RE.search(query)
        r = 0
        if m:
            r = int(m.group("r") or m.group("r2") or 0)
        elif rounds:
            r = rounds[0]
        if r:
            return _structured(
                description=f"{fight_label} reaches round {r}",
                fighter_pick=fighter_a,
                outcome_type=f"START_{r}",
                outcome_round=r,
                selection_raw=raw,
                note="start/time",
            )

    # ---- Distance / not distance / ID ----
    if "not_distance" in hints:
        return _structured(
            description=f"{fight_label} does NOT go the distance",
            fighter_pick=fighter_a,
            outcome_type="NOT_DISTANCE",
            selection_raw=raw,
            note="distance",
        )
    if "distance" in hints and "id" not in hints:
        return _structured(
            description=f"{fight_label} goes the distance",
            fighter_pick=fighter_a,
            outcome_type="DISTANCE",
            selection_raw=raw,
            note="distance",
        )
    if "id" in hints:
        return _structured(
            description=f"{fighter_pick} wins inside the distance",
            fighter_pick=fighter_pick,
            outcome_type="ID",
            selection_raw=raw,
            note="ID",
        )

    # ---- Fight-level method (either fighter) ----
    if "fight_method" in hints:
        if "sub" in hints and "ko" not in hints:
            return _structured(
                description=f"{fight_label} ends by Submission (either fighter)",
                fighter_pick=fighter_a,
                outcome_type="FIGHT_SUB",
                selection_raw=raw,
                note="fight method",
            )
        return _structured(
            description=f"{fight_label} ends by KO/TKO (either fighter)",
            fighter_pick=fighter_a,
            outcome_type="FIGHT_KO",
            selection_raw=raw,
            note="fight method",
        )

    # ---- Method + single round ----
    m = _METHOD_IN_ROUND_RE.search(query)
    if m and len(rounds) <= 1:
        method = m.group("method").lower()
        r = int(m.group("r"))
        if method.startswith("sub"):
            ot = f"SUB_{r}"
            desc = f"{fighter_pick} by Submission (Round {r})"
        else:
            ot = f"KO_{r}"
            desc = f"{fighter_pick} by KO/TKO (Round {r})"
        return _structured(
            description=desc,
            fighter_pick=fighter_pick,
            outcome_type=ot,
            outcome_round=r,
            selection_raw=raw,
            note="method+round",
        )

    # ---- Round win only ----
    if rounds and len(rounds) == 1 and not (
        {"ko", "sub", "dec", "ud", "sd", "ml", "totals"} & hints
    ):
        r = rounds[0]
        if re.search(r"\bround\b|\br\s*\d\b|\bwins?\b", q) or _WINS_ROUND_RE.search(query):
            return _structured(
                description=f"{fighter_pick} wins in Round {r}",
                fighter_pick=fighter_pick,
                outcome_type=f"R_{r}",
                outcome_round=r,
                selection_raw=raw,
                note="round",
            )

    # ---- Method-only / ML / decision variants ----
    if "ud" in hints:
        return _structured(
            description=f"{fighter_pick} by Unanimous Decision",
            fighter_pick=fighter_pick,
            outcome_type="UD",
            selection_raw=raw,
            note="UD",
        )
    if "sd" in hints:
        return _structured(
            description=f"{fighter_pick} by Split Decision",
            fighter_pick=fighter_pick,
            outcome_type="SD",
            selection_raw=raw,
            note="SD",
        )
    if "dec" in hints and "ko" not in hints and "sub" not in hints:
        return _structured(
            description=f"{fighter_pick} by Decision",
            fighter_pick=fighter_pick,
            outcome_type="DEC",
            selection_raw=raw,
            note="decision",
        )
    if "sub" in hints and "ko" not in hints and not rounds:
        return _structured(
            description=f"{fighter_pick} by Submission",
            fighter_pick=fighter_pick,
            outcome_type="SUB",
            selection_raw=raw,
            note="submission",
        )
    if "ko" in hints and "sub" not in hints and not rounds:
        if re.search(r"ko\s*/?\s*sub|ko\s+or\s+sub", q):
            return _structured(
                description=f"{fighter_pick} by KO/TKO or Submission",
                fighter_pick=fighter_pick,
                outcome_type="KO_OR_SUB",
                selection_raw=raw,
                note="ko/sub",
            )
        return _structured(
            description=f"{fighter_pick} by KO/TKO",
            fighter_pick=fighter_pick,
            outcome_type="KO_TKO",
            selection_raw=raw,
            note="KO",
        )
    if "ko" in hints and "sub" in hints and not rounds:
        return _structured(
            description=f"{fighter_pick} by KO/TKO or Submission",
            fighter_pick=fighter_pick,
            outcome_type="KO_OR_SUB",
            selection_raw=raw,
            note="ko/sub",
        )

    market = _norm(leg.get("market_name") or "")
    if "ml" in hints or re.search(r"fight betting|moneyline|to win", market):
        return _structured(
            description=f"{fighter_pick} ML",
            fighter_pick=fighter_pick,
            outcome_type="ML",
            selection_raw=raw,
            note="ML",
        )

    sel = _norm(leg.get("selection_raw") or leg.get("description") or "")
    fp = _norm(fighter_pick)
    last = fp.split()[-1] if fp.split() else ""
    if sel and (sel == fp or sel == last or (fp in sel and len(sel) <= len(fp) + 4)):
        if not ({"ko", "sub", "dec", "ud", "sd", "totals", "distance", "id"} & hints):
            return _structured(
                description=f"{fighter_pick} ML",
                fighter_pick=fighter_pick,
                outcome_type="ML",
                selection_raw=raw,
                note="ML",
            )

    if leg.get("outcome_type"):
        ot = str(leg["outcome_type"]).upper()
        desc = rebuild_description_from_stored(
            fighter_pick=fighter_pick,
            outcome_type=ot,
            outcome_round=leg.get("outcome_round"),
            fighter_a=fighter_a,
            fighter_b=fighter_b,
            current_description=leg.get("description") or raw or ot,
        )
        return _structured(
            description=desc,
            fighter_pick=fighter_pick,
            outcome_type=ot,
            outcome_round=leg.get("outcome_round"),
            selection_raw=raw,
            note="parser",
        )

    return None


def try_round_combo_leg(
    leg: dict[str, Any],
    *,
    fighter_pick: str,
) -> Optional[dict[str, Any]]:
    """Back-compat — use try_structure_leg when fight corners are known."""
    return try_structure_leg(
        leg,
        fighter_pick=fighter_pick,
        fighter_a=fighter_pick,
        fighter_b=fighter_pick,
    )


def _play_side_fighter(play: Any, fighter_a: str, fighter_b: str) -> str:
    return fighter_a if getattr(play, "side", 1) == 1 else fighter_b


def score_play_against_leg(
    play: Any,
    *,
    query: str,
    fighter_pick: Optional[str],
    fighter_a: str,
    fighter_b: str,
) -> float:
    """Heuristic score; higher = better match to the slip selection."""
    label = getattr(play, "label", "") or ""
    ot = (getattr(play, "offer_type_id", "") or "").upper()
    blob = _norm(f"{label} {ot} {getattr(play, 'category', '')}")
    q = _norm(query)
    if not q:
        return 0.0

    score = 0.0
    q_toks = _tokens(q)
    p_toks = _tokens(blob)
    if q_toks and p_toks:
        overlap = q_toks & p_toks
        overlap -= {"to", "win", "by", "in", "or", "the", "and", "of", "vs"}
        score += 8.0 * len(overlap)

    side_fighter = _play_side_fighter(play, fighter_a, fighter_b)
    if fighter_pick:
        fp = fighter_pick.lower()
        sf = side_fighter.lower()
        last = sf.split()[-1] if sf.split() else ""
        if fp == sf or fp in sf or sf in fp or (last and last in fp):
            score += 18.0
        elif last and last not in q and last not in _norm(fighter_pick):
            score -= 12.0

    hints = _method_hints(q)
    rounds = _extract_rounds(q)

    if "ko" in hints:
        if ot in {"KO", "END_KO"} or ot.startswith("KO_"):
            score += 22.0
        elif "ko" in blob or "tko" in blob:
            score += 12.0
        if "sub" in ot.lower() and "ko" not in ot.lower():
            score -= 8.0

    if "sub" in hints:
        if ot in {"SUB", "END_SUB"} or ot.startswith("SUB_"):
            score += 22.0
        elif "sub" in blob:
            score += 12.0

    if "ud" in hints and ot == "UD":
        score += 24.0
    if "sd" in hints and ot == "SD":
        score += 24.0
    if "dec" in hints and ot in {"DEC", "UD", "SD"}:
        score += 20.0

    if "ml" in hints and ot == "STRAIGHT":
        score += 25.0

    if "id" in hints and ot == "ID":
        score += 24.0
    if "distance" in hints and ot == "DISTANCE":
        score += 22.0
    if "not_distance" in hints and ("NOT" in ot or "doesn't" in blob or "not" in blob):
        score += 22.0

    if "totals" in hints and ot.startswith("OVERUNDER"):
        score += 18.0
        ou = _OU_RE.search(query)
        if ou and ou.group("line") in ot:
            score += 12.0

    if "end_round" in hints and ot.startswith("END_"):
        score += 22.0
    if "start_round" in hints and ot.startswith("START_"):
        score += 22.0

    if rounds:
        ot_round = None
        m = re.match(r"^(?:KO|SUB|R|END|START)_(\d)$", ot)
        if m:
            ot_round = int(m.group(1))
        if ot_round is not None:
            if ot_round in rounds:
                score += 28.0
                if len(rounds) > 1:
                    score += 4.0
            else:
                score -= 15.0
        elif any(f"round {r}" in blob or f"r{r}" in blob for r in rounds):
            score += 14.0
        if len(rounds) >= 1 and ot in {"KO", "SUB", "STRAIGHT", "DEC", "ID"}:
            score -= 14.0

    if q and (q in blob or blob in q):
        score += 20.0

    return score


def best_play_for_leg(
    leg: dict[str, Any],
    catalog: Any,
    *,
    fighter_a: str,
    fighter_b: str,
) -> tuple[Optional[Any], float]:
    query = " ".join(
        filter(
            None,
            [
                leg.get("selection_raw"),
                leg.get("description"),
                leg.get("market_name"),
            ],
        )
    )
    fighter_pick = leg.get("fighter_pick")
    best = None
    best_score = -1.0
    for play in getattr(catalog, "plays", []) or []:
        s = score_play_against_leg(
            play,
            query=query,
            fighter_pick=fighter_pick,
            fighter_a=fighter_a,
            fighter_b=fighter_b,
        )
        if s > best_score:
            best_score = s
            best = play
    return best, best_score


async def match_legs_to_card(
    legs: list[dict[str, Any]],
    *,
    event: Optional[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Enrich slip legs by matching against the event card's FightOdds catalogs.

    Returns (legs, notes). Unmatched legs are left as free-text.
    """
    notes: list[str] = []
    if not legs:
        return legs, notes

    fights: list = []
    if event:
        try:
            fights = await card_data.fetch_fights_for_event(event)
        except Exception as e:
            log.warning("Could not load card for slip match (%s): %s", event, e)
            notes.append(f"Card lookup failed for `{event}` — kept OCR text.")
            return legs, notes

    if not fights:
        notes.append("No fight card loaded — kept OCR text (set `event` for catalog match).")
        return legs, notes

    out: list[dict[str, Any]] = []
    matched_n = 0

    for leg in legs:
        hit = card_data.resolve_fighter_on_card(
            fighter_pick=leg.get("fighter_pick"),
            description=leg.get("description") or leg.get("selection_raw"),
            fights=fights,
        )
        # Also try market corners from OCR
        if hit is None:
            for name in (leg.get("fighter_a"), leg.get("fighter_b"), leg.get("fighter_pick")):
                if name:
                    hit = card_data.match_fighter_on_card(name, fights)
                    if hit:
                        break

        if hit is None:
            notes.append(f"No card fight for: {leg.get('description')}")
            out.append(leg)
            continue

        fighter, opponent, fight_label = hit
        # Orient corners like the card entry
        fighter_a = fighter
        fighter_b = opponent
        slug = None
        for fight in fights:
            a, b = card_data.fight_corners(fight)
            if {a.lower(), b.lower()} == {fighter.lower(), opponent.lower()}:
                fighter_a, fighter_b = a, b
                slug = card_data.fight_slug(fight)
                break

        # Prefer the card-canonical fighter name as pick when OCR was close
        enriched = dict(leg)
        if not enriched.get("fighter_pick"):
            enriched["fighter_pick"] = fighter
        else:
            # Canonicalize to card spelling when last names match
            card_hit = card_data.match_fighter_on_card(enriched["fighter_pick"], fights)
            if card_hit:
                enriched["fighter_pick"] = card_hit[0]

        # Structure common book markets without needing a catalog
        structured = try_structure_leg(
            enriched,
            fighter_pick=enriched["fighter_pick"],
            fighter_a=fighter_a,
            fighter_b=fighter_b,
        )
        if structured is not None:
            matched_n += 1
            note = structured.get("match_note") or "structured"
            notes.append(
                f"Matched → {structured['description']} "
                f"[{structured['outcome_type']}] ({note})"
            )
            out.append(structured)
            continue

        catalog = try_load_prop_catalog(
            slug, fighter_a=fighter_a, fighter_b=fighter_b
        )
        if catalog is None or not getattr(catalog, "plays", None):
            notes.append(f"No props catalog for {fight_label}")
            out.append(enriched)
            continue

        play, score = best_play_for_leg(
            enriched, catalog, fighter_a=fighter_a, fighter_b=fighter_b
        )
        if play is None or score < _MIN_SCORE:
            notes.append(
                f"No confident prop match for `{enriched.get('description')}` "
                f"(best {score:.0f})"
            )
            out.append(enriched)
            continue

        mapped = map_play_to_leg(play, fighter_a=fighter_a, fighter_b=fighter_b)
        mapped["match_score"] = round(score, 1)
        mapped["match_play_label"] = getattr(play, "label", None)
        mapped["match_offer_type"] = getattr(play, "offer_type_id", None)
        mapped["selection_raw"] = leg.get("selection_raw") or leg.get("description")
        mapped["matched"] = True
        matched_n += 1
        notes.append(
            f"Matched → {mapped['description']} "
            f"[{mapped.get('match_offer_type')}] score={score:.0f}"
        )
        out.append(mapped)

    if matched_n:
        notes.insert(0, f"Catalog-matched {matched_n}/{len(legs)} leg(s).")
    return out, notes
