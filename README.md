# nonstop Kino Telegram Bot

> **Try it:** the bot is live at [**@nonstop_kino_bot**](https://t.me/nonstop_kino_bot) – open it in Telegram and send `/start`.

Telegram bot that sends a daily (and on-demand) cinema program from
[nonstopkino.at](https://nonstopkino.at/), filtered by Bundesland and language version,
enriched with year, duration, genre, IMDb rating, poster, trailer and cinema address/distance.

## Features

- **Setup wizard** (`/start`): Bundesland, one or more language versions
  (DF / OV / OmdU / OmeU), daily notification time (default 08:00, or off),
  optional home location (Telegram location or a typed address) for distances.
- **Daily summary** at the chosen time (also `/kurz`): a condensed list of today's matching
  screenings (`time · title · cinema · version`), grouped by Vormittag / Mittag /
  Nachmittag / Abend. Tapping a title opens the movie details.
- **On demand, detailed**: `/heute` (today), `/jetzt` (next 4 hours), `/abend` (tonight
  from 18:00), `/morgen` (tomorrow). A single message with tab buttons per time-of-day
  section (a section that does not fit is split into "Abend 1/2", "Abend 2/2"). One line per
  screening: `time · title · cinema (distance) · EN, UT DE · genre · Tipp`, where
  `EN, UT DE` = spoken language, subtitle language (`UT –` = none, `DE, synchr.` = dubbed). Tapping a
  title opens the details. English aliases:
  `/today`, `/now`, `/evening`, `/tomorrow`.
- **Details**: poster, director, cast, year, duration, genre, IMDb rating, spoken language,
  subtitle language, country of origin, description, trailer link (YouTube search when
  nonstopkino has none), all upcoming screenings with version legend, cinema address with
  Google-Maps link, website and distance, plus a ▶️ Trailer button that sends the trailer as
  a playable Telegram video (fetched by Telegram from the source URL, up to 20 MB; larger
  trailers are sent as a link).
- Titles in lists are `t.me/<bot>?start=` deep links (one tap on mobile; Telegram Desktop may
  show a Start confirmation). Set `DETAILS_LINK=text` for a visible `/f_<id>` command link
  after each title instead, which works with one tap on every client.
- **Local cache** in a mounted `./data` directory (SQLite + image files), so restarts do
  not trigger new web requests:
  - program page: refreshed every 6 h (`PROGRAM_TTL_HOURS`)
  - movie details, cinema list: 30 days
  - IMDb metadata: 14 days; geocoding results: forever; downloaded posters + Telegram
    `file_id`s: forever (a poster is uploaded to Telegram once and then reused)
  - when no OMDb key is configured, the public IMDb ratings dataset (~8 MB) is
    downloaded once a week and ratings are looked up locally.

## Quick start

```bash
cp .env.example .env         # put your bot token (from @BotFather) in .env
mkdir -p data                # cache dir, mounted into the container
docker compose up -d --build
docker compose logs -f
```

Then open the bot in Telegram and send `/start`.

The container runs as uid/gid 1000 by default. If your host user has a different id,
add `UID=<id>` and `GID=<id>` to `.env` (or `chown` the `data` directory accordingly).

## Configuration

All settings are environment variables, see [`.env.example`](.env.example).

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | – | **required** |
| `OMDB_API_KEY` | – | optional but recommended; enables **genre, country of origin**, rating & poster from OMDb (free key, 1000 req/day). Without it only rating + IMDb link are available |
| `TZ` | `Europe/Vienna` | timezone for times and notifications |
| `PROGRAM_TTL_HOURS` | `6` | how often the program is re-fetched |
| `EVENING_FROM_HOUR` | `18` | `/abend` shows screenings starting at or after this hour (also the "Abend" section boundary) |
| `NOW_WINDOW_HOURS` | `4` | `/jetzt` shows screenings starting within this many hours |
| `DETAILS_LINK` | `deep` | `deep`: title is a tap link; `text`: visible `/f_<id>` command after the title |
| `DEFAULT_NOTIFY_TIME` | `08:00` | default daily notification time |
| `USE_IMDB_DATASET` | `1` | set `0` to skip the weekly IMDb ratings dataset download |
| `PREFETCH_DAYS` | `7` | details of movies playing within N days are pre-fetched in the background |

### Where the data comes from

| Data | Source | Requests |
|---|---|---|
| Screenings | `nonstopkino.at/programm/` (one page, all cinemas) | 1 per 6 h |
| Movie details (director, cast, year, duration, language, description, trailer, image) | `nonstopkino.at/movies/<slug>/` | 1 per movie per 30 days |
| Cinemas (address, website) | `nonstopkino.at/kinos/` | 1 per 30 days |
| Cinema / home coordinates | Nominatim (OpenStreetMap) | once per address |
| IMDb id, poster | IMDb suggestion API (no key) | 1–3 per movie per 14 days |
| Rating, genre, poster | OMDb (with key) **or** IMDb ratings dataset (no key, no genre) | 1 per movie / 1 per week |

## Commands

| Command | |
|---|---|
| `/start` | help, or the setup wizard on first use |
| `/setup` | run the setup wizard again |
| `/einstellungen` `/settings` | show & change settings (Bundesland, languages, time, home, notifications on/off) |
| `/kurz` `/daily` | today's condensed list (same as the daily message) |
| `/heute` `/today` | all of today's screenings, detailed |
| `/jetzt` `/now` | screenings starting within the next 4 hours, detailed |
| `/abend` `/evening` | tonight's screenings |
| `/morgen` `/tomorrow` | tomorrow's program |
| `/stop` | turn the daily message off |
| `/loeschen` `/delete` | delete all stored data about you |
| `/status` | cache statistics |
| `/refresh` | force a program refresh (at most every 30 min) |

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... DATA_DIR=./data .venv/bin/python -m bot.main
```

Layout: `bot/scraper.py` (HTML parsing), `bot/catalog.py` (cache refresh & queries),
`bot/imdb.py` (metadata), `bot/geo.py` (geocoding/distances), `bot/images.py`
(poster cache), `bot/formatting.py` (messages), `bot/handlers.py` (Telegram),
`bot/jobs.py` (scheduler), `bot/db.py` (SQLite).
