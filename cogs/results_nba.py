from __future__ import annotations

import io

import discord
from discord import app_commands
from discord.ext import commands

import chart
from betting_math import get_user_settings
from embeds import build_results_embed
from checks import is_admin


class ResultsNBACog(commands.Cog):
    """Registers a single flat command: /results-nba
    (no select-event subcommand -- just one all-time view with weekly and
    monthly breakdowns built in)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="results-nba", description="Show your all-time NBA betting results")
    @is_admin()
    async def results_nba(self, interaction: discord.Interaction):
        db = self.bot.db  # type: ignore[attr-defined]
        bets = await db.get_all_bets("nba", interaction.user.id)
        unit_value, currency = await get_user_settings(db, interaction.user.id)
        embed = build_results_embed(
            title="NBA — All-Time Results",
            bets=bets,
            unit_value=unit_value,
            currency=currency,
            icon_url=interaction.user.display_avatar.url,
            include_weekly=True,
            include_monthly=True,
            include_biggest_wins=True,
            include_bet_list=True,
            bet_list_limit=10,
        )

        chart_bytes = chart.build_profit_chart(bets, unit_value, currency)
        files = []
        if chart_bytes:
            image_file = discord.File(io.BytesIO(chart_bytes), filename="profit_chart.png")
            embed.set_image(url="attachment://profit_chart.png")
            files.append(image_file)

        await interaction.response.send_message(embed=embed, files=files)


async def setup(bot: commands.Bot):
    await bot.add_cog(ResultsNBACog(bot))