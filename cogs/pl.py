from __future__ import annotations

import io

import discord
from discord import app_commands
from discord.ext import commands

import chart
from betting_math import get_user_settings
from checks import is_admin, resolve_allowed_target, target_user_id_from_namespace
from embeds import build_pl_embed
from views import CardShareView


class PLCog(commands.Cog):
    """Focused profit/loss view -- overall or per event."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        db = self.bot.db  # type: ignore[attr-defined]
        owner_id = target_user_id_from_namespace(interaction)
        events = await db.get_distinct_events("ufc", owner_id)
        current_lower = current.lower()
        matches = [e for e in events if current_lower in e.lower()]
        return [app_commands.Choice(name=e[:100], value=e[:100]) for e in matches[:25]]

    @app_commands.command(
        name="pl",
        description="Show profit/loss — yours or another allowed user",
    )
    @is_admin()
    @app_commands.describe(
        scope="Overall history, or a single event",
        event="Required when scope is Event — pick which card to pull P/L for",
        user="Optional: view another allowed user's P/L",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Overall (all-time)", value="overall"),
            app_commands.Choice(name="Event", value="event"),
        ]
    )
    @app_commands.autocomplete(event=event_autocomplete)
    async def pl(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str],
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

        if scope.value == "event" and not event:
            await interaction.response.send_message(
                "Pick an **event** when using Event scope.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        unit_value, currency = await get_user_settings(db, owner_id)
        whose = f" · {owner.display_name}" if viewing_other else ""

        if scope.value == "event":
            bets = await db.get_bets_for_event_matching(event, "ufc", owner_id)  # type: ignore[arg-type]
            if not bets:
                await interaction.followup.send(
                    f"No UFC bets found for **{event}**{whose}.",
                    ephemeral=True,
                )
                return
            embed = build_pl_embed(
                title=f"P/L — {event}{whose}",
                bets=bets,
                unit_value=unit_value,
                currency=currency,
                icon_url=owner.display_avatar.url,
                event=event,
            )
            chart_title = f"Units — {event}{whose}"
            share_event = event
        else:
            bets = await db.get_all_bets("ufc", owner_id)
            embed = build_pl_embed(
                title=f"P/L — Overall (All-Time UFC){whose}",
                bets=bets,
                unit_value=unit_value,
                currency=currency,
                icon_url=owner.display_avatar.url,
                include_event_breakdown=True,
            )
            chart_title = f"Cumulative Units — Overall{whose}"
            share_event = None

        files: list[discord.File] = []
        chart_bytes = chart.build_profit_chart(
            bets, unit_value, currency, in_units=True, title=chart_title
        )
        if chart_bytes:
            image_file = discord.File(io.BytesIO(chart_bytes), filename="pl_curve.png")
            embed.set_image(url="attachment://pl_curve.png")
            files.append(image_file)

        await interaction.followup.send(
            embed=embed,
            files=files,
            view=CardShareView(
                invoker_id=interaction.user.id,
                owner_user_id=owner_id,
                kind="pl",
                event=share_event,
                pl_scope=scope.value,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PLCog(bot))
