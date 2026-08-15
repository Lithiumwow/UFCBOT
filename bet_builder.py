"""
Interactive bet builder for /bet-ufc.

After picking a fight, props come from FightIQ's live FightOdds catalog
(labels only — no odds). Browse Popular / category / search, pick a play,
and the leg is added. Free-text remains as a fallback.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import discord

from card_data import fight_corners, fight_slug
from leg_parser import parse_leg_line
from prop_play_map import map_play_to_leg, select_option_texts
from props_loader import try_load_prop_catalog

from betting_math import parse_stake_odds

MAX_LEGS = 6
PAGE_SIZE = 25

CATEGORY_LABELS = {
    "moneyline": "Moneyline",
    "totals": "Totals O/U",
    "distance": "Distance / start",
    "method_fight": "Fight method",
    "method_fighter": "Fighter method",
    "round_fighter": "Round winner",
    "round_method": "Round + method",
    "other": "Other",
}

CATEGORY_ORDER = [
    "moneyline",
    "totals",
    "distance",
    "method_fight",
    "method_fighter",
    "round_fighter",
    "round_method",
    "other",
]


class BetBuilderSession:
    """Shared state for one /bet-ufc builder run."""

    def __init__(
        self,
        *,
        event: str | None,
        fights: list,
        invoker_id: int,
        cog,
        max_legs: int = MAX_LEGS,
        finish_label: str = "✅ Finish & Log Bet",
        append_only: bool = False,
        on_append=None,
    ):
        self.event = event
        # list of (fighter_a, fighter_b, slug|None)
        self.fights = fights
        self.invoker_id = invoker_id
        self.cog = cog
        self.legs: list[dict] = []
        self.message: discord.Message | None = None
        self.max_legs = max_legs
        self.finish_label = finish_label
        # When True, finish skips units/odds and calls on_append(interaction, legs)
        self.append_only = append_only
        self.on_append = on_append

    def summary_text(self) -> str:
        header = f"🥊 **Building bet for {self.event or '(no event set)'}**"
        if not self.legs:
            return (
                f"{header}\n\nNo legs added yet. Pick a fight below, or add a free-text leg."
            )
        lines = "\n".join(
            f"{i}. {leg['description']}" for i, leg in enumerate(self.legs, start=1)
        )
        remaining = self.max_legs - len(self.legs)
        footer = (
            f"\n\n{remaining} more leg(s) can be added."
            if remaining > 0
            else "\n\nMax legs reached."
        )
        return f"{header}\n\n{lines}{footer}"

    async def refresh_message(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.summary_text(), view=BuilderView(self))
        except discord.HTTPException:
            pass


def _check_invoker(interaction: discord.Interaction, session: BetBuilderSession) -> bool:
    return interaction.user.id == session.invoker_id


class PropBrowseState:
    """In-progress prop browsing for one fight."""

    def __init__(
        self,
        session: BetBuilderSession,
        *,
        fighter_a: str,
        fighter_b: str,
        slug: Optional[str],
        catalog: Any,
    ):
        self.session = session
        self.fighter_a = fighter_a
        self.fighter_b = fighter_b
        self.slug = slug
        self.catalog = catalog
        self.mode: str = "popular"  # popular | category | search
        self.category: Optional[str] = None
        self.query: Optional[str] = None
        self.page: int = 0
        self.plays: list = []
        self._refresh_plays()

    def _refresh_plays(self) -> None:
        if self.catalog is None:
            self.plays = []
            return
        if self.mode == "search" and self.query:
            self.plays = self.catalog.filter(query=self.query, limit=None)
        elif self.mode == "category" and self.category:
            self.plays = self.catalog.filter(category=self.category, limit=None)
        else:
            self.plays = self.catalog.filter(popular_only=True, limit=None)
            if not self.plays:
                self.plays = self.catalog.filter(limit=None)

    def page_slice(self) -> list:
        start = self.page * PAGE_SIZE
        return self.plays[start : start + PAGE_SIZE]

    def total_pages(self) -> int:
        if not self.plays:
            return 1
        return max(1, (len(self.plays) + PAGE_SIZE - 1) // PAGE_SIZE)

    def heading(self) -> str:
        fight = f"**{self.fighter_a} vs {self.fighter_b}**"
        if self.mode == "search" and self.query:
            scope = f'Search: "{self.query}"'
        elif self.mode == "category" and self.category:
            scope = CATEGORY_LABELS.get(self.category, self.category)
        else:
            scope = "Popular props"
        page = self.page + 1
        pages = self.total_pages()
        n = len(self.plays)
        return (
            f"{fight} — {scope}\n"
            f"_Page {page}/{pages} · {n} play(s). Odds not shown — pick a market._"
        )


class PropPlaySelect(discord.ui.Select):
    def __init__(self, state: PropBrowseState):
        self.state = state
        options = []
        for play in state.page_slice()[:PAGE_SIZE]:
            # Discord select labels are capped at 100 chars and do not wrap —
            # use last names + abbreviated props so the outcome stays visible.
            preview = map_play_to_leg(play, fighter_a=state.fighter_a, fighter_b=state.fighter_b)
            full = preview.get("description") or play.label or play.offer_type_id or "Play"
            category_text = CATEGORY_LABELS.get(play.category, play.category or "")
            label, opt_desc = select_option_texts(
                full,
                fighter_a=state.fighter_a,
                fighter_b=state.fighter_b,
                category_label=category_text,
            )
            options.append(
                discord.SelectOption(
                    label=label,
                    value=play.id[:100],
                    description=opt_desc,
                )
            )
        if not options:
            options = [
                discord.SelectOption(
                    label="No props on this page",
                    value="__empty__",
                )
            ]
        super().__init__(
            placeholder="Pick a prop / method / round…",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        session = self.state.session
        if not _check_invoker(interaction, session):
            await interaction.response.send_message(
                "🚫 Not your bet builder.", ephemeral=True
            )
            return
        if self.values[0] == "__empty__":
            await interaction.response.defer()
            return
        if len(session.legs) >= session.max_legs:
            await interaction.response.send_message(
                "⚠️ Max legs reached.", ephemeral=True
            )
            return

        play = self.state.catalog.get(self.values[0]) if self.state.catalog else None
        if play is None:
            # Fallback: match from current page list
            play = next(
                (p for p in self.state.plays if p.id == self.values[0]), None
            )
        if play is None:
            await interaction.response.send_message(
                "⚠️ That prop is no longer available. Try again.", ephemeral=True
            )
            return

        leg = map_play_to_leg(
            play,
            fighter_a=self.state.fighter_a,
            fighter_b=self.state.fighter_b,
        )
        session.legs.append(leg)
        await interaction.response.defer()
        await session.refresh_message()


class PropCategorySelect(discord.ui.Select):
    def __init__(self, state: PropBrowseState):
        self.state = state
        cats: set[str] = set()
        if state.catalog is not None:
            for p in state.catalog.plays:
                if p.category:
                    cats.add(p.category)
        ordered = [c for c in CATEGORY_ORDER if c in cats] or list(cats)
        options = [
            discord.SelectOption(
                label=CATEGORY_LABELS.get(c, c)[:100],
                value=c[:100],
            )
            for c in ordered[:25]
        ]
        if not options:
            options = [discord.SelectOption(label="No categories", value="__none__")]
        super().__init__(
            placeholder="Or browse by category…",
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if not _check_invoker(interaction, self.state.session):
            await interaction.response.send_message(
                "🚫 Not your bet builder.", ephemeral=True
            )
            return
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        self.state.mode = "category"
        self.state.category = self.values[0]
        self.state.query = None
        self.state.page = 0
        self.state._refresh_plays()
        await interaction.response.edit_message(
            content=self.state.heading(),
            view=PropBrowseView(self.state),
        )


class PropSearchModal(discord.ui.Modal, title="Search props"):
    def __init__(self, state: PropBrowseState):
        super().__init__()
        self.state = state
        self.query_input = discord.ui.TextInput(
            label="Search",
            placeholder="e.g. submission, round 2, over 2.5",
            max_length=80,
            required=True,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.state.mode = "search"
        self.state.query = self.query_input.value.strip()
        self.state.category = None
        self.state.page = 0
        self.state._refresh_plays()
        await interaction.response.edit_message(
            content=self.state.heading(),
            view=PropBrowseView(self.state),
        )


class PropBrowseView(discord.ui.View):
    def __init__(self, state: PropBrowseState):
        super().__init__(timeout=300)
        self.state = state
        self.add_item(PropPlaySelect(state))
        self.add_item(PropCategorySelect(state))

        popular_btn = discord.ui.Button(
            label="⭐ Popular", style=discord.ButtonStyle.primary, row=2
        )
        popular_btn.callback = self._popular
        self.add_item(popular_btn)

        search_btn = discord.ui.Button(
            label="🔎 Search", style=discord.ButtonStyle.secondary, row=2
        )
        search_btn.callback = self._search
        self.add_item(search_btn)

        if state.page > 0:
            prev_btn = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary, row=3
            )
            prev_btn.callback = self._prev
            self.add_item(prev_btn)

        if state.page + 1 < state.total_pages():
            next_btn = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary, row=3
            )
            next_btn.callback = self._next
            self.add_item(next_btn)

        back_btn = discord.ui.Button(
            label="↩ Back to fights", style=discord.ButtonStyle.danger, row=3
        )
        back_btn.callback = self._back
        self.add_item(back_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return _check_invoker(interaction, self.state.session)

    async def _popular(self, interaction: discord.Interaction):
        self.state.mode = "popular"
        self.state.category = None
        self.state.query = None
        self.state.page = 0
        self.state._refresh_plays()
        await interaction.response.edit_message(
            content=self.state.heading(),
            view=PropBrowseView(self.state),
        )

    async def _search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PropSearchModal(self.state))

    async def _prev(self, interaction: discord.Interaction):
        self.state.page = max(0, self.state.page - 1)
        await interaction.response.edit_message(
            content=self.state.heading(),
            view=PropBrowseView(self.state),
        )

    async def _next(self, interaction: discord.Interaction):
        self.state.page = min(self.state.total_pages() - 1, self.state.page + 1)
        await interaction.response.edit_message(
            content=self.state.heading(),
            view=PropBrowseView(self.state),
        )

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=self.state.session.summary_text(),
            view=BuilderView(self.state.session),
        )


class FightSelect(discord.ui.Select):
    def __init__(self, session: BetBuilderSession):
        self.session = session
        options = []
        for idx, fight in enumerate(session.fights[:25]):
            a, b = fight_corners(fight)
            options.append(
                discord.SelectOption(label=f"{a} vs {b}"[:100], value=str(idx))
            )
        super().__init__(placeholder="Pick a fight...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not _check_invoker(interaction, self.session):
            await interaction.response.send_message(
                "🚫 Not your bet builder.", ephemeral=True
            )
            return
        fight = self.session.fights[int(self.values[0])]
        fighter_a, fighter_b = fight_corners(fight)
        slug = fight_slug(fight)

        await interaction.response.defer()

        catalog = None
        if slug:
            catalog = await asyncio.to_thread(
                try_load_prop_catalog,
                slug,
                fighter_a=fighter_a,
                fighter_b=fighter_b,
            )
        elif fighter_a and fighter_b:
            catalog = await asyncio.to_thread(
                try_load_prop_catalog,
                None,
                fighter_a=fighter_a,
                fighter_b=fighter_b,
            )

        if catalog is None or not getattr(catalog, "plays", None):
            note = (
                f"**{fighter_a} vs {fighter_b}**\n\n"
                "⚠️ Couldn't load props for this fight. "
                "Use **Free-Text Leg** from the main builder, or pick another fight."
            )
            try:
                await interaction.edit_original_response(
                    content=note + "\n\n" + self.session.summary_text(),
                    view=BuilderView(self.session),
                )
            except discord.HTTPException:
                pass
            return

        state = PropBrowseState(
            self.session,
            fighter_a=fighter_a,
            fighter_b=fighter_b,
            slug=slug,
            catalog=catalog,
        )
        if getattr(catalog, "event_name", "") == "__fightiq_fallback__":
            heading = (
                f"**{fighter_a} vs {fighter_b}** — Method / round markets\n"
                f"_FightOdds props not posted yet — showing FightIQ method catalog "
                f"(no odds). Page 1/{state.total_pages()} · {len(state.plays)} play(s)._"
            )
        else:
            heading = state.heading()

        try:
            await interaction.edit_original_response(
                content=heading,
                view=PropBrowseView(state),
            )
        except discord.HTTPException:
            pass


class FreeTextLegModal(discord.ui.Modal, title="Add a Free-Text Leg"):
    """For anything the catalog can't express — still parses Fighter - Outcome."""

    def __init__(self, session: BetBuilderSession):
        super().__init__()
        self.session = session
        self.leg_input = discord.ui.TextInput(
            label="Leg description",
            placeholder="e.g. Fight goes over 1.5 rounds combined",
            max_length=200,
        )
        self.add_item(self.leg_input)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.leg_input.value.strip()
        if text and len(self.session.legs) < self.session.max_legs:
            self.session.legs.append(parse_leg_line(text))
        await interaction.response.defer()
        await self.session.refresh_message()


class FinishBetModal(discord.ui.Modal, title="Finish Bet"):
    """Last step — units and odds before the bet is logged."""

    def __init__(self, session: BetBuilderSession):
        super().__init__()
        self.session = session
        self.units_input = discord.ui.TextInput(
            label="Units", default="1.0", required=True, max_length=10
        )
        self.odds_type_input = discord.ui.TextInput(
            label="Odds type",
            default="american",
            placeholder="american (default) or decimal",
            required=False,
            max_length=12,
        )
        self.odds_input = discord.ui.TextInput(
            label="Odds",
            placeholder="-150, +120, or 1.67",
            required=False,
            max_length=12,
        )
        self.add_item(self.units_input)
        self.add_item(self.odds_type_input)
        self.add_item(self.odds_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            units = float(self.units_input.value)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Units must be a number, e.g. `1.5`.", ephemeral=True
            )
            return

        try:
            odds, odds_format = parse_stake_odds(
                self.odds_input.value, odds_format=self.odds_type_input.value
            )
        except (ValueError, Exception):
            await interaction.response.send_message(
                "⚠️ Odds must be American (`-150`, `+120`) or decimal (`1.67`). "
                "Set Odds type to `american` or `decimal`.",
                ephemeral=True,
            )
            return

        await self.session.cog._log_bet(
            interaction,
            sport="ufc",
            event=self.session.event,
            legs=self.session.legs,
            units=units,
            odds=odds,
            odds_format=odds_format,
            use_followup=False,
        )
        try:
            await self.session.message.delete()
        except discord.HTTPException:
            pass


class BuilderView(discord.ui.View):
    def __init__(self, session: BetBuilderSession):
        super().__init__(timeout=600)
        self.session = session

        if len(session.legs) < session.max_legs and session.fights:
            self.add_item(FightSelect(session))

        freetext_btn = discord.ui.Button(
            label="📝 Free-Text Leg", style=discord.ButtonStyle.secondary, row=1
        )
        freetext_btn.callback = self._freetext_callback
        self.add_item(freetext_btn)

        if session.legs:
            finish_btn = discord.ui.Button(
                label=session.finish_label, style=discord.ButtonStyle.success, row=1
            )
            finish_btn.callback = self._finish_callback
            self.add_item(finish_btn)

        cancel_btn = discord.ui.Button(
            label="❌ Cancel", style=discord.ButtonStyle.danger, row=1
        )
        cancel_btn.callback = self._cancel_callback
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return _check_invoker(interaction, self.session)

    async def _freetext_callback(self, interaction: discord.Interaction):
        if len(self.session.legs) >= self.session.max_legs:
            await interaction.response.send_message(
                "⚠️ Max legs reached.", ephemeral=True
            )
            return
        await interaction.response.send_modal(FreeTextLegModal(self.session))

    async def _finish_callback(self, interaction: discord.Interaction):
        if self.session.append_only and self.session.on_append is not None:
            await self.session.on_append(interaction, list(self.session.legs))
            try:
                if self.session.message is not None:
                    await self.session.message.delete()
            except discord.HTTPException:
                pass
            return
        await interaction.response.send_modal(FinishBetModal(self.session))

    async def _cancel_callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        try:
            await self.session.message.delete()
        except discord.HTTPException:
            pass