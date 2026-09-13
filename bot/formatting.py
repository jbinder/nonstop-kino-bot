"""Rendering of Telegram messages (HTML parse mode)."""
from __future__ import annotations

import hashlib
import re
from datetime import date
from html import escape
from urllib.parse import quote_plus

from . import config, geo

MAX_MESSAGE = 4096
MAX_CAPTION = 1024
WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAYS_DE_LONG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def short_id(slug: str) -> str:
    return hashlib.sha1(slug.encode()).hexdigest()[:12]


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS_DE_LONG[d.weekday()]}, {d.strftime('%d.%m.')}"


def fmt_short_day(d: date) -> str:
    return f"{WEEKDAYS_DE[d.weekday()]} {d.strftime('%d.%m.')}"


def language_line(code: str, spoken: str | None) -> str:
    if code == "DF":
        return "🗣 Deutsch (synchronisiert)"
    parts = [f"🗣 {spoken}" if spoken else "🗣 Originalfassung"]
    sub = config.SUBTITLES.get(code, "–")
    parts.append(f"💬 {sub} UT" if sub != "–" else "💬 keine UT")
    return " · ".join(parts)


def rating_str(meta: dict | None) -> str | None:
    if not meta or meta.get("rating") is None:
        return None
    s = f"IMDb ⭐ {meta['rating']:.1f}"
    if meta.get("votes"):
        v = meta["votes"]
        s += f" ({v / 1e6:.1f}M)" if v >= 1_000_000 else f" ({v / 1000:.0f}k)" if v >= 1000 else f" ({v})"
    return s


def facts_line(movie: dict | None, meta: dict | None) -> str:
    parts: list[str] = []
    if meta and meta.get("genre"):
        parts.append(escape(meta["genre"]))
    if movie and movie.get("year"):
        parts.append(str(movie["year"]))
    elif meta and meta.get("imdb_year"):
        parts.append(str(meta["imdb_year"]))
    dur = (movie or {}).get("duration_min") or (meta or {}).get("runtime_min")
    if dur:
        parts.append(f"{dur} min")
    r = rating_str(meta)
    if r:
        parts.append(r)
    return " · ".join(parts)


def highlight_tags(highlights: str) -> str:
    tags = [config.HIGHLIGHT_LABELS.get(h, h) for h in highlights.split(",") if h]
    return " ".join(tags)


def sections() -> list[tuple[str, int, int]]:
    """(label, from_hour, to_hour) time-of-day buckets used for pagination."""
    ev = config.EVENING_FROM_HOUR
    return [
        ("🌅 Vormittag", 0, 12),
        ("☀️ Mittag", 12, 14),
        ("🌤 Nachmittag", 14, ev),
        ("🌙 Abend", ev, 24),
    ]


def split_sections(screenings: list[dict]) -> list[tuple[str, list[dict]]]:
    out = []
    for label, lo, hi in sections():
        rows = [s for s in screenings if lo <= s["start_local"].hour < hi]
        if rows:
            out.append((label, rows))
    return out


def _list_header(heading: str, user: dict, screenings: list[dict]) -> str:
    langs = ", ".join(user.get("languages") or []) or "alle Sprachfassungen"
    bl = config.BUNDESLAENDER.get(user.get("bundesland") or "", "alle Bundesländer")
    n_movies = len({s["movie_slug"] for s in screenings})
    return (
        f"🎬 <b>{escape(heading)}</b>\n"
        f"<i>{escape(bl)} · {escape(langs)} · {len(screenings)} Vorstellungen · {n_movies} Filme</i>\n"
    )


def _pack(header: str, blocks: list[str], cont_header: str, limit: int = MAX_MESSAGE - 50,
          by_line: bool = False) -> list[str]:
    """Join blocks into as few messages as possible without breaking a block; a block that
    does not fit into a whole message on its own (or every block, with by_line) is filled in
    line by line instead. A section header line is never left orphaned at the end of a message."""
    msgs: list[str] = []
    cur = header

    def flush():
        nonlocal cur
        carry = ""
        lines = cur.rstrip().split("\n")
        if len(lines) > 1 and lines[-1].startswith("<b>") and lines[-1].endswith("</b>"):
            carry = "\n" + lines.pop() + "\n"
            cur = "\n".join(lines)
        if cur.strip() and cur.strip() != cont_header.strip():
            msgs.append(cur.rstrip())
        cur = cont_header + carry

    for b in blocks:
        if not by_line and len(cont_header) + len(b) + 1 <= limit:
            if len(cur) + len(b) + 1 > limit:
                flush()
            cur += b + "\n"
            continue
        for ln in b.split("\n"):
            if len(cur) + len(ln) + 1 > limit:
                flush()
            cur += ln + "\n"
    flush()
    return msgs


def details_link(slug: str) -> str:
    return f"/f_{short_id(slug)[:8]}"


# ----------------------------------------------------------------------------- condensed (daily)
def render_condensed(heading: str, user: dict, screenings: list[dict], movies: dict[str, dict], cinema_info,
                     empty_text: str, link_mode: str = "deep", bot_username: str | None = None) -> list[str]:
    """One line per screening, grouped by time of day: time · title · cinema · version.
    The title is tappable and makes the bot send the movie details."""
    header = _list_header(heading, user, screenings)
    if not screenings:
        return [header + f"\n{escape(empty_text)}"]
    blocks: list[str] = []
    for label, rows in split_sections(screenings):
        lines = [f"\n<b>{label}</b>"]
        for s in sorted(rows, key=lambda x: (x["start_local"], x["title"].lower())):
            lines.append(render_line(s, movies.get(s["movie_slug"]), None, cinema_info, link_mode, bot_username, detailed=False))
        blocks.append("\n".join(lines))
    blocks.append("\n<i>Filmtitel antippen für Details & Trailer · ausführlich: /heute · /jetzt · /abend</i>")
    return _pack(header, blocks, f"<i>{escape(heading)} (Fortsetzung)</i>\n", by_line=True)


# ----------------------------------------------------------------------------- detailed (one line per screening)
def spoken_codes(spoken: str | None, limit: int = 3) -> str:
    """'Englisch, Hebräisch' -> 'EN/HE' (unknown names are abbreviated to 3 letters)."""
    codes: list[str] = []
    for name in re.split(r"[,/;]|\bund\b", spoken or ""):
        name = name.strip()
        if not name:
            continue
        c = config.LANGUAGE_CODES.get(name.lower()) or name[:3].upper()
        if c not in codes:
            codes.append(c)
    return "/".join(codes[:limit])


_SUBTITLE_TEXT = {"OmdU": "UT DE", "OmeU": "UT EN", "OV": "UT –", "DF": "synchr."}


def version_text(code: str, spoken: str | None) -> str:
    """Compact version label for lists, ISO codes for both languages:
    'EN, UT DE' / 'ES, UT –' / 'DE, synchr.'"""
    if code == "DF":
        return "DE, synchr."
    sp = spoken_codes(spoken) or "OV"
    sub = _SUBTITLE_TEXT.get(code)
    return f"{sp}, {sub}" if sub else f"{sp} {code}"


def version_name(code: str) -> str:
    return config.LANGUAGES.get(code, code)


def highlight_text(highlights: str) -> str:
    return ", ".join(config.HIGHLIGHT_TEXT.get(h, h) for h in sorted(set(highlights.split(","))) if h)


def _title_link(s: dict, link_mode: str, bot_username: str | None) -> str:
    """Tappable title. link_mode 'deep' = t.me deep link on the title (sends /start f_<id>),
    'text' = bold title followed by a visible /f_<id> command link."""
    title = escape(s["title"])
    sid = short_id(s["movie_slug"])[:8]
    if bot_username and link_mode == "deep":
        return f'<a href="https://t.me/{bot_username}?start=f_{sid}">{title}</a>'
    return f"<b>{title}</b> /f_{sid}"


def render_line(s: dict, movie: dict | None, meta: dict | None, cinema_info, link_mode: str,
                bot_username: str | None, detailed: bool = True) -> str:
    ci = cinema_info(s["venue_slug"])
    cinema = escape(ci.get("name") or s["venue_name"])
    if detailed:
        dist = geo.format_km(ci.get("distance_km"))
        if dist:
            cinema += f" <i>({dist})</i>"
    parts = [f"<b>{s['start_local'].strftime('%H:%M')}</b> {_title_link(s, link_mode, bot_username)}", cinema,
             escape(version_text(s["language"], (movie or {}).get("spoken_language")))]
    if detailed:
        if meta and meta.get("genre"):
            parts.append(escape(meta["genre"].split(",")[0].strip()))
        tags = highlight_text(s["highlights"])
        if tags:
            parts.append(f"<i>{escape(tags)}</i>")
    return " · ".join(parts)


def render_detailed(
    heading: str,
    user: dict,
    screenings: list[dict],
    movies: dict[str, dict],
    meta: dict[str, dict],
    cinema_info,
    empty_text: str,
    link_mode: str = "deep",
    bot_username: str | None = None,
) -> list[tuple[str, str]]:
    """Return (tab_label, text) pages: one per time-of-day section, split further only if a
    single section does not fit into one message. Every page is self-contained."""
    header = _list_header(heading, user, screenings)
    if not screenings:
        return [("", header + f"\n{escape(empty_text)}")]
    pages: list[tuple[str, str]] = []
    for label, rows in split_sections(screenings):
        lines = [render_line(s, movies.get(s["movie_slug"]), meta.get(s["movie_slug"]), cinema_info, link_mode, bot_username)
                 for s in sorted(rows, key=lambda x: (x["start_local"], x["title"].lower()))]
        sec = f"\n<b>{label}</b> <i>({len(rows)} Vorstellungen)</i>\n"
        texts = _pack(header + sec, lines, header + sec, limit=MAX_MESSAGE - 150, by_line=True)
        for i, t in enumerate(texts):
            tab = label if len(texts) == 1 else f"{label} {i + 1}/{len(texts)}"
            if len(texts) > 1:
                t = t.replace(sec, f"\n<b>{label}</b> <i>({len(rows)} Vorstellungen · Seite {i + 1}/{len(texts)})</i>\n", 1)
            pages.append((tab, t))
    return pages


def render_details(
    movie: dict,
    meta: dict | None,
    screenings: list[dict],
    user: dict | None,
    cinema_info,
) -> tuple[str, str]:
    """Return (caption_html, body_html). The caption fits under a photo, the body is the rest."""
    title = escape(movie.get("title") or (meta or {}).get("imdb_title") or "")
    head = [f"<b>{title}</b>"]
    people = []
    if movie.get("director"):
        people.append(f"Regie: {escape(movie['director'])}")
    if movie.get("cast"):
        people.append(f"Mit: {escape(movie['cast'].strip(' ,'))}")
    if people:
        head.append(" · ".join(people))
    facts = facts_line(movie, meta)
    if facts:
        head.append(facts)
    origin = []
    if movie.get("spoken_language"):
        origin.append(f"Sprache: {escape(movie['spoken_language'])}")
    if screenings:
        subs = [config.SUBTITLES[c] for c in config.SUBTITLES if any(s["language"] == c for s in screenings)]
        subs = [x for x in subs if x != "–"]
        dubbed = any(s["language"] == "DF" for s in screenings)
        origin.append("Untertitel: " + (", ".join(subs) if subs else ("– (deutsche Fassung)" if dubbed else "keine")))
    if meta and meta.get("country"):
        origin.append(f"Land: {escape(meta['country'])}")
    if origin:
        head.append(" · ".join(origin))
    links = []
    if movie.get("trailer_url"):
        links.append(f'<a href="{escape(movie["trailer_url"])}">▶️ Trailer</a>')
    elif movie.get("title"):
        q = quote_plus(f"{movie['title']} trailer")
        links.append(f'<a href="https://www.youtube.com/results?search_query={q}">▶️ Trailer (YouTube-Suche)</a>')
    if movie.get("url"):
        links.append(f'<a href="{escape(movie["url"])}">🔗 nonstopkino.at</a>')
    if meta and meta.get("imdb_id"):
        links.append(f'<a href="https://www.imdb.com/title/{meta["imdb_id"]}/">IMDb</a>')
    if links:
        head.append(" · ".join(links))
    caption = "\n".join(head)

    body: list[str] = []
    desc = movie.get("description") or (meta or {}).get("plot")
    if desc:
        body.append(f"<i>{escape(desc)}</i>")

    if screenings:
        body.append("")
        body.append("📅 <b>Vorstellungen</b>")
        by_day: dict[str, list[dict]] = {}
        for s in screenings:
            by_day.setdefault(s["date_local"], []).append(s)
        for day_iso, shows in list(by_day.items())[:7]:
            d = date.fromisoformat(day_iso)
            items = []
            for s in sorted(shows, key=lambda x: x["start_local"]):
                ci = cinema_info(s["venue_slug"])
                items.append(f"{s['start_local'].strftime('%H:%M')} {escape(ci.get('name') or s['venue_name'])} ({escape(s['language'])})")
            body.append(f"<b>{fmt_short_day(d)}</b>: " + " · ".join(items))
        codes = sorted({s["language"] for s in screenings}, key=list(config.LANGUAGES).index)
        body.append("<i>" + " · ".join(f"{escape(c)} = {escape(version_name(c))}" for c in codes) + "</i>")

        body.append("")
        body.append("📍 <b>Kinos</b>")
        seen: set[str] = set()
        for s in screenings:
            if s["venue_slug"] in seen:
                continue
            seen.add(s["venue_slug"])
            ci = cinema_info(s["venue_slug"])
            name = escape(ci.get("name") or s["venue_name"])
            line = f"<b>{name}</b>"
            if ci.get("address"):
                link = geo.maps_link(ci.get("lat"), ci.get("lon"), ci.get("address"))
                addr = escape(ci["address"])
                line += " – " + (f'<a href="{link}">{addr}</a>' if link else addr)
            dist = geo.format_km(ci.get("distance_km"))
            if dist:
                line += f" <i>({dist} entfernt)</i>"
            if ci.get("website"):
                line += f' · <a href="{escape(ci["website"])}">Website</a>'
            body.append(line)

    return caption, "\n".join(body)


def settings_summary(user: dict) -> str:
    bl = config.BUNDESLAENDER.get(user.get("bundesland") or "", "– (nicht gesetzt)")
    langs = ", ".join(user.get("languages") or []) or "alle"
    home = user.get("home_label") or "–"
    notify = f"{user['notify_time']} Uhr" if user.get("notify_enabled") else "aus"
    return (
        "⚙️ <b>Deine Einstellungen</b>\n"
        f"📍 Bundesland: <b>{escape(bl)}</b>\n"
        f"🗣 Sprachfassungen: <b>{escape(langs)}</b>\n"
        f"⏰ Tägliche Benachrichtigung: <b>{escape(notify)}</b>\n"
        f"🏠 Zuhause: <b>{escape(home)}</b>"
    )
