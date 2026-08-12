"""
Renders the /spread-sheet event recap as a drawn table image (PNG), matching
the classic straight-picks / props / parlays recap layout:

  Straights:  Pick | Opponent | W/L | Odds | Unit Bet | Unit Profit | Cash Profit | ROI
  Props:      Fight | Pick     | W/L | Odds | Unit Bet | Unit Profit | Cash Profit | ROI
  Parlays:    Fight | Pick     | one W/L for the whole slip (last leg row)

Uses matplotlib's *bundled* DejaVu Sans fonts so no system font pack is required.
"""
from __future__ import annotations

import io
import os
from typing import Any, Optional

import matplotlib
from PIL import Image, ImageDraw, ImageFont

import chart
from bet_types import categorize_legs, effective_legs
from betting_math import bet_profit_native, bet_stake_native, format_native_with_usd
from card_data import match_fighter_on_card, resolve_fighter_on_card
from grading import _name_matches

_FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")

# Colors (close to the classic white-table recap look)
BG = "#FFFFFF"
TEXT = "#1A1A1A"
MUTED = "#6B6B6B"
HEADER_BG = "#F0F0F0"
SECTION_TOTAL_BG = "#E4E4E4"
TOTAL_BG = "#CDEFD3"
TOTAL_LOSS_BG = "#F8D7DA"
GRID = "#D4D4D4"
CLUSTER_GRID = "#EBEBEB"
PARLAY_BAND = "#F7F7F7"
WIN_GREEN = "#0E7A0E"
LOSS_RED = "#C41E3A"
PENDING = "#888888"

# Shared numeric column keys across all sections (indices 3-7)
# Col 0-1 change labels by section; col 2 is the W/L letter (no header text)
N_COLS = 8


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(os.path.join(_FONT_DIR, name), size)


def _find_opponent(fighter: Optional[str], fights: list[tuple[str, str]]) -> str:
    if not fighter:
        return ""
    for fight in fights:
        a, b = fight[0], fight[1]
        if _name_matches(fighter, a):
            return b
        if _name_matches(fighter, b):
            return a
    return ""


def _fight_label(fighter: Optional[str], fights: list[tuple[str, str]]) -> str:
    if not fighter:
        return ""
    hit = match_fighter_on_card(fighter, fights)
    if hit:
        return hit[2]  # "A vs B"
    opp = _find_opponent(fighter, fights)
    return f"{fighter} vs {opp}" if opp else fighter


def _resolve_fighter(
    leg: dict[str, Any], fights: list[tuple[str, str]]
) -> Optional[str]:
    """Canonical card fighter for the leg (pick validated, else free-text scrape)."""
    hit = resolve_fighter_on_card(
        fighter_pick=leg.get("fighter_pick"),
        description=leg.get("description"),
        fights=fights,
    )
    if hit:
        return hit[0]
    # Fall back to raw pick text when the card is empty/unavailable
    return leg.get("fighter_pick") or None


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _result_letter(status: Optional[str]) -> str:
    if status == "won":
        return "W"
    if status == "loss":
        return "L"
    if status == "void":
        return "V"
    if status == "pending":
        return "—"
    return ""


def _result_color(result: str) -> str:
    if result == "W":
        return WIN_GREEN
    if result == "L":
        return LOSS_RED
    if result in ("V", "—"):
        return PENDING
    return TEXT


def _profit_color(amount: Optional[float], settled: bool) -> str:
    if not settled or amount is None:
        return TEXT
    if amount > 0:
        return WIN_GREEN
    if amount < 0:
        return LOSS_RED
    return TEXT


def _fmt_odds(odds: Optional[int]) -> str:
    if odds is None:
        return ""
    return f"+{odds}" if odds > 0 else str(odds)


def _fmt_unit_bet(units: Optional[float]) -> str:
    if units is None:
        return ""
    return f"{units:g}"


def _fmt_unit_profit(amount: Optional[float]) -> str:
    if amount is None:
        return ""
    return f"{amount:+.2f}" if amount != 0 else "0.00"


def _fmt_cash(amount: Optional[float], currency: str) -> str:
    if amount is None:
        return ""
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency, currency + " ")
    sign = "-" if amount < 0 else ("+" if amount > 0 else "")
    return f"{sign}{symbol}{abs(amount):,.2f}"


def _fmt_cash_converted(amount: Optional[float], currency: str) -> str:
    if amount is None:
        return ""
    return format_native_with_usd(amount, currency, signed=True)


def _fmt_roi(amount: Optional[float]) -> str:
    if amount is None:
        return ""
    return f"{amount:+.0%}"


def _settled_metrics(bet: dict[str, Any], unit_value: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (profit_units, profit_cash, roi) for settled slips; else Nones."""
    status = bet.get("status")
    if status not in ("won", "loss"):
        return None, None, None
    profit_native = bet_profit_native(bet, unit_value)
    profit_units = profit_native / unit_value
    stake_native = bet_stake_native(bet, unit_value)
    roi = (profit_native / stake_native) if stake_native else None
    return profit_units, profit_native, roi


class _Row:
    def __init__(
        self,
        cells: Optional[list[tuple[str, str, bool]]] = None,
        *,
        bar_text: str = "",
        bar_bg: Optional[str] = None,
        height: int = 26,
        group_end: bool = True,
        is_parlay_boundary: bool = False,
        right_align_from: int = 3,
    ):
        self.cells = cells
        self.bar_text = bar_text
        self.bar_bg = bar_bg
        self.height = height
        self.group_end = group_end
        self.is_parlay_boundary = is_parlay_boundary
        self.right_align_from = right_align_from


def _header_cells(col0: str, col1: str) -> list[tuple[str, str, bool]]:
    # col2 is the result letter column (header intentionally blank)
    return [
        (col0, TEXT, True),
        (col1, TEXT, True),
        ("", TEXT, True),
        ("Odds", TEXT, True),
        ("Unit Bet", TEXT, True),
        ("Unit Profit", TEXT, True),
        ("Cash Profit", TEXT, True),
        ("ROI", TEXT, True),
    ]


def _data_cells(
    *,
    col0: str,
    col1: str,
    result: str,
    odds: Optional[int],
    units: Optional[float],
    profit_units: Optional[float],
    profit_cash: Optional[float],
    roi: Optional[float],
    currency: str,
    settled: bool,
    bold_left: bool = True,
) -> list[tuple[str, str, bool]]:
    p_color = _profit_color(profit_units if profit_units is not None else profit_cash, settled)
    r_color = _profit_color(roi, settled and roi is not None)
    return [
        (_clean(col0), TEXT, bold_left),
        (_clean(col1), TEXT, False),
        (result, _result_color(result), True),
        (_fmt_odds(odds), TEXT, False),
        (_fmt_unit_bet(units), TEXT, False),
        (_fmt_unit_profit(profit_units), p_color, False),
        (_fmt_cash(profit_cash, currency), p_color, False),
        (_fmt_roi(roi), r_color, True),
    ]


def _totals_cells(
    label: str,
    unit_bet: float,
    unit_profit: float,
    cash_profit: float,
    roi: Optional[float],
    currency: str,
    *,
    convert_cash: bool = False,
) -> list[tuple[str, str, bool]]:
    color = WIN_GREEN if unit_profit > 0 else LOSS_RED if unit_profit < 0 else TEXT
    cash_str = _fmt_cash_converted(cash_profit, currency) if convert_cash else _fmt_cash(cash_profit, currency)
    return [
        (label, TEXT, True),
        ("", TEXT, False),
        ("", TEXT, False),
        ("", TEXT, False),
        (_fmt_unit_bet(unit_bet), TEXT, True),
        (_fmt_unit_profit(unit_profit), color, True),
        (cash_str, color, True),
        (_fmt_roi(roi), color, True),
    ]


def _prop_pick_text(leg: dict[str, Any]) -> str:
    """Prefer the free-text description; strip redundant fighter prefix when
    the Fight column already shows the matchup."""
    return leg.get("description") or leg.get("fighter_pick") or "Pick"


def build_event_recap_image(
    *,
    event_name: str,
    event_date: Optional[str],
    bets: list[dict[str, Any]],
    legs_by_bet_id: dict[int, list[dict[str, Any]]],
    fights: list[tuple[str, str]],
    unit_value: float,
    currency: str,
) -> bytes:
    rows: list[_Row] = []

    grouped: dict[str, list[dict[str, Any]]] = {"Straight Pick": [], "Prop": [], "Parlay": []}
    for bet in bets:
        legs = effective_legs(bet, legs_by_bet_id.get(bet["id"], []))
        grouped[categorize_legs(legs)].append(bet)

    # Running totals per major group
    straight_units = straight_profit_u = straight_profit_c = 0.0
    prop_parlay_units = prop_parlay_profit_u = prop_parlay_profit_c = 0.0
    any_bets = False

    # ---------- Straight Picks ----------
    if grouped["Straight Pick"]:
        any_bets = True
        rows.append(_Row(bar_text="Straight Picks", height=28))
        hdr = _Row(cells=_header_cells("Pick", "Opponent"), height=24)
        hdr.bar_bg = HEADER_BG
        rows.append(hdr)

        for bet in grouped["Straight Pick"]:
            legs = effective_legs(bet, legs_by_bet_id.get(bet["id"], []))
            leg = legs[0] if legs else {}
            fighter = _resolve_fighter(leg, fights) or ""
            pick = fighter or (leg.get("description") or bet.get("bet_title") or "Pick")
            opponent = ""
            if fighter:
                hit = match_fighter_on_card(fighter, fights)
                opponent = hit[1] if hit else _find_opponent(fighter, fights)
            status = bet.get("status")
            result = _result_letter(status)
            pu, pc, roi = _settled_metrics(bet, unit_value)

            cells = _data_cells(
                col0=pick,
                col1=opponent,
                result=result,
                odds=bet.get("odds"),
                units=bet.get("units"),
                profit_units=pu,
                profit_cash=pc,
                roi=roi,
                currency=currency,
                settled=status in ("won", "loss"),
            )
            rows.append(_Row(cells=cells, height=26))

            units = bet.get("units") or 0
            straight_units += units
            if pu is not None:
                straight_profit_u += pu
                straight_profit_c += pc or 0

        s_roi = (straight_profit_c / (straight_units * unit_value)) if straight_units else None
        tot = _Row(
            cells=_totals_cells(
                "Straight Pick Event Totals:",
                straight_units,
                straight_profit_u,
                straight_profit_c,
                s_roi,
                currency,
            ),
            height=28,
        )
        tot.bar_bg = SECTION_TOTAL_BG
        rows.append(tot)
        rows.append(_Row(height=12))

    # ---------- Props ----------
    if grouped["Prop"]:
        any_bets = True
        rows.append(_Row(bar_text="Prop Picks", height=28))
        hdr = _Row(cells=_header_cells("Fight", "Pick"), height=24)
        hdr.bar_bg = HEADER_BG
        rows.append(hdr)

        for bet in grouped["Prop"]:
            legs = effective_legs(bet, legs_by_bet_id.get(bet["id"], []))
            leg = legs[0] if legs else {}
            fighter = _resolve_fighter(leg, fights)
            fight = _fight_label(fighter, fights) if fighter else ""
            pick = _prop_pick_text(leg)
            # If we have no structured fight, put description under Pick and leave Fight blank
            if not fight and not fighter:
                fight = ""
            status = bet.get("status")
            result = _result_letter(status)
            pu, pc, roi = _settled_metrics(bet, unit_value)

            cells = _data_cells(
                col0=fight,
                col1=pick,
                result=result,
                odds=bet.get("odds"),
                units=bet.get("units"),
                profit_units=pu,
                profit_cash=pc,
                roi=roi,
                currency=currency,
                settled=status in ("won", "loss"),
            )
            rows.append(_Row(cells=cells, height=26))

            units = bet.get("units") or 0
            prop_parlay_units += units
            if pu is not None:
                prop_parlay_profit_u += pu
                prop_parlay_profit_c += pc or 0

        rows.append(_Row(height=8))

    # ---------- Parlays ----------
    if grouped["Parlay"]:
        any_bets = True
        rows.append(_Row(bar_text="Parlays", height=28))
        hdr = _Row(cells=_header_cells("Fight", "Pick"), height=24)
        hdr.bar_bg = HEADER_BG
        rows.append(hdr)

        for p_i, bet in enumerate(grouped["Parlay"]):
            legs = effective_legs(bet, legs_by_bet_id.get(bet["id"], []))
            status = bet.get("status")
            overall = _result_letter(status)
            pu, pc, roi = _settled_metrics(bet, unit_value)
            band = PARLAY_BAND if p_i % 2 == 1 else None

            for i, leg in enumerate(legs):
                fighter = _resolve_fighter(leg, fights)
                fight = _fight_label(fighter, fights) if fighter else ""
                if not fight:
                    fight = ""
                pick = _prop_pick_text(leg)

                # Odds / stake / P&L on the first leg row; a single W/L for the
                # whole slip on the last leg row. Never show per-leg W/L.
                is_first = i == 0
                is_last = i == len(legs) - 1
                cells = _data_cells(
                    col0=fight,
                    col1=pick,
                    result=overall if is_last else "",
                    odds=bet.get("odds") if is_first else None,
                    units=bet.get("units") if is_first else None,
                    profit_units=pu if is_first else None,
                    profit_cash=pc if is_first else None,
                    roi=roi if is_first else None,
                    currency=currency,
                    settled=is_first and status in ("won", "loss"),
                    bold_left=True,
                )
                leg_row = _Row(
                    cells=cells,
                    height=24,
                    group_end=is_last,
                    is_parlay_boundary=is_last,
                )
                leg_row.bar_bg = band
                rows.append(leg_row)

            rows.append(_Row(height=6))

            units = bet.get("units") or 0
            prop_parlay_units += units
            if pu is not None:
                prop_parlay_profit_u += pu
                prop_parlay_profit_c += pc or 0

    # ---------- Grand event total only (no gray subtotal bar above it) ----------
    if any_bets:
        grand_units = straight_units + prop_parlay_units
        grand_pu = straight_profit_u + prop_parlay_profit_u
        grand_pc = straight_profit_c + prop_parlay_profit_c
        # ROI vs settled stake only (pending units shouldn't shrink ROI)
        settled_stake_u = 0.0
        for bet in bets:
            if bet.get("status") in ("won", "loss"):
                settled_stake_u += float(bet.get("units") or 0)
        grand_roi = (grand_pc / (settled_stake_u * unit_value)) if settled_stake_u else None
        grand = _Row(
            cells=_totals_cells(
                "Event Totals (Props, Parlays & Straight Bets):",
                grand_units,
                grand_pu,
                grand_pc,
                grand_roi,
                currency,
                convert_cash=True,
            ),
            height=30,
        )
        grand.bar_bg = TOTAL_BG if grand_pu >= 0 else TOTAL_LOSS_BG
        rows.append(grand)

    # ---------- render table + top-right units panel (same light sheet) ----------
    title = _clean(event_name) + (f", {event_date}" if event_date else "")
    title_font = _font(16, bold=True)
    section_font = _font(13, bold=True)
    cell_font = _font(11, bold=False)
    cell_bold_font = _font(11, bold=True)

    padding = 18
    col_gap = 14

    panel_img: Optional[Image.Image] = None
    panel_bytes = chart.build_sheet_units_panel(bets, unit_value, width_px=340, height_px=168)
    if panel_bytes:
        panel_img = Image.open(io.BytesIO(panel_bytes)).convert("RGB")

    measure_img = Image.new("RGB", (10, 10))
    measure_draw = ImageDraw.Draw(measure_img)

    # floors: Pick/Fight, Opponent/Pick, Result, Odds, Unit Bet, Unit Profit, Cash, ROI
    min_widths = [130, 150, 22, 48, 60, 72, 110, 48]
    col_widths = list(min_widths)

    for row in rows:
        if row.cells is None:
            continue
        for i, (text, _color, bold) in enumerate(row.cells):
            if not text:
                continue
            font = cell_bold_font if bold else cell_font
            tw = measure_draw.textlength(text, font=font)
            if i == 0 and tw > col_widths[0] + col_widths[1] + col_gap:
                col_widths[0] = max(col_widths[0], int(tw * 0.55))
            elif i == 0:
                col_widths[0] = max(col_widths[0], tw)
            else:
                col_widths[i] = max(col_widths[i], tw)

    col_widths = [int(w) + 8 for w in col_widths]

    col_x = [padding]
    for w in col_widths[:-1]:
        col_x.append(col_x[-1] + w + col_gap)

    content_width = col_x[-1] + col_widths[-1] + padding
    title_w = int(measure_draw.textlength(title, font=title_font)) + padding * 2

    panel_w = panel_img.width if panel_img else 0
    panel_h = panel_img.height if panel_img else 0
    header_height = max(36, panel_h + 8) if panel_img else 32
    width = max(
        content_width,
        title_w + (panel_w + padding if panel_img else 0),
        panel_w + padding * 2,
    )

    total_height = padding * 2 + header_height + sum(r.height for r in rows) + 4

    img = Image.new("RGB", (width, total_height), BG)
    draw = ImageDraw.Draw(img)

    # Title left, light units panel top-right
    title_y = padding + max(0, (header_height - 20) // 2) if panel_img else padding
    draw.text((padding, title_y), title, font=title_font, fill=TEXT)
    if panel_img is not None:
        px = width - padding - panel_w
        py = padding
        draw.rectangle(
            [px - 1, py - 1, px + panel_w, py + panel_h],
            outline=GRID,
            width=1,
        )
        img.paste(panel_img, (px, py))

    y = padding + header_height

    for row in rows:
        if row.bar_bg and row.cells is not None:
            draw.rectangle([padding - 4, y, width - padding + 4, y + row.height], fill=row.bar_bg)

        if row.cells is None:
            if row.bar_text:
                draw.text(
                    (padding, y + (row.height - 14) // 2),
                    row.bar_text,
                    font=section_font,
                    fill=TEXT,
                )
        else:
            for i, (text, color, bold) in enumerate(row.cells):
                if not text:
                    continue
                font = cell_bold_font if bold else cell_font
                x = col_x[i]
                if i >= row.right_align_from:
                    tw = draw.textlength(text, font=font)
                    x = col_x[i] + col_widths[i] - tw
                elif i == 2:
                    tw = draw.textlength(text, font=font)
                    x = col_x[i] + (col_widths[i] - tw) / 2
                draw.text((x, y + (row.height - 13) // 2), text, font=font, fill=color)

        if row.cells is not None and row.bar_bg not in (HEADER_BG, SECTION_TOTAL_BG, TOTAL_BG, TOTAL_LOSS_BG):
            if row.is_parlay_boundary:
                line_color, line_width = GRID, 2
            elif row.group_end:
                line_color, line_width = GRID, 1
            else:
                line_color, line_width = CLUSTER_GRID, 1
            draw.line(
                [(padding - 4, y + row.height), (width - padding + 4, y + row.height)],
                fill=line_color,
                width=line_width,
            )

        y += row.height

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()
