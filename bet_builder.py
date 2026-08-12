"""
Interactive, click-only bet builder for /bet-ufc. Discord modals can only
contain plain text boxes (no dropdowns), so this instead walks through
real Select menus (dropdowns), populated with the actual card, so nothing
has to be typed for a structured leg:

    Pick a FIGHT (dropdown, e.g. "Islam Makhachev vs Ian Machado Garry")
        -> pick who wins + how, OR a fight-level (either-fighter) outcome
            -> pick round if relevant -> leg added

The outcome dropdown, scoped to one fight, has two kinds of entries:
  - per-fighter picks, e.g. "Islam Makhachev - Submission",
    "Ian Machado Garry - KO/TKO or Submission"
  - fight-level picks that don't name a specific fighter, e.g.
    "Fight Ends by KO/TKO (Either Fighter)", "Fight Goes the Distance"

A "📝 Free-Text Leg" button covers anything that doesn't fit that shape
(props, totals, etc) via a single-field modal -- typing is still available
there for whoever wants it, including the "Fighter - Outcome" shorthand if
you'd rather type one leg than click through it.
"""
from __future__ import annotations

import discord

from leg_parser import describe_outcome, parse_leg_line

MAX_LEGS = 6

# Per-fighter picks -- who wins, and how.
PER_FIGHTER_OPTIONS = [
    ("Moneyline (just the winner)", "ML"),
    ("KO/TKO", "KO_TKO"),
    ("Submission", "SUB"),
    ("Decision", "DEC"),
    ("KO/TKO or Submission", "KO_OR_SUB"),
]

# Fight-level picks -- about how the fight ends, regardless of who wins.
FIGHT_LEVEL_OPTIONS = [
    ("Fight Ends by KO/TKO (Either Fighter)", "FIGHT_KO"),
    ("Fight Ends by Submission (Either Fighter)", "FIGHT_SUB"),
    ("Fight Goes the Distance (Decision)", "DISTANCE"),
    ("Fight Does NOT Go the Distance (KO/TKO or Sub)", "NOT_DISTANCE"),
]

# Total-rounds Over/Under -- also fight-level/winner-agnostic.
# "Under 0.5 Rounds" = fight ends before 2:30 of round 1.
# "Over 2.5 Rounds" = fight lasts past 2:30 of round 3 (i.e. into R3 second half+).
# A decision counts as lasting the full scheduled length (3 or 5 rounds).
TOTAL_ROUNDS_OPTIONS = [
    ("Over 0.5 Rounds", "OVER_0_5"),
    ("Over 1.5 Rounds", "OVER_1_5"),
    ("Over 2.5 Rounds", "OVER_2_5"),
    ("Over 3.5 Rounds", "OVER_3_5"),
    ("Over 4.5 Rounds", "OVER_4_5"),
    ("Under 0.5 Rounds", "UNDER_0_5"),
    ("Under 1.5 Rounds", "UNDER_1_5"),
    ("Under 2.5 Rounds", "UNDER_2_5"),
    ("Under 3.5 Rounds", "UNDER_3_5"),
    ("Under 4.5 Rounds", "UNDER_4_5"),
]

ROUND_NEEDED_OUTCOMES = {"KO_TKO", "SUB", "KO_OR_SUB", "FIGHT_KO", "FIGHT_SUB"}

# Kept short for the actual bet label/description -- the dropdown option
# text above is more verbose ("(Either Fighter)" etc) to make the pick
# clear while *choosing*, but that clarifier doesn't need to live in the
# logged bet's description too.
_FIGHT_LEVEL_LABELS = {
    "FIGHT_KO": "ends by KO/TKO",
    "FIGHT_SUB": "ends by Submission",
    "DISTANCE": "goes the distance",
    "NOT_DISTANCE": "does NOT go the distance",
}


def _fight_level_description(fighter_a: str, fighter_b: str, outcome_type: str, round_num: int | None) -> str:
    total_rounds_label = {value: label for label, value in TOTAL_ROUNDS_OPTIONS}.get(outcome_type)
    if total_rounds_label:
        # e.g. "Islam Makhachev vs Ian Machado Garry - Over 2.5 Rounds"
        return f"{fighter_a} vs {fighter_b} - {total_rounds_label}"

    desc = f"{fighter_a} vs {fighter_b} {_FIGHT_LEVEL_LABELS[outcome_type]}"
    if round_num and outcome_type in ("FIGHT_KO", "FIGHT_SUB"):
        desc += f" (Round {round_num})"
    return desc


class BetBuilderSession:
    """Shared state for one /bet-ufc builder run, referenced by every
    dropdown/button/modal involved."""

    def __init__(
        self, *, event: str | None, fights: list[tuple[str, str]], invoker_id: int, cog,
    ):
        self.event = event
        self.fights = fights  # list of (fighter_a, fighter_b)
        self.invoker_id = invoker_id
        self.cog = cog
        self.legs: list[dict] = []
        self.message: discord.Message | None = None
        # Pending leg-in-progress state, set once a fight+outcome is picked
        # and cleared once the round step (if any) finalizes it.
        self._pending_fighter: str | None = None  # anchor for matching, always set
        self._pending_outcome: str | None = None
        self._pending_is_fight_level: bool = False
        self._pending_fighter_a: str | None = None
        self._pending_fighter_b: str | None = None

    def summary_text(self) -> str:
        header = f"🥊 **Building bet for {self.event or '(no event set)'}**"
        if not self.legs:
            return f"{header}\n\nNo legs added yet. Pick a fight below, or add a free-text leg."
        lines = "\n".join(f"{i}. {leg['description']}" for i, leg in enumerate(self.legs, start=1))
        remaining = MAX_LEGS - len(self.legs)
        footer = f"\n\n{remaining} more leg(s) can be added." if remaining > 0 else "\n\nMax legs reached."
        return f"{header}\n\n{lines}{footer}"

    def finalize_pending_leg(self, round_val: int | None) -> None:
        if self._pending_is_fight_level:
            description = _fight_level_description(
                self._pending_fighter_a, self._pending_fighter_b, self._pending_outcome, round_val
            )
        else:
            description = describe_outcome(self._pending_fighter, self._pending_outcome, round_val)

        self.legs.append(
            {
                "description": description,
                "fighter_pick": self._pending_fighter,
                "outcome_type": self._pending_outcome,
                "outcome_round": round_val,
            }
        )
        self._pending_fighter = None
        self._pending_outcome = None
        self._pending_is_fight_level = False
        self._pending_fighter_a = None
        self._pending_fighter_b = None

    async def refresh_message(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=self.summary_text(), view=BuilderView(self))
        except discord.HTTPException:
            pass


def _check_invoker(interaction: discord.Interaction, session: BetBuilderSession) -> bool:
    return interaction.user.id == session.invoker_id


class RoundSelect(discord.ui.Select):
    def __init__(self, session: BetBuilderSession):
        self.session = session
        options = [discord.SelectOption(label=f"Round {n}", value=str(n)) for n in range(1, 6)]
        options.append(discord.SelectOption(label="Not specified", value="none"))
        super().__init__(placeholder="Which round? (optional)", options=options)

    async def callback(self, interaction: discord.Interaction):
        if not _check_invoker(interaction, self.session):
            await interaction.response.send_message("🚫 Not your bet builder.", ephemeral=True)
            return
        round_val = None if self.values[0] == "none" else int(self.values[0])
        self.session.finalize_pending_leg(round_val)
        await interaction.response.defer()
        await self.session.refresh_message()


class RoundSelectView(discord.ui.View):
    def __init__(self, session: BetBuilderSession):
        super().__init__(timeout=300)
        self.add_item(RoundSelect(session))


class FightOutcomeSelect(discord.ui.Select):
    """Scoped to ONE fight -- lists both fighters crossed with every
    per-fighter outcome, plus the fight-level (either-fighter) outcomes,
    all in one dropdown. Total-rounds Over/Under is a separate dropdown
    (see TotalRoundsSelect) shown alongside this one, since combining
    everything into a single 24-option list would be unwieldy."""

    def __init__(self, session: BetBuilderSession, fighter_a: str, fighter_b: str):
        self.session = session
        self.fighter_a = fighter_a
        self.fighter_b = fighter_b

        options = []
        for fighter in (fighter_a, fighter_b):
            for label, value in PER_FIGHTER_OPTIONS:
                option_label = f"{fighter} - {label}"
                options.append(
                    discord.SelectOption(label=option_label[:100], value=f"F|{fighter}|{value}"[:100])
                )
        for label, value in FIGHT_LEVEL_OPTIONS:
            options.append(discord.SelectOption(label=label[:100], value=f"M|{value}"[:100]))

        super().__init__(placeholder="Who wins, and how?", options=options[:25], row=0)

    async def callback(self, interaction: discord.Interaction):
        if not _check_invoker(interaction, self.session):
            await interaction.response.send_message("🚫 Not your bet builder.", ephemeral=True)
            return

        kind, *rest = self.values[0].split("|")

        if kind == "F":
            fighter, outcome = rest
            self.session._pending_fighter = fighter
            self.session._pending_outcome = outcome
            self.session._pending_is_fight_level = False
            prompt_subject = fighter
        else:  # "M" -- fight-level
            (outcome,) = rest
            self.session._pending_fighter = self.fighter_a  # anchor for matching only
            self.session._pending_outcome = outcome
            self.session._pending_is_fight_level = True
            self.session._pending_fighter_a = self.fighter_a
            self.session._pending_fighter_b = self.fighter_b
            prompt_subject = f"{self.fighter_a} vs {self.fighter_b}"

        if outcome in ROUND_NEEDED_OUTCOMES:
            await interaction.response.edit_message(
                content=f"**{prompt_subject}** — {outcome.replace('_', '/')}. Which round?",
                view=RoundSelectView(self.session),
            )
        else:
            self.session.finalize_pending_leg(None)
            await interaction.response.defer()
            await self.session.refresh_message()


class TotalRoundsSelect(discord.ui.Select):
    """Separate dropdown for Over/Under total rounds -- also fight-level
    and winner-agnostic, kept apart from FightOutcomeSelect so neither
    dropdown gets overloaded with options."""

    def __init__(self, session: BetBuilderSession, fighter_a: str, fighter_b: str):
        self.session = session
        self.fighter_a = fighter_a
        self.fighter_b = fighter_b
        options = [
            discord.SelectOption(label=label[:100], value=value[:100])
            for label, value in TOTAL_ROUNDS_OPTIONS
        ]
        super().__init__(placeholder="...or total rounds Over/Under", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        if not _check_invoker(interaction, self.session):
            await interaction.response.send_message("🚫 Not your bet builder.", ephemeral=True)
            return

        outcome = self.values[0]
        self.session._pending_fighter = self.fighter_a  # anchor for matching only
        self.session._pending_outcome = outcome
        self.session._pending_is_fight_level = True
        self.session._pending_fighter_a = self.fighter_a
        self.session._pending_fighter_b = self.fighter_b
        self.session.finalize_pending_leg(None)  # the O/U line IS the pick, no round sub-step
        await interaction.response.defer()
        await self.session.refresh_message()


class FightOutcomeView(discord.ui.View):
    def __init__(self, session: BetBuilderSession, fighter_a: str, fighter_b: str):
        super().__init__(timeout=300)
        self.add_item(FightOutcomeSelect(session, fighter_a, fighter_b))
        self.add_item(TotalRoundsSelect(session, fighter_a, fighter_b))


class FightSelect(discord.ui.Select):
    def __init__(self, session: BetBuilderSession):
        self.session = session
        options = [
            discord.SelectOption(label=f"{a} vs {b}"[:100], value=str(idx))
            for idx, (a, b) in enumerate(session.fights[:25])
        ]
        super().__init__(placeholder="Pick a fight...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not _check_invoker(interaction, self.session):
            await interaction.response.send_message("🚫 Not your bet builder.", ephemeral=True)
            return
        fighter_a, fighter_b = self.session.fights[int(self.values[0])]
        await interaction.response.edit_message(
            content=f"**{fighter_a} vs {fighter_b}** — who wins, and how?",
            view=FightOutcomeView(self.session, fighter_a, fighter_b),
        )


class FreeTextLegModal(discord.ui.Modal, title="Add a Free-Text Leg"):
    """For anything the dropdowns can't express -- props, totals, etc.
    Still understands the 'Fighter - Outcome' shorthand if typed here."""

    def __init__(self, session: BetBuilderSession):
        super().__init__()
        self.session = session
        self.leg_input = discord.ui.TextInput(
            label="Leg description",
            placeholder="e.g. Fight goes over 1.5 rounds combined",
            max_length=200,
        )
        self.add_item(self.leg_input)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.leg_input.value.strip()
        if text and len(self.session.legs) < MAX_LEGS:
            self.session.legs.append(parse_leg_line(text))
        await interaction.response.defer()
        await self.session.refresh_message()


class FinishBetModal(discord.ui.Modal, title="Finish Bet"):
    """Last step -- units and odds, right before the bet actually gets
    logged. Kept separate from the fight-picking flow since these are
    plain numbers, not something there's a sensible dropdown for."""

    def __init__(self, session: BetBuilderSession):
        super().__init__()
        self.session = session
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

        await self.session.cog._log_bet(
            interaction,
            sport="ufc",
            event=self.session.event,
            legs=self.session.legs,
            units=units,
            odds=odds,
            use_followup=False,
        )
        try:
            await self.session.message.delete()
        except discord.HTTPException:
            pass


class BuilderView(discord.ui.View):
    def __init__(self, session: BetBuilderSession):
        super().__init__(timeout=600)
        self.session = session

        if len(session.legs) < MAX_LEGS and session.fights:
            self.add_item(FightSelect(session))

        freetext_btn = discord.ui.Button(
            label="📝 Free-Text Leg", style=discord.ButtonStyle.secondary, row=1
        )
        freetext_btn.callback = self._freetext_callback
        self.add_item(freetext_btn)

        if session.legs:
            finish_btn = discord.ui.Button(
                label="✅ Finish & Log Bet", style=discord.ButtonStyle.success, row=1
            )
            finish_btn.callback = self._finish_callback
            self.add_item(finish_btn)

        cancel_btn = discord.ui.Button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=1)
        cancel_btn.callback = self._cancel_callback
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return _check_invoker(interaction, self.session)

    async def _freetext_callback(self, interaction: discord.Interaction):
        if len(self.session.legs) >= MAX_LEGS:
            await interaction.response.send_message("⚠️ Max legs reached.", ephemeral=True)
            return
        await interaction.response.send_modal(FreeTextLegModal(self.session))

    async def _finish_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(FinishBetModal(self.session))

    async def _cancel_callback(self, interaction: discord.Interaction):
        try:
            await self.session.message.delete()
        except discord.HTTPException:
            pass