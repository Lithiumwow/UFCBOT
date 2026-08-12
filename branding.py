"""
Event branding logos for Discord embeds + bot avatar.

DWCS vs UFC is detected from the event name. Local PNGs live in ./assets
(also pushed to GitHub so Discord can load them via raw.githubusercontent).
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

_ASSETS = Path(__file__).resolve().parent / "assets"
_AVATAR_PATH = _ASSETS / "bot_avatar.png"
_AVATAR_HASH_PATH = _ASSETS / ".bot_avatar_hash"

log = logging.getLogger("ufc-bet-bot.branding")

# Public raw URLs after assets are on main (Discord thumbnails need a URL).
_RAW_BASE = "https://raw.githubusercontent.com/Lithiumwow/UFCBOT/main/assets"

_DWCS_RE = re.compile(
    r"contender\s*series|\bdwcs\b|\bdwtncs\b|dana\s*white'?s?\s*contender",
    re.I,
)

_LOGO_FILES = {
    "dwcs": _ASSETS / "dwcs_logo.png",
    "ufc": _ASSETS / "ufc_logo.png",
}


def event_brand(event: Optional[str]) -> str:
    """Return 'dwcs' or 'ufc' for an event label."""
    if event and _DWCS_RE.search(event):
        return "dwcs"
    return "ufc"


def logo_path(brand: str) -> Optional[Path]:
    path = _LOGO_FILES.get(brand)
    if path is not None and path.is_file():
        return path
    return None


def logo_filename(brand: str) -> str:
    return f"{brand}_logo.png"


def event_logo_url(event: Optional[str]) -> str:
    """HTTPS URL Discord can fetch for embed thumbnails."""
    brand = event_brand(event)
    return f"{_RAW_BASE}/{logo_filename(brand)}"


def event_logo_file(event: Optional[str]):
    """Optional local File attachment (attachment://) if raw CDN is unavailable."""
    import discord

    brand = event_brand(event)
    path = logo_path(brand)
    if path is None:
        return None
    return discord.File(path, filename=logo_filename(brand))


def apply_event_logo(embed, event: Optional[str]):
    """Set embed thumbnail to the UFC or DWCS logo (GitHub raw URL)."""
    embed.set_thumbnail(url=event_logo_url(event))
    return None


def brand_color(event: Optional[str]):
    import discord

    if event_brand(event) == "dwcs":
        return discord.Color.from_rgb(180, 20, 20)
    return discord.Color.from_rgb(210, 10, 10)


def brand_label(event: Optional[str]) -> str:
    return "DWCS" if event_brand(event) == "dwcs" else "UFC"


async def sync_bot_avatar(bot) -> None:
    """Upload assets/bot_avatar.png as the bot profile picture when it changes."""
    path = _AVATAR_PATH if _AVATAR_PATH.is_file() else logo_path("ufc")
    if path is None or not path.is_file():
        return
    if bot.user is None:
        return

    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    prev = _AVATAR_HASH_PATH.read_text(encoding="utf-8").strip() if _AVATAR_HASH_PATH.is_file() else ""
    if digest == prev:
        return

    try:
        await bot.user.edit(avatar=data)
        _AVATAR_HASH_PATH.write_text(digest, encoding="utf-8")
        log.info("Updated bot avatar from %s", path.name)
    except Exception as e:
        # Discord rate-limits avatar changes; don't crash startup.
        log.warning("Could not update bot avatar: %s", e)
