# Number Upload Bot

A Telegram bot where the admin uploads phone-number files per panel.

## Flow
1. Admin opens 🛠 Admin Panel → 📤 Upload Numbers
2. Selects a panel (button)
3. Sends a `.txt` / `.csv` file
4. Numbers are parsed and stored, tagged by panel

## Run
- `python bot.py`

## Env vars
- `TELEGRAM_BOT_TOKEN` (required)
- `ADMIN_IDS` — comma-separated Telegram user IDs (default `7430635878`)
- `DATA_DIR` — directory for the SQLite db (default: app folder)
- `PORT` — health server port (Railway provides this)

## Panels
Panels live in `providers.json`. Each panel's API (for OTP fetching) is added
later, one panel at a time.

## Notes
- Storage is SQLite. On Railway without a volume this is **ephemeral** — fine for
  testing the flow; a persistent database is added before real use.
