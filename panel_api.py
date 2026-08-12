"""PebbleHost / Pterodactyl client API helpers (power actions)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

import config

log = logging.getLogger("ufc-bet-bot.panel")


class PanelError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    key = (config.PANEL_API_KEY or "").strip()
    if not key:
        raise PanelError("PANEL_API_KEY is not set in .env")
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "UFCBOT/1.0 (+discord-restart)",
    }


def send_power_signal(signal: str = "restart") -> None:
    """
    POST /api/client/servers/{id}/power with {"signal": "restart"|"stop"|"start"|"kill"}.
    Returns on HTTP 204 / 2xx; raises PanelError otherwise.
    """
    signal = signal.strip().lower()
    if signal not in {"restart", "stop", "start", "kill"}:
        raise PanelError(f"Invalid power signal: {signal}")

    server_id = (config.PANEL_SERVER_ID or "").strip()
    if not server_id:
        raise PanelError("PANEL_SERVER_ID is not set in .env")

    base = (config.PANEL_API_URL or "https://panel.pebblehost.com").rstrip("/")
    url = f"{base}/api/client/servers/{server_id}/power"
    body = json.dumps({"signal": signal}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 202, 204):
                raise PanelError(f"Unexpected status {resp.status}")
            log.info("Panel power signal %r sent for server %s", signal, server_id)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise PanelError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise PanelError(f"Network error: {e}") from e


def panel_configured() -> bool:
    return bool(
        (config.PANEL_API_KEY or "").strip()
        and (config.PANEL_SERVER_ID or "").strip()
    )
