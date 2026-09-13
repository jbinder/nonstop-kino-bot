"""Configuration from environment variables."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "").strip()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
IMAGE_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "kino.sqlite3"

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Vienna"))

BASE_URL = "https://nonstopkino.at"
PROGRAM_URL = f"{BASE_URL}/programm/"
CINEMAS_URL = f"{BASE_URL}/kinos/"
USER_AGENT = os.environ.get(
    "USER_AGENT", "nonstopkino-telegram-bot/1.0 (+https://github.com/; personal use)"
)

# Cache lifetimes
PROGRAM_TTL_HOURS = _int("PROGRAM_TTL_HOURS", 6)
MOVIE_TTL_DAYS = _int("MOVIE_TTL_DAYS", 30)
CINEMA_TTL_DAYS = _int("CINEMA_TTL_DAYS", 30)
IMDB_TTL_DAYS = _int("IMDB_TTL_DAYS", 14)
IMDB_FAIL_TTL_DAYS = _int("IMDB_FAIL_TTL_DAYS", 2)
RATINGS_DATASET_TTL_DAYS = _int("RATINGS_DATASET_TTL_DAYS", 7)
# Set to 0 to disable downloading the IMDb ratings dataset (used when no OMDb key).
USE_IMDB_DATASET = _int("USE_IMDB_DATASET", 1) == 1

# Behaviour
EVENING_FROM_HOUR = _int("EVENING_FROM_HOUR", 18)
NOW_WINDOW_HOURS = _int("NOW_WINDOW_HOURS", 4)
# How movie titles in lists open the details: "deep" = title is a t.me deep link (one tap on
# mobile; Telegram Desktop may show a Start confirmation), "text" = visible /f_<id> command link.
DETAILS_LINK = os.environ.get("DETAILS_LINK", "deep").strip().lower()
if DETAILS_LINK not in ("deep", "text"):
    DETAILS_LINK = "deep"  # /jetzt shows screenings starting within this window
DEFAULT_NOTIFY_TIME = os.environ.get("DEFAULT_NOTIFY_TIME", "08:00")
PREFETCH_DAYS = _int("PREFETCH_DAYS", 7)  # prefetch movie details for screenings this far ahead
FETCH_DELAY_SECONDS = float(os.environ.get("FETCH_DELAY_SECONDS", "0.5"))

BUNDESLAENDER: dict[str, str] = {
    "wien": "Wien",
    "niederoesterreich": "Niederösterreich",
    "oberoesterreich": "Oberösterreich",
    "salzburg": "Salzburg",
    "steiermark": "Steiermark",
    "kaernten": "Kärnten",
    "tirol": "Tirol",
    "vorarlberg": "Vorarlberg",
    "burgenland": "Burgenland",
}

LANGUAGES: dict[str, str] = {
    "DF": "Deutsche Fassung (synchronisiert)",
    "OV": "Originalfassung ohne Untertitel",
    "OmdU": "Original mit deutschen Untertiteln",
    "OmeU": "Original mit englischen Untertiteln",
}

SUBTITLES: dict[str, str] = {
    "DF": "–",
    "OV": "–",
    "OmdU": "Deutsch",
    "OmeU": "English",
}

# ISO codes for the spoken-language names used on nonstopkino.at (used in the compact lists).
LANGUAGE_CODES: dict[str, str] = {
    "deutsch": "DE", "englisch": "EN", "französisch": "FR", "spanisch": "ES", "italienisch": "IT",
    "portugiesisch": "PT", "niederländisch": "NL", "flämisch": "NL", "dänisch": "DA", "schwedisch": "SV",
    "norwegisch": "NO", "finnisch": "FI", "isländisch": "IS", "polnisch": "PL", "tschechisch": "CS",
    "slowakisch": "SK", "ungarisch": "HU", "rumänisch": "RO", "bulgarisch": "BG", "griechisch": "EL",
    "türkisch": "TR", "russisch": "RU", "ukrainisch": "UK", "serbisch": "SR", "kroatisch": "HR",
    "bosnisch": "BS", "slowenisch": "SL", "albanisch": "SQ", "georgisch": "KA", "armenisch": "HY",
    "hebräisch": "HE", "arabisch": "AR", "persisch": "FA", "farsi": "FA", "dari": "FA", "kurdisch": "KU",
    "hindi": "HI", "bengalisch": "BN", "tamil": "TA", "telugu": "TE", "malayalam": "ML", "marathi": "MR",
    "punjabi": "PA", "urdu": "UR", "nepalesisch": "NE", "singhalesisch": "SI", "japanisch": "JA",
    "koreanisch": "KO", "chinesisch": "ZH", "mandarin": "ZH", "kantonesisch": "YUE", "tibetisch": "BO",
    "mongolisch": "MN", "kasachisch": "KK", "thai": "TH", "vietnamesisch": "VI", "indonesisch": "ID",
    "malaiisch": "MS", "tagalog": "TL", "filipino": "FIL", "amharisch": "AM", "swahili": "SW",
    "wolof": "WO", "hausa": "HA", "yoruba": "YO", "zulu": "ZU", "afrikaans": "AF", "lettisch": "LV",
    "litauisch": "LT", "estnisch": "ET", "katalanisch": "CA", "galicisch": "GL", "baskisch": "EU",
    "jiddisch": "YI", "latein": "LA", "gälisch": "GA", "walisisch": "CY", "luxemburgisch": "LB",
    "stumm": "stumm", "ohne dialog": "ohne Dialog",
}

HIGHLIGHT_TEXT: dict[str, str] = {
    "tip": "Tipp", "retrospective": "Retro", "festival": "Festival", "q&a": "Q&A", "openAir": "Open Air",
    "filmOfTheWeek": "Film der Woche", "3d": "3D", "special": "Special", "classic": "Classic",
}

HIGHLIGHT_LABELS: dict[str, str] = {
    "tip": "💡 Tipp",
    "retrospective": "🎞 Retro",
    "festival": "🎪 Festival",
    "q&a": "🎤 Q&A",
    "openAir": "🌙 Open Air",
    "filmOfTheWeek": "🏅 Film der Woche",
    "3d": "🕶 3D",
    "special": "✨ Special",
    "classic": "🏛 Classic",
}
