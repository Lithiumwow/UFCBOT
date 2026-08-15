from __future__ import annotations

import io

import discord
from discord import app_commands
from discord.ext import commands

import chart
from betting_math import get_user_settings
from embeds import build_results_embed
from checks import is_admin


class ResultsUFCCog(commands.GroupCog, name="results-ufc", description="Show your UFC betting results"):
    """
    Registers as a slash command group:
      /results-ufc all-time
      /results-ufc select-event event:<autocomplete>
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    async def event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        db = self.bot.db  # type: ignore[attr-defined]
        events = await db.get_distinct_events("ufc", interaction.user.id)
        current_lower = current.lower()
        matches = [e for e in events if current_lower in e.lower()]
        return [app_commands.Choice(name=e[:100], value=e[:100]) for e in matches[:25]]

    @app_commands.command(name="all-time", description="Show results across every logged UFC bet")
    @is_admin()
    async def all_time(self, interaction: discord.Interaction):
        db = self.bot.db  # type: ignore[attr-defined]
        bets = await db.get_all_bets("ufc", interaction.user.id)
        if not bets:
            await interaction.response.send_message(
                "No UFC bets logged yet. Use `/bet-ufc` to add one.",
                ephemeral=True,
            )
            return

        unit_value, currency = await get_user_settings(db, interaction.user.id)
        legs_by_bet_id = {
            bet["id"]: await db.get_legs_for_bet(bet["id"]) for bet in bets
        }
        embed = build_results_embed(
            title="UFC — All-Time Results",
            bets=bets,
            unit_value=unit_value,
            currency=currency,
            icon_url=interaction.user.display_avatar.url,
            include_monthly=True,
            include_biggest_wins=True,
            include_bet_list=True,
            bet_list_limit=25,
            legs_by_bet_id=legs_by_bet_id,
        )

        chart_bytes = chart.build_profit_chart(bets, unit_value, currency)
        files = []
        if chart_bytes:
            image_file = discord.File(io.BytesIO(chart_bytes), filename="profit_chart.png")
            embed.set_image(url="attachment://profit_chart.png")
            files.append(image_file)

        await interaction.response.send_message(embed=embed, files=files)

    @app_commands.command(name="select-event", description="Show results for one specific UFC event")
    @is_admin()
    @app_commands.describe(event="Which event to view (from cards you've logged bets on)")
    @app_commands.autocomplete(event=event_autocomplete)
    async def select_event(self, interaction: discord.Interaction, event: str):
        db = self.bot.db  # type: ignore[attr-defined]
        # Fuzzy match aliases (FightOdds vs ESPN / short vs full names)
        bets = await db.get_bets_for_event_matching(event, "ufc", interaction.user.id)
        if not bets:
            logged = await db.get_distinct_events("ufc", interaction.user.id)
            hint = ", ".join(f"**{e}**" for e in logged[:8]) if logged else "(none)"
            await interaction.response.send_message(
                f"No bets matched **{event}**.\nYour logged cards: {hint}",
                ephemeral=True,
            )
            return

        unit_value, currency = await get_user_settings(db, interaction.user.id)
        legs_by_bet_id = {
            bet["id"]: await db.get_legs_for_bet(bet["id"]) for bet in bets
        }
        embed = build_results_embed(
            title=event,
            bets=bets,
            unit_value=unit_value,
            currency=currency,
            icon_url=interaction.user.display_avatar.url,
            include_bet_list=True,
            event=event,
            legs_by_bet_id=legs_by_bet_id,
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ResultsUFCCog(bot))