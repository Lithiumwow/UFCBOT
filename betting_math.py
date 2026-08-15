"""
Profit calculation for a single bet, in units, using American odds --
plus currency helpers. Each user has their own currency (config.USER_CURRENCY)
and their own unit size (config.DEFAULT_UNIT_VALUE, overridable per-user via
/unit-size and stored in the database) -- callers (embeds.py, chart.py,
cogs) fetch the requesting user's unit_value/currency and pass them in here
explicitly, rather than this module reaching for global state, since a
single bot instance now serves multiple users with different settings.

Profit rules:
- status == "won":
    odds is None -> flat payout of `units` (no odds were recorded)
    odds > 0 (underdog)  -> profit = units * (odds / 100)
    odds < 0 (favorite)  -> profit = units * (100 / abs(odds))
- status == "loss":  profit = -units
- status == "void" or "pending": profit = 0.0 (stake returned / not yet settled)
"""
from typing import Optional

import config

CURRENCY_SYMBOLS = config.CURRENCY_SYMBOLS
SUPPORTED_CURRENCIES = ("GBP", "EUR", "USD")


def personalize_collab_bet(bet: dict, viewer_id: int) -> dict:
    """A collab bet is one DB row shared by two people (host = user_id,
    partner = co_user_id), each with their own stake -- the host's own
    units/odds live in the normal `units`/`odds` columns, the partner's
    in `partner_units`/`partner_odds`. Every consumer (embeds, /card,
    /pl, /results, notifications, spreadsheets) should call this right
    after fetching a bet so it always shows the VIEWER's own numbers,
    not necessarily the host's -- returns a shallow copy with units/odds
    swapped in for the partner's case; the host's case and any
    non-collab bet are returned completely unchanged (no copy needed)."""
    if not bet.get("is_collab") or viewer_id != bet.get("co_user_id"):
        return bet
    out = dict(bet)
    if bet.get("partner_units") is not None:
        out["units"] = bet["partner_units"]
    if bet.get("partner_odds") is not None:
        out["odds"] = bet["partner_odds"]
    return out


def get_currency_for_user(user_id: int) -> str:
    """Config fallback when the user hasn't saved a currency in the DB yet."""
    return config.USER_CURRENCY.get(user_id, "GBP")


async def get_user_settings(db, user_id: int) -> tuple[float, str]:
    """Returns (unit_value, currency) for a user -- the one place both are
    resolved together, since almost every embed/chart call needs both.

    Currency preference order: DB override → config.USER_CURRENCY → GBP.
    Changing currency only affects slip display / cash figures; auto-grading
    is unaffected (it settles Won/Loss from fight results, not currency).
    """
    unit_value = await db.get_unit_value(user_id, config.DEFAULT_UNIT_VALUE)
    stored = await db.get_currency(user_id)
    if stored and stored.upper() in SUPPORTED_CURRENCIES:
        currency = stored.upper()
    else:
        currency = get_currency_for_user(user_id)
    return unit_value, currency


def units_to_native(units: float, unit_value: float) -> float:
    return units * unit_value


def native_to_usd(amount: float, currency: str) -> float:
    rate = config.CURRENCY_TO_USD_RATE.get(currency, 1.0)
    return amount * rate


def format_currency(amount: float, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol}{abs(amount):,.2f}"


def format_native_with_usd(amount_native: float, currency: str, *, signed: bool = False) -> str:
    """e.g. '£100.00 ($133.00)' or, if signed, '+£100.00 (+$133.00)'.
    If the user's own currency IS USD, just shows the one figure (no point
    converting USD to USD)."""
    native_str = format_currency(amount_native, currency)
    if signed and amount_native >= 0:
        native_str = f"+{native_str}"

    if currency == "USD":
        return native_str

    usd = native_to_usd(amount_native, currency)
    usd_str = format_currency(usd, "USD")
    if signed and amount_native >= 0:
        usd_str = f"+{usd_str}"
    return f"{native_str} ({usd_str})"


def calculate_profit(units: float, odds: Optional[int], status: str) -> float:
    status = (status or "").lower()
    if status == "won":
        if odds is None:
            return units
        if odds > 0:
            return units * (odds / 100)
        elif odds < 0:
            return units * (100 / abs(odds))
        return units  # odds == 0, treat as flat
    if status == "loss":
        return -units
    return 0.0  # void / pending


def bet_stake_native(bet: dict, unit_value: float) -> float:
    """The amount actually staked, in the bet owner's currency. Uses the
    bookmaker's real figure (from the now-removed OCR feature, if an older
    bet still has one) when present, otherwise units * unit_value."""
    stake = bet.get("stake_gbp")  # legacy column name from the OCR era
    if stake is not None:
        return stake
    return units_to_native(bet.get("units", 1.0), unit_value)


def bet_profit_native(bet: dict, unit_value: float) -> float:
    """Actual/settled profit for a bet, in the bet owner's currency."""
    status = (bet.get("status") or "").lower()
    stake = bet_stake_native(bet, unit_value)
    returns = bet.get("returns_gbp")  # legacy column name from the OCR era

    if status == "won":
        if returns is not None:
            return returns - stake
        return units_to_native(
            calculate_profit(bet.get("units", 1.0), bet.get("odds"), "won"), unit_value
        )
    if status == "loss":
        return -stake
    return 0.0  # void / pending


def bet_potential_win_native(bet: dict, unit_value: float) -> float:
    """Hypothetical profit *if* a pending bet wins, in the bet owner's currency."""
    stake = bet_stake_native(bet, unit_value)
    returns = bet.get("returns_gbp")
    if returns is not None:
        return returns - stake
    return units_to_native(
        calculate_profit(bet.get("units", 1.0), bet.get("odds"), "won"), unit_value
    )


def format_odds(odds: Optional[int]) -> str:
    if odds is None:
        return "N/A"
    return f"+{odds}" if odds > 0 else str(odds)