"""
/collab — two allowed users build one shared UFC slip together.

Flow:
  1. Host: /collab start [event] → lobby message + invite code
  2. Partner: Join button or /collab join code
  3. Each: Add Play (reuses FightIQ prop builder)
  4. Host: Finalize → units/odds → one bet with co_user_id (visible to both)
"""
from __future__ import annotations

import secrets
import string
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import card_data
import config
from bet_builder import BetBuilderSession, BuilderView
from betting_math import get_user_settings
from checks import is_admin
from embeds import build_bet_embed
from views import BetView

COLLAB_MAX_LEGS_PER_USER = 3
_CODE_ALPHABET = string.ascii_uppercase + string.digits


def _new_code(length: int = 5) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def _mention(user_id: Optional[int]) -> str:
    if user_id is None:
        return "_(waiting…)_"
    return f"<@{user_id}>"


async def _resolve_display(bot: commands.Bot, user_id: int) -> str:
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            return f"User {user_id}"
    return user.display_name


async def build_lobby_embed(
    bot: commands.Bot, db, session: dict
) -> discord.Embed:
    legs = await db.get_collab_legs(session["id"])
    host_name = await _resolve_display(bot, session["host_user_id"])
    partner_id = session.get("partner_user_id")
    partner_name = await _resolve_display(bot, partner_id) if partner_id else None

    embed = discord.Embed(
        title="🤝 Collab Slip",
        description=(
            f"**Event:** {session.get('event') or 'TBD'}\n"
            f"**Invite code:** `{session['code']}`\n"
            f"Partner can press **Join**, or run `/collab join code:{session['code']}`"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Host", value=f"{host_name} · {_mention(session['host_user_id'])}", inline=True)
    embed.add_field(
        name="Partner",
        value=(
            f"{partner_name} · {_mention(partner_id)}"
            if partner_id
            else "_(open — waiting for partner)_"
        ),
        inline=True,
    )

    if not legs:
        embed.add_field(name="Legs", value="_No plays yet — each of you add at least one._", inline=False)
    else:
        lines = []
        for i, leg in enumerate(legs, start=1):
            who = await _resolve_display(bot, leg["user_id"])
            lines.append(f"**{i}.** {leg['description']}  · _{who}_")
        embed.add_field(name="Legs", value="\n".join(lines)[:1000], inline=False)

    host_legs = sum(1 for L in legs if L["user_id"] == session["host_user_id"])
    partner_legs = (
        sum(1 for L in legs if L["user_id"] == partner_id) if partner_id else 0
    )
    ready = bool(partner_id and host_legs >= 1 and partner_legs >= 1)
    embed.set_footer(
        text=(
            "Ready to finalize — host presses Finalize."
            if ready
            else "Need: partner joined + ≥1 play from each collaborator."
        )
    )
    return embed


class FinalizeCollabModal(discord.ui.Modal, title="Finalize Collab Slip"):
    def __init__(self, cog: "CollabCog", session_id: int):
        super().__init__()
        self.cog = cog
        self.session_id = session_id
        self.units_input = discord.ui.TextInput(
            label="Units", default="1.0", required=True, max_length=10
        )
        self.odds_input = discord.ui.TextInput(
            label="Odds (American, e.g. -150 or 120)", required=False, max_length=10
        )
        self.add_item(self.units_input)
        self.add_item(self.odds_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            units = float(self.units_input.value)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Units must be a number, e.g. `1.5`.", ephemeral=True
            )
            return

        odds_raw = (self.odds_input.value or "").strip()
        odds = None
        if odds_raw:
            try:
                odds = int(odds_raw)
            except ValueError:
                await interaction.response.send_message(
                    "⚠️ Odds must be a whole number, e.g. `-150` or `120`.",
                    ephemeral=True,
                )
                return

        await self.cog.finalize_session(interaction, self.session_id, units, odds)


class CollabLobbyView(discord.ui.View):
    def __init__(self, cog: "CollabCog", session_id: int):
        super().__init__(timeout=1800)
        self.cog = cog
        self.session_id = session_id

    async def _session(self, interaction: discord.Interaction) -> Optional[dict]:
        db = interaction.client.db  # type: ignore[attr-defined]
        return await db.get_collab_session(self.session_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in config.ALLOWED_USER_IDS:
            await interaction.response.send_message(
                "🚫 You're not authorized to use this bot.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Join Collab", style=discord.ButtonStyle.primary, row=0)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db  # type: ignore[attr-defined]
        session = await self._session(interaction)
        if not session or session["status"] != "open":
            await interaction.response.send_message(
                "This collab is closed.", ephemeral=True
            )
            return
        if interaction.user.id == session["host_user_id"]:
            await interaction.response.send_message(
                "You're already the host.", ephemeral=True
            )
            return
        if session.get("partner_user_id") == interaction.user.id:
            await interaction.response.send_message(
                "You're already in this collab.", ephemeral=True
            )
            return
        if session.get("partner_user_id") is not None:
            await interaction.response.send_message(
                "This collab already has a partner.", ephemeral=True
            )
            return

        ok = await db.join_collab_session(self.session_id, interaction.user.id)
        if not ok:
            await interaction.response.send_message(
                "Couldn't join — collab may already be full.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Joined collab `{session['code']}`. Add your play next.", ephemeral=True
        )
        await self.cog.refresh_lobby_message(interaction.client, self.session_id)

    @discord.ui.button(label="Add My Play", style=discord.ButtonStyle.secondary, row=0)
    async def add_play_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_add_play(interaction, self.session_id)

    @discord.ui.button(label="Finalize Slip", style=discord.ButtonStyle.success, row=1)
    async def finalize_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db  # type: ignore[attr-defined]
        session = await self._session(interaction)
        if not session or session["status"] != "open":
            await interaction.response.send_message(
                "This collab is closed.", ephemeral=True
            )
            return
        if interaction.user.id != session["host_user_id"]:
            await interaction.response.send_message(
                "Only the host can finalize the slip.", ephemeral=True
            )
            return

        partner_id = session.get("partner_user_id")
        if not partner_id:
            await interaction.response.send_message(
                "Wait for a partner to join first.", ephemeral=True
            )
            return

        legs = await db.get_collab_legs(self.session_id)
        host_n = sum(1 for L in legs if L["user_id"] == session["host_user_id"])
        partner_n = sum(1 for L in legs if L["user_id"] == partner_id)
        if host_n < 1 or partner_n < 1:
            await interaction.response.send_message(
                "Each collaborator needs at least one play before finalizing.",
                ephemeral=True,
            )
            return
        if len(legs) > 6:
            await interaction.response.send_message(
                "Too many legs (max 6 on one slip).", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            FinalizeCollabModal(self.cog, self.session_id)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db  # type: ignore[attr-defined]
        session = await self._session(interaction)
        if not session:
            await interaction.response.send_message(
                "Collab not found.", ephemeral=True
            )
            return
        uid = interaction.user.id
        if uid not in (session["host_user_id"], session.get("partner_user_id")):
            await interaction.response.send_message(
                "Only collab members can cancel.", ephemeral=True
            )
            return
        if session["status"] != "open":
            await interaction.response.send_message(
                "Already closed.", ephemeral=True
            )
            return

        await db.set_collab_status(self.session_id, "cancelled")
        await interaction.response.edit_message(
            content=f"🚫 Collab `{session['code']}` cancelled.",
            embed=None,
            view=None,
        )
        self.stop()


class CollabCog(
    commands.GroupCog,
    name="collab",
    description="Build one UFC slip together with another allowed user",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    def _upcoming_events(self) -> list[dict]:
        raw = getattr(self.bot, "cached_events", []) or []
        return card_data.filter_upcoming_events(raw)

    async def ufc_event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current_lower = current.lower()
        choices = []
        for ev in self._upcoming_events():
            prefix = "🔴 LIVE — " if ev.get("is_live") else ""
            try:
                date_bit = f" ({ev['date']:%b %d, %Y})"
            except Exception:
                date_bit = ""
            label = f"{prefix}{ev['short_name']}{date_bit}"
            if current_lower in label.lower():
                choices.append(
                    app_commands.Choice(name=label[:100], value=ev["short_name"][:100])
                )
        return choices[:25]

    async def refresh_lobby_message(self, client, session_id: int) -> None:
        db = client.db
        session = await db.get_collab_session(session_id)
        if not session or not session.get("message_id") or not session.get("channel_id"):
            return
        channel = client.get_channel(session["channel_id"])
        if channel is None:
            try:
                channel = await client.fetch_channel(session["channel_id"])
            except discord.HTTPException:
                return
        try:
            msg = await channel.fetch_message(session["message_id"])
        except discord.HTTPException:
            return
        embed = await build_lobby_embed(client, db, session)
        try:
            await msg.edit(embed=embed, view=CollabLobbyView(self, session_id))
        except discord.HTTPException:
            pass

    async def start_add_play(
        self, interaction: discord.Interaction, session_id: int
    ) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        session = await db.get_collab_session(session_id)
        if not session or session["status"] != "open":
            await interaction.response.send_message(
                "This collab is closed.", ephemeral=True
            )
            return

        uid = interaction.user.id
        if uid not in (session["host_user_id"], session.get("partner_user_id")):
            await interaction.response.send_message(
                "Join the collab first.", ephemeral=True
            )
            return

        legs = await db.get_collab_legs(session_id)
        mine = sum(1 for L in legs if L["user_id"] == uid)
        remaining = COLLAB_MAX_LEGS_PER_USER - mine
        if remaining <= 0:
            await interaction.response.send_message(
                f"You've already added {COLLAB_MAX_LEGS_PER_USER} play(s) to this collab.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        event = session.get("event")
        fights: list = []
        event_pk = None
        if event:
            upcoming = self._upcoming_events()
            matched = card_data.match_event_in_list(event, upcoming) if upcoming else None
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
                fights = []

        async def on_append(inter: discord.Interaction, new_legs: list[dict]):
            if not inter.response.is_done():
                await inter.response.defer(ephemeral=True)
            for leg in new_legs:
                await db.add_collab_leg(
                    session_id,
                    uid,
                    leg["description"],
                    fighter_pick=leg.get("fighter_pick"),
                    outcome_type=leg.get("outcome_type"),
                    outcome_round=leg.get("outcome_round"),
                )
            await inter.followup.send(
                f"✅ Added **{len(new_legs)}** play(s) to collab `{session['code']}`.",
                ephemeral=True,
            )
            await self.refresh_lobby_message(inter.client, session_id)

        builder = BetBuilderSession(
            event=event,
            fights=fights,
            invoker_id=uid,
            cog=self,
            max_legs=remaining,
            finish_label="✅ Add to Collab",
            append_only=True,
            on_append=on_append,
        )
        note = ""
        if event and not fights:
            note = "\n\n⚠️ Card not loaded — use **Free-Text Leg**."
        elif fights:
            note = f"\n\n_Loaded **{len(fights)}** fights. Add up to **{remaining}** play(s)._"

        message = await interaction.followup.send(
            content=builder.summary_text() + note,
            view=BuilderView(builder),
            ephemeral=True,
        )
        builder.message = message

    async def finalize_session(
        self,
        interaction: discord.Interaction,
        session_id: int,
        units: float,
        odds: Optional[int],
    ) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        session = await db.get_collab_session(session_id)
        if not session or session["status"] != "open":
            await interaction.response.send_message(
                "This collab is already closed.", ephemeral=True
            )
            return
        if interaction.user.id != session["host_user_id"]:
            await interaction.response.send_message(
                "Only the host can finalize.", ephemeral=True
            )
            return

        partner_id = session.get("partner_user_id")
        if not partner_id:
            await interaction.response.send_message(
                "Need a partner first.", ephemeral=True
            )
            return

        collab_legs = await db.get_collab_legs(session_id)
        host_n = sum(1 for L in collab_legs if L["user_id"] == session["host_user_id"])
        partner_n = sum(1 for L in collab_legs if L["user_id"] == partner_id)
        if host_n < 1 or partner_n < 1:
            await interaction.response.send_message(
                "Each collaborator needs at least one play.", ephemeral=True
            )
            return

        bet_title = "\n".join(L["description"] for L in collab_legs)
        bet_id = await db.add_bet(
            user_id=session["host_user_id"],
            guild_id=session.get("guild_id") or interaction.guild_id,
            channel_id=session.get("channel_id") or interaction.channel_id,
            event=session.get("event"),
            bet_title=bet_title,
            units=units,
            odds=odds,
            sport="ufc",
            co_user_id=partner_id,
            is_collab=True,
        )
        for idx, leg in enumerate(collab_legs):
            await db.add_bet_leg(
                bet_id,
                idx,
                leg["description"],
                fighter_pick=leg.get("fighter_pick"),
                outcome_type=leg.get("outcome_type"),
                outcome_round=leg.get("outcome_round"),
                added_by=leg.get("user_id"),
            )

        await db.set_collab_status(session_id, "finalized")

        bet_row = await db.get_bet(bet_id)
        unit_value, currency = await get_user_settings(db, interaction.user.id)
        host = interaction.user
        partner = self.bot.get_user(partner_id)
        if partner is None:
            try:
                partner = await self.bot.fetch_user(partner_id)
            except discord.HTTPException:
                partner = None

        embed = build_bet_embed(
            bet_row,
            unit_value=unit_value,
            currency=currency,
            user=host,
            co_user=partner,
        )
        view = BetView(bet_id)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        message = await interaction.original_response()
        await db.set_message_id(bet_id, message.id)
        interaction.client.add_view(view)  # type: ignore[attr-defined]

        if session.get("message_id") and session.get("channel_id"):
            channel = interaction.client.get_channel(session["channel_id"])
            if channel is None:
                try:
                    channel = await interaction.client.fetch_channel(session["channel_id"])
                except discord.HTTPException:
                    channel = None
            if channel is not None:
                try:
                    lobby = await channel.fetch_message(session["message_id"])
                    await lobby.edit(
                        content=(
                            f"✅ Collab `{session['code']}` finalized as bet **#{bet_id}** "
                            f"· {_mention(session['host_user_id'])} + {_mention(partner_id)}"
                        ),
                        embed=None,
                        view=None,
                    )
                except discord.HTTPException:
                    pass

    @app_commands.command(name="start", description="Start a collab slip and invite a partner")
    @is_admin()
    @app_commands.describe(event="UFC event for this collab slip")
    @app_commands.autocomplete(event=ufc_event_autocomplete)
    async def collab_start(
        self, interaction: discord.Interaction, event: str | None = None
    ):
        await interaction.response.defer()
        db = self.bot.db  # type: ignore[attr-defined]

        if event:
            upcoming = self._upcoming_events()
            matched = card_data.match_event_in_list(event, upcoming) if upcoming else None
            if upcoming and matched is None:
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
                    f"❌ **{event}** isn’t available — only upcoming/live cards.\n"
                    f"Upcoming: {names or '(none)'}",
                    ephemeral=True,
                )
                return
            if matched is not None:
                event = matched.get("short_name") or matched.get("name") or event

        code = _new_code()
        for _ in range(8):
            if await db.get_collab_session_by_code(code) is None:
                break
            code = _new_code()

        session_id = await db.create_collab_session(
            code=code,
            host_user_id=interaction.user.id,
            event=event,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
        )
        session = await db.get_collab_session(session_id)
        embed = await build_lobby_embed(self.bot, db, session)
        view = CollabLobbyView(self, session_id)
        message = await interaction.followup.send(embed=embed, view=view)
        await db.set_collab_message_id(session_id, message.id)

    @app_commands.command(name="join", description="Join an open collab slip by invite code")
    @is_admin()
    @app_commands.describe(code="Invite code from /collab start")
    async def collab_join(self, interaction: discord.Interaction, code: str):
        db = self.bot.db  # type: ignore[attr-defined]
        session = await db.get_collab_session_by_code(code)
        if not session or session["status"] != "open":
            await interaction.response.send_message(
                f"No open collab found for `{code.upper()}`.", ephemeral=True
            )
            return
        if interaction.user.id == session["host_user_id"]:
            await interaction.response.send_message(
                "You're already the host of this collab.", ephemeral=True
            )
            return
        if session.get("partner_user_id") == interaction.user.id:
            await interaction.response.send_message(
                "You're already in this collab.", ephemeral=True
            )
            return
        if session.get("partner_user_id") is not None:
            await interaction.response.send_message(
                "That collab already has a partner.", ephemeral=True
            )
            return

        ok = await db.join_collab_session(session["id"], interaction.user.id)
        if not ok:
            await interaction.response.send_message(
                "Couldn't join — try again.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Joined collab `{session['code']}`"
            + (f" for **{session['event']}**" if session.get("event") else "")
            + ". Open the lobby message and press **Add My Play**.",
            ephemeral=True,
        )
        await self.refresh_lobby_message(self.bot, session["id"])

    @app_commands.command(name="status", description="Show an open collab lobby by code")
    @is_admin()
    @app_commands.describe(code="Invite code")
    async def collab_status(self, interaction: discord.Interaction, code: str):
        db = self.bot.db  # type: ignore[attr-defined]
        session = await db.get_collab_session_by_code(code)
        if not session:
            await interaction.response.send_message(
                f"No collab found for `{code.upper()}`.", ephemeral=True
            )
            return
        embed = await build_lobby_embed(self.bot, db, session)
        view = (
            CollabLobbyView(self, session["id"])
            if session["status"] == "open"
            else None
        )
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(CollabCog(bot))
