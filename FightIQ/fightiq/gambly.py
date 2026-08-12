"""Gambly / Unabated bet-slip generation client.

Uses Unabated Data API (Gambly B2B):
  POST https://data.unabated.com/api/v1/bet/generate
  GET  https://data.unabated.com/api/v1/bet/status/{requestId}

Consumer fallback without an API key: copy text + open https://gambly.com/chat
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

GENERATE_URL = "https://data.unabated.com/api/v1/bet/generate"
STATUS_URL = "https://data.unabated.com/api/v1/bet/status/{request_id}"
GAMBLY_CHAT = "https://gambly.com/chat"
QUICKPICK_HOME = "https://quickpick.pikkit.com/"
PLAYBOOK_HOME = "https://playbookbot.com/"


class GamblyClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        disable_ssl_verify: bool = True,
        max_polls: int = 40,
        poll_interval: float = 2.5,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("UNABATED_API_KEY")
            or os.environ.get("GAMBLY_API_KEY")
            or ""
        ).strip()
        self.disable_ssl_verify = disable_ssl_verify
        self.max_polls = max_polls
        self.poll_interval = poll_interval

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _opener(self) -> urllib.request.OpenerDirector:
        if self.disable_ssl_verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ctx)
            )
        return urllib.request.build_opener()

    def create_betslip(self, text: str) -> dict[str, Any] | None:
        """Submit ticket text → poll until Complete. Returns status payload."""
        if not self.configured:
            log.warning("Gambly/Unabated API key not set (UNABATED_API_KEY)")
            return None
        if not text or not text.strip():
            return None

        body = json.dumps(
            {
                "type": "text",
                "generateMobileLinks": True,
                "content": {"text": text},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            GENERATE_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": self.api_key,
                "User-Agent": "FightIQ/0.2",
                "Accept": "application/json",
            },
        )
        opener = self._opener()
        try:
            with opener.open(req, timeout=25) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            log.error("Gambly generate HTTP %s: %s", e.code, detail)
            return {"status": "error", "error": f"HTTP {e.code}", "detail": detail}
        except Exception as e:
            log.error("Gambly generate error: %s", e)
            return {"status": "error", "error": str(e)}

        request_id = (result or {}).get("requestId") or (result or {}).get("request_id")
        if not request_id:
            log.error("Gambly missing requestId: %s", result)
            return {"status": "error", "error": "missing requestId", "raw": result}

        time.sleep(1.5)
        return self.poll_status(str(request_id))

    def poll_status(self, request_id: str) -> dict[str, Any] | None:
        url = STATUS_URL.format(request_id=urllib.parse.quote(request_id))
        opener = self._opener()
        for i in range(self.max_polls):
            req = urllib.request.Request(
                url,
                headers={
                    "X-Api-Key": self.api_key,
                    "User-Agent": "FightIQ/0.2",
                    "Accept": "application/json",
                },
            )
            try:
                with opener.open(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(8)
                    continue
                log.error("Gambly status HTTP %s", e.code)
                return {"status": "error", "error": f"status HTTP {e.code}"}
            except Exception as e:
                log.warning("Gambly status poll error: %s", e)
                time.sleep(self.poll_interval)
                continue

            status = str((data or {}).get("status") or "unknown").lower()
            log.info("Gambly poll %s/%s status=%s", i + 1, self.max_polls, status)
            if status in {"complete", "completed", "done", "success"}:
                data["requestId"] = request_id
                return data
            if status in {"error", "failed", "expired"}:
                data["requestId"] = request_id
                return data
            time.sleep(self.poll_interval)

        return {
            "status": "timeout",
            "error": "Gambly did not finish in time",
            "requestId": request_id,
        }


def extract_gambly_link(payload: dict | None) -> str | None:
    if not payload:
        return None
    for key in ("shareUrl", "share_url", "url", "link", "betSlipUrl", "betslip_url"):
        v = payload.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    slips = payload.get("betSlips") or payload.get("bet_slips") or []
    if isinstance(slips, list):
        for slip in slips:
            if not isinstance(slip, dict):
                continue
            for key in ("shareUrl", "share_url", "deepLink", "deeplink", "url", "link"):
                v = slip.get(key)
                if isinstance(v, str) and v.startswith("http"):
                    return v
    return None


def format_legs_for_bot(legs: list[dict], *, combined: str | None = None) -> str:
    """
    Compact ticket text that QuickPick / Gambly parse well.

    e.g.
      Johnson wins in round 2 (Charles Johnson vs Jose Ochoa) +2137
      Johnson wins in round 3 (Charles Johnson vs Jose Ochoa) +3512
    """
    lines: list[str] = []
    for i, leg in enumerate(legs or [], 1):
        desc = (leg.get("description") or leg.get("label") or "").strip()
        odds = leg.get("formatted")
        if odds is None and leg.get("american") is not None:
            a = int(leg["american"])
            odds = f"+{a}" if a > 0 else str(a)
        if not desc:
            continue
        if odds:
            lines.append(f"{i}. {desc} @ {odds}")
        else:
            lines.append(f"{i}. {desc}")
    if combined:
        lines.append(f"Combined: {combined}")
    return "\n".join(lines)
