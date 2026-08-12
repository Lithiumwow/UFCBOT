"""Ensure FightIQ package is importable and load prop catalogs (labels only)."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("ufc-bet-bot.props")

_FIGHTIQ_ROOT = Path(__file__).resolve().parent / "FightIQ"
_path_ready = False


def ensure_fightiq_path() -> None:
    global _path_ready
    if _path_ready:
        return
    root = str(_FIGHTIQ_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    _path_ready = True


def load_prop_catalog(fight_slug: str, *, force: bool = False) -> Any:
    """
    Fetch FightOdds prop catalog for a fight.
    Prefer with_books=False (labels only). Retry with books if empty —
    some cards only expose prop names once book lines are present.
    """
    ensure_fightiq_path()
    from fightiq.props_catalog import get_prop_service

    svc = get_prop_service()
    catalog = svc.get_catalog(fight_slug, force=force, with_books=False)
    if not catalog.plays:
        catalog = svc.get_catalog(fight_slug, force=True, with_books=True)
    return catalog


def build_fallback_catalog(
    *,
    fighter_a: str,
    fighter_b: str,
    fight_slug: str = "",
) -> Any:
    """
    Synthetic catalog from FightIQ METHOD_MARKETS when FightOdds hasn't
    posted a prop table yet (common for far-out cards). Labels only.
    """
    ensure_fightiq_path()
    from fightiq.markets import METHOD_MARKETS
    from fightiq.props_catalog import Play, PropCatalog, category_for

    plays: list[Play] = []

    def _add(
        ot: str,
        label: str,
        *,
        side: int = 1,
        category: str | None = None,
        popular: bool = True,
    ) -> None:
        plays.append(
            Play(
                id=f"{ot}:{side}:{label.lower().replace(' ', '-')[:60]}",
                label=label,
                offer_type_id=ot,
                side=side,
                american=None,
                category=category or category_for(ot),
                fight_slug=fight_slug or "fallback",
                prop_name_pair=(label, ""),
                books={},
                popular=popular,
            )
        )

    # Moneyline
    _add("STRAIGHT", fighter_a, side=1, category="moneyline")
    _add("STRAIGHT", fighter_b, side=2, category="moneyline")

    # Per-fighter method / round markets from FightIQ
    for corner, name in ((1, fighter_a), (2, fighter_b)):
        for key, market in METHOD_MARKETS.items():
            if key == "ml":
                continue
            ot = market.offer_type_id or key.upper()
            _add(ot, f"{name} — {market.label}", side=corner)

    # Fight-level staples
    for label, ot in (
        ("Fight Goes the Distance", "DISTANCE"),
        ("Fight Does NOT Go the Distance", "DISTANCE"),
        ("Fight Ends by KO/TKO", "END_KO"),
        ("Fight Ends by Submission", "END_SUB"),
    ):
        _add(ot, label, side=1, category="method_fight" if ot.startswith("END") else "distance")

    for whole in (0, 1, 2, 3, 4):
        ot = f"OVERUNDER_{whole}.5"
        _add(ot, f"Over {whole}.5 rounds", side=1, category="totals")
        _add(ot, f"Under {whole}.5 rounds", side=2, category="totals")

    for n in range(1, 6):
        _add(f"END_{n}", f"Fight Ends in Round {n}", side=1, category="method_fight")

    return PropCatalog(
        fight_slug=fight_slug or "fallback",
        fight_label=f"{fighter_a} vs {fighter_b}",
        event_name="__fightiq_fallback__",
        plays=plays,
        sportsbooks=[],
        fetched_at=time.time(),
        with_books=False,
    )


def try_load_prop_catalog(
    fight_slug: Optional[str],
    *,
    fighter_a: str = "",
    fighter_b: str = "",
) -> Any | None:
    """
    Load live catalog when possible; fall back to FightIQ market templates
    so method/round picks still work before books post props.
    """
    catalog = None
    if fight_slug:
        try:
            catalog = load_prop_catalog(fight_slug)
        except Exception:
            log.exception("Failed to load prop catalog for %s", fight_slug)
            catalog = None

    if catalog is not None and catalog.plays:
        return catalog

    if fighter_a and fighter_b:
        log.info(
            "Using FightIQ method fallback catalog for %s vs %s (slug=%s)",
            fighter_a,
            fighter_b,
            fight_slug,
        )
        return build_fallback_catalog(
            fighter_a=fighter_a,
            fighter_b=fighter_b,
            fight_slug=fight_slug or "",
        )
    return None
