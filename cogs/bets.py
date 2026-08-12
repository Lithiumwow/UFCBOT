from __future__ import annotations

import io
import re

import discord
from discord import app_commands
from discord.ext import commands

import espn
import card_data
from bet_builder import BetBuilderSession, BuilderView
from betting_math import CURRENCY_SYMBOLS, get_user_settings
from embeds import build_bet_embed, build_results_embed
from views import BetView, CardShareView, ConfirmDeleteEventView
from checks import is_admin, resolve_allowed_target, target_user_id_from_namespace
from spreadsheet_image import build_event_recap_image
from leg_rematch import rematch_bets_to_card


class BetsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- autocompletes ----------

    def _upcoming_events(self) -> list[dict]:
        raw = getattr(self.bot, "cached_events", []) or []
        return card_data.filter_upcoming_events(raw)

    def _event_choices(self, events: list[dict], current: str) -> list[app_commands.Choice[str]]:
        current_lower = current.lower()
        choices = []
        for ev in events:
            if not card_data.is_event_upcoming(ev):
                continue
            prefix = "🔴 LIVE — " if ev.get("is_live") else ""
            try:
                date_bit = f" ({ev['date']:%b %d, %Y})"
            except Exception:
                date_bit = ""
            label = f"{prefix}{ev['short_name']}{date_bit}"
            if current_lower in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=ev["short_name"][:100]))
        return choices[:25]

    async def ufc_event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        # Upcoming-only (past cards never offered for new /bet-ufc slips)
        return self._event_choices(self._upcoming_events(), current)

    async def logged_ufc_event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        db = self.bot.db  # type: ignore[attr-defined]
        owner_id = target_user_id_from_namespace(interaction)
        events = await db.get_distinct_events("ufc", owner_id)
        current_lower = current.lower()
        matches = [e for e in events if current_lower in e.lower()]
        return [app_commands.Choice(name=e[:100], value=e[:100]) for e in matches[:25]]

    # ---------- shared bet-logging logic ----------

    async def _log_bet(
        self,
        interaction: discord.Interaction,
        *,
        sport: str,
        event: str | None,
        legs: list[dict],
        units: float | None,
        odds: int | None,
        use_followup: bool = False,
    ):
        """legs: list of {"description": str, "fighter_pick": str|None,
        "outcome_type": str|None, "outcome_round": int|None}, one per leg,
        in order. A leg without fighter_pick/outcome_type is a plain
        free-text leg and never gets auto-graded."""
        db = self.bot.db  # type: ignore[attr-defined]

        bet_title = "\n".join(leg["description"] for leg in legs) if legs else None

        bet_id = await db.add_bet(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            event=event,
            bet_title=bet_title,
            units=units if units is not None else 1.0,
            odds=odds,
            sport=sport,
        )

        for idx, leg in enumerate(legs):
            await db.add_bet_leg(
                bet_id,
                idx,
                leg["description"],
                fighter_pick=leg.get("fighter_pick"),
                outcome_type=leg.get("outcome_type"),
                outcome_round=leg.get("outcome_round"),
            )

        bet_row = await db.get_bet(bet_id)
        unit_value, currency = await get_user_settings(db, interaction.user.id)
        embed = build_bet_embed(bet_row, unit_value=unit_value, currency=currency, user=interaction.user)
        view = BetView(bet_id)

        # Owner-only control slip (buttons stay private). Use Share to post publicly.
        if use_followup:
            message = await interaction.followup.send(
                embed=embed, view=view, ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True
            )
            message = await interaction.original_response()
        await db.set_message_id(bet_id, message.id)

    # ---------- UFC ----------

    @app_commands.command(name="bet-ufc", description="Log a new UFC bet (straight, 2-leg, or parlay)")
    @is_admin()
    @app_commands.describe(event="Which UFC event this bet is for (autocompletes from upcoming events)")
    @app_commands.autocomplete(event=ufc_event_autocomplete)
    async def bet_ufc(self, interaction: discord.Interaction, event: str | None = None):
        # Card fetch (FightOdds/ESPN) often exceeds Discord's 3s reply window —
        # Contender Series and cold caches were silently timing out as "Unknown interaction".
        # Ephemeral so fight/outcome selects stay private to the invoker.
        await interaction.response.defer(ephemeral=True)

        matched = None
        # Hard-block past/finished cards even if the user pastes a free-text name
        if event:
            upcoming = self._upcoming_events()
            matched = card_data.match_event_in_list(event, upcoming)
            if upcoming and matched is None:
                # Cache can lag: re-fetch once before rejecting
                try:
                    fresh = await card_data.fetch_upcoming_events(limit=15)
                    self.bot.cached_events = fresh  # type: ignore[attr-defined]
                    upcoming = card_data.filter_upcoming_events(fresh)
                    matched = card_data.match_event_in_list(event, upcoming)
                except Exception:
                    matched = None
            if upcoming and matched is None:
                names = ", ".join(
                    (e.get("short_name") or e.get("name") or "?") for e in upcoming[:6]
                )
                await interaction.followup.send(
                    f"❌ **{event}** isn’t available for new bets — only **upcoming or live** "
                    f"UFC cards can be selected.\nUpcoming: {names or '(none found yet)'}",
                    ephemeral=True,
                )
                return
            if matched is not None:
                # Canonical upcoming label (FightOdds / ESPN short_name)
                event = matched.get("short_name") or matched.get("name") or event

        fights: list = []
        event_pk = None
        if event:
            # Prefer FightOdds pk from the upcoming cache when available
            if matched is not None and matched.get("source") == "fightodds":
                try:
                    event_pk = int(matched["id"])
                except (TypeError, ValueError, KeyError):
                    event_pk = None
            try:
                fights = await card_data.fetch_fights_for_event(
                    event, event_pk=event_pk, use_cache=False
                )
            except Exception:
                fights = []  # builder still works via the free-text-leg button

        session = BetBuilderSession(
            event=event,
            fights=fights,
            invoker_id=interaction.user.id,
            cog=self,
        )

        note = ""
        if event and not fights:
            note = (
                "\n\n⚠️ Couldn't load the fight card for this event yet — use "
                "**Free-Text Leg** or try again in a moment."
            )
        elif event and fights:
            note = f"\n\n_Loaded **{len(fights)}** fights from the card._"

        message = await interaction.followup.send(
            content=session.summary_text() + note,
            view=BuilderView(session),
            ephemeral=True,
        )
        session.message = message

    @app_commands.command(
        name="delete-event", description="Permanently delete every tracked UFC bet logged for one event"
    )
    @is_admin()
    @app_commands.describe(event="Which event's bets to delete (this cannot be undone)")
    @app_commands.autocomplete(event=logged_ufc_event_autocomplete)
    async def delete_event(self, interaction: discord.Interaction, event: str):
        db = self.bot.db  # type: ignore[attr-defined]
        bets = await db.get_bets_for_event(event, "ufc", interaction.user.id)
        if not bets:
            # Fall back to fuzzy aliases (same card under a different label)
            bets = await db.get_bets_for_event_matching(event, "ufc", interaction.user.id)
        if not bets:
            await interaction.response.send_message(
                f"No tracked bets found for **{event}**.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"⚠️ This will permanently delete **{len(bets)}** bet(s) logged for "
            f"**{event}**. This cannot be undone.",
            view=ConfirmDeleteEventView(
                event=event, sport="ufc", bet_count=len(bets), invoker_id=interaction.user.id
            ),
        )

    @app_commands.command(name="card", description="Show UFC bets for a card (yours or another allowed user)")
    @is_admin()
    @app_commands.describe(
        event="Which event to show (defaults to that user's most recent logged card)",
        user="Optional: view another allowed user's card",
    )
    @app_commands.autocomplete(event=logged_ufc_event_autocomplete)
    async def card(
        self,
        interaction: discord.Interaction,
        event: str | None = None,
        user: discord.User | None = None,
    ):
        db = self.bot.db  # type: ignore[attr-defined]
        resolved = resolve_allowed_target(interaction, user)
        if isinstance(resolved, str):
            await interaction.response.send_message(resolved, ephemeral=True)
            return
        owner_id, owner = resolved
        viewing_other = owner_id != interaction.user.id

        if not event:
            logged = await db.get_distinct_events("ufc", owner_id)
            event = logged[0] if logged else None

        if not event:
            who = f"**{owner.display_name}** doesn't" if viewing_other else "You don't"
            await interaction.response.send_message(
                f"{who} have any logged UFC bets yet, so there's no card to show. "
                "Log one with `/bet-ufc`, or pass `event` explicitly.",
                ephemeral=True,
            )
            return

        bets = await db.get_bets_for_event_matching(event, "ufc", owner_id)
        if not bets:
            logged = await db.get_distinct_events("ufc", owner_id)
            hint = ", ".join(f"**{e}**" for e in logged[:8]) if logged else "(none)"
            whose = f"{owner.display_name}'s" if viewing_other else "Your"
            await interaction.response.send_message(
                f"No bets matched **{event}**.\n"
                f"{whose} logged cards: {hint}",
                ephemeral=True,
            )
            return

        unit_value, currency = await get_user_settings(db, owner_id)
        title = f"{event} · {owner.display_name}" if viewing_other else event
        embed = build_results_embed(
            title=title,
            bets=bets,
            unit_value=unit_value,
            currency=currency,
            icon_url=owner.display_avatar.url,
            include_bet_list=True,
        )
        await interaction.response.send_message(
            embed=embed,
            view=CardShareView(
                invoker_id=interaction.user.id,
                owner_user_id=owner_id,
                kind="card",
                event=event,
                sport="ufc",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="spread-sheet",
        description="Visual recap image for a UFC card (yours or another allowed user)",
    )
    @is_admin()
    @app_commands.describe(
        event="Which event's bets to export",
        user="Optional: build the sheet for another allowed user",
    )
    @app_commands.autocomplete(event=logged_ufc_event_autocomplete)
    async def spread_sheet(
        self,
        interaction: discord.Interaction,
        event: str,
        user: discord.User | None = None,
    ):
        db = self.bot.db  # type: ignore[attr-defined]
        resolved = resolve_allowed_target(interaction, user)
        if isinstance(resolved, str):
            await interaction.response.send_message(resolved, ephemeral=True)
            return
        owner_id, owner = resolved
        viewing_other = owner_id != interaction.user.id

        bets = await db.get_bets_for_event_matching(event, "ufc", owner_id)
        if not bets:
            await interaction.response.send_message(
                f"No tracked bets found for **{event}**"
                + (f" ({owner.display_name})" if viewing_other else "")
                + ".",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            fights = await card_data.fetch_fights_for_event(event)
        except Exception:
            fights = []

        rematch_note = ""
        if fights:
            try:
                stats = await rematch_bets_to_card(db, bets, fights)
                if stats["created_legs"] or stats["updated_legs"] or stats["updated_bets"]:
                    rematch_note = (
                        f" · matched {stats['matched_fighters']} leg(s) to card"
                        f" (+{stats['created_legs']} new legs)"
                    )
            except Exception:
                pass

        for bet in bets:
            refreshed = await db.get_bet(bet["id"])
            if refreshed:
                bet.update(refreshed)
        legs_by_bet_id = {bet["id"]: await db.get_legs_for_bet(bet["id"]) for bet in bets}

        event_date = None
        for ev in getattr(self.bot, "cached_events", []):
            if ev["name"] == event or card_data._event_match_score(event, ev["name"]) >= 70:
                try:
                    event_date = f"{ev['date'].day} {ev['date']:%b %Y}"
                except Exception:
                    event_date = None
                break

        unit_value, currency = await get_user_settings(db, owner_id)

        image_bytes = build_event_recap_image(
            event_name=event,
            event_date=event_date,
            bets=bets,
            legs_by_bet_id=legs_by_bet_id,
            fights=fights,
            unit_value=unit_value,
            currency=currency,
        )

        safe_name = re.sub(r"[^\w\-]+", "_", event)[:60]
        fight_note = f" · {len(fights)} fights on card" if fights else " · card matchups unavailable"
        whose = f" · {owner.display_name}" if viewing_other else ""
        await interaction.followup.send(
            content=(
                f"📊 Recap for **{event}**{whose} ({len(bets)} bet(s){fight_note}{rematch_note})."
            ),
            file=discord.File(io.BytesIO(image_bytes), filename=f"{safe_name}.png"),
            view=CardShareView(
                invoker_id=interaction.user.id,
                owner_user_id=owner_id,
                kind="sheet",
                event=event,
                sport="ufc",
            ),
            ephemeral=True,
        )

    # ---------- NBA ----------

    @app_commands.command(name="bet-nba", description="Log a new NBA bet (straight, 2-leg, or parlay)")
    @is_admin()
    @app_commands.rename(
        leg_1="leg-1", leg_2="leg-2", leg_3="leg-3", leg_4="leg-4", leg_5="leg-5", leg_6="leg-6",
    )
    @app_commands.describe(
        event="Which NBA game this bet is for (free text -- e.g. 'Lakers vs Celtics'; games aren't tracked from ESPN)",
        leg_1="First (or only) leg, e.g. 'Lakers ML' or 'Over 220.5 points'",
        leg_2="Second leg, if this is a 2-leg or parlay bet (optional)",
        leg_3="Third leg, if this is a parlay (optional)",
        leg_4="Fourth leg, if this is a parlay (optional)",
        leg_5="Fifth leg, if this is a parlay (optional)",
        leg_6="Sixth leg, if this is a parlay (optional)",
        units="How many units you're staking on the whole bet (e.g. 1.5). Defaults to 1.0",
        odds="Combined American odds for the whole bet, e.g. -150 or +120 (optional)",
    )
    async def bet_nba(
        self,
        interaction: discord.Interaction,
        event: str | None = None,
        leg_1: str | None = None,
        leg_2: str | None = None,
        leg_3: str | None = None,
        leg_4: str | None = None,
        leg_5: str | None = None,
        leg_6: str | None = None,
        units: app_commands.Range[float, 0.01, 1000.0] | None = 1.0,
        odds: int | None = None,
    ):
        legs = []
        for text in (leg_1, leg_2, leg_3, leg_4, leg_5, leg_6):
            if text and text.strip():
                legs.append(
                    {"description": text.strip(), "fighter_pick": None, "outcome_type": None, "outcome_round": None}
                )

        await self._log_bet(
            interaction,
            sport="nba",
            event=event,
            legs=legs,
            units=units,
            odds=odds,
        )

    # ---------- settings ----------

    @app_commands.command(
        name="unit-size",
        description="Set how much 1 unit is worth and which currency to show on your slips",
    )
    @is_admin()
    @app_commands.describe(
        amount="How much 1 unit is worth (e.g. 100 for £100 / €100 / $100)",
        currency="Currency shown on your bet slips and P/L (GBP, EUR, or USD)",
    )
    @app_commands.choices(
        currency=[
            app_commands.Choice(name="GBP (£)", value="GBP"),
            app_commands.Choice(name="EUR (€)", value="EUR"),
            app_commands.Choice(name="USD ($)", value="USD"),
        ]
    )
    async def unit_size(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[float, 0.01, 1000000.0],
        currency: app_commands.Choice[str],
    ):
        db = self.bot.db  # type: ignore[attr-defined]
        code = currency.value.upper()
        await db.set_user_settings(
            interaction.user.id, unit_value=float(amount), currency=code
        )

        symbol = CURRENCY_SYMBOLS.get(code, code + " ")
        await interaction.response.send_message(
            f"✅ Your settings are now **1u = {symbol}{amount:,.2f}** ({code}). "
            "This applies to your bet slips, `/results`, `/pl`, and `/card`. "
            "Auto-grading is unchanged — currency is display-only.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BetsCog(bot))