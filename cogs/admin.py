"""Bot / hosting control commands (e.g. PebbleHost restart)."""

from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from checks import is_admin_ctx
import panel_api

log = logging.getLogger("ufc-bet-bot.admin")


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="restart")
    @is_admin_ctx()
    async def restart(self, ctx: commands.Context):
        """Restart this bot on the PebbleHost panel (brief downtime). Usage: !restart"""
        if not panel_api.panel_configured():
            await ctx.reply(
                "⚠️ Panel restart isn't configured. Set `PANEL_API_KEY` and "
                "`PANEL_SERVER_ID` in `.env`.",
                mention_author=False,
            )
            return

        status = await ctx.reply(
            "♻️ Sending restart to PebbleHost… the bot will go offline briefly.",
            mention_author=False,
        )
        try:
            await asyncio.to_thread(panel_api.send_power_signal, "restart")
            try:
                await status.edit(
                    content="✅ Restart signal accepted. Waiting for the process to come back…"
                )
            except Exception:
                pass
        except panel_api.PanelError as e:
            log.exception("Panel restart failed")
            try:
                await status.edit(content=f"❌ Restart failed: `{e}`")
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
