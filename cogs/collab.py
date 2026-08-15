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
from betting_math import (
    format_odds_with_alt,
    get_user_settings,
    parse_stake_odds,
    personalize_collab_bet,
)
from checks import is_admin
from embeds import build_bet_embed
from views import BetView, ShareDestinationView

COLLAB_MAX_LEGS_PER_USER = 3
_CODE_ALPHABET = string.ascii_uppercase + string.digits


def _new_code(length: int = 5) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def _mention(user_id: Optional[int]) -> str:
    if user_id is None:
        return "_(waiting…)_"
    return f"<@{user_id}>"


async def _resolve_user(bot: commands.Bot, user_id: int) -> Optional[discord.abc.User]:
    user = bot.get_user(user_id)
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except discord.HTTPException:
        return None


async def _resolve_display(bot: commands.Bot, user_id: int) -> str:
    user = await _resolve_user(bot, user_id)
    if user is None:
        return f"User {user_id}"
    return user.display_name


async def _member_line(bot: commands.Bot, user_id: int) -> str:
    user = await _resolve_user(bot, user_id)
    if user is None:
        return f"User {user_id}"
    return f"{user.display_name} · @{user.name}"


def _fmt_stake(units: Optional[float], odds: Optional[int], fmt: str | None = "american") -> str:
    if units is None:
        return "⏳ not submitted yet"
    odds_text = format_odds_with_alt(odds, fmt) if odds is not None else "no odds"
    return f"✅ {units:g}u @ {odds_text}"


async def build_lobby_embed(
    bot: commands.Bot, db, session: dict
) -> discord.Embed:
    legs = await db.get_collab_legs(session["id"])
    partner_id = session.get("partner_user_id")

    embed = discord.Embed(
        title="🤝 Collab Slip",
        description=(
            f"**Event:** {session.get('event') or 'TBD'}\n"
            f"**Invite code:** `{session['code']}`"
        ),
        color=discord.Color.blurple(),
    )

    host_line = await _member_line(bot, session["host_user_id"])
    host_line += f"\n{_fmt_stake(session.get('host_units'), session.get('host_odds'), session.get('host_odds_format'))}"
    member_lines = [host_line]
    if partner_id:
        partner_line = await _member_line(bot, partner_id)
        partner_line += f"\n{_fmt_stake(session.get('partner_units'), session.get('partner_odds'), session.get('partner_odds_format'))}"
        member_lines.append(partner_line)
    else:
        member_lines.append("_Waiting for partner_")
    embed.add_field(name="Members (each sets their own units & odds)", value="\n\n".join(member_lines), inline=False)

    if not legs:
        embed.add_field(name="Legs", value="_No plays yet — each of you add at least one._", inline=False)
    else:
        lines = []
        for i, leg in enumerate(legs, start=1):
            who = await _resolve_display(bot, leg["user_id"])
            lines.append(f"**{i}.** {leg['description']}  · _{who}_")
        embed.add_field(name="Legs", value="\n".join(lines)[:1000], inline=False)

    return embed


class FinalizeCollabModal(discord.ui.Modal, title="Set Your Stake"):
    def __init__(self, cog: "CollabCog", session_id: int):
        super().__init__()
        self.cog = cog
        self.session_id = session_id
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

        await self.cog.submit_stake(
            interaction, self.session_id, units, odds, odds_format=odds_format
        )


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

        partner_id = session.get("partner_user_id")
        if not partner_id:
            await interaction.response.send_message(
                "Wait for a partner to join first.", ephemeral=True
            )
            return

        if interaction.user.id not in (session["host_user_id"], partner_id):
            await interaction.response.send_message(
                "Only collab members can finalize.", ephemeral=True
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

    @discord.ui.button(label="📤 Share", style=discord.ButtonStyle.primary, row=1)
    async def share_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = await self._session(interaction)
        if not session:
            await interaction.response.send_message(
                "Collab not found.", ephemeral=True
            )
            return
        uid = interaction.user.id
        if uid not in (session["host_user_id"], session.get("partner_user_id")):
            await interaction.response.send_message(
                "Only collab members can share this slip.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        view = await ShareDestinationView.create(
            invoker_id=interaction.user.id,
            interaction=interaction,
            collab_session_id=self.session_id,
        )
        await interaction.followup.send(
            "📤 **Share collab slip**\n"
            "• Use the channel menu for **this server**, or\n"
            "• Pick **another server**, then a channel.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=2)
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

    async def submit_stake(
        self,
        interaction: discord.Interaction,
        session_id: int,
        units: float,
        odds: Optional[int],
        *,
        odds_format: str = "american",
    ) -> None:
        """Records the caller's own units/odds independently. Once BOTH
        the host and partner have submitted their own stake, the bet
        actually gets created -- not before.

        The lobby message the Finalize button lived on is edited in place
        so members see units/odds (and the finished slip) on the same embed.
        """
        db = self.bot.db  # type: ignore[attr-defined]
        session = await db.get_collab_session(session_id)
        if not session or session["status"] != "open":
            await interaction.response.send_message(
                "This collab is already closed.", ephemeral=True
            )
            return

        side = await db.set_collab_stake(
            session_id, interaction.user.id, units, odds, odds_format=odds_format
        )
        if side is None:
            await interaction.response.send_message(
                "You're not part of this collab.", ephemeral=True
            )
            return

        session = await db.get_collab_session(session_id)  # re-fetch with the new stake saved
        both_ready = (
            session.get("host_units") is not None and session.get("partner_units") is not None
        )

        if not both_ready:
            other_id = (
                session["partner_user_id"] if side == "host" else session["host_user_id"]
            )
            other_name = await _resolve_display(self.bot, other_id)
            odds_text = format_odds_with_alt(odds, odds_format) if odds is not None else "no odds"
            embed = await build_lobby_embed(self.bot, db, session)
            try:
                await interaction.response.edit_message(
                    embed=embed, view=CollabLobbyView(self, session_id)
                )
            except (discord.HTTPException, discord.InteractionResponded):
                await interaction.response.send_message(
                    f"✅ Your stake is set: **{units:g}u @ {odds_text}**. Waiting on "
                    f"**{other_name}** to set theirs too — the slip finalizes once both are in.",
                    ephemeral=True,
                )
                await self.refresh_lobby_message(interaction.client, session_id)
                return
            await interaction.followup.send(
                f"✅ Your stake is set: **{units:g}u @ {odds_text}**. Waiting on "
                f"**{other_name}** to set theirs too — the slip finalizes once both are in.",
                ephemeral=True,
            )
            return

        await self._create_collab_bet(interaction, session)

    def _register_bet_view(self, client, bet_id: int) -> None:
        """Register a fresh persistent BetView for restarts. Never pass a View
        that was already sent on a message — discord.py treats that instance
        as non-persistent and raises ValueError."""
        try:
            client.add_view(BetView(bet_id))
        except ValueError:
            pass

    async def _create_collab_bet(self, interaction: discord.Interaction, session: dict) -> None:
        """Both members have now submitted their own stake -- create the
        one shared bet row (host's own stake in units/odds, partner's in
        partner_units/partner_odds) and replace the lobby embed with the
        finished slip (same message, now with units + odds)."""
        db = self.bot.db  # type: ignore[attr-defined]
        session_id = session["id"]
        host_id = session["host_user_id"]
        partner_id = session["partner_user_id"]

        collab_legs = await db.get_collab_legs(session_id)
        bet_title = "\n".join(L["description"] for L in collab_legs)
        bet_id = await db.add_bet(
            user_id=host_id,
            guild_id=session.get("guild_id") or interaction.guild_id,
            channel_id=session.get("channel_id") or interaction.channel_id,
            event=session.get("event"),
            bet_title=bet_title,
            units=session["host_units"],
            odds=session["host_odds"],
            sport="ufc",
            co_user_id=partner_id,
            is_collab=True,
            partner_units=session["partner_units"],
            partner_odds=session["partner_odds"],
            odds_format=session.get("host_odds_format") or "american",
            partner_odds_format=session.get("partner_odds_format") or "american",
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
        host = self.bot.get_user(host_id) or await self._safe_fetch_user(host_id)
        partner = self.bot.get_user(partner_id) or await self._safe_fetch_user(partner_id)
        host_unit_value, host_currency = await get_user_settings(db, host_id)
        partner_unit_value, partner_currency = await get_user_settings(db, partner_id)

        canonical_embed = build_bet_embed(
            bet_row,
            unit_value=host_unit_value,
            currency=host_currency,
            user=host,
            co_user=partner,
            co_unit_value=partner_unit_value,
            co_currency=partner_currency,
        )
        persist_view = BetView(bet_id)
        self._register_bet_view(interaction.client, bet_id)

        lobby_id = session.get("message_id")
        edited = False
        try:
            await interaction.response.edit_message(
                content=None,
                embed=canonical_embed,
                view=persist_view,
            )
            edited = True
        except (discord.HTTPException, discord.InteractionResponded):
            edited = False

        if not edited and lobby_id and session.get("channel_id"):
            channel = interaction.client.get_channel(session["channel_id"])
            if channel is None:
                try:
                    channel = await interaction.client.fetch_channel(session["channel_id"])
                except discord.HTTPException:
                    channel = None
            if channel is not None:
                try:
                    lobby = await channel.fetch_message(lobby_id)
                    await lobby.edit(content=None, embed=canonical_embed, view=BetView(bet_id))
                    edited = True
                except discord.HTTPException:
                    pass

        if not interaction.response.is_done():
            await interaction.response.defer()

        await db.set_message_id(bet_id, lobby_id)

        await interaction.followup.send(
            f"✅ Collab `{session['code']}` finalized as bet **#{bet_id}** — "
            "the lobby message is now the slip.",
            ephemeral=True,
        )

        # Best-effort: also let the OTHER member know, with their own numbers.
        other_id = partner_id if interaction.user.id == host_id else host_id
        try:
            other_user = self.bot.get_user(other_id) or await self._safe_fetch_user(other_id)
            if other_user is not None:
                other_bet = personalize_collab_bet(bet_row, other_id)
                other_unit_value, other_currency = await get_user_settings(db, other_id)
                other_embed = build_bet_embed(
                    other_bet,
                    unit_value=other_unit_value,
                    currency=other_currency,
                    user=host,
                )
                await other_user.send(
                    content=f"🤝 Your collab slip (bet #{bet_id}) is finalized!",
                    embed=other_embed,
                )
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _safe_fetch_user(self, user_id: int) -> Optional[discord.abc.User]:
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None


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