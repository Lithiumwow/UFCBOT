from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import espn
from database import Database
from props_loader import ensure_fightiq_path
from views import BetView

# FightIQ package lives under ./FightIQ/fightiq
ensure_fightiq_path()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ufc-bet-bot")

INTENTS = discord.Intents.default()
# No privileged intents needed -- everything is slash commands and
# component interactions now (the old !eventstart/!eventend prefix
# commands, which needed Message Content, are now /event-start and /event-end).


class UFCBetBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.db = Database(config.DB_PATH)
        self.cached_events: list[dict] = []       # UFC only -- NBA games aren't tracked

    async def setup_hook(self) -> None:
        await self.db.connect()

        # Load cogs
        await self.load_extension("cogs.bets")
        await self.load_extension("cogs.results")
        await self.load_extension("cogs.results_nba")
        await self.load_extension("cogs.grading")
        await self.load_extension("cogs.pl")

        # Re-register a persistent BetView for every bet already stored, so
        # Won/Loss/Void/Delete buttons on old messages keep working after a
        # restart -- across both sports, since BetView itself is sport-agnostic.
        ufc_bets = await self.db.get_all_bets("ufc")
        nba_bets = await self.db.get_all_bets("nba")
        for bet in ufc_bets + nba_bets:
            self.add_view(BetView(bet["id"]))
        log.info(
            "Re-registered persistent views for %d existing bet(s) (%d UFC, %d NBA).",
            len(ufc_bets) + len(nba_bets), len(ufc_bets), len(nba_bets),
        )

        # Kick off the ESPN refresh loop (runs immediately, then on interval)
        self.refresh_events_loop.change_interval(hours=config.EVENT_REFRESH_HOURS)
        self.refresh_events_loop.start()

        # Bot is locked to a fixed set of users (see config.ALLOWED_USER_IDS).
        # Give anyone else a clean ephemeral message instead of a silent failure.
        async def on_app_command_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ):
            if isinstance(error, app_commands.CheckFailure):
                msg = "🚫 You're not authorized to use this bot."
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
                return
            log.exception("Unhandled app command error", exc_info=error)

        self.tree.on_error = on_app_command_error

        # Sync slash commands
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to guild %s.", len(synced), config.GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d command(s) globally (may take up to ~1hr to propagate).", len(synced))

    @tasks.loop(hours=72)
    async def refresh_events_loop(self):
        try:
            import card_data

            self.cached_events = await card_data.fetch_upcoming_events(limit=12)
            log.info(
                "Refreshed UFC event cache (%d): %s",
                len(self.cached_events),
                ", ".join(
                    (
                        f"[LIVE] {e['short_name']}"
                        if e.get("is_live")
                        else f"{e['short_name']}" + (f" ({e.get('source')})" if e.get("source") else "")
                    )
                    for e in self.cached_events
                )
                or "(none found)",
            )
        except Exception:
            log.exception("Failed to refresh UFC events; keeping previous cache.")
            # ESPN-only emergency fallback
            try:
                self.cached_events = await espn.fetch_upcoming_events("ufc", limit=5)
            except Exception:
                pass

    @refresh_events_loop.before_loop
    async def before_refresh_events_loop(self):
        await self.wait_until_ready()

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        # Reply-to-a-bet-message + @mention the bot, with "delete" in the
        # reply, deletes that bet. (Editing is now done via the ✏️ Edit
        # button on the bet card itself, not through this reply flow.)
        if message.reference and self.user in message.mentions:
            if message.author.id not in config.ALLOWED_USER_IDS:
                return  # bot is locked to specific users; silently ignore anyone else

            if "delete" not in message.content.lower():
                return

            bet, target = await self._resolve_reply_target(message)
            if bet is None:
                return

            await self._handle_delete(message, bet, target)

        # No prefix commands are registered anymore, but this stays as a
        # harmless no-op hook in case one gets added later.
        await self.process_commands(message)

    async def _resolve_reply_target(
        self, message: discord.Message
    ) -> tuple[dict | None, discord.Message | None]:
        ref = message.reference
        target = ref.resolved
        if target is None or isinstance(target, discord.DeletedReferencedMessage):
            try:
                target = await message.channel.fetch_message(ref.message_id)  # type: ignore[arg-type]
            except (discord.NotFound, discord.HTTPException):
                await message.reply(
                    "⚠️ I couldn't find the message you replied to.", mention_author=False
                )
                return None, None

        if target.author.id != self.user.id:  # type: ignore[union-attr]
            await message.reply("⚠️ That's not a bet message I posted.", mention_author=False)
            return None, None

        bet = await self.db.get_bet_by_message_id(target.id)
        if bet is None:
            await message.reply(
                "⚠️ I couldn't find a logged bet for that message.", mention_author=False
            )
            return None, None

        if bet["user_id"] != message.author.id:
            await message.reply(
                "🚫 This isn't your bet -- you can only edit or delete bets you logged yourself.",
                mention_author=False,
            )
            return None, None

        return bet, target

    async def _handle_delete(
        self, message: discord.Message, bet: dict, target: discord.Message
    ) -> None:
        await self.db.delete_bet(bet["id"])
        log.info("Deleted bet #%d via reply+mention from %s.", bet["id"], message.author)

        try:
            await target.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        confirmation = (
            f"🗑️ Deleted bet #{bet['id']}"
            + (f" ({bet['bet_title'].splitlines()[0]})" if bet.get("bet_title") else "")
            + " — it's removed from `/results`."
        )
        try:
            await message.channel.send(confirmation)
        except discord.HTTPException:
            pass

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


def main():
    bot = UFCBetBot()

    @bot.event
    async def on_ready():
        log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)

    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()