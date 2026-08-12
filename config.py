"""
Central config loader. Reads values from a local .env file (via python-dotenv)
so secrets never live in source code.
"""
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
    )

# Optional: if set, slash commands are synced to this single guild for
# instant updates during development. If blank/unset, commands sync globally.
_guild_id_raw = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(_guild_id_raw) if _guild_id_raw else None

DB_PATH = os.getenv("DB_PATH", "bets.db")

# Only these Discord user IDs are allowed to use the bot's commands/buttons.
# Each person's bets are fully isolated by their own user ID -- nobody sees
# or can touch anyone else's bets, even though they share one bot/database.
ALLOWED_USER_IDS = {
    1085484343050371092,
    458418768679272458,
}

# All auto-grading debug messages (per-leg settle notices, etc.) go to this
# one fixed channel, regardless of which channel the underlying bet was
# logged in.
DEBUG_CHANNEL_ID = 1536527804202487828

# How often (in hours) to refresh the cached list of upcoming UFC events.
# Default: every 3 days (72 hours). Note: since live/completed status only
# updates on this cadence too, a long interval means the "🔴 LIVE" tag and
# the removal of finished events can lag by up to this long.
EVENT_REFRESH_HOURS = float(os.getenv("EVENT_REFRESH_HOURS", "72"))

# ESPN's public (unofficial, undocumented) scoreboard endpoint. Only UFC
# events are tracked -- NBA bets use free-text event names instead.
ESPN_SCOREBOARD_URLS = {
    "ufc": "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard",
}

# Static ->USD conversion rates, shown alongside each currency's native
# amount in embeds. NOT live rates -- update in .env periodically if you
# want them to stay accurate (mid-market rates as of Aug 2026: GBP/USD
# ~1.33, EUR/USD ~1.15).
GBP_TO_USD_RATE = float(os.getenv("GBP_TO_USD_RATE", "1.33"))
EUR_TO_USD_RATE = float(os.getenv("EUR_TO_USD_RATE", "1.15"))

CURRENCY_TO_USD_RATE = {
    "GBP": GBP_TO_USD_RATE,
    "EUR": EUR_TO_USD_RATE,
    "USD": 1.0,
}

CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}

# Which currency each allowed user's bets/units are tracked in.
USER_CURRENCY = {
    1085484343050371092: "GBP",
    458418768679272458: "USD",
}

# Default unit size (in the user's own currency) for anyone who hasn't set
# a custom one via /unit-size yet.
DEFAULT_UNIT_VALUE = 100.00