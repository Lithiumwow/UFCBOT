"""
Generates a cumulative profit line chart (PNG bytes) for /pl and /results,
plus a compact light-theme sheet panel for /spread-sheet.
"""
from __future__ import annotations

import datetime
import io
from collections import OrderedDict
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")  # headless -- no display needed, safe for any server
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from betting_math import bet_profit_native, CURRENCY_SYMBOLS

# Discord-ish dark theme colors so the chart blends into an embed
_DARK_BG = "#2b2d31"
_DARK_GRID = "#3f4147"
_DARK_AXIS = "#4e5058"
_DARK_TEXT = "#b5bac1"

# Light sheet theme -- matches /spread-sheet white table
_SHEET_BG = "#FFFFFF"
_SHEET_GRID = "#E0E0E0"
_SHEET_AXIS = "#C9C9C9"
_SHEET_TEXT = "#4A4A4A"
_SHEET_BORDER = "#D4D4D4"

_GREEN = "#0E7A0E"
_RED = "#C41E3A"
_GREEN_SOFT = "#3ba55d"
_RED_SOFT = "#ed4245"


def _settled_delta(bet: dict[str, Any], unit_value: float, *, in_units: bool) -> float:
    profit_native = bet_profit_native(bet, unit_value)
    return (profit_native / unit_value) if in_units else profit_native


def _daily_cumulative(
    bets: list[dict[str, Any]], unit_value: float, *, in_units: bool
) -> tuple[list[datetime.datetime], list[float]]:
    """Roll same-day settled P/L into one point per calendar day so multi-slip
    fight nights don't draw as vertical spikes (good for all-time /pl)."""
    day_pnl: OrderedDict[datetime.date, float] = OrderedDict()

    settled = [b for b in bets if b["status"] in ("won", "loss")]
    settled.sort(key=lambda b: (b.get("created_at") or "", b.get("id") or 0))

    for b in settled:
        created = b.get("created_at") or ""
        try:
            dt = datetime.datetime.fromisoformat(created)
        except ValueError:
            continue
        day = dt.date()
        day_pnl[day] = day_pnl.get(day, 0.0) + _settled_delta(b, unit_value, in_units=in_units)

    if not day_pnl:
        return [], []

    dates: list[datetime.datetime] = []
    cumulative: list[float] = []
    running = 0.0
    for day, pnl in day_pnl.items():
        running += pnl
        dates.append(datetime.datetime.combine(day, datetime.time(12, 0)))
        cumulative.append(running)
    return dates, cumulative


def _ordered_unit_cumulative(bets: list[dict[str, Any]], unit_value: float) -> list[float]:
    """Cumulative units after each settled slip (time/id order)."""
    settled = [b for b in bets if b["status"] in ("won", "loss")]
    settled.sort(key=lambda b: (b.get("created_at") or "", b.get("id") or 0))
    out: list[float] = []
    running = 0.0
    for b in settled:
        running += _settled_delta(b, unit_value, in_units=True)
        out.append(running)
    return out


def _smooth_series(y: np.ndarray, alpha: float = 0.28) -> np.ndarray:
    """Light EMA then re-pin endpoints so the curve stays true to start/end."""
    if len(y) < 3:
        return y.copy()
    out = np.empty_like(y, dtype=float)
    out[0] = y[0]
    for i in range(1, len(y)):
        out[i] = alpha * y[i] + (1.0 - alpha) * out[i - 1]
    # pass reverse to calm one-step spikes further
    for i in range(len(y) - 2, -1, -1):
        out[i] = alpha * out[i] + (1.0 - alpha) * out[i + 1]
    out[0] = y[0]
    out[-1] = y[-1]
    return out


def _smooth_curve(
    x: np.ndarray, y: np.ndarray, points_per_seg: int = 28
) -> tuple[np.ndarray, np.ndarray]:
    """Catmull-Rom spline through keypoints."""
    if len(x) < 2:
        return x, y
    if len(x) == 2:
        t = np.linspace(0.0, 1.0, max(points_per_seg, 2))
        return x[0] + t * (x[1] - x[0]), y[0] + t * (y[1] - y[0])

    xs = np.concatenate([[x[0] - (x[1] - x[0])], x, [x[-1] + (x[-1] - x[-2])]])
    ys = np.concatenate([[y[0]], y, [y[-1]]])  # flat padding reduces end spikes

    out_x: list[float] = []
    out_y: list[float] = []
    for i in range(1, len(xs) - 2):
        p0x, p1x, p2x, p3x = xs[i - 1], xs[i], xs[i + 1], xs[i + 2]
        p0y, p1y, p2y, p3y = ys[i - 1], ys[i], ys[i + 1], ys[i + 2]
        steps = points_per_seg if i < len(xs) - 3 else points_per_seg + 1
        for j in range(steps):
            t = j / points_per_seg
            t2, t3 = t * t, t * t * t
            ox = 0.5 * (
                (2 * p1x)
                + (-p0x + p2x) * t
                + (2 * p0x - 5 * p1x + 4 * p2x - p3x) * t2
                + (-p0x + 3 * p1x - 3 * p2x + p3x) * t3
            )
            oy = 0.5 * (
                (2 * p1y)
                + (-p0y + p2y) * t
                + (2 * p0y - 5 * p1y + 4 * p2y - p3y) * t2
                + (-p0y + 3 * p1y - 3 * p2y + p3y) * t3
            )
            out_x.append(ox)
            out_y.append(oy)

    out_x[0], out_y[0] = float(x[0]), float(y[0])
    out_x[-1], out_y[-1] = float(x[-1]), float(y[-1])
    return np.array(out_x), np.array(out_y)


def build_sheet_units_panel(
    bets: list[dict[str, Any]],
    unit_value: float,
    *,
    width_px: int = 340,
    height_px: int = 170,
) -> Optional[bytes]:
    """Compact light-theme units curve for embedding in the event recap sheet.
    Even x-spacing (bet order) so same-night slips don't spike; no markers.
    """
    cumulative = _ordered_unit_cumulative(bets, unit_value)
    if not cumulative:
        return None

    y_raw = np.array([0.0] + cumulative, dtype=float)
    final = float(y_raw[-1])
    line_color = _GREEN if final >= 0 else _RED

    # Even spacing kills timestamp clustering spikes
    x_raw = np.arange(len(y_raw), dtype=float)
    # Stronger soften for the small panel so fight-night run doesn't look jagged
    y_soft = _smooth_series(y_raw, alpha=0.18)
    xs, ys = _smooth_curve(x_raw, y_soft, points_per_seg=48)

    dpi = 120
    fig_w = width_px / dpi
    fig_h = height_px / dpi
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    fig.patch.set_facecolor(_SHEET_BG)
    ax.set_facecolor(_SHEET_BG)

    ax.plot(xs, ys, color=line_color, linewidth=2.0, solid_capstyle="round", zorder=3)
    ax.fill_between(xs, ys, 0, where=(ys >= 0), interpolate=True, color=_GREEN, alpha=0.12, zorder=1)
    ax.fill_between(xs, ys, 0, where=(ys < 0), interpolate=True, color=_RED, alpha=0.12, zorder=1)
    ax.axhline(0, color=_SHEET_AXIS, linewidth=0.9, zorder=2)

    ax.set_title("Units", color=_SHEET_TEXT, fontsize=10, pad=6, loc="left", fontweight="bold")
    ax.tick_params(colors=_SHEET_TEXT, labelsize=7, length=2)
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_color(_SHEET_BORDER)
        spine.set_linewidth(0.8)
    ax.grid(True, color=_SHEET_GRID, linewidth=0.5, alpha=0.9)
    ax.set_xlim(xs[0], xs[-1])

    # Tiny final label inside the axes
    ax.annotate(
        f"{final:+.2f}u",
        xy=(xs[-1], final),
        xytext=(-4, 6),
        textcoords="offset points",
        ha="right",
        color=line_color,
        fontsize=9,
        fontweight="bold",
    )

    fig.subplots_adjust(left=0.12, right=0.96, top=0.82, bottom=0.12)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), dpi=dpi)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def build_profit_chart(
    bets: list[dict[str, Any]],
    unit_value: float,
    currency: str,
    *,
    in_units: bool = False,
    title: Optional[str] = None,
    aggregate_by: str = "day",
) -> Optional[bytes]:
    """Returns PNG bytes of a cumulative-profit-over-time chart, or None if
    there's nothing settled yet to plot. Dark theme for Discord embeds (/pl).
    """
    if aggregate_by == "bet":
        # Even-spaced unit series for cleaner event curve if ever called here
        units = _ordered_unit_cumulative(bets, unit_value) if in_units else None
        if units is None:
            dates, cumulative = _daily_cumulative(bets, unit_value, in_units=in_units)
            if not dates:
                return None
            plot_x = mdates.date2num([dates[0]] + dates)
            plot_y = np.array([0.0] + cumulative, dtype=float)
            use_dates = True
        else:
            plot_y = np.array([0.0] + units, dtype=float)
            plot_x = np.arange(len(plot_y), dtype=float)
            use_dates = False
    else:
        dates, cumulative = _daily_cumulative(bets, unit_value, in_units=in_units)
        if not dates:
            return None
        plot_x = mdates.date2num([dates[0]] + dates)
        plot_y = np.array([0.0] + cumulative, dtype=float)
        use_dates = True

    final = float(plot_y[-1])
    line_color = _GREEN_SOFT if final >= 0 else _RED_SOFT
    y_soft = _smooth_series(plot_y, alpha=0.30)
    xs, ys = _smooth_curve(plot_x, y_soft)

    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=150)
    fig.patch.set_facecolor(_DARK_BG)
    ax.set_facecolor(_DARK_BG)

    ax.plot(xs, ys, color=line_color, linewidth=2.4, solid_capstyle="round", zorder=3)
    ax.fill_between(xs, ys, 0, where=(ys >= 0), interpolate=True, color=_GREEN_SOFT, alpha=0.22, zorder=1)
    ax.fill_between(xs, ys, 0, where=(ys < 0), interpolate=True, color=_RED_SOFT, alpha=0.22, zorder=1)
    ax.axhline(0, color=_DARK_AXIS, linewidth=1.1, zorder=2)

    if title:
        chart_title = title
    elif in_units:
        chart_title = "Cumulative Units (u)"
    else:
        symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")
        chart_title = f"Cumulative Profit ({symbol})"

    ax.set_title(chart_title, color="white", fontsize=13, pad=12)
    if in_units:
        ax.set_ylabel("Units", color=_DARK_TEXT, fontsize=10)
    ax.tick_params(colors=_DARK_TEXT, labelsize=9)
    for spine in ax.spines.values():
        spine.set_color(_DARK_AXIS)
    ax.grid(True, color=_DARK_GRID, linewidth=0.5, alpha=0.6)

    if use_dates:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.xaxis_date()
        fig.autofmt_xdate()
    else:
        ax.set_xticks([])
        ax.set_xlabel("Settled slips →", color=_DARK_TEXT, fontsize=9)

    last_label = f"{final:+.2f}u" if in_units else f"{final:+,.2f}"
    ax.annotate(
        last_label,
        xy=(xs[-1], final),
        xytext=(8, 8),
        textcoords="offset points",
        color=line_color,
        fontsize=10,
        fontweight="bold",
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
