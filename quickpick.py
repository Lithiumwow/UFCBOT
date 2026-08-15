"""Pikkit QuickPick external API client (betslip share links)."""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

import config

log = logging.getLogger("ufc-bet-bot.quickpick")

DEFAULT_CREATE = "https://externalapi.pikkit.com/v1/quickpick/create"
DEFAULT_STATUS = "https://externalapi.pikkit.com/v1/quickpick/status"


class QuickPickClient:
    def __init__(
        self,
        api_key: str | None = None,
        create_url: str | None = None,
        *,
        max_polls: int = 40,
        poll_interval: float = 1.5,
    ) -> None:
        self.api_key = (api_key or getattr(config, "QUICKPICK_API_KEY", "") or "").strip()
        self.create_url = (
            create_url
            or getattr(config, "QUICKPICK_API_URL", "")
            or DEFAULT_CREATE
        ).strip()
        self.max_polls = max_polls
        self.poll_interval = poll_interval

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _open(self, req: urllib.request.Request, timeout: float = 25.0):
        return urllib.request.urlopen(req, timeout=timeout, context=self._ssl_context())

    def create_betslip(self, text: str) -> dict[str, Any] | None:
        if not self.configured:
            return None
        if not text or not text.strip():
            return None

        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self.create_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
                "User-Agent": "UFCBOT/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with self._open(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            log.error("QuickPick create HTTP %s: %s", e.code, detail)
            return {"status": "error", "message": f"HTTP {e.code}: {detail}"}
        except Exception as e:
            log.error("QuickPick create error: %s", e)
            return {"status": "error", "message": str(e)}

        request_id = (result or {}).get("request_id")
        if not request_id:
            return {"status": "error", "message": f"No request_id: {result}"}

        time.sleep(2)
        return self.poll_status(str(request_id))

    def poll_status(self, request_id: str) -> dict[str, Any] | None:
        status_url = f"{DEFAULT_STATUS}?request_id={urllib.parse.quote(request_id)}"
        for i in range(self.max_polls):
            req = urllib.request.Request(
                status_url,
                headers={
                    "X-API-Key": self.api_key,
                    "User-Agent": "UFCBOT/1.0",
                    "Accept": "application/json",
                },
            )
            try:
                with self._open(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(10)
                    continue
                log.error("QuickPick status HTTP %s", e.code)
                return {"status": "error", "message": f"status HTTP {e.code}"}
            except Exception as e:
                log.warning("QuickPick status poll error: %s", e)
                time.sleep(self.poll_interval)
                continue

            status = (data or {}).get("status", "unknown")
            log.info("QuickPick poll %s/%s status=%s", i + 1, self.max_polls, status)
            if status == "complete":
                return data
            if status in {"failed", "expired"}:
                return data or {"status": status}
            time.sleep(self.poll_interval)
        return {"status": "timeout", "message": "QuickPick did not finish in time"}


def extract_betslip_links(payload: dict[str, Any] | None) -> list[str]:
    """Collect unique http(s) share / deep links from a QuickPick payload."""
    if not payload:
        return []
    found: list[str] = []

    def _add(v: Any) -> None:
        if isinstance(v, str) and v.startswith("http") and v not in found:
            found.append(v)

    for key in ("link", "betslip_link", "url", "shareUrl", "share_url", "deepLink", "deeplink"):
        _add(payload.get(key))

    slips = payload.get("betSlips") or payload.get("bet_slips") or []
    if isinstance(slips, list):
        for slip in slips:
            if not isinstance(slip, dict):
                continue
            for key in (
                "shareUrl",
                "share_url",
                "deepLink",
                "deeplink",
                "url",
                "link",
                "betslip_link",
            ):
                _add(slip.get(key))

    books = payload.get("books") or payload.get("sportsbooks") or []
    if isinstance(books, list):
        for book in books:
            if isinstance(book, dict):
                for key in ("link", "url", "deepLink", "deeplink", "shareUrl"):
                    _add(book.get(key))

    return found


def format_collab_legs_for_quickpick(
    legs: list[dict[str, Any]],
    *,
    event: Optional[str] = None,
    combined_american: Optional[int] = None,
) -> str:
    from odds_math import format_american

    lines: list[str] = []
    if event:
        lines.append(f"Event: {event}")
    for i, leg in enumerate(legs or [], 1):
        desc = (leg.get("description") or "").strip()
        if not desc:
            continue
        odds = leg.get("odds_american")
        if odds is not None:
            lines.append(f"{i}. {desc} @ {format_american(int(odds))}")
        else:
            lines.append(f"{i}. {desc}")
    if combined_american is not None:
        lines.append(f"Combined: {format_american(int(combined_american))}")
    return "\n".join(lines)


async def request_betslip_links(text: str) -> tuple[list[str], dict[str, Any] | None]:
    """Run QuickPick create+poll off the event loop. Returns (links, raw)."""
    client = QuickPickClient()
    if not client.configured:
        return [], {"status": "error", "message": "QUICKPICK_API_KEY is not set"}
    raw = await asyncio.to_thread(client.create_betslip, text)
    links = extract_betslip_links(raw if isinstance(raw, dict) else None)
    return links, raw if isinstance(raw, dict) else None
