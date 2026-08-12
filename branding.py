"""
Event branding logos for Discord embeds.

DWCS vs UFC is detected from the event name. Local PNGs live in ./assets
(also pushed to GitHub so Discord can load them via raw.githubusercontent).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import discord

_ASSETS = Path(__file__).resolve().parent / "assets"

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


def event_logo_file(event: Optional[str]) -> Optional[discord.File]:
    """Optional local File attachment (attachment://) if raw CDN is unavailable."""
    brand = event_brand(event)
    path = logo_path(brand)
    if path is None:
        return None
    return discord.File(path, filename=logo_filename(brand))


def apply_event_logo(
    embed: discord.Embed,
    event: Optional[str],
    *,
    use_attachment: bool = False,
) -> Optional[discord.File]:
    """
    Set embed thumbnail to the UFC or DWCS logo.
    By default uses GitHub raw URL (no file needed).
    If use_attachment=True, uses attachment:// and returns a File to send.
    """
    brand = event_brand(event)
    if use_attachment:
        path = logo_path(brand)
        if path is None:
            return None
        name = logo_filename(brand)
        embed.set_thumbnail(url=f"attachment://{name}")
        return discord.File(path, filename=name)

    embed.set_thumbnail(url=event_logo_url(event))
    return None


def brand_color(event: Optional[str]) -> discord.Color:
    if event_brand(event) == "dwcs":
        return discord.Color.from_rgb(180, 20, 20)
    return discord.Color.from_rgb(210, 10, 10)


def brand_label(event: Optional[str]) -> str:
    return "DWCS" if event_brand(event) == "dwcs" else "UFC"
