"""
Restricts every slash command to a fixed set of Discord user IDs. Beyond
this gate, per-command/view logic additionally checks bet *ownership* so
allowed users still can't see or touch each other's bets.
"""
from __future__ import annotations

from typing import Optional, Union

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


def resolve_allowed_target(
    interaction: discord.Interaction,
    user: Optional[Union[discord.User, discord.Member]] = None,
) -> tuple[int, discord.abc.User] | str:
    """
    Whose bets to show for /card /pl /spread-sheet.
    Defaults to the invoker. Another user must be on ALLOWED_USER_IDS.
    Returns (target_id, target_user) or an error message string.
    """
    if user is None or user.id == interaction.user.id:
        return interaction.user.id, interaction.user
    if user.id not in config.ALLOWED_USER_IDS:
        return (
            "🚫 You can only view cards for users allowed on this bot "
            f"(not {user.mention})."
        )
    return user.id, user


def target_user_id_from_namespace(interaction: discord.Interaction) -> int:
    """For event autocomplete when a `user` option may already be filled."""
    try:
        raw = getattr(interaction.namespace, "user", None)
    except Exception:
        raw = None
    if raw is None:
        return interaction.user.id
    uid = getattr(raw, "id", None)
    if isinstance(uid, int) and uid in config.ALLOWED_USER_IDS:
        return uid
    return interaction.user.id