"""Telegram command, conversation and callback handlers."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from html import escape

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import config, formatting, geo, images, imdb
from .catalog import Catalog
from .db import Database

log = logging.getLogger(__name__)

BUNDESLAND, LANGS, NOTIFY, HOME = range(4)
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
_DETAILS_RE = re.compile(r"^/f_([0-9a-f]{6,12})(?:@\w+)?\s*$")

HELP = (
    "🎬 <b>nonstop Kino Bot</b>\n"
    "Tägliches Kinoprogramm von <a href=\"https://nonstopkino.at\">nonstopkino.at</a>, gefiltert nach deinen Wünschen.\n\n"
    "/kurz – heutiges Programm in Kurzform, wie die tägliche Nachricht (auch /daily)\n"
    "/heute – alle heutigen Vorstellungen mit Details (auch /today)\n"
    "/jetzt – Vorstellungen in den nächsten {window} Stunden (auch /now)\n"
    "/abend – Vorstellungen heute Abend ab {evening}:00 (auch /evening)\n"
    "/morgen – Programm für morgen (auch /tomorrow)\n"
    "/einstellungen – Einstellungen anzeigen/ändern (auch /settings)\n"
    "/setup – Einrichtung von vorne starten\n"
    "/stop – tägliche Benachrichtigung aus\n"
    "/loeschen – alle meine Daten löschen\n"
    "/status – Cache-Status\n\n"
    "Tipp: Tippe auf einen Filmtitel für Beschreibung, Trailer, Poster und Kinoadresse.\n"
    "Sprachfassung in den Listen: <i>EN, UT DE</i> = Englisch mit deutschen Untertiteln, "
    "<i>ES, UT –</i> = Spanisch ohne Untertitel, <i>DE, synchr.</i> = deutsche Fassung."
).format(evening=config.EVENING_FROM_HOUR, window=config.NOW_WINDOW_HOURS)


def _db(ctx: ContextTypes.DEFAULT_TYPE) -> Database:
    return ctx.bot_data["db"]


def _catalog(ctx: ContextTypes.DEFAULT_TYPE) -> Catalog:
    return ctx.bot_data["catalog"]


def _now() -> datetime:
    return datetime.now(config.TZ)


# --------------------------------------------------------------------------- keyboards
def _bundesland_kb() -> InlineKeyboardMarkup:
    items = list(config.BUNDESLAENDER.items())
    rows = [
        [InlineKeyboardButton(name, callback_data=f"bl:{key}") for key, name in items[i : i + 2]]
        for i in range(0, len(items), 2)
    ]
    return InlineKeyboardMarkup(rows)


def _langs_kb(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for code, label in config.LANGUAGES.items():
        mark = "✅" if code in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {code} – {label}", callback_data=f"lang:{code}")])
    rows.append([InlineKeyboardButton("Fertig ✔️", callback_data="lang:done")])
    return InlineKeyboardMarkup(rows)


def _notify_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Standard ({config.DEFAULT_NOTIFY_TIME})", callback_data=f"notify:{config.DEFAULT_NOTIFY_TIME}")],
            [
                InlineKeyboardButton("07:00", callback_data="notify:07:00"),
                InlineKeyboardButton("09:00", callback_data="notify:09:00"),
                InlineKeyboardButton("12:00", callback_data="notify:12:00"),
            ],
            [InlineKeyboardButton("Keine tägliche Nachricht", callback_data="notify:off")],
        ]
    )


def _home_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Standort senden", request_location=True)], [KeyboardButton("Überspringen")]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


def _settings_kb(user: dict) -> InlineKeyboardMarkup:
    toggle = "🔕 Benachrichtigung aus" if user.get("notify_enabled") else "🔔 Benachrichtigung an"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📍 Bundesland", callback_data="set:bundesland"),
             InlineKeyboardButton("🗣 Sprachen", callback_data="set:langs")],
            [InlineKeyboardButton("⏰ Uhrzeit", callback_data="set:notify"),
             InlineKeyboardButton("🏠 Zuhause", callback_data="set:home")],
            [InlineKeyboardButton(toggle, callback_data="set:toggle")],
        ]
    )


# --------------------------------------------------------------------------- setup wizard
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = _db(ctx).ensure_user(update.effective_chat.id)
    if ctx.args and ctx.args[0].startswith("f_"):
        # deep link from a condensed list: t.me/<bot>?start=f_<id>
        slug = _slug_for_short_id(_db(ctx), ctx.args[0][2:])
        if slug:
            await send_details(ctx, update.effective_chat.id, slug)
        else:
            await update.message.reply_html("😕 Film nicht (mehr) im Programm.")
        return ConversationHandler.END
    if user["setup_done"] and update.message and update.message.text and update.message.text.startswith("/start"):
        await update.message.reply_html(HELP + "\n\n" + formatting.settings_summary(user), disable_web_page_preview=True)
        return ConversationHandler.END
    ctx.user_data["wizard"] = True
    await update.message.reply_html(
        "👋 Willkommen! Ich schicke dir täglich das nonstop-Kinoprogramm, passend zu deinen Einstellungen.\n\n"
        "1️⃣ In welchem <b>Bundesland</b> gehst du ins Kino?",
        reply_markup=_bundesland_kb(),
    )
    return BUNDESLAND


async def set_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry from the /settings buttons: edit a single setting."""
    q = update.callback_query
    await q.answer()
    ctx.user_data["wizard"] = False
    user = _db(ctx).ensure_user(q.message.chat_id)
    what = q.data.split(":", 1)[1]
    if what == "bundesland":
        await q.message.reply_html("📍 Bundesland wählen:", reply_markup=_bundesland_kb())
        return BUNDESLAND
    if what == "langs":
        ctx.user_data["langs"] = list(user["languages"])
        await q.message.reply_html("🗣 Sprachfassungen wählen (mehrere möglich):", reply_markup=_langs_kb(ctx.user_data["langs"]))
        return LANGS
    if what == "notify":
        await q.message.reply_html(
            "⏰ Wann soll die tägliche Nachricht kommen? Uhrzeit tippen (z.B. <code>7:30</code>) oder wählen:",
            reply_markup=_notify_kb(),
        )
        return NOTIFY
    if what == "home":
        await q.message.reply_html(
            "🏠 Schick mir deinen Standort oder tippe eine Adresse (z.B. <i>Mariahilfer Straße 1, Wien</i>).\n"
            "Damit zeige ich die Entfernung zu den Kinos an.",
            reply_markup=_home_kb(),
        )
        return HOME
    if what == "toggle":
        user = _db(ctx).update_user(q.message.chat_id, notify_enabled=0 if user["notify_enabled"] else 1)
        ctx.bot_data["schedule_user"](user)
        await q.edit_message_text(formatting.settings_summary(user), parse_mode=ParseMode.HTML, reply_markup=_settings_kb(user))
    return ConversationHandler.END


async def wizard_bundesland(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    if key not in config.BUNDESLAENDER:
        return BUNDESLAND
    db = _db(ctx)
    user = db.update_user(q.message.chat_id, bundesland=key)
    await q.edit_message_text(f"📍 Bundesland: <b>{escape(config.BUNDESLAENDER[key])}</b>", parse_mode=ParseMode.HTML)
    if not ctx.user_data.get("wizard"):
        return await _finish_single(update, ctx, user)
    ctx.user_data["langs"] = list(user["languages"]) or ["OV", "OmdU", "OmeU"]
    await q.message.reply_html(
        "2️⃣ Welche <b>Sprachfassungen</b> interessieren dich? (mehrere möglich, dann <i>Fertig</i>)",
        reply_markup=_langs_kb(ctx.user_data["langs"]),
    )
    return LANGS


async def wizard_langs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    code = q.data.split(":", 1)[1]
    sel: list[str] = ctx.user_data.setdefault("langs", [])
    if code == "done":
        if not sel:
            await q.answer("Bitte mindestens eine Sprachfassung wählen.", show_alert=True)
            return LANGS
        await q.answer()
        user = _db(ctx).update_user(q.message.chat_id, languages=[c for c in config.LANGUAGES if c in sel])
        await q.edit_message_text(f"🗣 Sprachfassungen: <b>{escape(', '.join(user['languages']))}</b>", parse_mode=ParseMode.HTML)
        if not ctx.user_data.get("wizard"):
            return await _finish_single(update, ctx, user)
        await q.message.reply_html(
            "3️⃣ Wann soll die tägliche Nachricht kommen? Uhrzeit tippen (z.B. <code>7:30</code>) oder wählen:",
            reply_markup=_notify_kb(),
        )
        return NOTIFY
    if code in config.LANGUAGES:
        if code in sel:
            sel.remove(code)
        else:
            sel.append(code)
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=_langs_kb(sel))
        except BadRequest:
            pass
    return LANGS


def _parse_time(text: str) -> str | None:
    m = _TIME_RE.match(text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    return f"{h:02d}:{mi:02d}"


async def wizard_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    db = _db(ctx)
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        val = q.data.split(":", 1)[1]
        chat_id = q.message.chat_id
        if val == "off":
            user = db.update_user(chat_id, notify_enabled=0)
            await q.edit_message_text("🔕 Keine tägliche Nachricht. Du kannst jederzeit /heute oder /jetzt nutzen.")
        else:
            user = db.update_user(chat_id, notify_time=val, notify_enabled=1)
            await q.edit_message_text(f"⏰ Tägliche Nachricht um <b>{val}</b> Uhr", parse_mode=ParseMode.HTML)
        reply = q.message.reply_html
    else:
        chat_id = update.effective_chat.id
        t = _parse_time(update.message.text or "")
        if t is None:
            await update.message.reply_html("Bitte eine Uhrzeit wie <code>8:00</code> eingeben oder einen Button wählen.", reply_markup=_notify_kb())
            return NOTIFY
        user = db.update_user(chat_id, notify_time=t, notify_enabled=1)
        await update.message.reply_html(f"⏰ Tägliche Nachricht um <b>{t}</b> Uhr")
        reply = update.message.reply_html
    ctx.bot_data["schedule_user"](user)
    if not ctx.user_data.get("wizard"):
        return await _finish_single(update, ctx, user)
    await reply(
        "4️⃣ <b>Optional:</b> Schick mir deinen Standort oder tippe eine Adresse (z.B. <i>Mariahilfer Straße 1, Wien</i>), "
        "dann zeige ich die Entfernung zu den Kinos an.",
        reply_markup=_home_kb(),
    )
    return HOME


async def wizard_home(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    db = _db(ctx)
    chat_id = update.effective_chat.id
    msg = update.message
    if msg.location:
        lat, lon = msg.location.latitude, msg.location.longitude
        user = db.update_user(chat_id, home_lat=lat, home_lon=lon, home_label=f"📍 {lat:.4f}, {lon:.4f}")
        await msg.reply_html("🏠 Standort gespeichert.", reply_markup=ReplyKeyboardRemove())
    else:
        text = (msg.text or "").strip()
        if text.lower() in ("überspringen", "skip", "-", "/skip", "keine"):
            user = db.update_user(chat_id, home_lat=None, home_lon=None, home_label=None)
            await msg.reply_html("🏠 Kein Zuhause gesetzt – Entfernungen werden nicht angezeigt.", reply_markup=ReplyKeyboardRemove())
        else:
            await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            res = await geo.geocode(db, text)
            if res is None:
                await msg.reply_html("😕 Adresse nicht gefunden. Bitte anders formulieren, Standort senden oder <i>Überspringen</i>.", reply_markup=_home_kb())
                return HOME
            lat, lon, label = res
            user = db.update_user(chat_id, home_lat=lat, home_lon=lon, home_label=text)
            await msg.reply_html(f"🏠 Zuhause: <b>{escape(text)}</b>\n<i>{escape(label)}</i>", reply_markup=ReplyKeyboardRemove())
    if not ctx.user_data.get("wizard"):
        return await _finish_single(update, ctx, user)
    return await _finish_wizard(update, ctx, user)


async def _finish_single(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user: dict) -> int:
    await ctx.bot.send_message(
        user["chat_id"], formatting.settings_summary(user), parse_mode=ParseMode.HTML, reply_markup=_settings_kb(user)
    )
    return ConversationHandler.END


async def _finish_wizard(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user: dict) -> int:
    user = _db(ctx).update_user(user["chat_id"], setup_done=1)
    ctx.bot_data["schedule_user"](user)
    ctx.user_data["wizard"] = False
    await ctx.bot.send_message(
        user["chat_id"],
        "✅ Fertig! " + formatting.settings_summary(user) + "\n\nSo sieht deine tägliche Nachricht aus:",
        parse_mode=ParseMode.HTML,
    )
    await send_program(ctx, user, mode="daily")
    await ctx.bot.send_message(user["chat_id"], HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    return ConversationHandler.END


async def wizard_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["wizard"] = False
    await update.message.reply_text("Abgebrochen.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --------------------------------------------------------------------------- program commands
def _list_query(mode: str, day, ref: datetime) -> tuple[str, str, datetime | None, datetime | None, int | None]:
    """heading, empty_text, not_before, not_after, from_hour for a list mode."""
    if mode == "now":
        not_after = ref + timedelta(hours=config.NOW_WINDOW_HOURS)
        return (
            f"Ab {ref.strftime('%H:%M')} bis {not_after.strftime('%H:%M')} – {formatting.fmt_day(day)}",
            f"In den nächsten {config.NOW_WINDOW_HOURS} Stunden keine passenden Vorstellungen. Versuch /abend oder /heute.",
            ref, not_after, None,
        )
    if mode == "evening":
        return (
            f"Heute Abend ab {config.EVENING_FROM_HOUR}:00 – {formatting.fmt_day(day)}",
            "Heute Abend keine passenden Vorstellungen mehr.",
            ref, None, config.EVENING_FROM_HOUR,
        )
    empty = "Morgen keine passenden Vorstellungen." if mode == "tomorrow" else "Heute keine passenden Vorstellungen."
    return f"Programm {formatting.fmt_day(day)}", empty, None, None, None


def _link_mode(ctx) -> str:
    return config.DETAILS_LINK


async def _detailed_pages(ctx, user: dict, mode: str, day, ref: datetime) -> list[tuple[str, str]]:
    cat = _catalog(ctx)
    heading, empty, not_before, not_after, from_hour = _list_query(mode, day, ref)
    screenings = cat.screenings(user, day, not_before=not_before, not_after=not_after, from_hour=from_hour)
    movies, meta = await cat.enrich(sorted({s["movie_slug"] for s in screenings}))
    return formatting.render_detailed(heading, user, screenings, movies, meta, lambda v: cat.cinema_info(v, user), empty,
                                      link_mode=_link_mode(ctx), bot_username=ctx.bot.username)


def _page_view(pages: list[tuple[str, str]], idx: int, mode: str, day, ref: datetime):
    """(text, keyboard) for page idx; the keyboard has one tab per page."""
    idx = max(0, min(idx, len(pages) - 1))
    text = pages[idx][1]
    if len(pages) < 2:
        return text, None
    key = f"p:{mode}:{day.isoformat()}:{ref.strftime('%H%M')}"
    buttons = [
        InlineKeyboardButton(("• " if i == idx else "") + label, callback_data=f"{key}:{i}")
        for i, (label, _) in enumerate(pages)
    ]
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    return text, InlineKeyboardMarkup(rows)


async def send_program(ctx: ContextTypes.DEFAULT_TYPE, user: dict, mode: str) -> None:
    """mode: daily (condensed, several messages) | today | now | evening | tomorrow (detailed, paged)"""
    cat = _catalog(ctx)
    chat_id = user["chat_id"]
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    ok = await cat.ensure_program()
    if not ok:
        await ctx.bot.send_message(chat_id, "😕 Das Programm konnte gerade nicht geladen werden. Bitte später nochmal versuchen.")
        return
    now = _now()
    day = now.date() + timedelta(days=1) if mode == "tomorrow" else now.date()
    tip = "\n\n<i>Tipp: Mit /einstellungen kannst du ein Bundesland wählen.</i>" if not user.get("bundesland") else ""

    async def build() -> list[tuple[str, InlineKeyboardMarkup | None]]:
        if mode == "daily":
            heading, empty, *_ = _list_query("today", day, now)
            screenings = cat.screenings(user, day)
            movies, _meta = await cat.enrich(sorted({s["movie_slug"] for s in screenings}))
            msgs = formatting.render_condensed(heading, user, screenings, movies, lambda v: cat.cinema_info(v, user), empty,
                                               link_mode=_link_mode(ctx), bot_username=ctx.bot.username)
            msgs[-1] += tip
            return [(m, None) for m in msgs]
        pages = await _detailed_pages(ctx, user, mode, day, now)
        text, kb = _page_view(pages, 0, mode, day, now)
        return [(text + tip, kb)]

    for text, kb in await build():
        await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


async def cb_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Tab buttons of a detailed list: re-render the requested time-of-day page in place."""
    q = update.callback_query
    try:
        _, mode, day_s, hhmm, idx_s = q.data.split(":")
        day = datetime.fromisoformat(day_s).date()
        ref = datetime.combine(day, datetime.strptime(hhmm, "%H%M").time(), tzinfo=config.TZ)
        idx = int(idx_s)
    except ValueError:
        await q.answer()
        return
    user = _db(ctx).ensure_user(q.message.chat_id)
    await q.answer()
    pages = await _detailed_pages(ctx, user, mode, day, ref)
    text, kb = _page_view(pages, idx, mode, day, ref)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def _program_cmd(mode: str):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = _db(ctx).ensure_user(update.effective_chat.id)
        await send_program(ctx, user, mode)

    return handler


# --------------------------------------------------------------------------- details
def _slug_for_short_id(db: Database, sid: str) -> str | None:
    if len(sid) < 6:
        return None
    for r in db._rows("SELECT movie_slug AS s FROM screenings UNION SELECT slug AS s FROM movies"):
        if formatting.short_id(r["s"]).startswith(sid):
            return r["s"]
    return None


async def cmd_details_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/f_<id> links embedded in the program lists."""
    m = _DETAILS_RE.match(update.message.text or "")
    slug = _slug_for_short_id(_db(ctx), m.group(1)) if m else None
    if slug is None:
        await update.message.reply_html("😕 Film nicht (mehr) im Programm.")
        return
    await send_details(ctx, update.effective_chat.id, slug)


async def cb_details(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline buttons from older list messages."""
    q = update.callback_query
    slug = _slug_for_short_id(_db(ctx), q.data.split(":", 1)[1])
    if slug is None:
        await q.answer("Film nicht mehr im Programm.", show_alert=True)
        return
    await q.answer()
    await send_details(ctx, q.message.chat_id, slug)


async def send_details(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, slug: str) -> None:
    db = _db(ctx)
    cat = _catalog(ctx)
    user = db.ensure_user(chat_id)
    await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
    movie = await cat.ensure_movie(slug)
    if not movie:
        await ctx.bot.send_message(chat_id, "😕 Details konnten nicht geladen werden.")
        return
    meta = await cat.ensure_metadata(slug)
    screenings = cat.db.screenings_for_movie(slug, _now().date().isoformat())
    for s in screenings:
        s["start_local"] = datetime.fromisoformat(s["start_utc"]).astimezone(config.TZ)
    screenings = [s for s in screenings if s["start_local"] >= _now() - timedelta(minutes=30)]
    # apply the user's Bundesland/language filter; fall back to everything if nothing matches
    filtered = [
        s for s in screenings
        if (not user.get("bundesland") or s["bundesland"] == user["bundesland"])
        and (not user.get("languages") or s["language"] in user["languages"])
    ]
    note = ""
    if filtered:
        screenings = filtered
    elif screenings:
        note = "\n<i>Keine Vorstellungen passend zu deinen Einstellungen – hier alle Regionen/Fassungen:</i>"
    caption, body = formatting.render_details(movie, meta, screenings, user, lambda v: cat.cinema_info(v, user))
    if note:
        body = body.replace("📅 <b>Vorstellungen</b>", "📅 <b>Vorstellungen</b>" + note, 1)

    poster_url = (
        imdb.sized_poster((meta or {}).get("poster_url"))
        or movie.get("backdrop_url")
        or (screenings[0].get("image_url") if screenings else None)
    )
    full = caption + ("\n\n" + body if body else "")
    kb = None
    if movie.get("trailer_url"):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Trailer abspielen", callback_data=f"t:{formatting.short_id(slug)[:8]}")]])
    sent_photo = False
    if poster_url:
        fits = len(full) <= formatting.MAX_CAPTION
        sent_photo = await _send_photo(ctx, db, chat_id, poster_url, full if fits else caption,
                                       reply_markup=kb if fits else None)
    parts = _split(full) if not sent_photo else (_split(body) if len(full) > formatting.MAX_CAPTION and body else [])
    for i, part in enumerate(parts):
        await ctx.bot.send_message(chat_id, part, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                   reply_markup=kb if i == len(parts) - 1 else None)


async def cb_trailer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the trailer by URL: Telegram fetches files up to 20 MB itself; larger ones get a link."""
    q = update.callback_query
    db = _db(ctx)
    slug = _slug_for_short_id(db, q.data.split(":", 1)[1])
    movie = db.get_movie(slug) if slug else None
    if not movie or not movie.get("trailer_url"):
        await q.answer("Kein Trailer verfügbar.", show_alert=True)
        return
    await q.answer()
    chat_id = q.message.chat_id
    url = movie["trailer_url"]
    caption = f"▶️ <b>{escape(movie.get('title') or '')}</b> – Trailer"
    link_msg = f'▶️ <a href="{escape(url)}">Trailer: {escape(movie.get("title") or "")}</a>'
    row = db.get_image(url)
    if row and row["path"] == "unsendable":
        await ctx.bot.send_message(chat_id, link_msg)
        return
    await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
    try:
        msg = await ctx.bot.send_video(chat_id, images.get_file_id(db, url) or url, caption=caption,
                                       parse_mode=ParseMode.HTML, supports_streaming=True, read_timeout=60)
        if msg.video:
            db.save_image(url, None, msg.video.file_id)
    except BadRequest as e:
        log.info("send_video by url failed for %s (%s), sending link", url, e)
        db.save_image(url, "unsendable", None)  # remember: too large for Telegram to fetch
        await ctx.bot.send_message(chat_id, link_msg)


async def _send_photo(ctx, db: Database, chat_id: int, url: str, caption: str, reply_markup=None) -> bool:
    file_id = images.get_file_id(db, url)
    if file_id:
        try:
            await ctx.bot.send_photo(chat_id, file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            return True
        except BadRequest as e:
            log.info("cached file_id rejected (%s), re-uploading", e)
            db.clear_image_file_id(url)
    path = await images.get_image_path(db, url)
    if path is None:
        return False
    try:
        with path.open("rb") as fh:
            msg = await ctx.bot.send_photo(chat_id, InputFile(fh, filename=path.name), caption=caption,
                                           parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except BadRequest as e:
        log.warning("send_photo failed for %s: %s", url, e)
        return False
    if msg.photo:
        images.remember_file_id(db, url, msg.photo[-1].file_id)
    return True


def _split(text: str, limit: int = formatting.MAX_MESSAGE - 20) -> list[str]:
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


# --------------------------------------------------------------------------- misc commands
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP, disable_web_page_preview=True)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = _db(ctx).ensure_user(update.effective_chat.id)
    await update.message.reply_html(formatting.settings_summary(user), reply_markup=_settings_kb(user))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = _db(ctx).update_user(update.effective_chat.id, notify_enabled=0)
    ctx.bot_data["schedule_user"](user)
    await update.message.reply_html("🔕 Tägliche Benachrichtigung ausgeschaltet. Mit /einstellungen wieder einschalten.")


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = _db(ctx).get_user(chat_id)
    if user:
        user["notify_enabled"] = 0
        ctx.bot_data["schedule_user"](user)
        _db(ctx).delete_user(chat_id)
    await update.message.reply_html("🗑 Deine Daten wurden gelöscht. Mit /start kannst du neu beginnen.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(ctx)
    age = db.program_age()
    age_s = f"{age.total_seconds() / 60:.0f} min" if age else "nie"
    n_movies = db._row("SELECT COUNT(*) AS n FROM movies")["n"]
    n_imdb = db._row("SELECT COUNT(*) AS n FROM imdb WHERE ok=1")["n"]
    n_cin = db._row("SELECT COUNT(*) AS n FROM cinemas WHERE lat IS NOT NULL")["n"]
    n_img = db._row("SELECT COUNT(*) AS n FROM images")["n"]
    src = "OMDb" if config.OMDB_API_KEY else ("IMDb-Datensatz" if config.USE_IMDB_DATASET else "nur IMDb-Suche")
    await update.message.reply_html(
        "📦 <b>Cache</b>\n"
        f"Programm: {db.screening_count()} Vorstellungen, aktualisiert vor {age_s}\n"
        f"Filme: {n_movies} · IMDb-Daten: {n_imdb} ({src}, {db.ratings_count()} Ratings lokal)\n"
        f"Kinos mit Koordinaten: {n_cin} · Bilder: {n_img}\n"
        f"Nutzer: {len(db.all_users())}"
    )


async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    age = _db(ctx).program_age()
    if age is not None and age < timedelta(minutes=30):
        await update.message.reply_text(f"Programm ist aktuell (vor {age.total_seconds() / 60:.0f} min geladen).")
        return
    await update.message.reply_text("Aktualisiere Programm …")
    ok = await _catalog(ctx).ensure_program(force=True)
    await update.message.reply_text("✅ Programm aktualisiert." if ok else "😕 Aktualisierung fehlgeschlagen.")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("update %s caused error", update, exc_info=ctx.error)


# --------------------------------------------------------------------------- registration
def register(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("setup", cmd_start),
            CallbackQueryHandler(set_entry, pattern=r"^set:"),
        ],
        states={
            BUNDESLAND: [CallbackQueryHandler(wizard_bundesland, pattern=r"^bl:")],
            LANGS: [CallbackQueryHandler(wizard_langs, pattern=r"^lang:")],
            NOTIFY: [
                CallbackQueryHandler(wizard_notify, pattern=r"^notify:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_notify),
            ],
            HOME: [MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, wizard_home)],
        },
        fallbacks=[CommandHandler("cancel", wizard_cancel), CommandHandler("abbrechen", wizard_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conv)
    for names, mode in (
        (("kurz", "daily"), "daily"),
        (("heute", "today"), "today"),
        (("jetzt", "now"), "now"),
        (("abend", "evening"), "evening"),
        (("morgen", "tomorrow"), "tomorrow"),
    ):
        app.add_handler(CommandHandler(list(names), _program_cmd(mode)))
    app.add_handler(CommandHandler(["einstellungen", "settings"], cmd_settings))
    app.add_handler(CommandHandler(["help", "hilfe"], cmd_help))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler(["loeschen", "delete"], cmd_delete))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler(["refresh", "aktualisieren"], cmd_refresh))
    app.add_handler(MessageHandler(filters.Regex(_DETAILS_RE), cmd_details_link))
    app.add_handler(CallbackQueryHandler(cb_details, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(cb_trailer, pattern=r"^t:"))
    app.add_handler(CallbackQueryHandler(cb_page, pattern=r"^p:"))
    app.add_error_handler(on_error)
