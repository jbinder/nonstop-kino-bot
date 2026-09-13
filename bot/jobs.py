"""Scheduled jobs: daily user notifications and periodic cache refreshes."""
from __future__ import annotations

import logging
from datetime import time, timedelta

from telegram.error import Forbidden
from telegram.ext import Application, ContextTypes

from . import config, imdb
from .catalog import Catalog
from .db import Database
from .handlers import send_program

log = logging.getLogger(__name__)


def _job_name(chat_id: int) -> str:
    return f"daily:{chat_id}"


def schedule_user(app: Application, user: dict) -> None:
    """(Re)schedule the daily message for one user according to their settings."""
    jq = app.job_queue
    for job in jq.get_jobs_by_name(_job_name(user["chat_id"])):
        job.schedule_removal()
    if not user.get("notify_enabled"):
        return
    try:
        hh, mm = (int(x) for x in user["notify_time"].split(":"))
        t = time(hour=hh, minute=mm, tzinfo=config.TZ)
    except (ValueError, AttributeError):
        log.warning("invalid notify_time for %s: %r", user["chat_id"], user.get("notify_time"))
        return
    jq.run_daily(daily_job, time=t, name=_job_name(user["chat_id"]), chat_id=user["chat_id"], data=user["chat_id"])
    log.info("scheduled daily message for %s at %s", user["chat_id"], t.strftime("%H:%M"))


async def daily_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    user = db.get_user(ctx.job.data)
    if not user or not user.get("notify_enabled"):
        return
    try:
        await send_program(ctx, user, mode="daily")
    except Forbidden:
        log.info("user %s blocked the bot, disabling notifications", user["chat_id"])
        db.update_user(user["chat_id"], notify_enabled=0)
    except Exception:  # noqa: BLE001
        log.exception("daily job failed for %s", user["chat_id"])


async def refresh_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cat: Catalog = ctx.bot_data["catalog"]
    await cat.ensure_program(force=True)


async def ratings_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.bot_data["db"]
    if config.USE_IMDB_DATASET and not config.OMDB_API_KEY and not db.ratings_dataset_fresh():
        if await imdb.refresh_ratings_dataset(db):
            n = imdb.refresh_dataset_ratings(db)
            log.info("updated %d cached ratings from dataset", n)


async def startup_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cat: Catalog = ctx.bot_data["catalog"]
    await cat.ensure_program()
    await cat.ensure_cinemas()
    cat.start_prefetch()


def schedule_all(app: Application) -> None:
    db: Database = app.bot_data["db"]
    for user in db.all_users():
        schedule_user(app, user)
    jq = app.job_queue
    jq.run_once(startup_job, when=1, name="startup")
    jq.run_repeating(refresh_job, interval=timedelta(hours=config.PROGRAM_TTL_HOURS),
                     first=timedelta(hours=config.PROGRAM_TTL_HOURS), name="refresh")
    jq.run_repeating(ratings_job, interval=timedelta(hours=12), first=timedelta(minutes=10), name="ratings")
