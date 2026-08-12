"""Bot / hosting control commands (e.g. PebbleHost restart)."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

import config
import panel_api

log = logging.getLogger("ufc-bet-bot.admin")
# Surface restart attempts even when other admin logs are quiet
log.setLevel(logging.INFO)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="restart")
    async def restart(self, ctx: commands.Context):
        """Restart this bot on the PebbleHost panel. Usage: !restart"""
        if ctx.author.id not in config.ALLOWED_USER_IDS:
            await ctx.reply(
                "🚫 You're not authorized to restart the bot.",
                mention_author=False,
            )
            return

        if not panel_api.panel_configured():
            await ctx.reply(
                "⚠️ Panel restart isn't configured. Set `PANEL_API_KEY` and "
                "`PANEL_SERVER_ID` in `.env`.",
                mention_author=False,
            )
            return

        # Prefer channel send over reply — more reliable if reply perms are odd
        status = await ctx.send(
            f"♻️ {ctx.author.mention} Sending restart to PebbleHost… "
            "the bot will go offline briefly."
        )
        log.warning("!restart requested by %s (%s)", ctx.author, ctx.author.id)
        try:
            await asyncio.to_thread(panel_api.send_power_signal, "restart")
            try:
                await status.edit(
                    content="✅ Restart signal accepted. Waiting for the process to come back…"
                )
            except discord.HTTPException:
                pass
        except panel_api.PanelError as e:
            log.exception("Panel restart failed")
            try:
                await status.edit(content=f"❌ Restart failed: `{e}`")
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
