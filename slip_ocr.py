"""
OCR a bookmaker slip screenshot and turn it into structured bet legs.

Image path prefers OpenAI Vision when OPENAI_API_KEY is set, then falls
back to RapidOCR (local ONNX). Paste slip text to skip image OCR.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import aiohttp

from leg_parser import parse_leg_line
from odds_math import american_to_decimal, combine_parlay, decimal_to_american

log = logging.getLogger("ufc-bet-bot.slip_ocr")

_DEC_ODDS_RE = re.compile(r"^\d+(?:\.\d{1,3})?$")
_AMERICAN_RE = re.compile(r"^[+-]\d{2,5}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?:\s+\w+)?$", re.I)
_DATE_RE = re.compile(
    r"^(?:mon|tue|wed|thu|fri|sat|sun)\b|"
    r"^\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    re.I,
)
_NOISE_RE = re.compile(
    r"^(?:reuse\s+selections?|share|close|betslip|balance(?:\s*:.*)?|"
    r"method\s+of\s+victory(?:\s+double\s+chance)?|"
    r"to\s+win\s+fight|fight\s+betting|double\s+chance|"
    r"alt\.?\s*round\s+betting(?:\s*\([^)]*\))?|"
    r"money\s*line|moneyline|total\s+rounds?|add\s+to\s+bet\s*slip|"
    r"confirm|cancel|odds\s+boost|same\s+game\s+parlay|bet\s+slip|"
    r"your\s+bet|selections?|place\s+bet|stake|returns?|potential\s+returns?|"
    r"cash\s*out|wager|to\s+win|round\s+robin|accept\s+odds\s+movements|"
    r"save\s+for\s+later|enter\s+wager\s+amount|"
    r"\d+\s*%?\s*profit\s+boost|valid\s+on\s+any|"
    r"\d+\s*leg\s+parlay)$",
    re.I,
)
_MARKET_LABEL_RE = re.compile(
    r"^(?:to\s+win\s+fight|money\s*line|moneyline|double\s+chance|"
    r"alt\.?\s*round\s+betting(?:\s*\([^)]*\))?|"
    r"method\s+of\s+victory(?:\s+double\s+chance)?)$",
    re.I,
)
_MARKET_LINE_RE = re.compile(
    r"^(?P<market>.+?)\s*[-–—]\s*(?P<a>.+?)\s+v(?:s\.?)?\s+(?P<b>.+)$",
    re.I,
)
_VS_LINE_RE = re.compile(
    r"^(?P<a>.+?)\s+v(?:s\.?)?\s+(?P<b>.+)$",
    re.I,
)
_KO_ROUNDS_RE = re.compile(
    r"(?P<fighter>.+?)\s+to\s+win\s+by\s+ko/?tko\s+in\s+rounds?\s+(?P<r1>\d)\s*or\s*(?P<r2>\d)",
    re.I,
)
_ROUND_OR_RE = re.compile(
    r"(?P<fighter>.+?)\s+round\s+(?P<r1>\d)\s*or\s*(?P<r2>\d)\b",
    re.I,
)
_ROUND_OR_DEC_RE = re.compile(
    r"^(?P<fighter>.+?)\s+rounds?\s+(?P<rounds>\d+(?:\s*,\s*\d+)*)\s*,?\s*or\s+(?:by\s+)?decision\b",
    re.I,
)
# Real bet selections — not bare fighter names / UI chrome
_SELECTION_RE = re.compile(
    r"^(?P<head>.+?)\s+"
    r"(?:"
    r"by\s+(?:ko(?:/?tko)?(?:\s*,\s*tko)?(?:\s*,\s*dq)?(?:\s+or\s+submission)?|"
    r"ko/?tko(?:\s+or\s+submission)?|submission|decision|"
    r"unanimous\s+decision|split\s+decision|points)|"
    r"to\s+win(?:\s+(?:fight|by\s+\w[\w/,]*))?|"
    r"wins?\s+(?:in\s+)?(?:round|inside)|"
    r"round\s+\d|"
    r"(?:over|under)\s+\d|"
    r"\bML\b|"
    r"goes?\s+the\s+distance|"
    r"does\s+not\s+go|"
    r"ends?\s+(?:by|in)"
    r")",
    re.I,
)
# Ends mid-phrase because OCR wrapped "…DQ or" / "Submission"
_OR_WRAP_RE = re.compile(r"\bor\s*$", re.I)
_CONT_OUTCOME_RE = re.compile(
    r"^(?:(?:by\s+)?(?:submission|ko/?tko|decision|points|dq))$",
    re.I,
)
_ML_MARKET_RE = re.compile(
    r"^to\s+win(?:\s+fight)?$|^money\s*line$|^moneyline$",
    re.I,
)
_NAME_ONLY_RE = re.compile(
    r"^[A-Za-z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*){0,4}$"
)
_INLINE_ODDS_RE = re.compile(
    r"^(?P<sel>.+?)\s+(?P<odds>[+-]?\d{2,5}|\d+\.\d{1,3})$"
)
_PARLAY_HEADER_RE = re.compile(
    r"(?P<n>\d+)\s*leg\s+parlay|\b(?:treble|parlay|acca|accumulator)\b",
    re.I,
)
_ET_TIME_RE = re.compile(
    r"^(?:mon|tue|wed|thu|fri|sat|sun)\b.*\d{1,2}:\d{2}",
    re.I,
)

_rapid_engine = None

_VISION_SYSTEM = (
    "You read sportsbook bet-slip screenshots (FanDuel, DraftKings, etc.). "
    "Return ONLY valid JSON (no markdown). Extract every betting selection "
    "leg accurately. Ignore UI chrome (Clear All, Cash Out, Balance, "
    "wager boxes, boosts, X remove icons)."
)

_VISION_USER = """Extract this UFC/MMA bet slip into JSON with this shape:
{
  "event": "event name if visible else null",
  "book": "FanDuel|DraftKings|BetMGM|other|null",
  "combined_american": null,
  "combined_decimal": 45.77,
  "legs": [
    {
      "description": "Ian Machado Garry +5.5",
      "fighter_pick": "Ian Machado Garry",
      "market": "Fight Spread",
      "line": 5.5,
      "american_odds": null,
      "decimal_odds": 1.95,
      "fighter_a": "Islam Makhachev",
      "fighter_b": "Ian Machado Garry"
    }
  ],
  "raw_text": "line-by-line transcription of each selection + market + matchup + odds"
}

Critical rules:
1. The PICK is the highlighted/selected name (often green or bold) — NOT the opponent.
   Example: selection "Kaue Fernandes" under "To Win Fight" with matchup
   "Jalin Turner vs Kaue Fernandes" → fighter_pick = "Kaue Fernandes" (not Turner).
2. Fight Spread: include the handicap in description, e.g. "Ian Machado Garry +5.5".
   Set market="Fight Spread" and line to the number (5.5 or -5.5).
3. Method markets: keep full text
   ("by KO, TKO, DQ or Submission", "by KO, TKO or DQ").
4. To Win Fight / Moneyline: description like "Kaue Fernandes to win";
   fighter_pick must be the selected fighter only.
5. Odds: use the number printed on that leg (FanDuel is usually DECIMAL like 1.95).
   Set decimal_odds when the slip shows decimals; leave american_odds null unless
   American odds (+/-) are printed.
6. combined_decimal / combined_american: use the PARLAY TOTAL printed on the slip
   (e.g. "6 Fold" then 45.77). Do NOT invent a total. Prefer the large combined
   decimal when shown.
7. One object per selection only — never turn matchup-only lines into legs.
"""


def _get_rapid_ocr():
    global _rapid_engine
    if _rapid_engine is None:
        try:
            from rapidocr import RapidOCR  # v3+ (Python 3.13 OK)
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR  # legacy

        _rapid_engine = RapidOCR()
    return _rapid_engine


def _image_data_url(image: bytes, filename: str = "slip.png") -> str:
    lower = (filename or "").lower()
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        mime = "image/jpeg"
    elif lower.endswith(".webp"):
        mime = "image/webp"
    else:
        mime = "image/png"
    b64 = base64.b64encode(image).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _openai_credentials() -> tuple[str, str]:
    try:
        import config

        key = (getattr(config, "OPENAI_API_KEY", None) or "").strip()
        model = (getattr(config, "OPENAI_VISION_MODEL", None) or "gpt-4o-mini").strip()
    except Exception:
        import os

        key = (os.getenv("OPENAI_API_KEY") or "").strip()
        model = (os.getenv("OPENAI_VISION_MODEL") or "gpt-4o-mini").strip()
    return key, model or "gpt-4o-mini"


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise RuntimeError("Vision response was not JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise RuntimeError("Vision JSON root must be an object")
    return data


async def _vision_parse_slip(
    image: bytes,
    *,
    filename: str = "slip.png",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict[str, Any]:
    key, default_model = _openai_credentials()
    key = (api_key or key).strip()
    model = (model or default_model).strip() or "gpt-4o-mini"
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")

    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _VISION_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_USER},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": _image_data_url(image, filename),
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"OpenAI Vision HTTP {resp.status}: {body[:300]}")
            data = json.loads(body)

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected OpenAI response: {e}") from e

    parsed = _extract_json_object(content)
    usage = data.get("usage") or {}
    parsed["_usage"] = {
        "model": data.get("model") or model,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(
            usage.get("total_tokens")
            or (
                int(usage.get("prompt_tokens") or 0)
                + int(usage.get("completion_tokens") or 0)
            )
        ),
    }
    log.info(
        "OCR via OpenAI Vision model=%s legs=%s tokens=%s",
        model,
        len(parsed.get("legs") or []),
        parsed["_usage"]["total_tokens"],
    )
    return parsed


def _vision_payload_to_slip(payload: dict[str, Any]) -> dict[str, Any]:
    """Map Vision JSON into the same shape as parse_bookmaker_slip()."""
    notes: list[str] = ["Parsed with OpenAI Vision"]
    raw_text = str(payload.get("raw_text") or "").strip()
    event = payload.get("event")
    if event:
        notes.append(f"Vision event: {event}")
    book = payload.get("book")
    if book:
        notes.append(f"Vision book: {book}")

    legs: list[dict[str, Any]] = []
    leg_decimals: list[float] = []
    leg_americans: list[int] = []

    def _record_leg_odds(description: str, item: dict[str, Any]) -> None:
        am = item.get("american_odds")
        dec = item.get("decimal_odds")
        try:
            if dec is not None and float(dec) > 1:
                d = float(dec)
                leg_decimals.append(d)
                notes.append(f"Leg decimal {d:g}: {description}")
                leg_americans.append(decimal_to_american(Decimal(str(d))))
                return
        except (TypeError, ValueError, InvalidOperation):
            pass
        try:
            if am is not None and str(am).strip() != "":
                am_i = int(am)
                leg_americans.append(am_i)
                notes.append(f"Leg american {am_i:+d}: {description}")
        except (TypeError, ValueError):
            pass

    for item in payload.get("legs") or []:
        if not isinstance(item, dict):
            continue
        description = (
            item.get("description")
            or item.get("selection")
            or item.get("label")
            or ""
        ).strip()
        if not description:
            continue
        market_name = (item.get("market") or item.get("market_name") or "").strip() or None
        fighter_a = (item.get("fighter_a") or "").strip() or None
        fighter_b = (item.get("fighter_b") or "").strip() or None
        fighter_pick = (item.get("fighter_pick") or "").strip() or None
        line_val = item.get("line")

        is_spread = bool(
            (market_name and re.search(r"spread|handicap", market_name, re.I))
            or (
                re.search(r"[+-]\d+(?:\.\d+)?", description)
                and re.search(r"spread|handicap", description, re.I)
            )
        )
        if is_spread or (
            line_val is not None
            and market_name
            and re.search(r"spread|handicap", market_name, re.I)
        ):
            try:
                line_f = float(line_val) if line_val is not None else None
            except (TypeError, ValueError):
                line_f = None
            if line_f is None:
                m_line = re.search(r"([+-]\d+(?:\.\d+)?)", description)
                if m_line:
                    try:
                        line_f = float(m_line.group(1))
                    except ValueError:
                        line_f = None
            pick = fighter_pick or description
            pick = re.sub(r"\s*[+-]\d+(?:\.\d+)?\s*$", "", pick).strip() or pick
            pick = re.sub(r"\s+vs\.?\s+.*$", "", pick, flags=re.I).strip() or pick
            if line_f is not None:
                sign = "+" if line_f > 0 else ""
                description = f"{pick} {sign}{line_f:g}"
                ot = "SPREAD_" + str(line_f).replace(".", "_").replace("-", "m")
            else:
                description = pick
                ot = "SPREAD"
            legs.append(
                {
                    "description": description,
                    "fighter_pick": pick,
                    "outcome_type": ot,
                    "outcome_round": None,
                    "selection_raw": description,
                    "fighter_a": fighter_a,
                    "fighter_b": fighter_b,
                    "market_name": market_name or "Fight Spread",
                    "spread_line": line_f,
                }
            )
            _record_leg_odds(description, item)
            continue

        # Moneyline / to win — trust fighter_pick (green selection), not opponent
        if market_name and re.search(r"money\s*line|to\s+win", market_name, re.I):
            base = fighter_pick or description
            base = re.sub(r"\s+to\s+win\b.*$", "", base, flags=re.I).strip() or base
            base = re.sub(r"\s+vs\.?\s+.*$", "", base, flags=re.I).strip() or base
            if fighter_pick:
                base = fighter_pick
            description = (
                base
                if re.search(r"\b(to win|ml)\b", base, re.I)
                else f"{base} to win"
            )

        leg = _selection_to_leg(
            description,
            fighter_a=fighter_a,
            fighter_b=fighter_b,
            market_name=market_name,
        )
        if not leg:
            leg = {
                "description": description,
                "fighter_pick": fighter_pick,
                "outcome_type": None,
                "outcome_round": None,
                "selection_raw": description,
                "fighter_a": fighter_a,
                "fighter_b": fighter_b,
                "market_name": market_name,
            }
        if fighter_pick:
            leg["fighter_pick"] = fighter_pick
            desc = leg.get("description") or description
            if fighter_pick.lower() not in desc.lower():
                m = re.search(r"\b(by\s+.+|to\s+win.*)$", desc, re.I)
                leg["description"] = (
                    f"{fighter_pick} {m.group(1)}" if m else f"{fighter_pick} to win"
                )
        legs.append(leg)
        _record_leg_odds(leg.get("description") or description, item)

    combined_american: Optional[int] = None
    combined_decimal: Optional[float] = None
    # Prefer printed decimal parlay total (FanDuel "6 Fold 45.77")
    for key in ("combined_decimal", "parlay_decimal"):
        val = payload.get(key)
        try:
            if val is not None and float(val) > 1:
                combined_decimal = float(val)
                combined_american = decimal_to_american(Decimal(str(combined_decimal)))
                notes.append(f"Parlay decimal from vision: {combined_decimal:g}")
                break
        except (TypeError, ValueError, InvalidOperation):
            continue
    if combined_american is None:
        for key in ("combined_american", "parlay_american", "odds"):
            val = payload.get(key)
            if val is None or str(val).strip() == "":
                continue
            try:
                combined_american = int(val)
                combined_decimal = float(american_to_decimal(combined_american))
                notes.append(f"Parlay total from vision: {combined_american:+d}")
                break
            except (TypeError, ValueError, Exception):
                continue

    if combined_american is None and len(leg_decimals) >= 2:
        prod = Decimal("1")
        for d in leg_decimals:
            prod *= Decimal(str(d))
        combined_decimal = float(prod)
        try:
            combined_american = decimal_to_american(prod)
        except (InvalidOperation, ValueError):
            pass
        notes.append(
            f"Combined {len(leg_decimals)} leg decimals -> {combined_decimal:g}"
        )
    elif combined_american is None and len(leg_americans) >= 2:
        try:
            combo = combine_parlay(leg_americans)
            combined_american = combo["combined_american"]
            combined_decimal = combo["combined_decimal"]
            notes.append(
                f"Combined {len(leg_americans)} leg americans -> {combined_american:+d}"
            )
        except Exception:
            pass

    if not raw_text and legs:
        raw_text = "\n".join(L.get("description") or "" for L in legs)

    return {
        "legs": legs,
        "odds": combined_american,
        "decimal_odds": combined_decimal,
        "notes": notes,
        "raw_text": raw_text,
        "event_hint": event,
        "source": "openai_vision",
    }


async def ocr_image_bytes(
    image: bytes,
    *,
    filename: str = "slip.png",
    api_key: Optional[str] = None,
) -> str:
    """Return OCR / transcription text (Vision preferred, RapidOCR fallback)."""
    key, _model = _openai_credentials()
    key = (api_key or key).strip()
    if key:
        try:
            vision = await _vision_parse_slip(image, filename=filename, api_key=key)
            text = str(vision.get("raw_text") or "").strip()
            if not text:
                # reconstruct from legs
                text = "\n".join(
                    str(x.get("description") or "").strip()
                    for x in (vision.get("legs") or [])
                    if isinstance(x, dict) and x.get("description")
                )
            if text:
                return text
        except Exception as e:
            log.warning("OpenAI Vision OCR failed, falling back to RapidOCR: %s", e)

    text = await asyncio.to_thread(_ocr_rapid_sync, image)
    if not text or len(text.strip()) < 8:
        raise RuntimeError("OCR returned no usable text — try a clearer screenshot.")
    log.info("OCR via RapidOCR (%d chars)", len(text))
    return text.strip()


def _ocr_rapid_sync(image: bytes) -> str:
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(image))
    if img.mode != "RGB":
        img = img.convert("RGB")

    arr = np.asarray(img)
    result = _get_rapid_ocr()(arr)

    # rapidocr v3 → RapidOCROutput(txts=...)
    txts = getattr(result, "txts", None)
    if txts:
        return "\n".join(str(t).strip() for t in txts if t and str(t).strip())

    # legacy rapidocr_onnxruntime → (list of [box, text, score], elapsed)
    if isinstance(result, tuple) and result and result[0]:
        lines: list[str] = []
        for row in result[0]:
            if not row or len(row) < 2:
                continue
            text = str(row[1]).strip()
            if text:
                lines.append(text)
        return "\n".join(lines)

    raise RuntimeError("RapidOCR returned empty result")


def _to_american(token: str) -> Optional[int]:
    token = token.strip()
    if _AMERICAN_RE.match(token):
        return int(token)
    if _DEC_ODDS_RE.match(token):
        try:
            d = Decimal(token)
            if d > Decimal("1"):
                return decimal_to_american(d)
        except (InvalidOperation, ValueError):
            return None
    return None


def _is_noise_line(line: str) -> bool:
    s = line.strip()
    if not s or len(s) < 2:
        return True
    if _NOISE_RE.match(s):
        return True
    if _TIME_RE.match(s) or _DATE_RE.match(s) or _ET_TIME_RE.match(s):
        return True
    if _DEC_ODDS_RE.match(s) or _AMERICAN_RE.match(s):
        return False  # odds alone — handled by neighbor scan
    # Tiny OCR junk (status icons, battery %)
    if len(s) <= 2 and not s.isalpha():
        return True
    return False


def _looks_like_selection(line: str) -> bool:
    """True only for real bet lines, not bare names / market labels."""
    s = line.strip()
    if not s or _is_noise_line(s):
        return False
    if _MARKET_LINE_RE.match(s) or _MARKET_LABEL_RE.match(s) or _VS_LINE_RE.match(s):
        # VS lines are matchups, not selections — unless they also look like picks
        if _VS_LINE_RE.match(s) and not _SELECTION_RE.match(s):
            return False
        if _MARKET_LINE_RE.match(s) or _MARKET_LABEL_RE.match(s):
            return False
    # Strip trailing odds for the check
    m = _INLINE_ODDS_RE.match(s)
    head = m.group("sel").strip() if m else s
    if _SELECTION_RE.match(head) or _ROUND_OR_DEC_RE.match(head):
        return True
    if _KO_ROUNDS_RE.search(head) or _ROUND_OR_RE.search(head):
        return True
    return False


def _normalize_slip_lines(lines: list[str]) -> list[str]:
    """
    Repair common OCR wraps from FanDuel / DraftKings slips:

      'Fighter by KO, TKO, DQ or' + '1.64' + 'Submission'
        → 'Fighter by KO, TKO, DQ or Submission' + '1.64'

      'Islam Makhachev Round 4, 5, or' + '-110' + 'by Decision'
        → 'Islam Makhachev Round 4, 5, or by Decision' + '-110'

      'Joel Alvarez 1.35' + 'To Win Fight'
        → 'Joel Alvarez to win' + '1.35'

      'Joel Alvarez' + '-310' + 'MONEYLINE'
        → 'Joel Alvarez to win' + '-310'
    """
    out: list[str] = []
    i = 0
    while i < len(lines):
        cur = lines[i].strip()
        if not cur:
            i += 1
            continue

        # Wrap: "... or" [odds] "Submission" / "by Decision"
        if _OR_WRAP_RE.search(cur):
            j = i + 1
            odds_tok: Optional[str] = None
            while j < len(lines) and (
                _DEC_ODDS_RE.match(lines[j].strip())
                or _AMERICAN_RE.match(lines[j].strip())
            ):
                odds_tok = lines[j].strip()
                j += 1
            if j < len(lines) and _CONT_OUTCOME_RE.match(lines[j].strip()):
                joined = f"{cur} {lines[j].strip()}"
                out.append(joined)
                if odds_tok:
                    out.append(odds_tok)
                i = j + 1
                continue

        # Name + inline odds, next line is ML market
        inline = _INLINE_ODDS_RE.match(cur)
        if inline and _NAME_ONLY_RE.match(inline.group("sel").strip()):
            j = i + 1
            while j < len(lines) and (
                _DEC_ODDS_RE.match(lines[j].strip())
                or _AMERICAN_RE.match(lines[j].strip())
                or _DATE_RE.match(lines[j].strip())
                or _TIME_RE.match(lines[j].strip())
                or _ET_TIME_RE.match(lines[j].strip())
            ):
                j += 1
            if j < len(lines) and _ML_MARKET_RE.match(lines[j].strip()):
                name = inline.group("sel").strip()
                out.append(f"{name} to win")
                out.append(inline.group("odds"))
                i = j + 1
                continue

        # Bare name + odds on next line + MONEYLINE / To Win Fight
        if _NAME_ONLY_RE.match(cur) and not _looks_like_selection(cur):
            j = i + 1
            if j < len(lines) and (
                _DEC_ODDS_RE.match(lines[j].strip())
                or _AMERICAN_RE.match(lines[j].strip())
            ):
                odds_tok = lines[j].strip()
                k = j + 1
                # skip CASH OUT etc.
                while k < len(lines) and _is_noise_line(lines[k]) and not _ML_MARKET_RE.match(
                    lines[k].strip()
                ):
                    if _MARKET_LABEL_RE.match(lines[k].strip()) or _ML_MARKET_RE.match(
                        lines[k].strip()
                    ):
                        break
                    k += 1
                if k < len(lines) and (
                    _ML_MARKET_RE.match(lines[k].strip())
                    or _MARKET_LABEL_RE.match(lines[k].strip())
                    and re.search(r"moneyline|to win", lines[k], re.I)
                ):
                    out.append(f"{cur} to win")
                    out.append(odds_tok)
                    i = k + 1
                    continue

        out.append(cur)
        i += 1
    return out


def _peek_odds(lines: list[str], start: int) -> tuple[Optional[float], Optional[int], int]:
    """Read decimal/American odds on this line or the next few."""
    for j in range(start, min(start + 3, len(lines))):
        tok = lines[j].strip()
        # "Selection 3.05"
        m = _INLINE_ODDS_RE.match(tok)
        if m and j == start:
            am = _to_american(m.group("odds"))
            try:
                dec = float(m.group("odds")) if _DEC_ODDS_RE.match(m.group("odds")) else None
            except ValueError:
                dec = None
            if dec is None and am is not None:
                try:
                    dec = float(american_to_decimal(am))
                except Exception:
                    pass
            return dec, am, j
        if _DEC_ODDS_RE.match(tok) or _AMERICAN_RE.match(tok):
            am = _to_american(tok)
            try:
                dec = float(tok) if _DEC_ODDS_RE.match(tok) else None
            except ValueError:
                dec = None
            if dec is None and am is not None:
                try:
                    dec = float(american_to_decimal(am))
                except Exception:
                    pass
            return dec, am, j
    return None, None, start


def _find_parlay_total(lines: list[str]) -> Optional[int]:
    """Pick up '+1530' near a '5 leg Parlay' footer."""
    for i, line in enumerate(lines):
        if not _PARLAY_HEADER_RE.search(line):
            continue
        # same line or neighbors
        for j in range(max(0, i - 1), min(len(lines), i + 4)):
            tok = lines[j].strip()
            m = re.search(r"([+-]\d{3,5})\b", tok)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    pass
            if _AMERICAN_RE.match(tok):
                return int(tok)
    return None


def parse_bookmaker_slip(text: str) -> dict[str, Any]:
    """
    Parse OCR / pasted slip text into legs.

    Only lines that look like real selections (e.g. "Islam Makhachev by
    Submission") become legs. UI chrome, dates, times, market labels, and
    bare opponent names are ignored.
    """
    notes: list[str] = []
    raw_lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    lines = _normalize_slip_lines([ln for ln in raw_lines if ln])

    combined_decimal: Optional[float] = None
    combined_american: Optional[int] = None
    leg_decimals: list[float] = []
    leg_americans: list[int] = []

    footer_total = _find_parlay_total(lines)
    if footer_total is not None:
        combined_american = footer_total
        try:
            combined_decimal = float(american_to_decimal(footer_total))
        except Exception:
            pass
        notes.append(f"Parlay total from slip footer: {footer_total:+d}")

    legs: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if _PARLAY_HEADER_RE.search(line) and not _looks_like_selection(line):
            i += 1
            continue
        if _is_noise_line(line) and not _looks_like_selection(line):
            i += 1
            continue
        if not _looks_like_selection(line):
            i += 1
            continue

        selection = line
        inline = _INLINE_ODDS_RE.match(line)
        if inline:
            selection = inline.group("sel").strip()

        leg_dec, leg_am, odds_at = _peek_odds(lines, i)
        if leg_dec and leg_dec > 1:
            leg_decimals.append(leg_dec)
            notes.append(f"Leg decimal {leg_dec:g}: {selection}")
        if leg_am is not None:
            leg_americans.append(leg_am)
            notes.append(f"Leg american {leg_am:+d}: {selection}")

        # Optional market / matchup context on following lines
        fighter_a = fighter_b = None
        market_name = None
        j = max(i, odds_at) + 1
        for k in range(j, min(j + 6, len(lines))):
            nxt = lines[k]
            if _looks_like_selection(nxt):
                break
            m = _MARKET_LINE_RE.match(nxt)
            if m:
                market_name = m.group("market").strip()
                fighter_a = m.group("a").strip()
                fighter_b = m.group("b").strip()
                break
            vm = _VS_LINE_RE.match(nxt)
            if vm and not _SELECTION_RE.match(nxt):
                fighter_a = vm.group("a").strip()
                fighter_b = vm.group("b").strip()
                continue
            if _MARKET_LABEL_RE.match(nxt):
                market_name = nxt.strip()
                continue
            if re.search(r"\bmethod of victory\b|\bdouble chance\b|\bmoneyline\b", nxt, re.I):
                market_name = nxt.strip()
                continue

        leg = _selection_to_leg(
            selection,
            fighter_a=fighter_a,
            fighter_b=fighter_b,
            market_name=market_name,
        )
        if leg and leg.get("description"):
            # Drop bare-name / no-outcome leftovers
            if not leg.get("outcome_type") and not _looks_like_selection(selection):
                i = max(i, odds_at) + 1
                continue
            leg["selection_raw"] = selection
            if fighter_a:
                leg["fighter_a"] = fighter_a
            if fighter_b:
                leg["fighter_b"] = fighter_b
            if market_name:
                leg["market_name"] = market_name
            legs.append(leg)

        i = max(i, odds_at) + 1

    # Combine per-leg odds when footer didn't give a total
    if combined_american is None and len(leg_americans) >= 2:
        try:
            combo = combine_parlay(leg_americans)
            combined_american = combo["combined_american"]
            combined_decimal = combo["combined_decimal"]
            notes.append(
                f"Combined {len(leg_americans)} leg americans -> {combined_american:+d}"
            )
        except Exception:
            pass
    if combined_american is None and len(leg_decimals) >= 2:
        prod = Decimal("1")
        for d in leg_decimals:
            prod *= Decimal(str(d))
        combined_decimal = float(prod)
        try:
            combined_american = decimal_to_american(prod)
        except (InvalidOperation, ValueError):
            pass
        notes.append(
            f"Combined {len(leg_decimals)} leg decimals -> {combined_decimal:g}"
        )

    return {
        "legs": legs,
        "odds": combined_american,
        "decimal_odds": combined_decimal,
        "notes": notes,
        "raw_text": text,
    }


def _selection_to_leg(
    selection: str,
    *,
    fighter_a: Optional[str],
    fighter_b: Optional[str],
    market_name: Optional[str],
) -> Optional[dict[str, Any]]:
    sel = selection.strip()
    if not sel or len(sel) < 3:
        return None

    m = _ROUND_OR_DEC_RE.match(sel)
    if m:
        fighter = m.group("fighter").strip()
        rounds = re.findall(r"\d+", m.group("rounds"))
        label = ", ".join(rounds)
        ot = "R_" + "_".join(rounds) + "_DEC"
        return {
            "description": f"{fighter} Round {label} or by Decision",
            "fighter_pick": fighter,
            "outcome_type": ot,
            "outcome_round": None,
            "selection_raw": sel,
            "fighter_a": fighter_a,
            "fighter_b": fighter_b,
            "market_name": market_name,
        }

    m = _KO_ROUNDS_RE.search(sel)
    if m:
        fighter = m.group("fighter").strip()
        r1, r2 = m.group("r1"), m.group("r2")
        return {
            "description": f"{fighter} by KO/TKO in Rounds {r1} or {r2}",
            "fighter_pick": fighter,
            "outcome_type": None,
            "outcome_round": None,
            "selection_raw": sel,
            "fighter_a": fighter_a,
            "fighter_b": fighter_b,
            "market_name": market_name,
        }

    m = _ROUND_OR_RE.search(sel)
    if m:
        fighter = m.group("fighter").strip()
        r1, r2 = m.group("r1"), m.group("r2")
        return {
            "description": f"{fighter} Round {r1} or {r2}",
            "fighter_pick": fighter,
            "outcome_type": None,
            "outcome_round": None,
            "selection_raw": sel,
            "fighter_a": fighter_a,
            "fighter_b": fighter_b,
            "market_name": market_name,
        }

    parsed = parse_leg_line(sel)
    if not parsed.get("fighter_pick") and fighter_a and fighter_b:
        low = sel.lower()
        for name in (fighter_a, fighter_b):
            parts = name.split()
            if name.lower() in low or (parts and parts[-1].lower() in low):
                parsed["fighter_pick"] = name
                if not parsed.get("description"):
                    parsed["description"] = sel
                break
    # Require a real outcome — bare names are not legs
    if not parsed.get("outcome_type") and not (
        _SELECTION_RE.match(sel)
        or _KO_ROUNDS_RE.search(sel)
        or _ROUND_OR_RE.search(sel)
        or _ROUND_OR_DEC_RE.match(sel)
    ):
        return None
    if market_name and parsed.get("description"):
        pass  # keep selection text as description
    elif not parsed.get("description"):
        parsed["description"] = sel
    return {
        "description": parsed.get("description") or sel,
        "fighter_pick": parsed.get("fighter_pick"),
        "outcome_type": parsed.get("outcome_type"),
        "outcome_round": parsed.get("outcome_round"),
        "selection_raw": sel,
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "market_name": market_name,
    }


async def image_to_slip(image: bytes, *, filename: str = "slip.png") -> dict[str, Any]:
    """
    Parse a slip screenshot into legs.

    Prefers OpenAI Vision structured JSON when OPENAI_API_KEY is set.
    Falls back to RapidOCR + local line parser. After confirm, bets still
    grade through the existing ESPN auto-grader like any other logged slip.
    """
    key, _model = _openai_credentials()
    if key:
        try:
            vision = await _vision_parse_slip(image, filename=filename, api_key=key)
            result = _vision_payload_to_slip(vision)
            usage = vision.get("_usage") if isinstance(vision, dict) else None
            if usage:
                result["openai_usage"] = usage
            if result.get("legs"):
                result["ocr_text"] = result.get("raw_text") or ""
                return result
            log.warning("Vision returned no legs — falling back to RapidOCR")
        except Exception as e:
            log.warning("OpenAI Vision slip parse failed: %s", e)

    text = await ocr_image_bytes(image, filename=filename)
    # If Vision key exists but structured path failed, ocr_image_bytes may
    # already have used Vision for raw_text — parse locally either way.
    result = parse_bookmaker_slip(text)
    result["ocr_text"] = text
    if not result.get("source"):
        result["source"] = "rapidocr" if not key else "openai_vision_text+local"
    return result


def text_to_slip(text: str) -> dict[str, Any]:
    result = parse_bookmaker_slip(text)
    result["ocr_text"] = text
    result["source"] = "text"
    return result
