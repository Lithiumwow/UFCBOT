"""
Embed builders shared between the bet-logging cog and the persistent button
views (kept separate to avoid circular imports between cogs/bets.py and
views.py).

Every builder here takes an explicit `unit_value` and `currency` for the
bets' owner (all bets passed to build_results_embed always belong to one
user, since every caller already scopes queries by user_id) -- see
betting_math.py for why these are passed in rather than read from globals.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import discord

from betting_math import (
    bet_potential_win_native,
    bet_profit_native,
    bet_stake_native,
    format_odds,
    format_odds_with_alt,
    format_native_with_usd,
)
from branding import apply_event_logo, brand_color, brand_label, event_brand
from bet_types import categorize_bet, effective_legs
from card_data import infer_fighter_from_text, resolve_fighter_on_card


STATUS_COLOR = {
    "pending": discord.Color.gold(),
    "won": discord.Color.brand_green(),
    "loss": discord.Color.brand_red(),
    "void": discord.Color.light_grey(),
}

STATUS_LABEL = {
    "pending": "⏳ Pending",
    "won": "✅ Won",
    "loss": "❌ Loss",
    "void": "➖ Void",
}

STATUS_EMOJI = {
    "pending": "🥊",
    "won": "🏆",
    "loss": "💀",
    "void": "🔄",
}

LEG_EMOJI = {
    "won": "✅",
    "loss": "❌",
    "void": "⏸️",
    "pending": "⏳",
}


def _collab_member_ids(bet: dict[str, Any], structured: list[dict[str, Any]] | None) -> list[int]:
    """Host, partner, then anyone else who added a leg — unique, in order."""
    ids: list[int] = []
    for uid in (bet.get("user_id"), bet.get("co_user_id")):
        if uid and uid not in ids:
            ids.append(uid)
    for leg in structured or []:
        uid = leg.get("added_by")
        if uid and uid not in ids:
            ids.append(uid)
    return ids


def collect_card_user_ids(
    bets: list[dict[str, Any]],
    legs_by_bet_id: dict[int, list[dict[str, Any]]] | None = None,
) -> set[int]:
    """User ids needed to label collab members / per-play authors on /card."""
    ids: set[int] = set()
    legs_map = legs_by_bet_id or {}
    for b in bets:
        if b.get("user_id"):
            ids.add(b["user_id"])
        if b.get("co_user_id"):
            ids.add(b["co_user_id"])
        for leg in legs_map.get(b["id"], []):
            if leg.get("added_by"):
                ids.add(leg["added_by"])
    return ids


def _description_has_matchup(text: str) -> bool:
    return bool(re.search(r"\bvs\.?\b", text or "", re.I))


def _card_leg_text(
    description: str,
    *,
    fighter_pick: str | None = None,
    fights: list | None = None,
) -> str:
    """On /card, only add the matchup when the line names no fighter.

    "Charles Johnson to Win in Rd 3" stays as-is.
    "Fight ends in Round 2" becomes "A vs B · Fight ends in Round 2".
    """
    desc = (description or "").strip()
    if not desc or not fights or _description_has_matchup(desc):
        return desc
    if infer_fighter_from_text(desc, fights):
        return desc

    hit = resolve_fighter_on_card(
        fighter_pick=fighter_pick, description=desc, fights=fights
    )
    if not hit:
        return desc
    return f"{hit[2]} · {desc}"


def _leg_lines(
    bets: list[dict[str, Any]],
    unit_value: float,
    *,
    singular_label: str | None = None,
    legs_by_bet_id: dict[int, list[dict[str, Any]]] | None = None,
    member_names: dict[int, str] | None = None,
    fights: list | None = None,
) -> list[str]:
    """
    Straight / Prop: compact single line.
    Parlay / Collab: numbered block with each leg on its own line.
    Collab headers list members (Collab 1 Miller/Lithiumwow) and each play
    is tagged with who picked it.
    """
    lines = []
    counter = 0
    legs_map = legs_by_bet_id or {}
    names = member_names or {}
    for b in bets:
        structured = legs_map.get(b["id"])
        if structured is not None:
            structured_legs = [
                leg for leg in effective_legs(b, structured)
                if (leg.get("description") or "").strip()
            ]
            legs = [(leg.get("description") or "").strip() for leg in structured_legs]
        else:
            structured_legs = None
            legs = _get_legs(b)
        # Recaps (/card, /results) always show American odds, even if the
        # user entered decimal on the slip itself.
        odds_str = format_odds(b.get("odds"), "american")
        units = bet_stake_native(b, unit_value) / unit_value
        emoji = LEG_EMOJI.get(b.get("status"), "❔")
        is_collab = bool(b.get("is_collab") or b.get("co_user_id"))

        if is_collab and singular_label:
            counter += 1
            member_ids = _collab_member_ids(b, structured_legs)
            name_bit = "/".join(names[uid] for uid in member_ids if uid in names)
            header = f"**Collab {counter}**" if not name_bit else f"**Collab {counter} {name_bit}**"
            play_lines = []
            rows = structured_legs if structured_legs is not None else [
                {"description": desc} for desc in legs
            ]
            for row in rows:
                desc = (row.get("description") or "").strip()
                if not desc:
                    continue
                desc = _card_leg_text(
                    desc,
                    fighter_pick=row.get("fighter_pick") or b.get("fighter_pick"),
                    fights=fights,
                )
                who = names.get(row.get("added_by")) if row.get("added_by") else None
                play_lines.append(f"▸ {desc} - {who}" if who else f"▸ {desc}")
            block = "\n".join(play_lines) if play_lines else "_No plays_"
            lines.append(f"{header}\n{block}\n`{odds_str}`  ·  {units:g}u  {emoji}")
        elif len(legs) > 1 and singular_label:
            counter += 1
            prefix = "🤝 " if is_collab else ""
            play_lines = []
            rows = structured_legs if structured_legs is not None else [
                {"description": desc} for desc in legs
            ]
            for row in rows:
                desc = (row.get("description") or "").strip()
                if not desc:
                    continue
                desc = _card_leg_text(
                    desc,
                    fighter_pick=row.get("fighter_pick") or b.get("fighter_pick"),
                    fights=fights,
                )
                play_lines.append(f"▸ {desc}")
            leg_block = "\n".join(play_lines)
            lines.append(
                f"**{prefix}{singular_label} {counter}**\n{leg_block}\n"
                f"`{odds_str}`  ·  {units:g}u  {emoji}"
            )
        else:
            if structured_legs:
                parts = [
                    _card_leg_text(
                        (leg.get("description") or "").strip(),
                        fighter_pick=leg.get("fighter_pick") or b.get("fighter_pick"),
                        fights=fights,
                    )
                    for leg in structured_legs
                    if (leg.get("description") or "").strip()
                ]
                label = " + ".join(parts) if len(parts) > 1 else (parts[0] if parts else "Untitled bet")
            else:
                label = (
                    " + ".join(legs)
                    if len(legs) > 1
                    else (legs[0] if legs else "Untitled bet")
                )
                label = _card_leg_text(
                    label,
                    fighter_pick=b.get("fighter_pick"),
                    fights=fights,
                )
            if is_collab:
                label = f"🤝 {label}"
            lines.append(f"▸ {label}  `{odds_str}`  ·  {units:g}u  {emoji}")
    return lines


def _chunk_lines(lines: list[str], max_len: int = 1000, sep: str = "\n") -> list[str]:
    """Join lines/blocks with `sep` between them, splitting into multiple
    chunks if the combined text would exceed a single embed field's
    character limit."""
    chunks: list[str] = []
    current_lines: list[str] = []
    for line in lines:
        candidate = current_lines + [line]
        if len(sep.join(candidate)) > max_len and current_lines:
            chunks.append(sep.join(current_lines))
            current_lines = [line]
        else:
            current_lines = candidate
    if current_lines:
        chunks.append(sep.join(current_lines))
    return chunks


def _get_legs(bet: dict[str, Any]) -> list[str]:
    raw = bet.get("bet_title") or ""
    return [leg.strip() for leg in raw.split("\n") if leg.strip()]


def _leg_category_singular(n: int) -> str:
    """For a single bet slip's own title (e.g. 'Parlay' not 'Parlays')."""
    if n == 2:
        return "2 Leg"
    return "Parlay"


# Match /spread-sheet sections
CARD_CATEGORY_ORDER = ["Straight Pick", "Prop", "Parlay", "Collab"]
CARD_CATEGORY_HEADERS = {
    "Straight Pick": "Straight Picks",
    "Prop": "Prop Picks",
    "Parlay": "Parlays",
    "Collab": "Collab Slips",
}
CARD_CATEGORY_SINGULAR = {
    "Straight Pick": None,
    "Prop": None,
    "Parlay": "Parlay",
    "Collab": "Collab",
}


def _card_category(
    bet: dict[str, Any],
    legs: list[dict[str, Any]] | None = None,
) -> str:
    if bet.get("is_collab") or bet.get("co_user_id") is not None:
        return "Collab"
    return categorize_bet(bet, legs)


def _stake_block(
    *,
    units: float,
    odds: Optional[int],
    unit_value: float,
    currency: str,
    odds_format: str | None,
    bet: dict[str, Any],
) -> tuple[str, str, str]:
    """Returns (stake_text, odds_text, result_text) for one person's numbers."""
    view = dict(bet)
    view["units"] = units
    view["odds"] = odds
    stake_native = bet_stake_native(view, unit_value)
    stake = (
        f"**{units:g} u**\n{format_native_with_usd(stake_native, currency)}"
    )
    odds_text = f"**{format_odds_with_alt(odds, odds_format)}**"
    status = (bet.get("status") or "pending").lower()
    if status == "pending":
        potential = bet_potential_win_native(view, unit_value)
        result = (
            f"**{potential / unit_value:+.2f} u**  ·  "
            f"{format_native_with_usd(potential, currency, signed=True)}"
        )
    elif status in ("won", "loss"):
        profit = bet_profit_native(view, unit_value)
        result = (
            f"**{profit / unit_value:+.2f} u**  ·  "
            f"{format_native_with_usd(profit, currency, signed=True)}"
        )
    else:
        result = "—"
    return stake, odds_text, result


def build_bet_embed(
    bet: dict[str, Any],
    *,
    unit_value: float,
    currency: str,
    user: Optional[discord.abc.User] = None,
    co_user: Optional[discord.abc.User] = None,
    co_unit_value: Optional[float] = None,
    co_currency: Optional[str] = None,
) -> discord.Embed:
    status = bet.get("status", "pending")
    units = bet.get("units", 1.0)
    legs = _get_legs(bet)

    is_parlay = len(legs) > 1
    if len(legs) <= 1:
        title_text = legs[0] if legs else "Untitled Bet"
    else:
        title_text = f"{_leg_category_singular(len(legs))} · {len(legs)} legs"

    if bet.get("is_collab"):
        title_text = f"🤝 Collab · {title_text}"

    embed = discord.Embed(
        title=f"{STATUS_EMOJI.get(status, '🥊')}  {title_text}",
        description=f"🗓️ **{bet.get('event') or 'Event TBD'}**",
        color=STATUS_COLOR.get(status, brand_color(bet.get("event"))),
    )

    if user is not None and co_user is not None:
        embed.set_author(
            name=f"{user.display_name} + {co_user.display_name} · Collab Slip",
            icon_url=user.display_avatar.url,
        )
    elif user is not None:
        embed.set_author(
            name=f"{user.display_name} · Bet Slip",
            icon_url=user.display_avatar.url,
        )

    # Event brand logo (UFC / DWCS) — not the user avatar
    apply_event_logo(embed, bet.get("event"))
    embed.set_footer(
        text=f"{brand_label(bet.get('event'))}  ·  Bet #{bet['id']}"
        + (f"  •  Logged {(bet.get('created_at') or '')[:10]}" if bet.get("created_at") else "")
    )

    if is_parlay:
        legs_value = "\n".join(f"**{i}.** {leg}" for i, leg in enumerate(legs, start=1))
        embed.add_field(name="🦵 Legs", value=legs_value, inline=False)

    host_stake, host_odds, host_result = _stake_block(
        units=units,
        odds=bet.get("odds"),
        unit_value=unit_value,
        currency=currency,
        odds_format=bet.get("odds_format"),
        bet=bet,
    )

    show_partner = (
        co_user is not None
        and (bet.get("is_collab") or bet.get("co_user_id") is not None)
        and bet.get("partner_units") is not None
    )
    if show_partner:
        host_name = user.display_name if user is not None else "Host"
        partner_name = co_user.display_name
        p_unit = co_unit_value if co_unit_value is not None else unit_value
        p_cur = co_currency or currency
        partner_stake, partner_odds, partner_result = _stake_block(
            units=bet.get("partner_units") or 0,
            odds=bet.get("partner_odds"),
            unit_value=p_unit,
            currency=p_cur,
            odds_format=bet.get("partner_odds_format") or bet.get("odds_format"),
            bet=bet,
        )
        embed.add_field(name=f"💰 {host_name}", value=f"{host_stake}\n{host_odds}", inline=True)
        embed.add_field(name=f"💰 {partner_name}", value=f"{partner_stake}\n{partner_odds}", inline=True)
        embed.add_field(name="📌 Status", value=f"**{STATUS_LABEL.get(status, status)}**", inline=True)
        if status == "pending":
            embed.add_field(
                name="🎯 Potential Win",
                value=f"**{host_name}**  {host_result}\n**{partner_name}**  {partner_result}",
                inline=False,
            )
        elif status in ("won", "loss"):
            embed.add_field(
                name="💵 Net Result",
                value=f"**{host_name}**  {host_result}\n**{partner_name}**  {partner_result}",
                inline=False,
            )
    else:
        embed.add_field(name="💰 Stake", value=host_stake, inline=True)
        embed.add_field(name="📈 Odds", value=host_odds, inline=True)
        embed.add_field(name="📌 Status", value=f"**{STATUS_LABEL.get(status, status)}**", inline=True)
        if status == "pending":
            embed.add_field(name="🎯 Potential Win", value=host_result, inline=False)
        elif status in ("won", "loss"):
            embed.add_field(name="💵 Net Result", value=host_result, inline=False)

    return embed


def _weekly_lines(bets: list[dict[str, Any]], unit_value: float, currency: str, max_weeks: int = 8) -> list[str]:
    import datetime
    from collections import defaultdict

    buckets: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for b in bets:
        if b["status"] not in ("won", "loss"):
            continue
        created = b.get("created_at") or ""
        try:
            dt = datetime.datetime.fromisoformat(created)
        except ValueError:
            continue
        iso_year, iso_week, _ = dt.isocalendar()
        buckets[(iso_year, iso_week)].append(b)

    lines = []
    for (iso_year, iso_week) in sorted(buckets.keys(), reverse=True)[:max_weeks]:
        week_bets = buckets[(iso_year, iso_week)]
        won = [b for b in week_bets if b["status"] == "won"]
        loss = [b for b in week_bets if b["status"] == "loss"]
        net_native = sum(bet_profit_native(b, unit_value) for b in week_bets)
        week_start = datetime.date.fromisocalendar(iso_year, iso_week, 1)  # Monday
        label = f"Wk of {week_start:%b %d}"
        lines.append(
            f"▸ **{label}**  ·  {len(won)}-{len(loss)}  ·  "
            f"{net_native / unit_value:+.2f}u  ·  {format_native_with_usd(net_native, currency, signed=True)}"
        )
    return lines


def _monthly_lines(bets: list[dict[str, Any]], unit_value: float, currency: str) -> list[str]:
    import datetime
    from collections import defaultdict

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in bets:
        if b["status"] not in ("won", "loss"):
            continue
        created = b.get("created_at") or ""
        month_key = created[:7]  # "YYYY-MM"
        if len(month_key) != 7:
            continue
        buckets[month_key].append(b)

    lines = []
    for month_key in sorted(buckets.keys(), reverse=True):  # most recent first
        month_bets = buckets[month_key]
        won = [b for b in month_bets if b["status"] == "won"]
        loss = [b for b in month_bets if b["status"] == "loss"]
        net_native = sum(bet_profit_native(b, unit_value) for b in month_bets)
        try:
            label = datetime.datetime.strptime(month_key, "%Y-%m").strftime("%b %Y")
        except ValueError:
            label = month_key
        lines.append(
            f"▸ **{label}**  ·  {len(won)}-{len(loss)}  ·  "
            f"{net_native / unit_value:+.2f}u  ·  {format_native_with_usd(net_native, currency, signed=True)}"
        )
    return lines


def _biggest_wins_lines(
    bets: list[dict[str, Any]], unit_value: float, currency: str, top_n: int = 5
) -> list[str]:
    """Top N wins by odds -- the American odds value itself sorts correctly
    here (more positive = bigger underdog = bigger payout, and this holds
    true right across the -100/+100 boundary), so no decimal conversion
    needed. Bets logged with no odds are excluded since there's nothing to
    rank them by."""
    won_with_odds = [b for b in bets if b["status"] == "won" and b.get("odds") is not None]
    if not won_with_odds:
        return []

    top = sorted(won_with_odds, key=lambda b: b["odds"], reverse=True)[:top_n]

    lines = []
    for rank, b in enumerate(top, start=1):
        legs = _get_legs(b)
        label = " + ".join(legs) if len(legs) > 1 else (legs[0] if legs else "Untitled bet")
        odds_str = format_odds(b.get("odds"), "american")
        profit_native = bet_profit_native(b, unit_value)
        event = b.get("event")
        event_suffix = f"  ({event})" if event else ""
        lines.append(
            f"**{rank}.** {label}  `{odds_str}`{event_suffix}\n"
            f"   {format_native_with_usd(profit_native, currency, signed=True)}"
        )
    return lines


def build_results_embed(
    *,
    title: str,
    bets: list[dict[str, Any]],
    unit_value: float,
    currency: str,
    icon_url: Optional[str] = None,
    include_bet_list: bool = False,
    bet_list_limit: Optional[int] = None,
    include_weekly: bool = False,
    include_monthly: bool = False,
    include_biggest_wins: bool = False,
    event: Optional[str] = None,
    legs_by_bet_id: Optional[dict[int, list[dict[str, Any]]]] = None,
    member_names: Optional[dict[int, str]] = None,
    fights: Optional[list] = None,
) -> discord.Embed:
    won = [b for b in bets if b["status"] == "won"]
    loss = [b for b in bets if b["status"] == "loss"]
    pending = [b for b in bets if b["status"] == "pending"]
    # Voids are excluded entirely from stats -- they never happened, betting-wise.
    counted_bets = won + loss + pending

    total_staked_native = sum(bet_stake_native(b, unit_value) for b in counted_bets)
    net_native = sum(bet_profit_native(b, unit_value) for b in won + loss)
    decided = len(won) + len(loss)
    win_rate = (len(won) / decided * 100) if decided else 0.0

    # Prefer explicit event; else infer from title / first bet
    brand_event = event or title
    if not event and bets:
        brand_event = bets[0].get("event") or title

    color = brand_color(brand_event)

    trend = "📈" if net_native > 0 else "📉" if net_native < 0 else "📊"

    embed = discord.Embed(
        title=f"{trend}  {title}",
        color=color,
    )
    apply_event_logo(embed, brand_event)
    if icon_url:
        embed.set_author(name=brand_label(brand_event), icon_url=icon_url)

    # Compact summary block instead of four separate small fields -- easier
    # to scan at a glance.
    summary_lines = [
        f"**Record**\u2003{len(won)}-{len(loss)}  ·  {win_rate:.1f}% win rate",
        f"**Staked**\u2003{total_staked_native / unit_value:.2f}u  ·  "
        f"{format_native_with_usd(total_staked_native, currency)}",
        f"**Net**\u2003{net_native / unit_value:+.2f}u  ·  "
        f"{format_native_with_usd(net_native, currency, signed=True)}",
    ]
    embed.description = "\n".join(summary_lines)

    if include_weekly:
        weekly_lines = _weekly_lines(bets, unit_value, currency)
        if weekly_lines:
            chunks = _chunk_lines(weekly_lines)
            for i, chunk in enumerate(chunks):
                field_name = "Weekly Breakdown" if i == 0 else "Weekly Breakdown (cont.)"
                embed.add_field(name=field_name, value=chunk, inline=False)

    if include_monthly:
        monthly_lines = _monthly_lines(bets, unit_value, currency)
        if monthly_lines:
            chunks = _chunk_lines(monthly_lines)
            for i, chunk in enumerate(chunks):
                field_name = "Monthly Breakdown" if i == 0 else "Monthly Breakdown (cont.)"
                embed.add_field(name=field_name, value=chunk, inline=False)

    if include_biggest_wins:
        biggest_wins_lines = _biggest_wins_lines(bets, unit_value, currency)
        if biggest_wins_lines:
            embed.add_field(
                name="🏅 Biggest Wins (by odds)",
                value="\n".join(biggest_wins_lines),
                inline=False,
            )

    if include_bet_list and bets:
        ordered = sorted(bets, key=lambda b: b["id"])
        list_title = "Bets"
        if bet_list_limit is not None and len(ordered) > bet_list_limit:
            ordered = ordered[-bet_list_limit:]
            list_title = f"Last {bet_list_limit} Plays"

        legs_map = legs_by_bet_id or {}
        grouped: dict[str, list[dict[str, Any]]] = {
            c: [] for c in CARD_CATEGORY_ORDER
        }
        for b in ordered:
            cat = _card_category(b, legs_map.get(b["id"]))
            if cat not in grouped:
                cat = "Prop"
            grouped[cat].append(b)

        present_categories = [c for c in CARD_CATEGORY_ORDER if grouped[c]]
        for idx, category in enumerate(present_categories):
            group_bets = grouped[category]
            if idx > 0:
                # Thin divider between category blocks so they read as
                # clearly separate sections rather than one long list.
                embed.add_field(name="\u200b", value="── ── ── ── ──", inline=False)

            singular = CARD_CATEGORY_SINGULAR.get(category)
            # Multi-leg bets get a blank line between each numbered block so
            # separate parlays don't run into each other; singles stay tight.
            sep = "\n\n" if singular else "\n"
            chunks = _chunk_lines(
                _leg_lines(
                    group_bets,
                    unit_value,
                    singular_label=singular,
                    legs_by_bet_id=legs_map,
                    member_names=member_names,
                    fights=fights,
                ),
                sep=sep,
            )
            header = CARD_CATEGORY_HEADERS.get(category, category)
            for i, chunk in enumerate(chunks):
                field_name = header if i == 0 else f"{header} (cont.)"
                embed.add_field(name=field_name, value=chunk, inline=False)

        if list_title != "Bets":
            embed.set_footer(text=f"Showing {list_title.lower()} out of {len(bets)} logged total.")

    if not counted_bets:
        embed.description = (embed.description or "") + "\n\n*No bets logged yet for this scope.*"

    return embed


def build_pl_embed(
    *,
    title: str,
    bets: list[dict[str, Any]],
    unit_value: float,
    currency: str,
    icon_url: Optional[str] = None,
    include_event_breakdown: bool = False,
    event: Optional[str] = None,
) -> discord.Embed:
    """Focused P/L summary -- record, staked, net, ROI. Optional per-event
    lines when viewing overall."""
    won = [b for b in bets if b["status"] == "won"]
    loss = [b for b in bets if b["status"] == "loss"]
    pending = [b for b in bets if b["status"] == "pending"]
    settled = won + loss

    total_staked = sum(bet_stake_native(b, unit_value) for b in settled + pending)
    settled_staked = sum(bet_stake_native(b, unit_value) for b in settled)
    net_native = sum(bet_profit_native(b, unit_value) for b in settled)
    decided = len(settled)
    win_rate = (len(won) / decided * 100) if decided else 0.0
    roi = (net_native / settled_staked) if settled_staked else 0.0

    brand_event = event
    if not brand_event and bets and not include_event_breakdown:
        brand_event = bets[0].get("event")
    # Overall P/L defaults to UFC branding unless every bet is DWCS
    if not brand_event and bets:
        brands = {event_brand(b.get("event")) for b in bets}
        brand_event = "DWCS" if brands == {"dwcs"} else "UFC"

    if net_native > 0:
        color = discord.Color.brand_green()
        trend = "📈"
    elif net_native < 0:
        color = discord.Color.brand_red()
        trend = "📉"
    else:
        color = brand_color(brand_event)
        trend = "📊"

    embed = discord.Embed(title=f"{trend}  {title}", color=color)
    apply_event_logo(embed, brand_event)
    if icon_url:
        embed.set_author(name=brand_label(brand_event), icon_url=icon_url)

    embed.description = "\n".join(
        [
            f"**Record**\u2003{len(won)}-{len(loss)}"
            + (f"  ·  {len(pending)} pending" if pending else "")
            + f"  ·  {win_rate:.1f}% WR",
            f"**Staked**\u2003{total_staked / unit_value:.2f}u  ·  "
            f"{format_native_with_usd(total_staked, currency)}",
            f"**P/L**\u2003{net_native / unit_value:+.2f}u  ·  "
            f"{format_native_with_usd(net_native, currency, signed=True)}",
            f"**ROI**\u2003{roi:+.0%} on settled action",
        ]
    )

    if include_event_breakdown and settled:
        from collections import defaultdict

        by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for b in settled:
            by_event[b.get("event") or "No event"].append(b)

        lines: list[str] = []
        # Sort events by most recent bet id so freshest cards sit on top.
        sorted_events = sorted(
            by_event.items(),
            key=lambda kv: max(b["id"] for b in kv[1]),
            reverse=True,
        )
        for event_name, event_bets in sorted_events[:12]:
            e_won = sum(1 for b in event_bets if b["status"] == "won")
            e_loss = sum(1 for b in event_bets if b["status"] == "loss")
            e_net = sum(bet_profit_native(b, unit_value) for b in event_bets)
            lines.append(
                f"▸ **{event_name}**  ·  {e_won}-{e_loss}  ·  "
                f"{e_net / unit_value:+.2f}u  ·  "
                f"{format_native_with_usd(e_net, currency, signed=True)}"
            )
        if lines:
            chunks = _chunk_lines(lines)
            for i, chunk in enumerate(chunks):
                name = "By Event" if i == 0 else "By Event (cont.)"
                embed.add_field(name=name, value=chunk, inline=False)
            if len(sorted_events) > 12:
                embed.set_footer(text=f"Showing last 12 events of {len(sorted_events)}.")

    if not bets:
        embed.description = "*No bets logged yet for this scope.*"

    return embed


BETSLIP_FIELD_NAME = "🔗 Betslip"
_BETSLIP_DESC_MARK = "**Betslip**"


def with_betslip_links(embed: discord.Embed, links: list[str]) -> discord.Embed:
    """Copy a /card embed and attach betslip links without adding extra fields.

    /card recaps already use many fields (Discord max 25). A new field is
    often dropped, so the URL goes on the title + description instead.
    """
    out = discord.Embed.from_dict(embed.to_dict())
    cleaned = [u.strip() for u in links if (u or "").strip().startswith("http")]
    if not cleaned:
        return out

    # Title becomes clickable.
    if not out.url:
        out.url = cleaned[0]

    desc = (out.description or "").strip()
    # Drop a prior betslip block so re-runs don't stack.
    if _BETSLIP_DESC_MARK in desc:
        desc = desc.split(_BETSLIP_DESC_MARK, 1)[0].rstrip()
    link_block = _BETSLIP_DESC_MARK + "\n" + "\n".join(cleaned[:5])
    combined = f"{desc}\n\n{link_block}".strip() if desc else link_block
    out.description = combined[:4096]
    return out