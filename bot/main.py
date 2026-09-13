"""Entry point: python -m bot.main"""
from __future__ import annotations

import logging
import sys

from telegram import BotCommand
from telegram.ext import Application, Defaults
from telegram.constants import ParseMode

from . import config, handlers, http, jobs
from .catalog import Catalog
from .db import Database

log = logging.getLogger("bot")

COMMANDS = [
    ("kurz", "Heutiges Programm in Kurzform"),
    ("heute", "Heutiges Programm mit Details"),
    ("jetzt", "Vorstellungen in den nächsten Stunden"),
    ("abend", "Vorstellungen heute Abend"),
    ("morgen", "Programm für morgen"),
    ("einstellungen", "Einstellungen anzeigen/ändern"),
    ("setup", "Einrichtung neu starten"),
    ("stop", "Tägliche Nachricht aus"),
    ("status", "Cache-Status"),
    ("help", "Hilfe"),
]


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    jobs.schedule_all(app)
    me = await app.bot.get_me()
    log.info("started as @%s", me.username)


async def post_shutdown(app: Application) -> None:
    await http.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    if not config.BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set")
        sys.exit(1)

    db = Database()
    catalog = Catalog(db)

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML, tzinfo=config.TZ))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["db"] = db
    app.bot_data["catalog"] = catalog
    app.bot_data["schedule_user"] = lambda user: jobs.schedule_user(app, user)
    handlers.register(app)

    log.info("data dir: %s", config.DATA_DIR)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
