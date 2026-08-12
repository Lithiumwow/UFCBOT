#!/usr/bin/env python3
"""FightIQ test web app — props, slip parse, auto-grade."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    roots = [
        ROOT / ".env",
        Path("/root/.local/ShannonsGambling/.env"),
        Path("/root/.local/Cdubactiveserver/.env"),
        Path("/root/.local/Controlsparkserver/.env"),
    ]
    for path in roots:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


_load_env()

from flask import Flask, jsonify, request, send_from_directory

from fightiq.client import FightOddsClient, FightOddsError
from fightiq.espn_mma import EspnMmaClient
from fightiq.gambly import (
    GAMBLY_CHAT,
    PLAYBOOK_HOME,
    QUICKPICK_HOME,
    GamblyClient,
    extract_gambly_link,
    format_legs_for_bot,
)
from fightiq.grader import SlipGrader
from fightiq.markets import METHOD_MARKETS
from fightiq.odds_math import (
    american_to_decimal,
    combine_parlay,
    format_american,
    imply_prob,
)
from fightiq.props_catalog import get_prop_service
from fightiq.quickpick import TURNSTILE_SITE_KEY, QuickPickClient
from fightiq.selectors import list_available_markets, resolve_leg
from fightiq.slip_parser import parse_quickpick_payload, parse_slip_text
from fightiq.store import TicketStore

app = Flask(__name__, static_folder="static", static_url_path="/static")
client = FightOddsClient()
props = get_prop_service()
store = TicketStore()
grader = SlipGrader(store=store, espn=EspnMmaClient(), odds=client)
quickpick = QuickPickClient()
gambly = GamblyClient()

def _fight_json(f) -> dict:
    return {
        "slug": f.slug,
        "label": f.label(),
        "event_name": f.event_name,
        "event_date": f.event_date,
        "event_pk": f.event_pk,
        "fighter1": f.fighter1_name,
        "fighter2": f.fighter2_name,
        "is_five_rounds": f.is_five_rounds,
        "odds": {
            "ml": [f.fighter1_odds, f.fighter2_odds],
            "sub": [f.fighter1_sub, f.fighter2_sub],
            "ko": [f.fighter1_ko, f.fighter2_ko],
            "dec": [f.fighter1_dec, f.fighter2_dec],
            "r1": [f.fighter1_r1, f.fighter2_r1],
            "r2": [f.fighter1_r2, f.fighter2_r2],
            "r3": [f.fighter1_r3, f.fighter2_r3],
            "itd": [f.fighter1_itd, f.fighter2_itd],
        },
        "formatted": {
            "ml": [format_american(f.fighter1_odds), format_american(f.fighter2_odds)],
            "sub": [format_american(f.fighter1_sub), format_american(f.fighter2_sub)],
            "ko": [format_american(f.fighter1_ko), format_american(f.fighter2_ko)],
            "dec": [format_american(f.fighter1_dec), format_american(f.fighter2_dec)],
            "r1": [format_american(f.fighter1_r1), format_american(f.fighter2_r1)],
            "r2": [format_american(f.fighter1_r2), format_american(f.fighter2_r2)],
            "r3": [format_american(f.fighter1_r3), format_american(f.fighter2_r3)],
            "itd": [format_american(f.fighter1_itd), format_american(f.fighter2_itd)],
        },
    }


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/ocr")
def ocr_page():
    """OCR / slip-image workspace (separate window from main odds feed)."""
    return send_from_directory(app.static_folder, "ocr.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "FightIQ", "source": "fightodds.io"})


@app.get("/api/sportsbooks")
def sportsbooks():
    return jsonify(props.sportsbooks())


@app.get("/api/markets")
def markets():
    """Legacy short method list (kept for compatibility)."""
    mode = (request.args.get("mode") or "parlay").lower()
    if mode == "straight":
        keys = ["ml"]
    elif mode == "prop":
        keys = [k for k, m in METHOD_MARKETS.items() if m.category == "prop"]
    else:
        keys = list(METHOD_MARKETS.keys())
    return jsonify(
        [
            {
                "key": k,
                "label": METHOD_MARKETS[k].label,
                "category": METHOD_MARKETS[k].category,
            }
            for k in keys
        ]
    )


_events_cache: dict[str, Any] = {"at": 0.0, "rows": None}
_EVENTS_TTL = 120.0


@app.get("/api/events")
def events():
    ufc_only = request.args.get("ufc", "1") != "0"
    limit = min(int(request.args.get("limit", 25)), 60)
    key = f"{ufc_only}:{limit}"
    now = time.time()
    cached = _events_cache.get("key")
    if (
        cached == key
        and _events_cache.get("rows") is not None
        and (now - float(_events_cache["at"])) < _EVENTS_TTL
    ):
        return jsonify(_events_cache["rows"])

    if ufc_only:
        from fightiq.bot_flow import TicketBuilder

        rows_ev = TicketBuilder(client).list_events(ufc_only=True, limit=limit)
    else:
        rows_ev = client.upcoming_events(first=limit)
    rows = [
        {
            "pk": e.pk,
            "name": e.name,
            "date": e.date,
            "promotion": e.promotion,
            "label": e.label(),
        }
        for e in rows_ev
    ]
    _events_cache["key"] = key
    _events_cache["rows"] = rows
    _events_cache["at"] = now
    return jsonify(rows)


@app.get("/api/card")
def card():
    pk_raw = (request.args.get("pk") or "").strip()
    q = (request.args.get("q") or "").strip()
    if pk_raw.isdigit():
        lookup = pk_raw
        try:
            fights = client.event_fights_by_pk(int(pk_raw))
        except FightOddsError as e:
            return jsonify({"error": str(e)}), 502
    elif q:
        lookup = q
        try:
            fights = client.event_fights_by_name(q)
        except FightOddsError as e:
            return jsonify({"error": str(e)}), 502
    else:
        return jsonify({"error": "missing q (event name or pk)"}), 400
    if not fights:
        return jsonify({"error": f"No fights for '{lookup}'", "fights": []}), 404
    # Seed prop catalog labels + warm prop cache for main-card fights
    for f in fights:
        props.remember_fight(f.slug, f.label(), f.event_name or "")
    warm_n = min(3, len(fights))
    for f in fights[:warm_n]:
        threading.Thread(
            target=props.prefetch,
            args=(f.slug,),
            daemon=True,
            name=f"prefetch-{f.slug[:20]}",
        ).start()
    return jsonify(
        {
            "query": lookup,
            "event_name": fights[0].event_name,
            "event_date": fights[0].event_date,
            "count": len(fights),
            "fights": [_fight_json(f) for f in fights],
            "prefetching": [f.slug for f in fights[:warm_n]],
        }
    )


@app.get("/api/fight/<path:slug>")
def fight(slug: str):
    try:
        f = client.fight_by_slug(slug)
    except FightOddsError as e:
        return jsonify({"error": str(e)}), 404
    board = {
        "fighter1": [
            {"key": k, "label": lab, "american": odd, "formatted": format_american(odd)}
            for k, lab, odd in list_available_markets(f, 1)
        ],
        "fighter2": [
            {"key": k, "label": lab, "american": odd, "formatted": format_american(odd)}
            for k, lab, odd in list_available_markets(f, 2)
        ],
    }
    data = _fight_json(f)
    data["board"] = board
    return jsonify(data)


@app.get("/api/fight/<path:slug>/plays")
def fight_plays(slug: str):
    """
    Full sportsbook-style prop list for a fight.

    Query params:
      sportsbook  – e.g. FanDuel (omit = best across books)
      q           – search string (round, ko, submission, over 2.5, …)
      popular     – 1 = only popular shortlist
      category    – moneyline|totals|distance|method_fight|method_fighter|…
      limit       – default 60
      offset      – pagination
      refresh     – 1 force re-fetch
    """
    sportsbook = (request.args.get("sportsbook") or "").strip() or None
    query = (request.args.get("q") or "").strip() or None
    popular = request.args.get("popular", "0") == "1"
    # When no search and no force-all, show popular shortlist first
    show_all = request.args.get("all", "0") == "1"
    if not query and not show_all and request.args.get("popular") is None:
        popular = True
    category = (request.args.get("category") or "").strip() or None
    try:
        limit = int(request.args.get("limit", 80))
    except ValueError:
        limit = 80
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    force = request.args.get("refresh", "0") == "1"
    # Fast path: best across books (popular feed). Full books if filtering a sportsbook.
    need_books = bool(sportsbook)

    try:
        catalog = props.get_catalog(slug, force=force, with_books=need_books)
        # If we only had a best-odds cache and client just picked a book, upgrade.
        if need_books and not catalog.with_books:
            catalog = props.get_catalog(slug, force=True, with_books=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    matched = catalog.filter(
        sportsbook=sportsbook,
        query=query,
        popular_only=popular and not query,
        category=category,
        limit=None,
        offset=0,
    )
    total = len(matched)
    page = matched[offset : offset + limit]

    # category counts for UI chips
    cats: dict[str, int] = {}
    for p in catalog.filter(sportsbook=sportsbook, query=query, popular_only=False):
        cats[p.category] = cats.get(p.category, 0) + 1

    return jsonify(
        {
            "fight_slug": catalog.fight_slug,
            "fight_label": catalog.fight_label,
            "event_name": catalog.event_name,
            "sportsbook": sportsbook,
            "query": query,
            "popular_only": popular and not query,
            "total": total,
            "total_all": len(catalog.plays),
            "offset": offset,
            "limit": limit,
            "categories": cats,
            "sportsbooks": catalog.sportsbooks,
            "with_books": catalog.with_books,
            "plays": [p.to_dict() for p in page],
        }
    )


@app.post("/api/price")
def price():
    """
    Price either:
      { fight_slug, fighter, market }  legacy short markets
      { fight_slug, play_id, sportsbook? } full prop catalog
    """
    body = request.get_json(force=True, silent=True) or {}
    slug = (body.get("fight_slug") or "").strip()
    play_id = (body.get("play_id") or "").strip()
    sportsbook = (body.get("sportsbook") or "").strip() or None

    if play_id and slug:
        try:
            # Need books only when sportsbook filter is set
            catalog = props.get_catalog(slug, with_books=bool(sportsbook))
            if sportsbook and not catalog.with_books:
                catalog = props.get_catalog(slug, force=True, with_books=True)
            play = catalog.get(play_id, sportsbook=sportsbook)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        if not play or play.american is None:
            return jsonify({"error": f"No price for play {play_id}"}), 404
        return jsonify(
            {
                "description": f"{play.label} ({catalog.fight_label})",
                "fight": catalog.fight_label,
                "fight_slug": slug,
                "play_id": play.id,
                "label": play.label,
                "market": play.offer_type_id,
                "american": play.american,
                "formatted": format_american(play.american),
                "source": f"book:{sportsbook}" if sportsbook else "best",
                "sportsbook": sportsbook,
                "books": play.books,
            }
        )

    fighter = (body.get("fighter") or "").strip()
    market = (body.get("market") or "ml").strip()
    if not slug or not fighter:
        return jsonify({"error": "fight_slug + (play_id | fighter+market) required"}), 400
    try:
        fight = client.fight_by_slug(slug)
        leg = resolve_leg(client, fight, fighter, market, refresh=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(
        {
            "description": leg.selection.description,
            "fight": leg.selection.fight_label,
            "fight_slug": leg.selection.fight_slug,
            "fighter": fight.fighter_name(leg.selection.corner),
            "market": leg.selection.market_key,
            "american": leg.american,
            "formatted": format_american(leg.american),
            "source": leg.source,
            "sportsbook": leg.sportsbook,
        }
    )


@app.post("/api/combine")
def combine():
    body = request.get_json(force=True, silent=True) or {}
    legs = body.get("legs") or []
    americans = []
    for leg in legs:
        if isinstance(leg, dict):
            a = leg.get("american")
        else:
            a = leg
        if a is None:
            continue
        americans.append(int(a))
    if not americans:
        return jsonify({"error": "no legs"}), 400
    try:
        if len(americans) == 1:
            a = americans[0]
            result = {
                "legs_american": americans,
                "combined_american": a,
                "combined_decimal": float(american_to_decimal(a)),
                "implied_prob": float(imply_prob(a)),
            }
        else:
            result = combine_parlay(americans)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    result["formatted"] = format_american(result["combined_american"])
    return jsonify(result)


# ---- Generate betslip (QuickPick / Gambly) ----------------------------------


@app.get("/api/betslip/providers")
def betslip_providers():
    return jsonify(
        {
            "quickpick": {
                "configured": quickpick.configured,
                "website": True,
                "turnstile_site_key": TURNSTILE_SITE_KEY,
                "home": QUICKPICK_HOME,
                "label": "QuickPick (Pikkit)",
            },
            "gambly": {
                "configured": gambly.configured,
                "home": GAMBLY_CHAT,
                "label": "Gambly",
            },
            "playbook": {
                "configured": False,
                "home": PLAYBOOK_HOME,
                "label": "Playbook",
            },
        }
    )


@app.post("/api/betslip/quickpick/create")
def quickpick_create():
    """
    Step 1 — website bot create (same as quickpick.pikkit.com Network tab).
    Body: { text | legs, turnstile_token }
    """
    body = request.get_json(force=True, silent=True) or {}
    legs = body.get("legs") or []
    combined = body.get("combined")
    text = (body.get("text") or "").strip()
    if not text and legs:
        text = format_legs_for_bot(legs, combined=combined)
    if not text:
        return jsonify({"error": "text or legs required"}), 400
    token = (
        body.get("turnstile_token")
        or body.get("turnstileToken")
        or body.get("token")
        or ""
    ).strip()

    # Prefer public website flow when Turnstile token present
    if token:
        result = quickpick.create_website(text, token)
        if not result:
            return jsonify({"error": "create failed", "provider": "quickpick"}), 502
        if result.get("status") == "error":
            return jsonify(
                {
                    "provider": "quickpick",
                    "mode": "error",
                    "error": result.get("error") or "create_failed",
                    "message": result.get("message") or "QuickPick create failed",
                    "text": text,
                }
            ), 502
        rid = result.get("requestID") or result.get("request_id")
        if not rid:
            return jsonify(
                {
                    "error": "no_request_id",
                    "message": "QuickPick create returned no requestID",
                    "raw": result,
                    "text": text,
                }
            ), 502
        return jsonify(
            {
                "provider": "quickpick",
                "mode": "website",
                "request_id": rid,
                "text": text,
                "status": "pending",
            }
        )

    # Fallback: external API key path (no turnstile) — create already polls
    if quickpick.configured:
        result = quickpick.create_betslip(text)
        if result and result.get("status") == "complete" and result.get("link"):
            return jsonify(
                {
                    "provider": "quickpick",
                    "mode": "api",
                    "status": "complete",
                    "link": result.get("link"),
                    "text": text,
                    "done": True,
                }
            )
        return jsonify(
            {
                "error": "api_failed",
                "message": "QuickPick external API did not complete",
                "text": text,
            }
        ), 502

    return jsonify(
        {
            "error": "turnstile_required",
            "message": "Pass turnstile_token for website QuickPick (or set QUICKPICK_API_KEY)",
            "turnstile_site_key": TURNSTILE_SITE_KEY,
            "text": text,
        }
    ), 400


@app.get("/api/betslip/quickpick/status")
def quickpick_status():
    """
    Step 2 — poll website get?request_id=
    Returns: { status, link?, message?, found? }
    """
    rid = (request.args.get("request_id") or "").strip()
    if not rid:
        return jsonify({"error": "request_id required"}), 400
    data = quickpick.get_website_status(rid) or {}
    status = str(data.get("status") or "pending").lower()
    link = data.get("link") or data.get("betslip_link") or data.get("url")
    return jsonify(
        {
            "provider": "quickpick",
            "request_id": rid,
            "status": status,
            "found": data.get("found"),
            "link": link,
            "message": data.get("message"),
            "complete": status == "complete" and bool(link),
            "raw": data,
        }
    )


@app.post("/api/betslip/generate")
def betslip_generate():
    """
    Turn a FightIQ ticket into a shareable betslip link.

    Body:
      { provider: "quickpick" | "gambly" | "playbook",
        text?: str,
        legs?: [...],
        combined?: str,
        turnstile_token?: str }  # for QuickPick website flow
    """
    body = request.get_json(force=True, silent=True) or {}
    provider = (body.get("provider") or "quickpick").strip().lower()
    if provider not in {"quickpick", "gambly", "playbook"}:
        return jsonify(
            {"error": "provider must be quickpick, gambly, or playbook"}
        ), 400

    legs = body.get("legs") or []
    combined = body.get("combined")
    text = (body.get("text") or "").strip()
    if not text and legs:
        text = format_legs_for_bot(legs, combined=combined)
    if not text:
        return jsonify({"error": "text or legs required"}), 400

    bot_text = text
    if legs:
        bot_text = format_legs_for_bot(legs, combined=combined) or text

    if provider == "playbook":
        return jsonify(
            {
                "provider": "playbook",
                "mode": "error",
                "needs_key": False,
                "text": bot_text,
                "open_url": PLAYBOOK_HOME,
                "message": (
                    "Playbook has no public API for remote generation — "
                    "use QuickPick for full inline results."
                ),
                "error": "playbook_no_api",
            }
        )

    if provider == "quickpick":
        token = (
            body.get("turnstile_token")
            or body.get("turnstileToken")
            or body.get("token")
            or ""
        ).strip()

        # 1) Website bot (create + wait) — mirrors DevTools Network tab
        if token:
            result = quickpick.create_betslip_website(bot_text, token)
            if result and result.get("status") == "complete" and result.get("link"):
                return jsonify(
                    {
                        "provider": "quickpick",
                        "mode": "website",
                        "status": "complete",
                        "link": result["link"],
                        "message": result.get("message"),
                        "request_id": result.get("request_id"),
                        "text": bot_text,
                    }
                )
            return jsonify(
                {
                    "provider": "quickpick",
                    "mode": "error",
                    "error": (result or {}).get("error") or "incomplete",
                    "message": (result or {}).get("message")
                    or "QuickPick website did not return a link",
                    "text": bot_text,
                    "raw": result,
                }
            ), 502

        # 2) External API key
        if quickpick.configured:
            try:
                result = quickpick.create_betslip(bot_text)
            except Exception as e:
                return jsonify(
                    {
                        "provider": "quickpick",
                        "mode": "error",
                        "error": str(e),
                        "text": bot_text,
                        "message": f"QuickPick error: {e}",
                    }
                ), 502
            if not result or result.get("status") != "complete":
                return jsonify(
                    {
                        "provider": "quickpick",
                        "mode": "error",
                        "text": bot_text,
                        "message": "QuickPick API did not return a completed slip",
                        "raw_status": (result or {}).get("status"),
                        "error": "incomplete",
                    }
                ), 502
            link = (
                result.get("link")
                or result.get("betslip_link")
                or result.get("url")
                or result.get("shareUrl")
            )
            if not link:
                return jsonify(
                    {
                        "provider": "quickpick",
                        "mode": "error",
                        "text": bot_text,
                        "message": "QuickPick complete but no link",
                        "error": "no_link",
                    }
                ), 502
            return jsonify(
                {
                    "provider": "quickpick",
                    "mode": "api",
                    "status": "complete",
                    "link": link,
                    "text": bot_text,
                }
            )

        return jsonify(
            {
                "provider": "quickpick",
                "mode": "error",
                "needs_turnstile": True,
                "turnstile_site_key": TURNSTILE_SITE_KEY,
                "text": bot_text,
                "message": (
                    "QuickPick website flow needs a Turnstile token "
                    "(browser will supply it). Refresh if captcha didn’t load."
                ),
                "error": "turnstile_required",
            }
        ), 400

    # Gambly
    if not gambly.configured:
        return jsonify(
            {
                "provider": "gambly",
                "mode": "error",
                "needs_key": True,
                "env_key": "UNABATED_API_KEY",
                "text": bot_text,
                "open_url": GAMBLY_CHAT,
                "message": (
                    "Gambly/Unabated API key missing. Add UNABATED_API_KEY "
                    "(or GAMBLY_API_KEY) to FightIQ .env for inline generation."
                ),
                "error": "missing_api_key",
            }
        )
    try:
        result = gambly.create_betslip(bot_text)
    except Exception as e:
        return jsonify(
            {
                "provider": "gambly",
                "mode": "error",
                "error": str(e),
                "text": bot_text,
                "message": f"Gambly error: {e}",
            }
        ), 502
    if not result or str(result.get("status", "")).lower() in {
        "error",
        "failed",
        "timeout",
    }:
        return jsonify(
            {
                "provider": "gambly",
                "mode": "error",
                "text": bot_text,
                "open_url": GAMBLY_CHAT,
                "message": (result or {}).get("error")
                or "Gambly did not finish generating a slip",
                "error": "incomplete",
            }
        ), 502
    link = extract_gambly_link(result)
    if not link:
        return jsonify(
            {
                "provider": "gambly",
                "mode": "error",
                "text": bot_text,
                "message": "Gambly complete but no share/deeplink found",
                "raw_keys": list((result or {}).keys()),
                "error": "no_link",
            }
        ), 502
    return jsonify(
        {
            "provider": "gambly",
            "mode": "api",
            "status": result.get("status"),
            "link": link,
            "text": bot_text,
        }
    )


# ---- Slip parse / store / grade --------------------------------------------


@app.get("/api/slips/status")
def slips_status():
    return jsonify(
        {
            "quickpick_configured": quickpick.configured,
            "store": str(store.db_path),
            "open_count": len(store.open_slips()),
        }
    )


@app.post("/api/slips/parse")
def slips_parse():
    """
    Parse slip text.
    Body: { text, use_quickpick?: bool, save?: bool }
    """
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    use_qp = bool(body.get("use_quickpick", True))
    save = bool(body.get("save", True))

    parsed = parse_slip_text(text)
    qp_result = None
    if use_qp and quickpick.configured:
        qp_result = quickpick.create_betslip(text)
        if qp_result and qp_result.get("status") == "complete":
            parsed = parse_quickpick_payload(qp_result, text)
        elif use_qp:
            parsed.notes.append(
                "QuickPick unavailable/failed — used local parser"
            )

    payload = parsed.to_dict()
    # Don't dump huge raw
    if parsed.quickpick_raw:
        payload["quickpick_raw_keys"] = list(parsed.quickpick_raw.keys())
    slip_id = None
    if save and parsed.legs:
        slip_id = store.save_parsed(payload | {"quickpick_raw": parsed.quickpick_raw})
        payload["slip_id"] = slip_id
        payload["saved"] = True
    else:
        payload["saved"] = False
    payload["quickpick_configured"] = quickpick.configured
    return jsonify(payload)


@app.get("/api/slips")
def slips_list():
    status = request.args.get("status")
    limit = min(int(request.args.get("limit", 40)), 100)
    return jsonify(store.list_slips(limit=limit, status=status))


@app.get("/api/slips/<slip_id>")
def slips_get(slip_id: str):
    slip = store.get_slip(slip_id)
    if not slip:
        return jsonify({"error": "not found"}), 404
    return jsonify(slip)


@app.post("/api/slips/<slip_id>/grade")
def slips_grade_one(slip_id: str):
    try:
        result = grader.grade_slip(slip_id)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(result)


@app.post("/api/slips/grade-open")
def slips_grade_open():
    try:
        results = grader.grade_all_open()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"graded": len(results), "results": results})


@app.get("/api/espn/scoreboard")
def espn_scoreboard():
    """Debug / preview ESPN UFC results feed."""
    try:
        rows = EspnMmaClient().results_window(days_back=7, days_forward=2)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(
        [
            {
                "event": r.event_name,
                "date": r.event_date,
                "f1": r.fighter1,
                "f2": r.fighter2,
                "winner": r.winner,
                "method": r.method,
                "round": r.round,
                "time": r.time,
                "completed": r.completed,
            }
            for r in rows[:80]
        ]
    )


def _autograde_loop(interval: int = 300) -> None:
    while True:
        try:
            open_n = len(store.open_slips())
            if open_n:
                app.logger.info("Autograde: %s open slips", open_n)
                grader.grade_all_open()
        except Exception as e:
            app.logger.exception("Autograde error: %s", e)
        time.sleep(max(60, interval))


def main():
    if os.environ.get("FIGHTIQ_AUTOGRADE", "1") != "0":
        t = threading.Thread(
            target=_autograde_loop,
            kwargs={"interval": int(os.environ.get("FIGHTIQ_GRADE_INTERVAL", "300"))},
            daemon=True,
            name="fightiq-autograde",
        )
        t.start()
        print("Autograde thread started (every 5m for open slips)", flush=True)
    app.run(host="0.0.0.0", port=9999, debug=False, threaded=True)


if __name__ == "__main__":
    main()
