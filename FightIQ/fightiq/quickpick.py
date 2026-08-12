"""QuickPick (Pikkit) client.

Two paths:
  1) Website bot (no API key) — same as quickpick.pikkit.com:
       POST https://prod-website.pikkit.app/betslip/bot/website/create
       GET  https://prod-website.pikkit.app/betslip/bot/website/get?request_id=…
     Requires a Cloudflare Turnstile token from the browser.

  2) External API (bankroll/Shannon bots) when QUICKPICK_API_KEY is set:
       POST https://externalapi.pikkit.com/v1/quickpick/create
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

WEBSITE_CREATE = "https://prod-website.pikkit.app/betslip/bot/website/create"
WEBSITE_GET = "https://prod-website.pikkit.app/betslip/bot/website/get"
# From quickpick.pikkit.com frontend config
TURNSTILE_SITE_KEY = "0x4AAAAAADwy1EZal2MP6ASX"

DEFAULT_CREATE = "https://externalapi.pikkit.com/v1/quickpick/create"
DEFAULT_STATUS = "https://externalapi.pikkit.com/v1/quickpick/status"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class QuickPickClient:
    def __init__(
        self,
        api_key: str | None = None,
        create_url: str | None = None,
        *,
        disable_ssl_verify: bool = True,
        max_polls: int = 60,
        poll_interval: float = 1.5,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("QUICKPICK_API_KEY")
            or os.environ.get("PIKKIT_API_KEY")
            or ""
        ).strip()
        self.create_url = (
            create_url
            or os.environ.get("QUICKPICK_API_URL")
            or DEFAULT_CREATE
        ).strip()
        self.disable_ssl_verify = disable_ssl_verify
        self.max_polls = max_polls
        self.poll_interval = poll_interval

    @property
    def configured(self) -> bool:
        """External API key present (optional — website path needs Turnstile instead)."""
        return bool(self.api_key)

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.disable_ssl_verify:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _open(self, req: urllib.request.Request, timeout: float = 25.0):
        return urllib.request.urlopen(
            req, timeout=timeout, context=self._ssl_context()
        )

    # ---- website flow (matches quickpick.pikkit.com Network tab) ------------

    def create_website(
        self, text: str, turnstile_token: str
    ) -> dict[str, Any] | None:
        """
        POST /betslip/bot/website/create
        Body: { text, images: [], turnstileToken, metadata }
        Returns: { requestID: "…" }  (token content-length ~40)
        """
        if not text or not text.strip():
            return None
        if not turnstile_token:
            return {
                "status": "error",
                "error": "turnstile_required",
                "message": "Cloudflare Turnstile token required",
            }

        body = {
            "text": text.strip(),
            "images": [],
            "turnstileToken": turnstile_token,
            "metadata": {
                "utm_source": "website",
                "utm_campaign": "fightiq",
            },
        }
        req = urllib.request.Request(
            WEBSITE_CREATE,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://quickpick.pikkit.com",
                "Referer": "https://quickpick.pikkit.com/",
                "User-Agent": UA,
            },
        )
        try:
            with self._open(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            log.error("QuickPick website create HTTP %s: %s", e.code, detail)
            try:
                err = json.loads(detail)
            except Exception:
                err = {"message": detail}
            return {
                "status": "error",
                "error": f"HTTP {e.code}",
                "message": err.get("message") or detail,
                "detail": err,
            }
        except Exception as e:
            log.error("QuickPick website create error: %s", e)
            return {"status": "error", "error": str(e), "message": str(e)}

        # Response may be plain JSON or text
        try:
            data = json.loads(raw)
        except Exception:
            # sometimes plain request id string
            rid = raw.strip().strip('"')
            if rid:
                return {"requestID": rid, "request_id": rid}
            return {"status": "error", "message": f"bad create response: {raw[:120]}"}

        if isinstance(data, str):
            return {"requestID": data, "request_id": data}
        # normalize id key
        rid = (
            data.get("requestID")
            or data.get("request_id")
            or data.get("requestId")
            or data.get("id")
        )
        if rid:
            data["requestID"] = rid
            data["request_id"] = rid
        return data

    def get_website_status(self, request_id: str) -> dict[str, Any] | None:
        """GET /betslip/bot/website/get?request_id=…"""
        url = f"{WEBSITE_GET}?request_id={urllib.parse.quote(str(request_id))}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://quickpick.pikkit.com",
                "Referer": "https://quickpick.pikkit.com/",
                "User-Agent": UA,
            },
        )
        try:
            with self._open(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            log.warning("QuickPick website get HTTP %s: %s", e.code, detail)
            return {"status": "error", "message": detail}
        except Exception as e:
            log.warning("QuickPick website get error: %s", e)
            return None

    def create_betslip_website(
        self, text: str, turnstile_token: str
    ) -> dict[str, Any] | None:
        """Create + poll website bot until complete or timeout."""
        created = self.create_website(text, turnstile_token)
        if not created or created.get("status") == "error":
            return created
        request_id = (
            created.get("requestID")
            or created.get("request_id")
            or created.get("requestId")
        )
        if not request_id:
            return {
                "status": "error",
                "message": "No requestID from QuickPick create",
                "raw": created,
            }

        for i in range(self.max_polls):
            time.sleep(self.poll_interval if i else 1.0)
            data = self.get_website_status(str(request_id))
            if not data:
                continue
            status = str(data.get("status") or "").lower()
            log.info(
                "QuickPick website poll %s/%s status=%s",
                i + 1,
                self.max_polls,
                status,
            )
            if status == "complete" and data.get("link"):
                data["request_id"] = request_id
                return data
            if status in {"error", "failed", "expired"}:
                data["request_id"] = request_id
                return data
            # processing / pending / not found yet
        return {
            "status": "timeout",
            "request_id": request_id,
            "message": "QuickPick did not finish in time",
        }

    # ---- external API (API key) --------------------------------------------

    def create_betslip(self, text: str) -> dict[str, Any] | None:
        """
        External API create + poll (needs QUICKPICK_API_KEY).
        Prefer create_betslip_website() for no-key public flow.
        """
        if not self.configured:
            log.warning("QuickPick API key not set (QUICKPICK_API_KEY)")
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
                "User-Agent": "FightIQ/0.3",
                "Accept": "application/json",
            },
        )
        try:
            with self._open(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            log.error("QuickPick create HTTP %s: %s", e.code, detail)
            return None
        except Exception as e:
            log.error("QuickPick create error: %s", e)
            return None

        request_id = (result or {}).get("request_id")
        if not request_id:
            log.error("QuickPick missing request_id: %s", result)
            return None

        time.sleep(2)
        return self.poll_status(str(request_id))

    def poll_status(self, request_id: str) -> dict[str, Any] | None:
        status_url = f"{DEFAULT_STATUS}?request_id={urllib.parse.quote(request_id)}"
        for i in range(self.max_polls):
            req = urllib.request.Request(
                status_url,
                headers={
                    "X-API-Key": self.api_key,
                    "User-Agent": "FightIQ/0.3",
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
                return None
            except Exception as e:
                log.warning("QuickPick status poll error: %s", e)
                time.sleep(self.poll_interval)
                continue

            status = (data or {}).get("status", "unknown")
            log.info("QuickPick poll %s/%s status=%s", i + 1, self.max_polls, status)
            if status == "complete":
                return data
            if status in {"failed", "expired"}:
                return None
            time.sleep(self.poll_interval)
        return None
