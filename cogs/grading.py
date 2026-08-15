"""
/event-start / /event-end -- slash commands that start/stop focused polling for an event.

Background loop also auto-discovers any UFC event with pending gradeable legs and
checks ESPN every 1 minute.

/grade -- manual settle for pending slips.
/rescan -- re-check every structured leg for an event (any status, not just
pending) against fresh ESPN data and correct anything wrong -- including
reverting a bad prior grade back to pending if it can no longer be confirmed.
"""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import card_data
import config
import espn
import grading
from betting_math import get_user_settings, personalize_collab_bet
from checks import is_admin
from embeds import build_bet_embed
from leg_rematch import rematch_bets_to_card
from prop_play_map import rebuild_description_from_stored
from views import BetView, EditBetModal

log = logging.getLogger("ufc-bet-bot.grading")


def _bet_label(bet: dict) -> str:
    """Short label for select menus -- '#12 · Event · first leg… · WON'."""
    title = (bet.get("bet_title") or "Untitled").splitlines()[0]
    if len(title) > 40:
        title = title[:37] + "…"
    event = bet.get("event") or "No event"
    if len(event) > 28:
        event = event[:25] + "…"
    status = bet.get("status") or "pending"
    status_suffix = "" if status == "pending" else f" · {status.upper()}"
    return f"#{bet['id']} · {event} · {title}{status_suffix}"[:100]


class GradeResultView(discord.ui.View):
    """Won / Loss / Void for one selected pending slip."""

    def __init__(self, *, bet_id: int, invoker_id: int):
        super().__init__(timeout=120)
        self.bet_id = bet_id
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "🚫 This isn't your grading prompt.", ephemeral=True
            )
            return False
        return True

    async def _settle(self, interaction: discord.Interaction, status: str) -> None:
        db = interaction.client.db  # type: ignore[attr-defined]
        bet = await db.get_bet(self.bet_id)
        if bet is None:
            await interaction.response.edit_message(
                content=f"Bet #{self.bet_id} no longer exists.", embed=None, view=None
            )
            self.stop()
            return

        if bet["user_id"] != interaction.user.id:
            await interaction.response.send_message(
                "🚫 You can only grade your own slips.", ephemeral=True
            )
            return

        await db.update_status(self.bet_id, status)
        await db.update_all_legs_status(self.bet_id, status)
        updated = await db.get_bet(self.bet_id)

        unit_value, currency = await get_user_settings(db, interaction.user.id)
        embed = build_bet_embed(
            personalize_collab_bet(updated, interaction.user.id),
            unit_value=unit_value, currency=currency, user=interaction.user,
        )

        # Best-effort redraw of the original bet message + persistent buttons.
        redrawn = False
        if updated.get("channel_id") and updated.get("message_id"):
            try:
                channel = interaction.client.get_channel(
                    updated["channel_id"]
                ) or await interaction.client.fetch_channel(updated["channel_id"])
                msg = await channel.fetch_message(updated["message_id"])
                await msg.edit(embed=embed, view=BetView(self.bet_id))
                redrawn = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        label = {"won": "✅ Won", "loss": "❌ Loss", "void": "➖ Void"}[status]
        note = "" if redrawn else "\n*(Original bet message not found — DB is still updated.)*"
        await interaction.response.edit_message(
            content=f"Graded bet **#{self.bet_id}** as **{label}**.{note}",
            embed=embed,
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Won", style=discord.ButtonStyle.success)
    async def won(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._settle(interaction, "won")

    @discord.ui.button(label="Loss", style=discord.ButtonStyle.danger)
    async def loss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._settle(interaction, "loss")

    @discord.ui.button(label="Void", style=discord.ButtonStyle.secondary)
    async def void(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._settle(interaction, "void")

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"⚠️ Permanently delete bet **#{self.bet_id}**? This can't be undone.",
            embed=None,
            view=GradeDeleteConfirmView(bet_id=self.bet_id, invoker_id=self.invoker_id),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Cancelled — nothing graded.", embed=None, view=None
        )
        self.stop()


class GradeDeleteConfirmView(discord.ui.View):
    """Shown when Delete is pressed from the /grade flow -- same
    confirm-before-permanently-wiping-it safety pattern as the normal
    BetView Delete button."""

    def __init__(self, *, bet_id: int, invoker_id: int):
        super().__init__(timeout=30)
        self.bet_id = bet_id
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "🚫 This isn't your grading prompt.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db  # type: ignore[attr-defined]
        bet = await db.get_bet(self.bet_id)
        if bet is None:
            await interaction.response.edit_message(
                content=f"Bet #{self.bet_id} no longer exists.", embed=None, view=None
            )
            self.stop()
            return
        if interaction.user.id != bet["user_id"] and interaction.user.id != bet.get("co_user_id"):
            await interaction.response.send_message(
                "🚫 You can only delete your own slips.", ephemeral=True
            )
            return

        await db.delete_bet(self.bet_id)

        # Best-effort: also update the original bet message so it doesn't
        # sit there looking gradeable/settleable forever.
        if bet.get("channel_id") and bet.get("message_id"):
            try:
                channel = interaction.client.get_channel(
                    bet["channel_id"]
                ) or await interaction.client.fetch_channel(bet["channel_id"])
                msg = await channel.fetch_message(bet["message_id"])
                await msg.edit(content="🗑️ *This bet was deleted.*", embed=None, view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.edit_message(
            content=f"🗑️ Bet **#{self.bet_id}** deleted completely.", embed=None, view=None
        )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db  # type: ignore[attr-defined]
        bet = await db.get_bet(self.bet_id)
        if bet is None:
            await interaction.response.edit_message(
                content=f"Bet #{self.bet_id} no longer exists.", embed=None, view=None
            )
            self.stop()
            return
        unit_value, currency = await get_user_settings(db, interaction.user.id)
        embed = build_bet_embed(
            personalize_collab_bet(bet, interaction.user.id),
            unit_value=unit_value, currency=currency, user=interaction.user,
        )
        await interaction.response.edit_message(
            content=f"Grade **bet #{self.bet_id}** — choose a result:",
            embed=embed,
            view=GradeResultView(bet_id=self.bet_id, invoker_id=self.invoker_id),
        )
        self.stop()


class GradeSelect(discord.ui.Select):
    def __init__(self, bets: list[dict], invoker_id: int):
        options = [
            discord.SelectOption(
                label=_bet_label(b)[:100],
                value=str(b["id"]),
                description=f"{b.get('units') or 1:g}u"
                + (f" @ {b['odds']:+d}" if b.get("odds") is not None else ""),
            )
            for b in bets[:25]
        ]
        super().__init__(
            placeholder="Pick a pending slip to grade…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.invoker_id = invoker_id

    async def callback(self, interaction: discord.Interaction):
        bet_id = int(self.values[0])
        db = interaction.client.db  # type: ignore[attr-defined]
        bet = await db.get_bet(bet_id)
        if bet is None or bet["status"] != "pending":
            await interaction.response.send_message(
                "That slip is gone or already graded.", ephemeral=True
            )
            return

        unit_value, currency = await get_user_settings(db, interaction.user.id)
        embed = build_bet_embed(
            personalize_collab_bet(bet, interaction.user.id),
            unit_value=unit_value, currency=currency, user=interaction.user,
        )
        await interaction.response.edit_message(
            content=f"Grade **bet #{bet_id}** — choose a result:",
            embed=embed,
            view=GradeResultView(bet_id=bet_id, invoker_id=self.invoker_id),
        )


class GradeSelectView(discord.ui.View):
    def __init__(self, bets: list[dict], invoker_id: int):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.add_item(GradeSelect(bets, invoker_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "🚫 This isn't your grading prompt.", ephemeral=True
            )
            return False
        return True


class EditSelect(discord.ui.Select):
    def __init__(self, bets: list[dict], invoker_id: int):
        options = [
            discord.SelectOption(
                label=_bet_label(b)[:100],
                value=str(b["id"]),
                description=f"{b.get('units') or 1:g}u"
                + (f" @ {b['odds']:+d}" if b.get("odds") is not None else ""),
            )
            for b in bets[:25]
        ]
        super().__init__(
            placeholder="Pick a bet to edit…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.invoker_id = invoker_id

    async def callback(self, interaction: discord.Interaction):
        bet_id = int(self.values[0])
        db = interaction.client.db  # type: ignore[attr-defined]
        bet = await db.get_bet(bet_id)
        if bet is None:
            await interaction.response.send_message(
                "That bet no longer exists.", ephemeral=True
            )
            return
        if interaction.user.id != bet["user_id"] and interaction.user.id != bet.get("co_user_id"):
            await interaction.response.send_message(
                "🚫 You can only edit your own slips.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content=f"Bet **#{bet_id}** — what would you like to do?",
            view=EditOrDeleteView(bet, interaction.user.id),
        )


class EditOrDeleteView(discord.ui.View):
    """Shown once a bet is picked via /edit-bet -- Edit or Delete live
    together here as sub-options of one command, rather than needing a
    separate top-level command just for deleting."""

    def __init__(self, bet: dict, viewer_id: int):
        super().__init__(timeout=120)
        self.bet = bet
        self.viewer_id = viewer_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "🚫 This isn't your editing prompt.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✏️ Edit Details", style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditBetModal(self.bet, viewer_id=self.viewer_id))

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # A collab bet is one shared row -- either person deleting it
        # removes it for both, there's no "just my half" to delete.
        note = (
            "\n\n⚠️ This is a **collab** slip — deleting it removes it for both of you."
            if self.bet.get("is_collab") else ""
        )
        await interaction.response.edit_message(
            content=f"⚠️ Permanently delete bet **#{self.bet['id']}**? This can't be undone.{note}",
            embed=None,
            view=GradeDeleteConfirmView(bet_id=self.bet["id"], invoker_id=self.viewer_id),
        )


class EditSelectView(discord.ui.View):
    def __init__(self, bets: list[dict], invoker_id: int):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.add_item(EditSelect(bets, invoker_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "🚫 This isn't your editing prompt.", ephemeral=True
            )
            return False
        return True


class GradingCog(commands.Cog):
    # How long to wait after a fight first shows as completed before
    # actually grading it -- ESPN's result text is sometimes incomplete
    # right when "Final" first appears (e.g. missing method/round) and
    # fills in more detail a bit after. Grading immediately can catch it
    # mid-update; a short buffer avoids that.
    SETTLE_BUFFER_SECONDS = 120.0

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._first_seen_completed: dict[str, float] = {}  # fight key -> monotonic timestamp
        self.grading_loop.start()

    def cog_unload(self):
        self.grading_loop.cancel()

    @staticmethod
    def _fight_key(event: str, fighter_a: str, fighter_b: str) -> str:
        return f"{event}::{fighter_a}::{fighter_b}"

    def _ready_to_settle(self, event: str, result: dict) -> bool:
        """True once a completed fight has been observed as completed for
        at least SETTLE_BUFFER_SECONDS. The first time we see it
        completed, we start the clock and hold off grading it -- not
        reset on every pass, so it settles SETTLE_BUFFER_SECONDS after
        first going final, not SETTLE_BUFFER_SECONDS after every check."""
        if not result.get("completed"):
            return False
        key = self._fight_key(event, result["fighter_a"], result["fighter_b"])
        now = time.monotonic()
        first_seen = self._first_seen_completed.get(key)
        if first_seen is None:
            self._first_seen_completed[key] = now
            return False
        return (now - first_seen) >= self.SETTLE_BUFFER_SECONDS

    # ---------- slash: /grade ----------

    async def event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        db = self.bot.db  # type: ignore[attr-defined]
        events = await db.get_distinct_events("ufc", interaction.user.id)
        current_lower = current.lower()
        matches = [e for e in events if current_lower in e.lower()]
        return [app_commands.Choice(name=e[:100], value=e[:100]) for e in matches[:25]]

    async def bet_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        db = self.bot.db  # type: ignore[attr-defined]
        pending = await db.get_pending_bets("ufc", interaction.user.id)
        current_lower = current.lower()
        choices = []
        for b in pending:
            label = _bet_label(b)
            if current_lower and current_lower not in label.lower() and current not in str(b["id"]):
                continue
            choices.append(app_commands.Choice(name=label[:100], value=str(b["id"])))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(
        name="grade",
        description="Manually grade a pending UFC slip (Won / Loss / Void)",
    )
    @is_admin()
    @app_commands.describe(
        bet_id="Optional: grade this bet id directly (autocompletes from pending slips)",
        event="Optional: only list pending slips for this event",
    )
    @app_commands.autocomplete(bet_id=bet_id_autocomplete, event=event_autocomplete)
    async def grade(
        self,
        interaction: discord.Interaction,
        bet_id: str | None = None,
        event: str | None = None,
    ):
        db = self.bot.db  # type: ignore[attr-defined]

        if bet_id is not None:
            try:
                bid = int(bet_id)
            except ValueError:
                await interaction.response.send_message(
                    "⚠️ `bet_id` must be a number.", ephemeral=True
                )
                return

            bet = await db.get_bet(bid)
            if bet is None:
                await interaction.response.send_message(
                    f"No bet found with id **#{bid}**.", ephemeral=True
                )
                return
            if not db.user_can_access_bet(bet, interaction.user.id):
                await interaction.response.send_message(
                    "🚫 You can only grade your own slips.", ephemeral=True
                )
                return
            if bet.get("sport") != "ufc":
                await interaction.response.send_message(
                    "⚠️ `/grade` is for UFC slips. Settle NBA slips from their bet cards.",
                    ephemeral=True,
                )
                return

            unit_value, currency = await get_user_settings(db, interaction.user.id)
            embed = build_bet_embed(
                personalize_collab_bet(bet, interaction.user.id),
                unit_value=unit_value, currency=currency, user=interaction.user,
            )
            already_graded_note = (
                f" (currently **{bet['status'].upper()}** — this will change it)"
                if bet["status"] != "pending" else ""
            )
            await interaction.response.send_message(
                content=f"Grade **bet #{bid}**{already_graded_note} — choose a result:",
                embed=embed,
                view=GradeResultView(bet_id=bid, invoker_id=interaction.user.id),
            )
            return

        pending = await db.get_pending_bets("ufc", interaction.user.id, event=event)
        if not pending:
            scope = f" for **{event}**" if event else ""
            await interaction.response.send_message(
                f"No pending UFC slips{scope}. Nothing to grade.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            content=(
                f"**{len(pending)}** pending slip(s)"
                + (f" for **{event}**" if event else "")
                + " — pick one to grade:"
            ),
            view=GradeSelectView(pending, interaction.user.id),
        )

    # ---------- /edit-bet ----------

    @app_commands.command(
        name="edit-bet",
        description="Edit or delete a bet you've already logged",
    )
    @is_admin()
    @app_commands.describe(
        bet_id="Optional: edit this bet id directly (autocompletes from your slips)",
        event="Optional: only list slips for this event",
    )
    @app_commands.autocomplete(bet_id=bet_id_autocomplete, event=event_autocomplete)
    async def edit_bet(
        self,
        interaction: discord.Interaction,
        bet_id: str | None = None,
        event: str | None = None,
    ):
        db = self.bot.db  # type: ignore[attr-defined]

        if bet_id is not None:
            try:
                bid = int(bet_id)
            except ValueError:
                await interaction.response.send_message(
                    "⚠️ `bet_id` must be a number.", ephemeral=True
                )
                return

            bet = await db.get_bet(bid)
            if bet is None:
                await interaction.response.send_message(
                    f"No bet found with id **#{bid}**.", ephemeral=True
                )
                return
            if interaction.user.id != bet["user_id"] and interaction.user.id != bet.get("co_user_id"):
                await interaction.response.send_message(
                    "🚫 You can only edit your own slips.", ephemeral=True
                )
                return

            await interaction.response.send_message(
                content=f"Bet **#{bid}** — what would you like to do?",
                view=EditOrDeleteView(bet, interaction.user.id),
                ephemeral=True,
            )
            return

        all_bets = await db.get_all_bets("ufc", interaction.user.id)
        if event:
            from card_data import _event_match_score
            all_bets = [
                b for b in all_bets
                if (b.get("event") or "") and _event_match_score(event, b["event"]) >= 70
            ]
        if not all_bets:
            scope = f" for **{event}**" if event else ""
            await interaction.response.send_message(
                f"No UFC slips{scope} found. Nothing to edit.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            content=(
                f"**{len(all_bets)}** slip(s)"
                + (f" for **{event}**" if event else "")
                + " — pick one to edit:"
            ),
            view=EditSelectView(all_bets, interaction.user.id),
        )

    # ---------- /rescan ----------

    @app_commands.command(
        name="rescan",
        description="Re-check ESPN results for an event and correct any wrongly-graded bets",
    )
    @is_admin()
    @app_commands.describe(
        event="Which event to rescan (autocompletes from events you've logged bets against)"
    )
    @app_commands.autocomplete(event=event_autocomplete)
    async def rescan(self, interaction: discord.Interaction, event: str):
        await interaction.response.defer()
        db = self.bot.db  # type: ignore[attr-defined]

        bets = await db.get_bets_for_event_matching(event, "ufc", interaction.user.id)
        if not bets:
            await interaction.followup.send(f"No bets found for **{event}**.", ephemeral=True)
            return

        try:
            fight_results = await espn.fetch_fight_results(event)
        except Exception:
            log.exception("Rescan: couldn't fetch ESPN results for %r.", event)
            await interaction.followup.send(
                "⚠️ Couldn't fetch ESPN results right now — try again shortly.", ephemeral=True
            )
            return

        if not fight_results:
            await interaction.followup.send(
                f"ESPN doesn't have result data for **{event}** yet.", ephemeral=True
            )
            return

        # (bet_id, leg_or_bet_label, old_status, new_status)
        corrections: list[tuple[int, str, str, str]] = []
        checked_legs = 0
        still_pending = 0
        unmatched: list[tuple[int, str, str]] = []  # (bet_id, fighter_pick, current_status)
        unresolved_details: list[tuple[int, str, str]] = []  # (bet_id, fighter_pick, raw_espn_text)

        for bet in bets:
            legs = await db.get_legs_for_bet(bet["id"])
            structured_legs = [leg for leg in legs if leg.get("fighter_pick") and leg.get("outcome_type")]
            if not structured_legs:
                continue  # nothing rescan can do for a free-text-only bet

            for leg in structured_legs:
                checked_legs += 1
                old_leg_status = leg["status"]

                result = grading.find_result_for_bet(leg, fight_results)
                if result is None:
                    # Couldn't find this fighter anywhere in the event's
                    # results at all -- surfaced explicitly rather than
                    # buried in "still pending", since this is usually a
                    # name-spelling mismatch (e.g. "Hasan" vs "Hassan")
                    # that needs a look, not just more waiting.
                    unmatched.append((bet["id"], leg["fighter_pick"], old_leg_status))
                    continue

                outcome = grading.grade_bet(leg, result)
                if outcome is None:
                    # Can't confidently grade this leg with current data/logic.
                    # If it was previously settled (possibly wrongly), walk it
                    # back to pending rather than leave a stale grade in place.
                    if old_leg_status != "pending":
                        await db.update_leg_status(leg["id"], "pending")
                        corrections.append((bet["id"], leg["description"], old_leg_status, "pending"))
                    else:
                        still_pending += 1
                        raw = (result.get("raw_text") or "")[:150]
                        unresolved_details.append((bet["id"], leg["fighter_pick"], raw))
                    continue

                new_leg_status, _method_confirmed = outcome
                if new_leg_status != old_leg_status:
                    await db.update_leg_status(leg["id"], new_leg_status)
                    corrections.append((bet["id"], leg["description"], old_leg_status, new_leg_status))

            # Re-aggregate this bet's overall status from its current (now
            # possibly corrected) leg statuses -- can move in either
            # direction: won/loss -> pending (walked back), or a stale
            # won/loss -> the actually-correct won/loss.
            all_legs = await db.get_legs_for_bet(bet["id"])
            statuses = [
                leg["status"] if (leg["fighter_pick"] and leg["outcome_type"]) else "pending"
                for leg in all_legs
            ]
            overall = grading.aggregate_bet_status(statuses)
            old_bet_status = bet["status"]
            resolved_overall = overall if overall is not None else "pending"

            if resolved_overall != old_bet_status:
                await db.update_status(bet["id"], resolved_overall)
                corrections.append((bet["id"], "(overall bet)", old_bet_status, resolved_overall))
                await self._redraw_bet_message(bet["id"])

        lines = [
            f"🔍 Rescanned **{event}** — checked {checked_legs} structured leg(s) "
            f"across {len(bets)} tracked bet(s)."
        ]
        if corrections:
            lines.append(f"\n**{len(corrections)} correction(s) made:**")
            for bet_id, label, old, new in corrections[:15]:
                lines.append(f"  • Bet #{bet_id} — {label}: `{old}` → `{new}`")
            if len(corrections) > 15:
                lines.append(f"  …and {len(corrections) - 15} more.")
        else:
            lines.append("No corrections needed — everything already matches ESPN's current data.")
        if unmatched:
            lines.append(
                f"\n⚠️ **{len(unmatched)} leg(s) couldn't be matched to a fighter on this card at all** "
                "-- usually a name-spelling mismatch (e.g. logged as \"Hasan\" but ESPN has \"Hassan\"). "
                "These were left as-is, not corrected:"
            )
            for bet_id, fighter_pick, status in unmatched[:10]:
                lines.append(f"  • Bet #{bet_id} — **{fighter_pick}** (currently `{status}`)")
            if len(unmatched) > 10:
                lines.append(f"  …and {len(unmatched) - 10} more.")
        if still_pending:
            lines.append(
                f"\n{still_pending} leg(s) found on the card but still can't be graded "
                "(fight not finished, or method/round not yet parseable):"
            )
            for bet_id, fighter_pick, raw in unresolved_details[:10]:
                snippet = f" — ESPN says: _{raw}_" if raw else " — ESPN gave no result text at all yet"
                lines.append(f"  • Bet #{bet_id} — **{fighter_pick}**{snippet}")
            if len(unresolved_details) > 10:
                lines.append(f"  …and {len(unresolved_details) - 10} more.")

        await interaction.followup.send("\n".join(lines))
        log.info(
            "Rescan of '%s' (requested by %s): %d correction(s), %d unmatched, %d still pending.",
            event, interaction.user, len(corrections), len(unmatched), still_pending,
        )

    # ---------- /fix-descriptions ----------

    @app_commands.command(
        name="fix-descriptions",
        description="Rebuild leg/bet text for already-logged bets using the current formatting rules",
    )
    @is_admin()
    @app_commands.describe(
        event="Which event to fix (autocompletes from events you've logged bets against)"
    )
    @app_commands.autocomplete(event=event_autocomplete)
    async def fix_descriptions(self, interaction: discord.Interaction, event: str):
        """Fixing how NEW bets get their description text built (in
        prop_play_map.py) doesn't retroactively rewrite text already
        stored for bets logged before that fix existed. This rebuilds
        that stored text -- description on each structured leg, and
        bet_title on the bet itself -- using the current rules, entirely
        from already-stored fighter_pick/outcome_type/outcome_round
        (no live FightOdds data needed)."""
        await interaction.response.defer()
        db = self.bot.db  # type: ignore[attr-defined]

        bets = await db.get_bets_for_event_matching(event, "ufc", interaction.user.id)
        if not bets:
            await interaction.followup.send(f"No bets found for **{event}**.", ephemeral=True)
            return

        try:
            fights = await card_data.fetch_fights_for_event(event)
        except Exception:
            fights = []

        fixed_legs = 0
        fixed_bets = 0

        for bet in bets:
            legs = await db.get_legs_for_bet(bet["id"])
            any_leg_changed = False

            for leg in legs:
                fighter_pick = leg.get("fighter_pick")
                outcome_type = leg.get("outcome_type")
                if not fighter_pick or not outcome_type:
                    continue  # free-text leg, nothing to rebuild

                match = card_data.match_fighter_on_card(fighter_pick, fights) if fights else None
                if match:
                    fighter_a, fighter_b = match[0], match[1]
                else:
                    fighter_a, fighter_b = fighter_pick, ""  # best effort without card data

                new_description = rebuild_description_from_stored(
                    fighter_pick=fighter_pick, outcome_type=outcome_type,
                    outcome_round=leg.get("outcome_round"),
                    fighter_a=fighter_a, fighter_b=fighter_b,
                    current_description=leg["description"],
                )
                if new_description != leg["description"]:
                    await db.update_leg_description(leg["id"], new_description)
                    fixed_legs += 1
                    any_leg_changed = True

            if any_leg_changed:
                fresh_legs = await db.get_legs_for_bet(bet["id"])
                new_bet_title = "\n".join(l["description"] for l in fresh_legs)
                await db.update_bet_title(bet["id"], new_bet_title)
                await self._redraw_bet_message(bet["id"])
                fixed_bets += 1

        if fixed_legs:
            await interaction.followup.send(
                f"✏️ Rebuilt **{fixed_legs}** leg description(s) across **{fixed_bets}** bet(s) "
                f"for **{event}**."
            )
        else:
            await interaction.followup.send(
                f"Nothing to fix for **{event}** — every structured leg's text already matches "
                "the current formatting rules."
            )

    # ---------- /event-start, /event-end ----------

    @app_commands.command(
        name="event-start", description="Start auto-grading a UFC event as fights finish"
    )
    @is_admin()
    @app_commands.describe(event="Which event to start watching (autocompletes from events you've logged bets against)")
    @app_commands.autocomplete(event=event_autocomplete)
    async def event_start(self, interaction: discord.Interaction, event: str):
        db = self.bot.db  # type: ignore[attr-defined]

        import card_data  # local import avoids cycles at module load

        monitored = await db.get_monitored_events()
        already = next(
            (m for m in monitored if m == event or card_data._event_match_score(event, m) >= 70),
            None,
        )
        if already is not None:
            note = "" if already == event else f" (already tracked as **{already}**)"
            await interaction.response.send_message(
                f"⚠️ **{event}** is already auto-grading{note} — no need to start it again.",
                ephemeral=True,
            )
            return

        await db.add_monitored_event(event, interaction.user.id)
        await interaction.response.send_message(
            f"🟢 Started auto-grading for **{event}** — ESPN is also checked every "
            "**1 minute** automatically for any pending structured/free-text legs "
            "we can parse (ML, method, distance, totals). Free-text that can’t be "
            "mapped still needs `/grade` or the Won/Loss buttons.",
        )
        log.info("Started monitoring '%s' (requested by %s).", event, interaction.user)
        # Kick an immediate pass so users don't wait a minute for the first check
        try:
            await self._grade_event(event, prepare=True)
        except Exception:
            log.exception("Immediate grade pass failed for %r", event)

    @app_commands.command(
        name="event-end", description="Stop auto-grading a UFC event (or every event, if left blank)"
    )
    @is_admin()
    @app_commands.describe(event="Which event to stop watching (optional -- leave blank to stop watching everything)")
    @app_commands.autocomplete(event=event_autocomplete)
    async def event_end(self, interaction: discord.Interaction, event: str | None = None):
        db = self.bot.db  # type: ignore[attr-defined]
        if event:
            await db.remove_monitored_event(event)
            await interaction.response.send_message(f"🔴 Stopped auto-grading for **{event}**.")
            log.info("Stopped monitoring '%s' (requested by %s).", event, interaction.user)
        else:
            monitored = await db.get_monitored_events()
            await db.clear_monitored_events()
            if monitored:
                listed = ", ".join(f"**{e}**" for e in monitored)
                await interaction.response.send_message(f"🔴 Stopped auto-grading for: {listed}.")
            else:
                await interaction.response.send_message("Nothing was being monitored.")
            log.info("Stopped all monitoring (requested by %s).", interaction.user)

    # ---------- grading loop ----------

    @tasks.loop(minutes=1)
    async def grading_loop(self):
        db = self.bot.db  # type: ignore[attr-defined]
        events: set[str] = set()
        try:
            events.update(await db.get_monitored_events())
            events.update(await db.get_events_with_pending_gradeable_legs())
        except Exception:
            log.exception("Failed to list events for grading; skipping this pass.")
            return

        if not events:
            return

        log.debug(
            "Auto-grade pass for %d event(s): %s",
            len(events),
            ", ".join(sorted(events)[:8]),
        )
        for event in sorted(events):
            try:
                await self._grade_event(event, prepare=True)
            except Exception:
                log.exception("Error grading event '%s'; will retry next pass.", event)

    @grading_loop.before_loop
    async def before_grading_loop(self):
        await self.bot.wait_until_ready()

    async def _prepare_event_for_grading(self, event: str) -> None:
        """Create/structure legs so free-text slips can be ESPN-graded."""
        db = self.bot.db  # type: ignore[attr-defined]
        # All pending UFC bets logged against this event (any user)
        cursor_bets = await db.get_all_bets("ufc")
        bets = [
            b
            for b in cursor_bets
            if b.get("status") == "pending"
            and b.get("event")
            and (
                b["event"] == event
                or card_data._event_match_score(event, b["event"]) >= 70
            )
        ]
        if not bets:
            return
        try:
            fights = await card_data.fetch_fights_for_event(event)
        except Exception:
            fights = []
        if not fights:
            return
        await rematch_bets_to_card(db, bets, fights)

    async def _grade_event(self, event: str, *, prepare: bool = False) -> None:
        db = self.bot.db  # type: ignore[attr-defined]

        if prepare:
            try:
                await self._prepare_event_for_grading(event)
            except Exception:
                log.exception("Prepare/structure failed for %r", event)

        pending_legs = await db.get_pending_graded_legs_for_event(event)
        if not pending_legs:
            # Only drop from explicit monitor list — auto-discovered events simply skip
            monitored = await db.get_monitored_events()
            if event in monitored:
                await db.remove_monitored_event(event)
                log.info("No pending graded legs left for '%s'; auto-stopped monitoring.", event)
            return

        try:
            fight_results = await espn.fetch_fight_results(event)
        except Exception:
            log.exception("Couldn't fetch ESPN results for '%s'.", event)
            return

        if not fight_results:
            return  # ESPN doesn't have this event's card yet/anymore

        affected_bet_ids: set[int] = set()

        for leg in pending_legs:
            affected_bet_ids.add(leg["bet_id"])
            result = grading.find_result_for_bet(leg, fight_results)
            if result is None:
                continue

            if not self._ready_to_settle(event, result):
                continue  # completed but still within the settle buffer -- wait

            outcome = grading.grade_bet(leg, result)
            if outcome is None:
                continue  # fight not completed yet

            new_status, _method_confirmed = outcome
            await db.update_leg_status(leg["id"], new_status)

        for bet_id in affected_bet_ids:
            all_legs = await db.get_legs_for_bet(bet_id)
            statuses = [
                leg["status"] if (leg["fighter_pick"] and leg["outcome_type"]) else "pending"
                for leg in all_legs
            ]
            overall = grading.aggregate_bet_status(statuses)
            if overall is None:
                continue

            await db.update_status(bet_id, overall)
            await self._redraw_bet_message(bet_id)
            log.info("Auto-graded bet #%d as %s", bet_id, overall)

        remaining = await db.get_pending_graded_legs_for_event(event)
        if not remaining and all(r["completed"] for r in fight_results):
            monitored = await db.get_monitored_events()
            if event in monitored:
                await db.remove_monitored_event(event)
                log.info("All fights on '%s' completed and graded; auto-stopped monitoring.", event)

    async def _redraw_bet_message(self, bet_id: int) -> None:
        """Silently redraws a bet's embed/buttons after grading or /rescan.
        No DM, channel notice, or debug posts. For a collab bet, this is
        the one shared/public message, so it shows BOTH people's own
        numbers side by side, matching how it was originally posted."""
        db = self.bot.db  # type: ignore[attr-defined]
        updated_bet = await db.get_bet(bet_id)
        if updated_bet is None or not updated_bet.get("channel_id") or not updated_bet.get("message_id"):
            return

        bettor = self.bot.get_user(updated_bet["user_id"])
        if bettor is None:
            try:
                bettor = await self.bot.fetch_user(updated_bet["user_id"])
            except discord.NotFound:
                bettor = None

        unit_value, currency = await get_user_settings(db, updated_bet["user_id"])

        co_user = None
        co_unit_value = None
        co_currency = None
        if updated_bet.get("is_collab") and updated_bet.get("co_user_id"):
            co_user = self.bot.get_user(updated_bet["co_user_id"])
            if co_user is None:
                try:
                    co_user = await self.bot.fetch_user(updated_bet["co_user_id"])
                except discord.NotFound:
                    co_user = None
            co_unit_value, co_currency = await get_user_settings(db, updated_bet["co_user_id"])

        embed = build_bet_embed(
            updated_bet, unit_value=unit_value, currency=currency, user=bettor,
            co_user=co_user, co_unit_value=co_unit_value, co_currency=co_currency,
        )
        try:
            channel = self.bot.get_channel(
                updated_bet["channel_id"]
            ) or await self.bot.fetch_channel(updated_bet["channel_id"])
            msg = await channel.fetch_message(updated_bet["message_id"])
            await msg.edit(embed=embed, view=BetView(bet_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GradingCog(bot))