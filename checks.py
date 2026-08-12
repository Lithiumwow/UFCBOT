"""
Restricts every slash command to a fixed set of Discord user IDs. Beyond
this gate, per-command/view logic additionally checks bet *ownership* so
allowed users still can't see or touch each other's bets.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in config.ALLOWED_USER_IDS

    return app_commands.check(predicate)


def is_admin_ctx():
    """Same allowed-user gate, for classic prefix commands (!eventstart /
    !eventend) instead of slash commands."""

    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.id in config.ALLOWED_USER_IDS

    return commands.check(predicate)