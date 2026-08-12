# UFC Bet Tracker Discord Bot

A personal Discord bot for logging UFC bets, resolving them with buttons, and
pulling profit/loss stats — with real American-odds payout math.

## File structure

```
ufc-bet-bot/
├── bot.py              # Entry point: intents, setup_hook, ESPN refresh loop, sync
├── config.py            # Loads .env values
├── database.py          # aiosqlite persistence layer
├── betting_math.py       # American-odds profit calculation
├── embeds.py             # Embed builders (bet card + results summary)
├── espn.py               # ESPN scoreboard fetcher (upcoming UFC events)
├── views.py              # Persistent Won/Loss/Void button View
├── cogs/
│   ├── __init__.py
│   ├── bets.py           # /bet command
│   └── results.py        # /results command
├── requirements.txt
├── .env.example
└── README.md
```

## 1. Create the Discord application & bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Go to **Bot** → **Reset Token** → copy it. This is your `DISCORD_TOKEN`.
3. Still on the **Bot** page: no privileged intents are required for this bot
   (it doesn't read message content or member lists — everything is slash
   commands and button clicks). You can leave Presence/Server Members/Message
   Content intents off.
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Send Messages`, `Embed Links`, `Read Message History`
   - Open the generated URL and invite the bot to your server.

## 2. Local setup

```bash
git clone <your-repo-or-copy-these-files>
cd ufc-bet-bot
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and paste in DISCORD_TOKEN (and optionally GUILD_ID)
```

`.env` fields:

| Key | Required | Notes |
|---|---|---|
| `DISCORD_TOKEN` | yes | From the Bot page above |
| `GUILD_ID` | no | Your server's ID. If set, slash commands sync **instantly** to that one server (right-click your server icon → Copy Server ID, with Developer Mode on). If left blank, commands sync **globally**, which can take up to ~1 hour to show up the first time. |
| `DB_PATH` | no | Defaults to `bets.db` in the project folder |
| `EVENT_REFRESH_HOURS` | no | Defaults to 3 — how often the ESPN event cache refreshes |

## 3. Run it

```bash
python bot.py
```

On startup the bot:
- connects to SQLite and creates the `bets` table if needed,
- re-registers a persistent button View for every bet already in the
  database (so old Won/Loss/Void buttons keep working after a restart),
- fetches the next 3 UFC events from ESPN and starts the refresh loop,
- syncs slash commands (guild-scoped if `GUILD_ID` is set, else global).

Slash commands don't need any manual registration step beyond this — the
`self.tree.sync()` call in `setup_hook` in `bot.py` registers `/bet` and
`/results` with Discord automatically every time the bot starts.

## 4. ESPN endpoint used

```
https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard
```

This is ESPN's public **scoreboard** endpoint for UFC (sport `mma`, league
`ufc`) — the same JSON feed their own site/app calls. `espn.py` queries it
with a `dates=YYYYMMDD-YYYYMMDD` parameter spanning the next 120 days
(the bare endpoint alone tends to only return the current week), then
filters to future events, sorts by date, and keeps the soonest 3.

**Important caveat:** this endpoint is public but unofficial and
undocumented by ESPN — there's no published contract, auth, or SLA, and the
shape or availability of the data could change or get rate-limited without
notice. It's fine for a personal-scale bot polling every few hours, but
don't build anything mission-critical on it. If it ever breaks, `/bet`
still works fine — you'd just type the event name manually instead of
picking from autocomplete.

## 5. How odds/profit math works

You asked for real odds tracking, so `/bet` has an optional `odds` field
(American odds, e.g. `-150` or `+120`). If you leave it blank, that bet is
treated as a flat 1:1 unit win/loss. Logic lives in `betting_math.py`:

- **Won**, odds given, favorite (negative, e.g. `-150`): `profit = units * (100 / |odds|)`
- **Won**, odds given, underdog (positive, e.g. `+120`): `profit = units * (odds / 100)`
- **Won**, no odds given: `profit = units` (flat)
- **Loss**: `profit = -units`
- **Void**: `profit = 0`

`/results` sums this per bet to get **Net Units**, plus win rate and a
W-L-V record, for either one event or all-time.

## 6. Commands

### `/bet`
All options are optional:
- `event` — autocompletes from the next 3 upcoming UFC events (ESPN)
- `bet_title` — free text, e.g. `"Jones ML"` or `"Fight goes over 1.5 rounds"`
- `units` — decimal, defaults to `1.0`
- `odds` — American odds, e.g. `-150` or `+120`

Posts an embed with **Won / Loss / Void** buttons. Clicking a button updates
the DB and re-colors the embed (green/red/gray). Buttons keep working after
a bot restart.

### `/results`
- `scope` — `All-Time` or `Per-Event` (defaults to All-Time)
- `event` — required when scope is Per-Event; autocompletes from events
  you've actually logged bets against

The response also includes **All-Time / Per-Event** toggle buttons so you
can flip the view without re-running the command.

## 7. Hosting it 24/7

Any small always-on host works — this bot is lightweight (SQLite, no web
server). A few solid options:

- **A cheap VPS** (e.g. a $4-6/mo droplet/Lightsail/Hetzner box): run it
  under `systemd` or inside `tmux`/`screen`, or wrap it with `pm2` /
  `supervisord` for auto-restart on crash.
- **Railway / Fly.io / Render** — deploy from a git repo, set env vars in
  their dashboard instead of a `.env` file, add a persistent volume for
  `bets.db` (important: don't lose your SQLite file on redeploy).
- **A Raspberry Pi / home server** — perfectly fine for a personal bot like
  this; just make sure it has a stable connection and auto-restarts on
  reboot (`systemd` service or `pm2`).

Example minimal `systemd` unit (`/etc/systemd/system/ufc-bet-bot.service`):

```ini
[Unit]
Description=UFC Bet Tracker Discord Bot
After=network.target

[Service]
WorkingDirectory=/opt/ufc-bet-bot
ExecStart=/opt/ufc-bet-bot/venv/bin/python bot.py
Restart=on-failure
EnvironmentFile=/opt/ufc-bet-bot/.env

[Install]
WantedBy=multi-user.target
```

Then: `sudo systemctl enable --now ufc-bet-bot`.

Whichever host you pick, back up `bets.db` periodically (it's the only
persistent state) — e.g. a nightly cron job copying it somewhere safe.
