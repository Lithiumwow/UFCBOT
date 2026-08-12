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

from typing import Any, Optional

import discord

from betting_math import (
    bet_potential_win_native,
    bet_profit_native,
    bet_stake_native,
    format_odds,
    format_native_with_usd,
)
from branding import apply_event_logo, brand_color, brand_label, event_brand


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


def _leg_lines(
    bets: list[dict[str, Any]], unit_value: float, singular_label: str | None = None
) -> list[str]:
    """
    ML (1 leg): compact single line, as before.
    Multi-leg bets (2 Legs / Parlays): numbered block with each leg on its
    own line, so a 3-leg parlay doesn't run together as one wrapped line.
    """
    lines = []
    counter = 0
    for b in bets:
        legs = _get_legs(b)
        odds_str = format_odds(b.get("odds"))
        units = bet_stake_native(b, unit_value) / unit_value
        emoji = LEG_EMOJI.get(b.get("status"), "❔")

        if len(legs) > 1 and singular_label:
            counter += 1
            prefix = "🤝 " if b.get("is_collab") else ""
            leg_block = "\n".join(f"▸ {leg}" for leg in legs)
            lines.append(
                f"**{prefix}{singular_label} {counter}**\n{leg_block}\n`{odds_str}`  ·  {units:g}u  {emoji}"
            )
        else:
            label = " + ".join(legs) if len(legs) > 1 else (legs[0] if legs else "Untitled bet")
            if b.get("is_collab"):
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


def _leg_category(n: int) -> str:
    if n <= 1:
        return "ML"
    if n == 2:
        return "2 Legs"
    return "Parlays"


def _leg_category_singular(n: int) -> str:
    """For a single bet slip's own title (e.g. 'Parlay' not 'Parlays')."""
    if n == 2:
        return "2 Leg"
    return "Parlay"


CATEGORY_ORDER = ["ML", "2 Legs", "Parlays"]


def build_bet_embed(
    bet: dict[str, Any],
    *,
    unit_value: float,
    currency: str,
    user: Optional[discord.abc.User] = None,
    co_user: Optional[discord.abc.User] = None,
) -> discord.Embed:
    status = bet.get("status", "pending")
    units = bet.get("units", 1.0)
    stake_native = bet_stake_native(bet, unit_value)
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

    embed.add_field(
        name="💰 Stake",
        value=f"**{units:g} u**\n{format_native_with_usd(stake_native, currency)}",
        inline=True,
    )
    embed.add_field(name="📈 Odds", value=f"**{format_odds(bet.get('odds'))}**", inline=True)
    embed.add_field(name="📌 Status", value=f"**{STATUS_LABEL.get(status, status)}**", inline=True)

    if status == "pending":
        potential_native = bet_potential_win_native(bet, unit_value)
        embed.add_field(
            name="🎯 Potential Win",
            value=f"**{potential_native / unit_value:+.2f} u**  ·  "
            f"{format_native_with_usd(potential_native, currency, signed=True)}",
            inline=False,
        )
    elif status in ("won", "loss"):
        profit_native = bet_profit_native(bet, unit_value)
        embed.add_field(
            name="💵 Net Result",
            value=f"**{profit_native / unit_value:+.2f} u**  ·  "
            f"{format_native_with_usd(profit_native, currency, signed=True)}",
            inline=False,
        )

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
        odds_str = format_odds(b.get("odds"))
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

        grouped: dict[str, list[dict[str, Any]]] = {"ML": [], "2 Legs": [], "Parlays": []}
        for b in ordered:
            grouped[_leg_category(len(_get_legs(b)))].append(b)

        present_categories = [c for c in CATEGORY_ORDER if grouped[c]]
        singular_labels = {"2 Legs": "2 Leg", "Parlays": "Parlay"}
        for idx, category in enumerate(present_categories):
            group_bets = grouped[category]
            if idx > 0:
                # Thin divider between category blocks so they read as
                # clearly separate sections rather than one long list.
                embed.add_field(name="\u200b", value="── ── ── ── ──", inline=False)

            singular = singular_labels.get(category)
            # Multi-leg bets get a blank line between each numbered block so
            # separate parlays don't run into each other; ML stays tightly packed.
            sep = "\n\n" if singular else "\n"
            chunks = _chunk_lines(
                _leg_lines(group_bets, unit_value, singular_label=singular), sep=sep
            )
            header = category
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