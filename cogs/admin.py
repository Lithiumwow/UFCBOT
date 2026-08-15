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

# Approximate list prices for gpt-4o-mini (USD per 1M tokens)
_PRICE_IN_PER_M = {
    "gpt-4o-mini": 0.15,
    "gpt-4o": 2.50,
}
_PRICE_OUT_PER_M = {
    "gpt-4o-mini": 0.60,
    "gpt-4o": 10.00,
}


def _est_cost_usd(prompt: int, completion: int, model: str | None) -> float:
    key = (model or "gpt-4o-mini").lower()
    in_rate = _PRICE_IN_PER_M["gpt-4o-mini"]
    out_rate = _PRICE_OUT_PER_M["gpt-4o-mini"]
    for name, rate in _PRICE_IN_PER_M.items():
        if name in key:
            in_rate = rate
            out_rate = _PRICE_OUT_PER_M[name]
            break
    return (prompt / 1_000_000.0) * in_rate + (completion / 1_000_000.0) * out_rate


def _fmt_block(label: str, block: dict, model_hint: str | None) -> str:
    cost = _est_cost_usd(block["prompt"], block["completion"], model_hint)
    return (
        f"**{label}**\n"
        f"Calls: `{block['calls']}`\n"
        f"Prompt: `{block['prompt']:,}` · Completion: `{block['completion']:,}`\n"
        f"Total: `{block['total']:,}` tokens\n"
        f"Est. cost: `${cost:.4f}`"
    )


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

    @commands.command(name="token", aliases=["tokens", "usage"])
    async def token(self, ctx: commands.Context):
        """Show OpenAI Vision token usage tracked by this bot. Usage: !token"""
        if ctx.author.id not in config.ALLOWED_USER_IDS:
            await ctx.reply(
                "🚫 You're not authorized to view token usage.",
                mention_author=False,
            )
            return

        key_set = bool(getattr(config, "OPENAI_API_KEY", "") or "")
        model = getattr(config, "OPENAI_VISION_MODEL", None) or "gpt-4o-mini"
        db = self.bot.db  # type: ignore[attr-defined]
        try:
            summary = await db.get_openai_usage_summary()
        except Exception as e:
            await ctx.reply(
                f"❌ Couldn't read usage: `{e}`",
                mention_author=False,
            )
            return

        models = summary.get("models") or []
        top_model = models[0]["model"] if models else model
        model_line = ", ".join(
            f"`{m['model'] or '?'}×{m['n']}`" for m in models
        ) or f"`{model}` (configured)"

        embed = discord.Embed(
            title="🔢 OpenAI token usage",
            description=(
                "Tracked from `/bet-slip` Vision calls on **this bot**.\n"
                f"API key: {'✅ set' if key_set else '❌ missing'} · model `{model}`"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Today (UTC)",
            value=_fmt_block("Today", summary["today"], top_model),
            inline=False,
        )
        embed.add_field(
            name="This month (UTC)",
            value=_fmt_block("Month", summary["month"], top_model),
            inline=False,
        )
        embed.add_field(
            name="All time (since tracking)",
            value=_fmt_block("All-time", summary["all"], top_model),
            inline=False,
        )
        embed.add_field(name="Models used", value=model_line, inline=False)
        embed.set_footer(
            text=(
                "Est. cost uses list prices (gpt-4o-mini ≈ $0.15/$0.60 per 1M). "
                "Tier 1 org limit is usually ~$100/mo — check platform.openai.com for exact spend."
            )
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
