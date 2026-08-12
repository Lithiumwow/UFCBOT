"""
Persistent View for the Won / Loss / Void buttons on each bet control slip.

Control slips are **ephemeral** (owner-only). Public shares use ShareDestinationView
to post a button-free embed into a chosen channel/server.

Persistence works by:
1. Buttons get a stable custom_id of the form "bet:{bet_id}:won" (etc).
2. timeout=None marks the view as persistent.
3. On startup, bot.py re-registers a BetView for every existing bet id.
"""
from __future__ import annotations

import discord

from betting_math import get_user_settings
from embeds import build_bet_embed, build_results_embed


async def _build_slip_embed(client: discord.Client, bet: dict) -> discord.Embed:
    bettor = client.get_user(bet["user_id"])
    if bettor is None:
        try:
            bettor = await client.fetch_user(bet["user_id"])
        except discord.NotFound:
            bettor = None
    db = client.db  # type: ignore[attr-defined]
    unit_value, currency = await get_user_settings(db, bet["user_id"])
    return build_bet_embed(bet, unit_value=unit_value, currency=currency, user=bettor)


async def _build_card_embed(
    client: discord.Client, *, event: str, sport: str, user_id: int
) -> discord.Embed | None:
    db = client.db  # type: ignore[attr-defined]
    bets = await db.get_bets_for_event_matching(event, sport, user_id)
    unit_value, currency = await get_user_settings(db, user_id)
    bettor = client.get_user(user_id)
    if bettor is None:
        try:
            bettor = await client.fetch_user(user_id)
        except discord.NotFound:
            bettor = None
    icon = bettor.display_avatar.url if bettor is not None else None
    return build_results_embed(
        title=event,
        bets=bets,
        unit_value=unit_value,
        currency=currency,
        icon_url=icon,
        include_bet_list=True,
    )


async def _channels_user_can_share(
    guild: discord.Guild, user: discord.abc.User, me: discord.Member | None
) -> list[discord.abc.GuildChannel]:
    """Text-like channels both the bot and user can send messages in."""
    out: list[discord.abc.GuildChannel] = []
    for ch in guild.text_channels:
        if me is not None:
            bot_perms = ch.permissions_for(me)
            if not (bot_perms.view_channel and bot_perms.send_messages):
                continue
        member = guild.get_member(user.id)
        if member is not None:
            user_perms = ch.permissions_for(member)
            if not (user_perms.view_channel and user_perms.send_messages):
                continue
        out.append(ch)
    return out[:25]


class BetView(discord.ui.View):
    def __init__(self, bet_id: int):
        super().__init__(timeout=None)
        self.bet_id = bet_id

        won_btn = discord.ui.Button(
            label="Won", style=discord.ButtonStyle.success, custom_id=f"bet:{bet_id}:won"
        )
        loss_btn = discord.ui.Button(
            label="Loss", style=discord.ButtonStyle.danger, custom_id=f"bet:{bet_id}:loss"
        )
        void_btn = discord.ui.Button(
            label="Void", style=discord.ButtonStyle.secondary, custom_id=f"bet:{bet_id}:void"
        )
        edit_btn = discord.ui.Button(
            label="✏️ Edit", style=discord.ButtonStyle.secondary, custom_id=f"bet:{bet_id}:edit"
        )
        delete_btn = discord.ui.Button(
            label="🗑️ Delete", style=discord.ButtonStyle.danger, custom_id=f"bet:{bet_id}:delete"
        )
        share_btn = discord.ui.Button(
            label="📤 Share",
            style=discord.ButtonStyle.primary,
            custom_id=f"bet:{bet_id}:share",
            row=1,
        )

        won_btn.callback = self._make_callback("won")
        loss_btn.callback = self._make_callback("loss")
        void_btn.callback = self._make_callback("void")
        edit_btn.callback = self._edit_callback
        delete_btn.callback = self._delete_callback
        share_btn.callback = self._share_callback

        self.add_item(won_btn)
        self.add_item(loss_btn)
        self.add_item(void_btn)
        self.add_item(edit_btn)
        self.add_item(delete_btn)
        self.add_item(share_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        db = interaction.client.db  # type: ignore[attr-defined]
        bet = await db.get_bet(self.bet_id)
        if bet is None:
            await interaction.response.send_message(
                "This bet no longer exists in the database.", ephemeral=True
            )
            return False
        if interaction.user.id != bet["user_id"]:
            await interaction.response.send_message(
                "🚫 These aren't your buttons to press -- this is someone else's bet.",
                ephemeral=True,
            )
            return False
        return True

    async def _delete_callback(self, interaction: discord.Interaction):
        db = interaction.client.db  # type: ignore[attr-defined]
        bet = await db.get_bet(self.bet_id)
        if bet is None:
            await interaction.response.send_message(
                "This bet no longer exists in the database.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            view=ConfirmDeleteView(bet_id=self.bet_id, original_view=self)
        )

    async def _edit_callback(self, interaction: discord.Interaction):
        db = interaction.client.db  # type: ignore[attr-defined]
        bet = await db.get_bet(self.bet_id)
        if bet is None:
            await interaction.response.send_message(
                "This bet no longer exists in the database.", ephemeral=True
            )
            return

        await interaction.response.send_modal(EditBetModal(bet))

    async def _share_callback(self, interaction: discord.Interaction):
        """Open ephemeral destination picker (this server's channels + other servers)."""
        # Defer first — listing mutual servers can exceed Discord's 3s reply window.
        await interaction.response.defer(ephemeral=True)
        view = await ShareDestinationView.create(
            invoker_id=interaction.user.id,
            interaction=interaction,
            bet_id=self.bet_id,
        )
        await interaction.followup.send(
            "📤 **Share slip** (no grading buttons on the posted message)\n"
            "• Use the channel menu for **this server**, or\n"
            "• Pick **another server**, then a channel.",
            view=view,
            ephemeral=True,
        )

    def _make_callback(self, status: str):
        async def callback(interaction: discord.Interaction):
            db = interaction.client.db  # type: ignore[attr-defined]
            bet = await db.get_bet(self.bet_id)
            if bet is None:
                await interaction.response.send_message(
                    "This bet no longer exists in the database.", ephemeral=True
                )
                return

            await db.update_status(self.bet_id, status)
            await db.update_all_legs_status(self.bet_id, status)
            updated_bet = await db.get_bet(self.bet_id)

            embed = await _build_slip_embed(interaction.client, updated_bet)
            await interaction.response.edit_message(embed=embed, view=self)

        return callback


class CardShareView(discord.ui.View):
    """Share button attached to `/card` results."""

    def __init__(self, *, event: str, invoker_id: int, sport: str = "ufc"):
        super().__init__(timeout=600)
        self.event = event
        self.invoker_id = invoker_id
        self.sport = sport

        share_btn = discord.ui.Button(
            label="📤 Share",
            style=discord.ButtonStyle.primary,
        )
        share_btn.callback = self._share_callback
        self.add_item(share_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "🚫 These aren't your buttons to press.", ephemeral=True
            )
            return False
        return True

    async def _share_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = await ShareDestinationView.create(
            invoker_id=interaction.user.id,
            interaction=interaction,
            card_event=self.event,
            card_sport=self.sport,
        )
        await interaction.followup.send(
            "📤 **Share card** (results summary for this event)\n"
            "• Use the channel menu for **this server**, or\n"
            "• Pick **another server**, then a channel.",
            view=view,
            ephemeral=True,
        )


class ShareDestinationView(discord.ui.View):
    """Pick a channel in this server, or jump to another mutual server then channel."""

    def __init__(
        self,
        *,
        invoker_id: int,
        other_guild_options: list[discord.SelectOption],
        bet_id: int | None = None,
        card_event: str | None = None,
        card_sport: str = "ufc",
    ):
        super().__init__(timeout=180)
        self.bet_id = bet_id
        self.card_event = card_event
        self.card_sport = card_sport
        self.invoker_id = invoker_id

        ch_select = discord.ui.ChannelSelect(
            placeholder="Channel in this server…",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
            ],
            min_values=1,
            max_values=1,
            row=0,
        )
        ch_select.callback = self._on_this_server_channel
        self.add_item(ch_select)

        if other_guild_options:
            guild_select = discord.ui.Select(
                placeholder="Or pick another server…",
                options=other_guild_options[:25],
                min_values=1,
                max_values=1,
                row=1,
            )
            guild_select.callback = self._on_other_guild
            self.add_item(guild_select)

    @classmethod
    async def create(
        cls,
        *,
        invoker_id: int,
        interaction: discord.Interaction,
        bet_id: int | None = None,
        card_event: str | None = None,
        card_sport: str = "ufc",
    ) -> ShareDestinationView:
        other: list[discord.SelectOption] = []
        here = interaction.guild
        # Cache only — no per-guild fetch_member (that caused Share timeouts).
        for guild in interaction.client.guilds:
            if here is not None and guild.id == here.id:
                continue
            if guild.get_member(invoker_id) is None:
                continue
            label = guild.name[:100] or f"Server {guild.id}"
            other.append(
                discord.SelectOption(
                    label=label,
                    value=str(guild.id),
                    description=f"Post on {label}"[:100],
                )
            )
            if len(other) >= 25:
                break
        return cls(
            bet_id=bet_id,
            card_event=card_event,
            card_sport=card_sport,
            invoker_id=invoker_id,
            other_guild_options=other,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "🚫 This share picker isn't yours.", ephemeral=True
            )
            return False
        return True

    async def _reply(
        self, interaction: discord.Interaction, content: str, **kwargs
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, **kwargs)
        else:
            await interaction.response.send_message(content, ephemeral=True, **kwargs)

    async def _post_to_channel(
        self, interaction: discord.Interaction, channel: discord.abc.Messageable
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            me = channel.guild.me
            if me is not None:
                perms = channel.permissions_for(me)
                if not (perms.view_channel and perms.send_messages):
                    where = getattr(channel, "mention", str(channel))
                    await self._reply(
                        interaction,
                        f"❌ I can't post in {where} (need View + Send Messages).",
                    )
                    return

        if self.bet_id is not None:
            db = interaction.client.db  # type: ignore[attr-defined]
            bet = await db.get_bet(self.bet_id)
            if bet is None:
                await self._reply(interaction, "This bet no longer exists.")
                return
            embed = await _build_slip_embed(interaction.client, bet)
            ok_label = f"bet **#{self.bet_id}**"
        elif self.card_event:
            embed = await _build_card_embed(
                interaction.client,
                event=self.card_event,
                sport=self.card_sport,
                user_id=self.invoker_id,
            )
            if embed is None:
                await self._reply(interaction, "Couldn't build the card summary.")
                return
            ok_label = f"card **{self.card_event}**"
        else:
            await self._reply(interaction, "Nothing to share.")
            return

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            await self._reply(interaction, f"❌ Failed to post: {e}")
            return

        dest = getattr(channel, "mention", None) or str(channel)
        guild_name = ""
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            guild_name = f" · **{channel.guild.name}**"
        await self._reply(
            interaction,
            f"✅ Shared {ok_label} to {dest}{guild_name}.",
        )
        self.stop()

    def _selected_channel_ids(self, interaction: discord.Interaction) -> list[int]:
        ids: list[int] = []
        for item in self.children:
            if isinstance(item, discord.ui.ChannelSelect) and item.values:
                for ch in item.values:
                    ids.append(int(ch.id))
                if ids:
                    return ids
        data = interaction.data or {}
        for raw in data.get("values") or []:
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        return ids

    async def _on_this_server_channel(self, interaction: discord.Interaction):
        ids = self._selected_channel_ids(interaction)
        if not ids:
            await self._reply(interaction, "No channel selected.")
            return

        channel_id = ids[0]
        channel = interaction.client.get_channel(channel_id)
        if channel is None and interaction.guild is not None:
            channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None
        if channel is None:
            await self._reply(interaction, "Couldn't open that channel.")
            return
        await self._post_to_channel(interaction, channel)

    async def _on_other_guild(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        guild_id = None
        for item in self.children:
            if isinstance(item, discord.ui.ChannelSelect):
                continue
            if isinstance(item, discord.ui.Select) and item.values:
                try:
                    guild_id = int(item.values[0])
                except (TypeError, ValueError):
                    guild_id = None
                break
        if guild_id is None:
            data = interaction.data or {}
            vals = data.get("values") or []
            if vals:
                try:
                    guild_id = int(vals[0])
                except (TypeError, ValueError):
                    guild_id = None

        if guild_id is None:
            await self._reply(interaction, "Invalid server.")
            return

        guild = interaction.client.get_guild(guild_id)
        if guild is None:
            await self._reply(interaction, "I can't reach that server anymore.")
            return

        me = guild.me
        channels = await _channels_user_can_share(guild, interaction.user, me)
        if not channels:
            await self._reply(
                interaction,
                f"No text channels on **{guild.name}** that both you and I can post in.",
            )
            return

        view = ShareGuildChannelView(
            invoker_id=self.invoker_id,
            guild_name=guild.name,
            channels=channels,
            bet_id=self.bet_id,
            card_event=self.card_event,
            card_sport=self.card_sport,
        )
        try:
            await interaction.edit_original_response(
                content=f"📤 Share to **{guild.name}** — pick a channel:",
                view=view,
            )
        except discord.HTTPException:
            await interaction.followup.send(
                f"📤 Share to **{guild.name}** — pick a channel:",
                view=view,
                ephemeral=True,
            )


class ShareGuildChannelView(discord.ui.View):
    """Channel list for a server that isn't the interaction's guild."""

    def __init__(
        self,
        *,
        invoker_id: int,
        guild_name: str,
        channels: list[discord.abc.GuildChannel],
        bet_id: int | None = None,
        card_event: str | None = None,
        card_sport: str = "ufc",
    ):
        super().__init__(timeout=180)
        self.bet_id = bet_id
        self.card_event = card_event
        self.card_sport = card_sport
        self.invoker_id = invoker_id

        options = [
            discord.SelectOption(
                label=f"#{ch.name}"[:100],
                value=str(ch.id),
                description=(ch.guild.name if hasattr(ch, "guild") else guild_name)[:100],
            )
            for ch in channels
        ]
        select = discord.ui.Select(
            placeholder=f"Channel on {guild_name}…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_channel
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "🚫 This share picker isn't yours.", ephemeral=True
            )
            return False
        return True

    async def _on_channel(self, interaction: discord.Interaction):
        channel_id = None
        for item in self.children:
            if isinstance(item, discord.ui.Select) and item.values:
                try:
                    channel_id = int(item.values[0])
                except (TypeError, ValueError):
                    channel_id = None
                break
        if channel_id is None:
            await interaction.response.send_message("Invalid channel.", ephemeral=True)
            return

        channel = interaction.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None
        if channel is None or not isinstance(
            channel, (discord.TextChannel, discord.Thread)
        ):
            await interaction.response.send_message(
                "Couldn't open that channel.", ephemeral=True
            )
            return

        dest = ShareDestinationView(
            bet_id=self.bet_id,
            card_event=self.card_event,
            card_sport=self.card_sport,
            invoker_id=self.invoker_id,
            other_guild_options=[],
        )
        await dest._post_to_channel(interaction, channel)


class ConfirmDeleteView(discord.ui.View):
    """Shown in place of BetView's buttons when Delete is pressed -- a
    misclick shouldn't be able to permanently wipe a bet with no way back."""

    def __init__(self, *, bet_id: int, original_view: BetView):
        super().__init__(timeout=30)
        self.bet_id = bet_id
        self.original_view = original_view

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        db = interaction.client.db  # type: ignore[attr-defined]
        bet = await db.get_bet(self.bet_id)
        if bet is None:
            await interaction.response.send_message(
                "This bet no longer exists in the database.", ephemeral=True
            )
            return False
        if interaction.user.id != bet["user_id"]:
            await interaction.response.send_message(
                "🚫 This isn't your bet to delete.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db  # type: ignore[attr-defined]
        await db.delete_bet(self.bet_id)
        await interaction.response.edit_message(
            content=f"🗑️ Bet #{self.bet_id} deleted — it's removed from `/results`.",
            embed=None,
            view=None,
        )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=self.original_view)
        self.stop()


class ConfirmDeleteEventView(discord.ui.View):
    """Confirmation prompt for /delete-event -- bulk-deletes every bet
    logged against one event, plus best-effort cleanup of their messages."""

    def __init__(self, *, event: str, sport: str, bet_count: int, invoker_id: int):
        super().__init__(timeout=30)
        self.event = event
        self.sport = sport
        self.bet_count = bet_count
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "🚫 You're not authorized to do this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db  # type: ignore[attr-defined]
        bets = await db.get_bets_for_event(self.event, self.sport, self.invoker_id)
        await db.delete_bets_for_event(self.event, self.sport, self.invoker_id)

        deleted_messages = 0
        for bet in bets:
            if not bet.get("channel_id") or not bet.get("message_id"):
                continue
            try:
                channel = interaction.client.get_channel(  # type: ignore[attr-defined]
                    bet["channel_id"]
                ) or await interaction.client.fetch_channel(  # type: ignore[attr-defined]
                    bet["channel_id"]
                )
                msg = await channel.fetch_message(bet["message_id"])
                await msg.delete()
                deleted_messages += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.edit_message(
            content=(
                f"🗑️ Deleted **{self.bet_count}** bet(s) for **{self.event}**"
                + (
                    f" and cleaned up {deleted_messages} message(s)."
                    if deleted_messages
                    else "."
                )
            ),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Cancelled — no bets were deleted.", view=None
        )
        self.stop()


class EditBetModal(discord.ui.Modal):
    """Pre-filled form for correcting an existing bet's details."""

    def __init__(self, bet: dict):
        super().__init__(title=f"Edit Bet #{bet['id']}")
        self.bet_id = bet["id"]

        self.event_input = discord.ui.TextInput(
            label="Event",
            default=bet.get("event") or "",
            required=False,
            max_length=200,
        )
        self.legs_input = discord.ui.TextInput(
            label="Legs (one per line)",
            style=discord.TextStyle.paragraph,
            default=bet.get("bet_title") or "",
            required=False,
            max_length=1000,
        )
        self.units_input = discord.ui.TextInput(
            label="Units",
            default=f"{bet.get('units', 1.0):g}",
            required=True,
            max_length=10,
        )
        self.odds_input = discord.ui.TextInput(
            label="Odds (e.g. -150 or 120, blank = none)",
            default=(str(bet["odds"]) if bet.get("odds") is not None else ""),
            required=False,
            max_length=10,
        )

        for item in (self.event_input, self.legs_input, self.units_input, self.odds_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            units = float(self.units_input.value)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Units must be a number, e.g. `1.5`.", ephemeral=True
            )
            return

        odds_raw = self.odds_input.value.strip()
        odds = None
        if odds_raw:
            try:
                odds = int(odds_raw)
            except ValueError:
                await interaction.response.send_message(
                    "⚠️ Odds must be a whole number, e.g. `-150` or `120`.", ephemeral=True
                )
                return

        event = self.event_input.value.strip() or None
        bet_title = self.legs_input.value.strip() or None

        db = interaction.client.db  # type: ignore[attr-defined]
        await db.update_bet_fields(
            self.bet_id, event=event, bet_title=bet_title, units=units, odds=odds
        )
        updated_bet = await db.get_bet(self.bet_id)
        if not updated_bet:
            await interaction.response.send_message("Bet not found.", ephemeral=True)
            return

        embed = await _build_slip_embed(interaction.client, updated_bet)
        # Modal opened from the ephemeral control slip — edit that message
        try:
            await interaction.response.edit_message(
                embed=embed, view=BetView(self.bet_id)
            )
            return
        except (discord.HTTPException, discord.InteractionResponded):
            pass

        await interaction.response.send_message(
            f"✅ Bet #{self.bet_id} updated.",
            embed=embed,
            ephemeral=True,
        )
