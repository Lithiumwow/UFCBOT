"""Bot / hosting control commands (e.g. PebbleHost restart)."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from checks import is_admin
import panel_api

log = logging.getLogger("ufc-bet-bot.admin")


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="restart",
        description="Restart this bot on the PebbleHost panel (brief downtime)",
    )
    @is_admin()
    async def restart(self, interaction: discord.Interaction):
        if not panel_api.panel_configured():
            await interaction.response.send_message(
                "⚠️ Panel restart isn't configured. Set `PANEL_API_KEY` and "
                "`PANEL_SERVER_ID` in `.env`.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "♻️ Sending restart to PebbleHost… the bot will go offline briefly.",
            ephemeral=True,
        )
        try:
            # Run sync HTTP off the event loop
            import asyncio

            await asyncio.to_thread(panel_api.send_power_signal, "restart")
            # May never get to edit if process dies mid-restart; that's fine.
            try:
                await interaction.edit_original_response(
                    content="✅ Restart signal accepted. Waiting for the process to come back…"
                )
            except discord.HTTPException:
                pass
        except panel_api.PanelError as e:
            log.exception("Panel restart failed")
            try:
                await interaction.edit_original_response(
                    content=f"❌ Restart failed: `{e}`"
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
