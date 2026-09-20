"""Loyihalar boti: machine / CTF / ZIP CTF yechimlarini saqlash va ulashish.

Admin (ADMIN_ID) hamma narsani boshqaradi. Boshqalar faqat admin ruxsat bergan
bo'lim va loyihalarni ko'ra oladi. Ruxsatsizlar uchun faqat "Adminga yozish" bor.
"""
import asyncio
import html
import io
import json
import logging
import os
import re
import sqlite3
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = BASE_DIR / "data" / "bot.db"
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_VERSION = 1
MAX_SEND = 49 * 1024 * 1024        # bot Telegramga shundan katta fayl yubora olmaydi
MAX_RECEIVE = 20 * 1024 * 1024     # bot Telegramdan shundan katta fayl yuklab ola olmaydi
FILE_EXT = {"photo": ".jpg", "video": ".mp4", "audio": ".mp3", "voice": ".ogg", "animation": ".mp4",
            "video_note": ".mp4"}

SECTIONS = {"machine": "🖥 Machine", "ctf": "🚩 CTF", "zip_ctf": "🗜 ZIP CTF"}
METHODS = {"script": "📜 Skript", "code": "💻 Oddiy kod", "tool": "🛠 Tool"}
MEDIA_KINDS = ("photo", "document", "video", "audio", "voice", "animation", "video_note")
PAGE_SIZE = 20
LOG_PAGE = 15
DIFFICULTY = {"easy": "🟢 Easy", "medium": "🟡 Medium", "hard": "🔴 Hard", "insane": "⚫ Insane"}
DURATIONS = [(0, "♾ Cheksiz"), (1, "1 kun"), (3, "3 kun"), (7, "1 hafta"), (30, "1 oy")]
AUTO_MODES = {"daily": "har kuni", "weekly": "har hafta", "off": "o'chiq"}
MATERIAL_ICONS = {"text": "📝", "photo": "🖼", "document": "📄", "video": "🎬", "audio": "🎵", "voice": "🎤",
                  "animation": "🎞", "video_note": "⭕"}

log = logging.getLogger("bot")
esc = html.escape


# ───────────────────────────── database ─────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT);
CREATE TABLE IF NOT EXISTS projects(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at TEXT);
CREATE TABLE IF NOT EXISTS entries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    section TEXT NOT NULL, title TEXT NOT NULL,
    method TEXT, method_detail TEXT, flag TEXT, difficulty TEXT, tags TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS materials(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, file_id TEXT, text TEXT, file_name TEXT);
CREATE TABLE IF NOT EXISTS grants(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE, username TEXT,
    sections TEXT NOT NULL, projects TEXT NOT NULL, expires_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS access_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER NOT NULL, entry_id INTEGER, label TEXT);
"""

conn: sqlite3.Connection


def init_db():
    global conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for table, column in (("materials", "file_name"), ("entries", "difficulty"), ("entries", "tags"),
                          ("grants", "expires_at"), ("projects", "created_at")):
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    conn.execute("UPDATE projects SET created_at=COALESCE((SELECT MIN(created_at) FROM entries "
                 "WHERE project_id=projects.id), datetime('now')) WHERE created_at IS NULL")
    conn.execute("DELETE FROM access_log WHERE ts < datetime('now', '-90 days')")
    conn.commit()


def q(sql, *args):
    return conn.execute(sql, args).fetchall()


def q1(sql, *args):
    return conn.execute(sql, args).fetchone()


def run(sql, *args):
    cur = conn.execute(sql, args)
    conn.commit()
    return cur


def touch_user(u):
    """Foydalanuvchini eslab qoladi va username bo'yicha berilgan ruxsatni ID ga bog'laydi."""
    uname = (u.username or "").lower() or None
    conn.execute(
        "INSERT INTO users(user_id, username, full_name) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
        (u.id, uname, u.full_name),
    )
    if uname:
        conn.execute("UPDATE OR IGNORE grants SET user_id=? WHERE user_id IS NULL AND username=?", (u.id, uname))
        conn.execute("DELETE FROM grants WHERE user_id IS NULL AND username=?", (uname,))
    conn.commit()


def get_setting(key, default=None):
    row = q1("SELECT value FROM settings WHERE key=?", key)
    return row["value"] if row else default


def set_setting(key, value):
    run("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)


def parse_tags(text):
    out = []
    for t in re.split(r"[,\n]", text):
        t = t.strip().lstrip("#").strip().lower()[:24]
        if t and t not in out:
            out.append(t)
    return out[:10]


def tags_to_db(tags):
    return "," + ",".join(tags) + "," if tags else None


def tags_from_db(value):
    return [t for t in (value or "").split(",") if t]


def tags_line(value):
    return " ".join("#" + t.replace(" ", "_") for t in tags_from_db(value))


def diff_icon(d):
    return DIFFICULTY[d].split()[0] + " " if d in DIFFICULTY else ""


def all_projects():
    return q("SELECT id, name FROM projects ORDER BY id")


def project_name(pid):
    row = q1("SELECT name FROM projects WHERE id=?", pid)
    return row["name"] if row else "?"


def _grant_dict(row):
    if not row:
        return None
    return {"id": row["id"], "user_id": row["user_id"], "username": row["username"],
            "sections": json.loads(row["sections"]), "projects": json.loads(row["projects"]),
            "expires_at": row["expires_at"]}


def is_expired(expires_at):
    return bool(expires_at) and datetime.fromisoformat(expires_at) <= datetime.now()


def expiry_text(expires_at):
    if not expires_at:
        return "♾ Cheksiz"
    if is_expired(expires_at):
        return "❌ Muddati tugagan"
    return f"⏳ {datetime.fromisoformat(expires_at):%d.%m.%Y %H:%M} gacha"


def raw_grant(uid):
    return _grant_dict(q1("SELECT * FROM grants WHERE user_id=?", uid))


def get_grant(uid):
    """Faqat amaldagi (muddati tugamagan) ruxsat."""
    g = raw_grant(uid)
    return None if g and is_expired(g["expires_at"]) else g


def find_grant(uid, username):
    if uid:
        return raw_grant(uid)
    return _grant_dict(q1("SELECT * FROM grants WHERE user_id IS NULL AND username=?", username))


def save_grant(uid, username, sections, projects, expires_at=None):
    old = find_grant(uid, username)
    if old:
        run("UPDATE grants SET user_id=?, username=?, sections=?, projects=?, expires_at=? WHERE id=?",
            uid, username, json.dumps(sections), json.dumps(projects), expires_at, old["id"])
    else:
        run("INSERT INTO grants(user_id, username, sections, projects, expires_at) VALUES(?,?,?,?,?)",
            uid, username, json.dumps(sections), json.dumps(projects), expires_at)


def allowed_sections(uid, pid):
    if uid == ADMIN_ID:
        return list(SECTIONS)
    g = get_grant(uid)
    if not g or not ("*" in g["projects"] or pid in g["projects"]):
        return []
    return [s for s in SECTIONS if s in g["sections"]]


def visible_projects(uid):
    if uid == ADMIN_ID:
        return all_projects()
    g = get_grant(uid)
    if not g:
        return []
    return [p for p in all_projects() if "*" in g["projects"] or p["id"] in g["projects"]]


def who(uid, username):
    """Foydalanuvchi nomi (oddiy matn)."""
    if uid:
        u = q1("SELECT username, full_name FROM users WHERE user_id=?", uid)
        name = u["full_name"] if u and u["full_name"] else ""
        uname = f" @{u['username']}" if u and u["username"] else ""
        return f"{name}{uname} ({uid})".strip()
    return f"@{username}"


# ───────────────────────────── helpers ─────────────────────────────

def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


trash: dict[int, list[int]] = {}  # bot yuborgan, keyingi ekranda o'chiriladigan xabarlar (foydalanuvchi bo'yicha)


def track(uid, *ids):
    lst = trash.setdefault(uid, [])
    lst.extend(i for i in ids if i and i not in lst)


async def purge(bot: Bot, uid, keep=None):
    ids = [i for i in trash.pop(uid, []) if i != keep]
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(uid, ids[i:i + 100])
        except TelegramAPIError:
            pass


async def show(ev, text, markup=None, keep_user=False):
    """Yangi ekran: eski xabarlarni o'chirib, bitta yangi xabar qoldiradi.

    Tugma bosilgan bo'lsa o'sha xabar tahrirlanadi, foydalanuvchi yozgan xabar bo'lsa u ham o'chiriladi.
    """
    bot = ev.bot
    if isinstance(ev, CallbackQuery):
        uid, mid = ev.from_user.id, ev.message.message_id
        await purge(bot, uid, keep=mid)
        track(uid, mid)
        try:
            await ev.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        await drop_message(ev)
    else:
        uid = ev.chat.id
        await purge(bot, uid)
        if not ev.from_user.is_bot and not keep_user:
            try:
                await ev.delete()
            except TelegramAPIError:
                pass
    sent = await bot.send_message(uid, text, reply_markup=markup)
    track(uid, sent.message_id)


async def drop_message(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except TelegramAPIError:
        pass


def extract_material(m: Message):
    for kind in MEDIA_KINDS:
        obj = getattr(m, kind)
        if obj:
            file_id = obj[-1].file_id if kind == "photo" else obj.file_id
            return {"kind": kind, "file_id": file_id, "text": m.caption, "name": getattr(obj, "file_name", None)}
    if m.text:
        return {"kind": "text", "file_id": None, "text": m.text, "name": None}
    return None


async def send_material(bot: Bot, chat_id, mat):
    for _ in range(2):
        try:
            if mat["kind"] == "text":
                msg = await bot.send_message(chat_id, mat["text"], parse_mode=None)
            elif mat["kind"] == "video_note":
                msg = await bot.send_video_note(chat_id, mat["file_id"])
            else:
                send = getattr(bot, f"send_{mat['kind']}")
                msg = await send(chat_id, **{mat["kind"]: mat["file_id"]}, caption=mat["text"], parse_mode=None)
            return msg.message_id
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramAPIError as e:
            log.warning("material yuborilmadi (%s): %s", mat["kind"], e)
            return


# ───────────────────────────── views ─────────────────────────────

def home_view(uid):
    if uid == ADMIN_ID:
        projects = all_projects()
        rows = [[(f"📁 {p['name']}", f"p:{p['id']}")] for p in projects]
        rows.append([("🔍 Qidiruv", "srch"), ("⚙️ Admin panel", "adm:panel")])
        text = "👑 <b>Admin</b>\nLoyihani tanlang:" if projects else \
            "👑 <b>Admin</b>\nHali loyiha yo'q. ⚙️ Admin panel → ➕ Loyiha qo'shish."
        return text, kb(*rows)
    if get_grant(uid):
        projects = visible_projects(uid)
        rows = [[(f"📁 {p['name']}", f"p:{p['id']}")] for p in projects]
        rows.append([("🔍 Qidiruv", "srch"), ("✍️ Adminga yozish", "contact")])
        text = "📂 <b>Loyihalar</b>\nTanlang:" if projects else "📂 Hozircha sizga ko'rsatiladigan loyiha yo'q."
        return text, kb(*rows)
    old = raw_grant(uid)
    text = ("⏳ Ruxsat muddatingiz tugagan.\nUzaytirish uchun adminga yozing." if old and is_expired(old["expires_at"])
            else "⛔ Sizda hozircha ruxsat yo'q.\nRuxsat olish uchun adminga yozing.")
    return text, kb([("✍️ Adminga yozish", "contact")])


def project_view(uid, pid):
    secs = allowed_sections(uid, pid)
    rows = []
    for s in secs:
        n = q1("SELECT COUNT(*) c FROM entries WHERE project_id=? AND section=?", pid, s)["c"]
        rows.append([(f"{SECTIONS[s]} ({n})", f"s:{pid}:{s}:0")])
    if uid == ADMIN_ID:
        rows.append([("🗑 O'chirish", f"adm:dp:{pid}")])
    rows.append([("◀️ Orqaga", "home")])
    return f"📁 <b>{esc(project_name(pid))}</b>\nBo'limni tanlang:", kb(*rows)


def section_tags(pid, sec):
    counter = {}
    for r in q("SELECT tags FROM entries WHERE project_id=? AND section=?", pid, sec):
        for t in tags_from_db(r["tags"]):
            counter[t] = counter.get(t, 0) + 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:30]


def entries_view(uid, pid, sec, page=0, flt=""):
    sql, args, label = "SELECT id, title, difficulty FROM entries WHERE project_id=? AND section=?", [pid, sec], ""
    if flt.startswith("d-") and flt[2:] in DIFFICULTY:
        sql += " AND difficulty=?"
        args.append(flt[2:])
        label = DIFFICULTY[flt[2:]]
    elif flt.startswith("t") and flt[1:].isdigit() and int(flt[1:]) < len(section_tags(pid, sec)):
        tag = section_tags(pid, sec)[int(flt[1:])][0]
        sql += " AND tags LIKE ?"
        args.append(f"%,{tag},%")
        label = "#" + tag
    else:
        flt = ""
    items = q(sql + " ORDER BY id", *args)
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    suffix = f":{flt}" if flt else ""
    rows = [[(diff_icon(r["difficulty"]) + r["title"][:55], f"e:{r['id']}")]
            for r in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    if pages > 1:
        rows.append([("⬅️", f"s:{pid}:{sec}:{page - 1}{suffix}"), (f"{page + 1}/{pages}", "noop"),
                     ("➡️", f"s:{pid}:{sec}:{page + 1}{suffix}")])
    has_meta = q1("SELECT 1 FROM entries WHERE project_id=? AND section=? AND (difficulty IS NOT NULL "
                  "OR tags IS NOT NULL) LIMIT 1", pid, sec)
    if flt:
        rows.append([("✖️ Filtrni olib tashlash", f"s:{pid}:{sec}:0")])
    elif has_meta:
        rows.append([("🏷 Filtr", f"fl:{pid}:{sec}")])
    if uid == ADMIN_ID:
        rows.append([("➕ Qo'shish", f"add:{pid}:{sec}")])
    rows.append([("◀️ Orqaga", f"p:{pid}")])
    head = f"📁 <b>{esc(project_name(pid))}</b> › {SECTIONS[sec]}\n"
    if label:
        head += f"🔍 Filtr: <b>{esc(label)}</b>\n"
    empty = "Bu filtr bo'yicha hech narsa yo'q." if flt else "Hali hech narsa qo'shilmagan."
    return head + "\n" + ("Tanlang:" if items else empty), kb(*rows)


def filter_view(pid, sec):
    diffs = {r["difficulty"]: r["c"] for r in q(
        "SELECT difficulty, COUNT(*) c FROM entries WHERE project_id=? AND section=? AND difficulty IS NOT NULL "
        "GROUP BY difficulty", pid, sec)}
    rows = [[(f"{DIFFICULTY[k]} ({diffs[k]})", f"s:{pid}:{sec}:0:d-{k}")] for k in DIFFICULTY if k in diffs]
    tags = section_tags(pid, sec)
    for i in range(0, len(tags), 2):
        rows.append([(f"#{t} ({n})", f"s:{pid}:{sec}:0:t{i + j}") for j, (t, n) in enumerate(tags[i:i + 2])])
    rows.append([("◀️ Orqaga", f"s:{pid}:{sec}:0")])
    return f"📁 <b>{esc(project_name(pid))}</b> › {SECTIONS[sec]}\n\n🏷 Filtrni tanlang:", kb(*rows)


def search_entries(uid, query):
    words = query.casefold().split()
    out = []
    for p in visible_projects(uid):
        secs = allowed_sections(uid, p["id"])
        if not secs:
            continue
        marks = ",".join("?" * len(secs))
        for e in q(f"SELECT * FROM entries WHERE project_id=? AND section IN ({marks}) ORDER BY id DESC", p["id"], *secs):
            hay = " ".join(filter(None, [
                e["title"], e["flag"], e["method_detail"], (e["tags"] or "").replace(",", " "), p["name"],
                METHODS.get(e["method"]), DIFFICULTY.get(e["difficulty"])])).casefold()
            if all(w in hay for w in words):
                out.append((p, e))
    return out


def log_access(uid, eid):
    e = q1("SELECT title, section, project_id FROM entries WHERE id=?", eid)
    label = f"{project_name(e['project_id'])} › {SECTIONS[e['section']]} › {e['title']}"
    run("INSERT INTO access_log(user_id, entry_id, label) VALUES(?,?,?)", uid, eid, label)


async def send_entry(bot: Bot, uid, eid):
    await purge(bot, uid)
    e = q1("SELECT * FROM entries WHERE id=?", eid)
    lines = [f"<b>{esc(e['title'])}</b>", f"📁 {esc(project_name(e['project_id']))} › {SECTIONS[e['section']]}"]
    if e["method"]:
        detail = f": {esc(e['method_detail'])}" if e["method_detail"] else ""
        lines.append(f"🔧 {METHODS[e['method']]}{detail}")
    if e["flag"]:
        lines.append(f"🚩 <code>{esc(e['flag'])}</code>")
    if e["difficulty"] in DIFFICULTY:
        lines.append(f"🎯 {DIFFICULTY[e['difficulty']]}")
    if e["tags"]:
        lines.append(f"🏷 {esc(tags_line(e['tags']))}")
    lines.append(f"📅 {e['created_at'][:10]}")
    track(uid, (await bot.send_message(uid, "\n".join(lines))).message_id)
    for mat in q("SELECT * FROM materials WHERE entry_id=? ORDER BY id", eid):
        track(uid, await send_material(bot, uid, mat))
    rows = []
    if uid == ADMIN_ID:
        rows.append([("✏️ Tahrirlash", f"edit:{eid}"), ("➕ Material", f"addmat:{eid}")])
        rows.append([("🗑 O'chirish", f"adm:dele:{eid}")])
    rows.append([("◀️ Orqaga", f"s:{e['project_id']}:{e['section']}:0")])
    track(uid, (await bot.send_message(uid, "⬆️ Hammasi yuqorida.", reply_markup=kb(*rows))).message_id)


# ───────────────────────────── states ─────────────────────────────

class Add(StatesGroup):
    name = State()
    method = State()
    detail = State()
    flag = State()
    difficulty = State()
    tags = State()
    files = State()


class NewProject(StatesGroup):
    name = State()


class Grant(StatesGroup):
    target = State()
    pick = State()


class Backup(StatesGroup):
    wait = State()


class Edit(StatesGroup):
    value = State()


class Search(StatesGroup):
    text = State()


class Del(StatesGroup):
    pick = State()


class Reply(StatesGroup):
    text = State()


class Contact(StatesGroup):
    text = State()


class IsAdmin(Filter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and u.id == ADMIN_ID)


class Touch(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and not u.is_bot:
            touch_user(u)
        return await handler(event, data)


common = Router()
admin = Router()
fallback = Router()
for r in (common, admin, fallback):
    r.message.filter(F.chat.type == "private")
admin.message.filter(IsAdmin())
admin.callback_query.filter(IsAdmin())


# ───────────────────────────── common: start, browse, contact ─────────────────────────────

@common.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await show(m, *home_view(m.from_user.id))


@common.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, *home_view(cb.from_user.id))
    await cb.answer()


@common.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@common.callback_query(F.data.startswith("p:"))
async def cb_project(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    if not allowed_sections(cb.from_user.id, pid):
        return await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
    await show(cb, *project_view(cb.from_user.id, pid))
    await cb.answer()


@common.callback_query(F.data.startswith("s:"))
async def cb_section(cb: CallbackQuery):
    parts = cb.data.split(":")
    pid, sec, page = int(parts[1]), parts[2], int(parts[3])
    if sec not in allowed_sections(cb.from_user.id, pid):
        return await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
    await show(cb, *entries_view(cb.from_user.id, pid, sec, page, parts[4] if len(parts) > 4 else ""))
    await cb.answer()


@common.callback_query(F.data.startswith("fl:"))
async def cb_filter(cb: CallbackQuery):
    _, pid, sec = cb.data.split(":")
    if sec not in allowed_sections(cb.from_user.id, int(pid)):
        return await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
    await show(cb, *filter_view(int(pid), sec))
    await cb.answer()


@common.callback_query(F.data.startswith("e:"))
async def cb_entry(cb: CallbackQuery):
    eid = int(cb.data.split(":")[1])
    e = q1("SELECT project_id, section FROM entries WHERE id=?", eid)
    if not e or e["section"] not in allowed_sections(cb.from_user.id, e["project_id"]):
        return await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
    await cb.answer()
    if cb.from_user.id != ADMIN_ID:
        log_access(cb.from_user.id, eid)
    await drop_message(cb)
    await send_entry(cb.bot, cb.from_user.id, eid)


@common.callback_query(F.data == "srch")
async def cb_search(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if uid != ADMIN_ID and not get_grant(uid):
        return await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
    await state.set_state(Search.text)
    await show(cb, "🔍 <b>Qidiruv</b>\n\nNom, flag, teg (masalan <i>web</i>), usul yoki qiyinlik (<i>easy</i>) yozing:",
               kb([("◀️ Orqaga", "home")]))
    await cb.answer()


@common.message(Search.text, F.text)
async def search_run(m: Message, state: FSMContext):
    uid = m.from_user.id
    if uid != ADMIN_ID and not get_grant(uid):
        await state.clear()
        return await show(m, *home_view(uid))
    query = m.text.strip()[:100]
    results = search_entries(uid, query)
    rows = [[(f"{diff_icon(e['difficulty'])}{e['title'][:35]} · {p['name'][:15]}", f"e:{e['id']}")]
            for p, e in results[:30]]
    rows.append([("◀️ Orqaga", "home")])
    head = f"🔍 <b>{esc(query)}</b>: {len(results)} ta natija" + (" (dastlabki 30 tasi)" if len(results) > 30 else "")
    await show(m, head + "\n\nYangi so'z yozsangiz, qayta qidiraman.", kb(*rows))


@common.callback_query(F.data == "contact")
async def cb_contact(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Contact.text)
    await show(cb, "✍️ Adminga xabaringizni yozing (matn, rasm yoki fayl):", kb([("◀️ Orqaga", "home")]))
    await cb.answer()


@common.message(Contact.text)
async def contact_send(m: Message, state: FSMContext):
    u = m.from_user
    info = (f"📩 <b>Yangi xabar</b>\n👤 {esc(u.full_name)}" + (f" @{u.username}" if u.username else "")
            + f"\n🆔 <code>{u.id}</code>")
    try:
        await m.copy_to(ADMIN_ID)
        await m.bot.send_message(ADMIN_ID, info, reply_markup=kb(
            [("✅ Ruxsat berish", f"gu:{u.id}"), ("✍️ Javob", f"rp:{u.id}")]))
    except TelegramAPIError as e:
        log.warning("adminga yuborilmadi: %s", e)
        return await show(m, "⚠️ Xabarni yuborib bo'lmadi, keyinroq urinib ko'ring.", kb([("◀️ Orqaga", "home")]))
    await state.clear()
    await show(m, "✅ Xabaringiz adminga yuborildi.", kb([("✉️ Yana yozish", "contact")], [("◀️ Orqaga", "home")]))


# ───────────────────────────── admin: panel, loyiha ─────────────────────────────

@admin.callback_query(F.data == "adm:panel")
async def adm_panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "⚙️ <b>Admin panel</b>", kb(
        [("➕ Loyiha qo'shish", "adm:newproj"), ("🗑 O'chirish", "adm:del")],
        [("➕ Ruxsat berish", "adm:grant"), ("👥 Foydalanuvchilar", "adm:users")],
        [("📊 Statistika", "adm:stats"), ("🛡 Kirish tarixi", "adm:log:0")],
        [("💾 Backup", "bk:menu")],
        [("◀️ Orqaga", "home")]))
    await cb.answer()


@admin.callback_query(F.data == "adm:newproj")
async def adm_newproj(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewProject.name)
    await show(cb, "📁 Yangi loyiha nomini yozing:", kb([("◀️ Orqaga", "adm:panel")]))
    await cb.answer()


@admin.message(NewProject.name, F.text)
async def newproj_name(m: Message, state: FSMContext):
    try:
        run("INSERT INTO projects(name, created_at) VALUES(?, datetime('now'))", m.text.strip()[:60])
    except sqlite3.IntegrityError:
        return await show(m, "⚠️ Bunday nomli loyiha bor. Boshqa nom yozing:", kb([("◀️ Orqaga", "adm:panel")]))
    await state.clear()
    text, markup = home_view(m.from_user.id)
    await show(m, "✅ Loyiha qo'shildi.\n\n" + text, markup)


@admin.callback_query(F.data.startswith("adm:delp:"))
async def adm_delp(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    n = q1("SELECT COUNT(*) c FROM entries WHERE project_id=?", pid)["c"]
    await show(cb, f"🗑 <b>{esc(project_name(pid))}</b> loyihasi va ichidagi <b>{n}</b> ta ish butunlay o'chiriladi. "
                   "Ishonchingiz komilmi?",
               kb([("✅ Ha, o'chirish", f"adm:delpc:{pid}"), ("❌ Yo'q", f"adm:dp:{pid}")]))
    await cb.answer()


@admin.callback_query(F.data.startswith("adm:delpc:"))
async def adm_delpc(cb: CallbackQuery):
    run("DELETE FROM projects WHERE id=?", int(cb.data.split(":")[2]))
    await show(cb, *home_view(cb.from_user.id))
    await cb.answer("🗑 O'chirildi")


@admin.callback_query(F.data == "adm:del")
async def adm_del(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    projects = all_projects()
    if not projects:
        return await cb.answer("Hali loyiha yo'q", show_alert=True)
    rows = [[(f"📁 {pr['name']}", f"adm:dp:{pr['id']}")] for pr in projects]
    rows.append([("◀️ Orqaga", "adm:panel")])
    await show(cb, "🗑 <b>Qaysi loyihadan o'chiramiz?</b>", kb(*rows))
    await cb.answer()


@admin.callback_query(F.data.startswith("adm:dp:"))
async def adm_del_project(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    pid = int(cb.data.split(":")[2])
    rows = []
    for sec, label in SECTIONS.items():
        n = q1("SELECT COUNT(*) c FROM entries WHERE project_id=? AND section=?", pid, sec)["c"]
        rows.append([(f"{label} ({n})", f"adm:ds:{pid}:{sec}")])
    rows.append([("🗑 Butun loyihani o'chirish", f"adm:delp:{pid}")])
    rows.append([("◀️ Orqaga", "adm:del")])
    await show(cb, f"🗑 <b>{esc(project_name(pid))}</b>\n\nQaysi bo'limdan o'chiramiz? "
                   "Yoki butun loyihani o'chiring.", kb(*rows))
    await cb.answer()


def del_view(d):
    items = q("SELECT id, title FROM entries WHERE project_id=? AND section=? ORDER BY id", d["pid"], d["sec"])
    ids = {r["id"] for r in items}
    sel = [i for i in d["sel"] if i in ids]
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(d["page"], pages - 1)
    rows = [[(("✅ " if r["id"] in sel else "⬜ ") + r["title"][:55], f"dl:t:{r['id']}")]
            for r in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    if pages > 1:
        rows.append([("⬅️", f"dl:pg:{page - 1}"), (f"{page + 1}/{pages}", "noop"), ("➡️", f"dl:pg:{page + 1}")])
    if items:
        rows.append([("☑️ Hammasini tanlash" if len(sel) < len(items) else "⬜ Tanlovni bekor qilish", "dl:all")])
    if sel:
        rows.append([(f"🗑 O'chirish ({len(sel)})", "dl:go")])
    rows.append([("◀️ Orqaga", f"adm:dp:{d['pid']}")])
    head = f"🗑 <b>{esc(project_name(d['pid']))}</b> › {SECTIONS[d['sec']]}\n\n"
    return head + ("O'chiriladiganlarni belgilang:" if items else "Bu bo'limda hech narsa yo'q."), kb(*rows)


@admin.callback_query(F.data.startswith("adm:ds:"))
async def adm_del_section(cb: CallbackQuery, state: FSMContext):
    _, _, pid, sec = cb.data.split(":")
    await state.set_state(Del.pick)
    await state.update_data(pid=int(pid), sec=sec, sel=[], page=0)
    await show(cb, *del_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Del.pick, F.data.startswith("dl:t:"))
async def dl_toggle(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    eid = int(cb.data.split(":")[2])
    await state.update_data(sel=[i for i in d["sel"] if i != eid] if eid in d["sel"] else d["sel"] + [eid])
    await show(cb, *del_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Del.pick, F.data.startswith("dl:pg:"))
async def dl_page(cb: CallbackQuery, state: FSMContext):
    await state.update_data(page=max(0, int(cb.data.split(":")[2])))
    await show(cb, *del_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Del.pick, F.data == "dl:all")
async def dl_all(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    ids = [r["id"] for r in q("SELECT id FROM entries WHERE project_id=? AND section=?", d["pid"], d["sec"])]
    await state.update_data(sel=[] if len(d["sel"]) >= len(ids) else ids)
    await show(cb, *del_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Del.pick, F.data == "dl:go")
async def dl_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    marks = ",".join("?" * len(d["sel"]))
    titles = [r["title"] for r in q(f"SELECT title FROM entries WHERE id IN ({marks}) ORDER BY id", *d["sel"])]
    if not titles:
        return await cb.answer("Hech narsa tanlanmagan", show_alert=True)
    shown = "\n".join(f"• {esc(t)}" for t in titles[:15]) + (f"\n… va yana {len(titles) - 15} ta" if len(titles) > 15 else "")
    await show(cb, f"🗑 <b>{len(titles)}</b> ta ish butunlay o'chiriladi:\n\n{shown}\n\nIshonchingiz komilmi?",
               kb([("✅ Ha, o'chirish", "dl:yes"), ("❌ Yo'q", "dl:no")]))
    await cb.answer()


@admin.callback_query(Del.pick, F.data == "dl:no")
async def dl_no(cb: CallbackQuery, state: FSMContext):
    await show(cb, *del_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Del.pick, F.data == "dl:yes")
async def dl_yes(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    marks = ",".join("?" * len(d["sel"]))
    n = run(f"DELETE FROM entries WHERE id IN ({marks}) AND project_id=? AND section=?",
            *d["sel"], d["pid"], d["sec"]).rowcount
    await state.update_data(sel=[], page=0)
    await show(cb, *del_view(await state.get_data()))
    await cb.answer(f"🗑 {n} ta o'chirildi")


@admin.callback_query(F.data.startswith("adm:dele:"))
async def adm_dele(cb: CallbackQuery):
    eid = int(cb.data.split(":")[2])
    await show(cb, "🗑 Bu ishni o'chirishga ishonchingiz komilmi?",
               kb([("✅ Ha", f"adm:delec:{eid}"), ("❌ Yo'q", f"e:{eid}")]))
    await cb.answer()


@admin.callback_query(F.data.startswith("adm:delec:"))
async def adm_delec(cb: CallbackQuery):
    e = q1("SELECT project_id, section FROM entries WHERE id=?", int(cb.data.split(":")[2]))
    if e:
        run("DELETE FROM entries WHERE id=?", int(cb.data.split(":")[2]))
        await show(cb, *entries_view(cb.from_user.id, e["project_id"], e["section"]))
    await cb.answer("🗑 O'chirildi")


# ───────────────────────────── admin: ish qo'shish (wizard) ─────────────────────────────

WIZARD_PREV = {"method": "name", "detail": "method", "flag": "detail", "difficulty": "flag",
               "tags": "difficulty", "files": "tags"}


def prompt(step, d):
    crumb = f"<i>{esc(project_name(d['pid']))} › {SECTIONS[d['section']]}</i>\n\n"
    back = ("◀️ Orqaga", "wz:back")
    if step == "name":
        text = {"machine": "🖥 <b>Machine</b> raqami va nomini yozing\n<i>Masalan: 12 - Lame</i>",
                "ctf": "🚩 <b>CTF</b> nomini yozing",
                "zip_ctf": "🗜 <b>ZIP CTF</b> nomini yozing"}[d["section"]]
        return crumb + text, kb([back])
    if step == "method":
        return crumb + "🔧 Nimadan foydalandingiz?", kb([(v, f"wz:m:{k}") for k, v in METHODS.items()], [back])
    if step == "detail":
        return (crumb + f"{METHODS[d['method']]}: qaysi / nima ekanini yozing\n<i>Masalan: nmap, exploit.py</i>",
                kb([("⏭ O'tkazib yuborish", "wz:skip")], [back]))
    if step == "flag":
        return crumb + "🚩 <b>Flag</b>ni yozing:", kb([back])
    if step == "difficulty":
        items = list(DIFFICULTY.items())
        return (crumb + "🎯 Qiyinlik darajasi?",
                kb([(v, f"wz:d:{k}") for k, v in items[:2]], [(v, f"wz:d:{k}") for k, v in items[2:]],
                   [("⏭ O'tkazib yuborish", "wz:skip")], [back]))
    if step == "tags":
        return (crumb + "🏷 Teglarni vergul bilan yozing\n<i>Masalan: web, sql injection, linux</i>",
                kb([("⏭ O'tkazib yuborish", "wz:skip")], [back]))
    return (crumb + "📎 Fayl, rasm yoki izoh (matn) yuboring — istalgan formatda, ketma-ket bir nechta.\n"
            f"Yuborilgan: <b>{len(d['files'])}</b>\nTugagach ✅ Tugatish ni bosing.",
            kb([("✅ Tugatish", "wz:done")], [back]))


async def ask(ev, state: FSMContext, step):
    await state.set_state(getattr(Add, step))
    await show(ev, *prompt(step, await state.get_data()))


@admin.callback_query(F.data.startswith("add:"))
async def add_start(cb: CallbackQuery, state: FSMContext):
    _, pid, sec = cb.data.split(":")
    await state.clear()
    await state.update_data(pid=int(pid), section=sec, method=None, detail=None, flag=None, difficulty=None, tags=None,
                            files=[], entry_id=None)
    await ask(cb, state, "name")
    await cb.answer()


@admin.callback_query(F.data.startswith("addmat:"))
async def add_material_start(cb: CallbackQuery, state: FSMContext):
    eid = int(cb.data.split(":")[1])
    e = q1("SELECT project_id, section FROM entries WHERE id=?", eid)
    await state.clear()
    await state.update_data(pid=e["project_id"], section=e["section"], files=[], entry_id=eid)
    await ask(cb, state, "files")
    await cb.answer()


@admin.callback_query(StateFilter(Add), F.data == "wz:back")
async def wz_back(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    cur = (await state.get_state()).split(":")[1]
    await cb.answer()
    if cur == "files" and d.get("entry_id"):
        await state.clear()
        await drop_message(cb)
        return await send_entry(cb.bot, cb.from_user.id, d["entry_id"])
    prev = WIZARD_PREV.get(cur)
    if prev:
        return await ask(cb, state, prev)
    await state.clear()
    await show(cb, *entries_view(cb.from_user.id, d["pid"], d["section"]))


@admin.message(Add.name, F.text)
async def add_name(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:200])
    await ask(m, state, "method")


@admin.callback_query(Add.method, F.data.startswith("wz:m:"))
async def add_method(cb: CallbackQuery, state: FSMContext):
    await state.update_data(method=cb.data.split(":")[2])
    await ask(cb, state, "detail")
    await cb.answer()


@admin.message(Add.detail, F.text)
async def add_detail(m: Message, state: FSMContext):
    await state.update_data(detail=m.text.strip()[:300])
    await ask(m, state, "flag")


@admin.callback_query(Add.detail, F.data == "wz:skip")
async def add_detail_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(detail=None)
    await ask(cb, state, "flag")
    await cb.answer()


@admin.message(Add.flag, F.text)
async def add_flag(m: Message, state: FSMContext):
    await state.update_data(flag=m.text.strip())
    await ask(m, state, "difficulty")


@admin.callback_query(Add.difficulty, F.data.startswith("wz:d:"))
async def add_difficulty(cb: CallbackQuery, state: FSMContext):
    await state.update_data(difficulty=cb.data.split(":")[2])
    await ask(cb, state, "tags")
    await cb.answer()


@admin.callback_query(Add.difficulty, F.data == "wz:skip")
async def add_difficulty_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(difficulty=None)
    await ask(cb, state, "tags")
    await cb.answer()


@admin.message(Add.tags, F.text)
async def add_tags(m: Message, state: FSMContext):
    await state.update_data(tags=tags_to_db(parse_tags(m.text)))
    await ask(m, state, "files")


@admin.callback_query(Add.tags, F.data == "wz:skip")
async def add_tags_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(tags=None)
    await ask(cb, state, "files")
    await cb.answer()


@admin.message(StateFilter(Add.name, Add.detail, Add.flag, Add.difficulty, Add.tags))
async def add_need_text(m: Message, state: FSMContext):
    step = (await state.get_state()).split(":")[1]
    text, markup = prompt(step, await state.get_data())
    await show(m, "⚠️ Iltimos, so'ralgan narsani yuboring.\n\n" + text, markup)


files_lock = asyncio.Lock()  # albomdagi fayllar bir vaqtda kelganda ro'yxat yo'qolmasligi uchun


@admin.message(Add.files)
async def add_files(m: Message, state: FSMContext):
    mat = extract_material(m)
    async with files_lock:
        d = await state.get_data()
        if not mat:
            text, markup = prompt("files", d)
            return await show(m, "⚠️ Bu turdagi xabar qo'llab-quvvatlanmaydi.\n\n" + text, markup)
        files = d["files"] + [mat]
        await state.update_data(files=files)
        await show(m, f"✅ Qabul qilindi ({len(files)}). Yana yuboring yoki tugating.",
                   kb([("✅ Tugatish", "wz:done")], [("◀️ Orqaga", "wz:back")]))


@admin.callback_query(Add.files, F.data == "wz:done")
async def add_done(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    eid = d.get("entry_id")
    if not eid:
        eid = run("INSERT INTO entries(project_id, section, title, method, method_detail, flag, difficulty, tags) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  d["pid"], d["section"], d["title"], d["method"], d["detail"], d["flag"], d["difficulty"],
                  d["tags"]).lastrowid
    for f in d["files"]:
        conn.execute("INSERT INTO materials(entry_id, kind, file_id, text, file_name) VALUES(?,?,?,?,?)",
                     (eid, f["kind"], f["file_id"], f["text"], f.get("name")))
    conn.commit()
    await state.clear()
    await cb.answer("✅ Saqlandi")
    await drop_message(cb)
    await send_entry(cb.bot, cb.from_user.id, eid)


# ───────────────────────────── admin: tahrirlash ─────────────────────────────

def edit_view(eid):
    e = q1("SELECT * FROM entries WHERE id=?", eid)
    if not e:
        return "Topilmadi.", kb([("◀️ Orqaga", "home")])
    method = METHODS.get(e["method"], "—") + (f": {esc(e['method_detail'])}" if e["method_detail"] else "")
    text = (f"✏️ <b>{esc(e['title'])}</b>\n\n🔧 {method}\n🚩 <code>{esc(e['flag'] or '—')}</code>\n"
            f"🎯 {DIFFICULTY.get(e['difficulty'], '—')}\n🏷 {esc(tags_line(e['tags']) or '—')}\n\nNimani o'zgartiramiz?")
    return text, kb([("📝 Nom", f"ef:{eid}:title"), ("🔧 Usul", f"ef:{eid}:method")],
                    [("🚩 Flag", f"ef:{eid}:flag"), ("🎯 Qiyinlik", f"ef:{eid}:difficulty")],
                    [("🏷 Teglar", f"ef:{eid}:tags"), ("📎 Materiallar", f"mt:{eid}")],
                    [("◀️ Orqaga", f"e:{eid}")])


def materials_view(eid):
    mats = q("SELECT * FROM materials WHERE entry_id=? ORDER BY id", eid)
    rows = []
    for i, mat in enumerate(mats, 1):
        base = (mat["file_name"] or mat["text"] or mat["kind"]).replace("\n", " ")
        rows.append([(f"{i}. {MATERIAL_ICONS.get(mat['kind'], '📎')} {base[:28]}", f"mte:{mat['id']}"),
                     ("🗑", f"mtd:{mat['id']}")])
    rows.append([("➕ Qo'shish", f"addmat:{eid}")])
    rows.append([("◀️ Orqaga", f"edit:{eid}")])
    text = ("📎 <b>Materiallar</b>\nNomini bossangiz matn/izohini o'zgartirasiz, 🗑 o'chiradi." if mats
            else "📎 Materiallar yo'q.")
    return text, kb(*rows)


@admin.callback_query(F.data.startswith("edit:"))
async def ed_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, *edit_view(int(cb.data.split(":")[1])))
    await cb.answer()


@admin.callback_query(F.data.startswith("ef:"))
async def ed_field(cb: CallbackQuery, state: FSMContext):
    _, eid, field = cb.data.split(":")
    eid = int(eid)
    e = q1("SELECT * FROM entries WHERE id=?", eid)
    back = [("◀️ Orqaga", f"edit:{eid}")]
    await cb.answer()
    if field == "method":
        return await show(cb, "🔧 Yangi usulni tanlang:", kb([(v, f"efm:{eid}:{k}") for k, v in METHODS.items()], back))
    if field == "difficulty":
        items = list(DIFFICULTY.items())
        return await show(cb, "🎯 Qiyinlik darajasi:", kb(
            [(v, f"efd:{eid}:{k}") for k, v in items[:2]], [(v, f"efd:{eid}:{k}") for k, v in items[2:]],
            [("❌ Olib tashlash", f"efd:{eid}:none")], back))
    labels = {"title": "📝 Nom", "flag": "🚩 Flag", "tags": "🏷 Teglar"}
    current = {"title": e["title"], "flag": e["flag"] or "", "tags": tags_line(e["tags"])}[field]
    hint = "\n<i>Tozalash uchun - yozing. Vergul bilan ajrating.</i>" if field == "tags" else ""
    await state.set_state(Edit.value)
    await state.update_data(eid=eid, field=field)
    await show(cb, f"{labels[field]}\nHozir: <code>{esc(current or '—')}</code>\n\nYangisini yozing:{hint}", kb(back))


@admin.callback_query(F.data.startswith("efd:"))
async def ed_difficulty(cb: CallbackQuery):
    _, eid, key = cb.data.split(":")
    run("UPDATE entries SET difficulty=? WHERE id=?", key if key in DIFFICULTY else None, int(eid))
    await show(cb, *edit_view(int(eid)))
    await cb.answer("✅ Saqlandi")


@admin.callback_query(F.data.startswith("efm:"))
async def ed_method(cb: CallbackQuery, state: FSMContext):
    _, eid, key = cb.data.split(":")
    eid = int(eid)
    run("UPDATE entries SET method=? WHERE id=?", key, eid)
    await state.set_state(Edit.value)
    await state.update_data(eid=eid, field="method_detail")
    await show(cb, f"{METHODS[key]} tanlandi.\n\nQaysi / nima ekanini yozing (masalan: nmap). Tozalash uchun - yozing:",
               kb([("⏭ O'zgarishsiz qoldirish", f"edit:{eid}")]))
    await cb.answer("✅ Saqlandi")


@admin.callback_query(F.data.startswith("mt:"))
async def ed_materials(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, *materials_view(int(cb.data.split(":")[1])))
    await cb.answer()


@admin.callback_query(F.data.startswith("mte:"))
async def ed_material_text(cb: CallbackQuery, state: FSMContext):
    mid = int(cb.data.split(":")[1])
    mat = q1("SELECT * FROM materials WHERE id=?", mid)
    if not mat:
        return await cb.answer("Topilmadi", show_alert=True)
    await state.set_state(Edit.value)
    await state.update_data(eid=mat["entry_id"], field="mat", mid=mid)
    hint = "" if mat["kind"] == "text" else "\n<i>Izohni tozalash uchun - yozing.</i>"
    await show(cb, f"✏️ Materialning matni/izohi\nHozir: <code>{esc((mat['text'] or '—')[:500])}</code>\n\n"
                   f"Yangisini yozing:{hint}", kb([("◀️ Orqaga", f"mt:{mat['entry_id']}")]))
    await cb.answer()


@admin.callback_query(F.data.startswith("mtd:"))
async def ed_material_delete(cb: CallbackQuery):
    mat = q1("SELECT entry_id FROM materials WHERE id=?", int(cb.data.split(":")[1]))
    if not mat:
        return await cb.answer("Topilmadi", show_alert=True)
    mid = cb.data.split(":")[1]
    await show(cb, "🗑 Bu materialni o'chiramizmi?",
               kb([("✅ Ha", f"mtdc:{mid}"), ("❌ Yo'q", f"mt:{mat['entry_id']}")]))
    await cb.answer()


@admin.callback_query(F.data.startswith("mtdc:"))
async def ed_material_delete_confirm(cb: CallbackQuery):
    mat = q1("SELECT entry_id FROM materials WHERE id=?", int(cb.data.split(":")[1]))
    if mat:
        run("DELETE FROM materials WHERE id=?", int(cb.data.split(":")[1]))
        await show(cb, *materials_view(mat["entry_id"]))
    await cb.answer("🗑 O'chirildi")


@admin.message(Edit.value, F.text)
async def ed_value(m: Message, state: FSMContext):
    d = await state.get_data()
    eid, field, val = d["eid"], d["field"], m.text.strip()
    if field == "mat":
        mat = q1("SELECT kind FROM materials WHERE id=?", d["mid"])
        if val == "-" and mat and mat["kind"] != "text":
            val = None
        elif val == "-":
            return await show(m, "⚠️ Matnli materialni bo'sh qoldirib bo'lmaydi.", kb([("◀️ Orqaga", f"mt:{eid}")]))
        elif mat and mat["kind"] != "text":
            val = val[:1024]
        run("UPDATE materials SET text=? WHERE id=?", val, d["mid"])
        await state.clear()
        return await show(m, *materials_view(eid))
    if field == "tags":
        run("UPDATE entries SET tags=? WHERE id=?", tags_to_db([] if val == "-" else parse_tags(val)), eid)
    elif field == "method_detail":
        run("UPDATE entries SET method_detail=? WHERE id=?", None if val == "-" else val[:300], eid)
    elif field == "title":
        run("UPDATE entries SET title=? WHERE id=?", val[:200], eid)
    elif field == "flag":
        run("UPDATE entries SET flag=? WHERE id=?", val, eid)
    await state.clear()
    await show(m, *edit_view(eid))


@admin.message(Edit.value)
async def ed_value_other(m: Message, state: FSMContext):
    d = await state.get_data()
    back = f"mt:{d['eid']}" if d.get("field") == "mat" else f"edit:{d['eid']}"
    await show(m, "⚠️ Matn yuboring.", kb([("◀️ Orqaga", back)]))


# ───────────────────────────── admin: ruxsatlar ─────────────────────────────

def grant_sections_view(d):
    rows = [[(("✅ " if s in d["sections"] else "⬜ ") + label, f"gp:s:{s}")] for s, label in SECTIONS.items()]
    rows.append([("➡️ Davom etish", "gp:next")])
    rows.append([("◀️ Bekor qilish", "adm:panel")])
    return f"👤 <b>{esc(who(d['t_uid'], d['t_username']))}</b>\n\nQaysi bo'limlarga ruxsat beramiz?", kb(*rows)


def grant_projects_view(d):
    rows = [[(("✅ " if p["id"] in d["projects"] else "⬜ ") + p["name"], f"gp:p:{p['id']}")] for p in all_projects()]
    rows.append([(("✅ " if d["all"] else "⬜ ") + "🌐 Hammasi (yangi loyihalar ham)", "gp:all")])
    rows.append([("💾 Saqlash", "gp:save")])
    rows.append([("◀️ Orqaga", "gp:sec")])
    return f"👤 <b>{esc(who(d['t_uid'], d['t_username']))}</b>\n\nQaysi loyihalarni ko'rsatamiz?", kb(*rows)


async def start_pick(ev, state: FSMContext, uid, username):
    g = find_grant(uid, username)
    await state.clear()
    await state.set_state(Grant.pick)
    await state.update_data(
        t_uid=uid, t_username=username,
        sections=g["sections"] if g else [],
        projects=[p for p in g["projects"] if p != "*"] if g else [],
        all="*" in g["projects"] if g else False)
    await show(ev, *grant_sections_view(await state.get_data()))


@admin.callback_query(F.data == "adm:grant")
async def adm_grant(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Grant.target)
    await show(cb, "👤 Foydalanuvchi <b>ID</b> yoki <b>@username</b> yuboring:", kb([("◀️ Orqaga", "adm:panel")]))
    await cb.answer()


@admin.callback_query(F.data.startswith("gu:"))
async def adm_grant_from_request(cb: CallbackQuery, state: FSMContext):
    uid = int(cb.data.split(":")[1])
    user = q1("SELECT username FROM users WHERE user_id=?", uid)
    await start_pick(cb.message, state, uid, user["username"] if user else None)
    await cb.answer()


@admin.message(Grant.target, F.text)
async def grant_target(m: Message, state: FSMContext):
    raw = re.sub(r"^(https?://)?(t\.me/)?", "", m.text.strip()).lstrip("@")
    if raw.isdigit():
        return await start_pick(m, state, int(raw), None)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", raw):
        return await show(m, "⚠️ ID (raqam) yoki to'g'ri @username yuboring:", kb([("◀️ Orqaga", "adm:panel")]))
    known = q1("SELECT user_id FROM users WHERE username=?", raw.lower())
    await start_pick(m, state, known["user_id"] if known else None, raw.lower())


@admin.callback_query(Grant.pick, F.data.startswith("gp:s:"))
async def gp_toggle_section(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    sec = cb.data.split(":")[2]
    secs = [s for s in d["sections"] if s != sec] if sec in d["sections"] else d["sections"] + [sec]
    await state.update_data(sections=secs)
    await show(cb, *grant_sections_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Grant.pick, F.data == "gp:next")
async def gp_next(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d["sections"]:
        return await cb.answer("Kamida bitta bo'lim tanlang", show_alert=True)
    if not all_projects():
        return await cb.answer("Avval loyiha qo'shing", show_alert=True)
    await show(cb, *grant_projects_view(d))
    await cb.answer()


@admin.callback_query(Grant.pick, F.data == "gp:sec")
async def gp_back_sections(cb: CallbackQuery, state: FSMContext):
    await show(cb, *grant_sections_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Grant.pick, F.data.startswith("gp:p:"))
async def gp_toggle_project(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    pid = int(cb.data.split(":")[2])
    projects = [p for p in d["projects"] if p != pid] if pid in d["projects"] else d["projects"] + [pid]
    await state.update_data(projects=projects, all=False)
    await show(cb, *grant_projects_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Grant.pick, F.data == "gp:all")
async def gp_toggle_all(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(all=not d["all"], projects=[])
    await show(cb, *grant_projects_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Grant.pick, F.data == "gp:save")
async def gp_save(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d["all"] and not d["projects"]:
        return await cb.answer("Kamida bitta loyiha tanlang", show_alert=True)
    rows = [[(label, f"gd:{days}")] for days, label in DURATIONS]
    rows.append([("◀️ Orqaga", "gp:pj")])
    await show(cb, f"👤 <b>{esc(who(d['t_uid'], d['t_username']))}</b>\n\n⏳ Ruxsat qancha muddatga?", kb(*rows))
    await cb.answer()


@admin.callback_query(Grant.pick, F.data == "gp:pj")
async def gp_back_projects(cb: CallbackQuery, state: FSMContext):
    await show(cb, *grant_projects_view(await state.get_data()))
    await cb.answer()


@admin.callback_query(Grant.pick, F.data.startswith("gd:"))
async def gp_duration(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    days = int(cb.data.split(":")[1])
    expires = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds") if days else None
    projects = ["*"] if d["all"] else d["projects"]
    save_grant(d["t_uid"], d["t_username"], d["sections"], projects, expires)
    await state.clear()
    if d["t_uid"]:
        try:
            await cb.bot.send_message(d["t_uid"], f"✅ Sizga ruxsat berildi!\n{expiry_text(expires)}",
                                      reply_markup=kb([("📂 Ochish", "home")]))
            note = "Foydalanuvchiga xabar yuborildi."
        except TelegramAPIError:
            note = "⚠️ Xabar yuborib bo'lmadi (u botni hali /start qilmagan)."
    else:
        note = "ℹ️ Foydalanuvchi botga /start bosganda ruxsat kuchga kiradi."
    names = "hammasi" if d["all"] else ", ".join(project_name(p) for p in projects)
    await show(cb, f"✅ <b>{esc(who(d['t_uid'], d['t_username']))}</b> uchun saqlandi\n"
                   f"🗂 {', '.join(SECTIONS[s] for s in d['sections'])}\n📁 {esc(names)}\n{expiry_text(expires)}\n\n{note}",
               kb([("◀️ Admin panel", "adm:panel")]))
    await cb.answer()


@admin.callback_query(F.data == "adm:users")
async def adm_users(cb: CallbackQuery):
    grants = [_grant_dict(r) for r in q("SELECT * FROM grants ORDER BY id")]
    rows = [[(f"👤 {who(g['user_id'], g['username'])}" + (" ❌" if is_expired(g["expires_at"]) else
                                                             " ⏳" if g["expires_at"] else ""), f"adm:u:{g['id']}")]
            for g in grants]
    rows.append([("◀️ Orqaga", "adm:panel")])
    await show(cb, "👥 <b>Ruxsat berilganlar</b>" if grants else "👥 Hali hech kimga ruxsat berilmagan.", kb(*rows))
    await cb.answer()


@admin.callback_query(F.data.startswith("adm:u:"))
async def adm_user(cb: CallbackQuery):
    g = _grant_dict(q1("SELECT * FROM grants WHERE id=?", int(cb.data.split(":")[2])))
    if not g:
        return await cb.answer("Topilmadi", show_alert=True)
    names = "hammasi" if "*" in g["projects"] else ", ".join(project_name(p) for p in g["projects"])
    await show(cb, f"👤 <b>{esc(who(g['user_id'], g['username']))}</b>\n"
                   f"🗂 {', '.join(SECTIONS[s] for s in g['sections'])}\n📁 {esc(names)}\n{expiry_text(g['expires_at'])}",
               kb([("✏️ O'zgartirish", f"adm:ge:{g['id']}"), ("🚫 Olib tashlash", f"adm:gr:{g['id']}")],
                  [("◀️ Orqaga", "adm:users")]))
    await cb.answer()


@admin.callback_query(F.data.startswith("adm:ge:"))
async def adm_grant_edit(cb: CallbackQuery, state: FSMContext):
    g = _grant_dict(q1("SELECT * FROM grants WHERE id=?", int(cb.data.split(":")[2])))
    await start_pick(cb, state, g["user_id"], g["username"])
    await cb.answer()


@admin.callback_query(F.data.startswith("adm:gr:"))
async def adm_grant_revoke(cb: CallbackQuery):
    run("DELETE FROM grants WHERE id=?", int(cb.data.split(":")[2]))
    await adm_users(cb)


@admin.callback_query(F.data.startswith("rp:"))
async def adm_reply_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Reply.text)
    await state.update_data(target=int(cb.data.split(":")[1]))
    await show(cb.message, "✍️ Javobingizni yozing (matn, rasm yoki fayl):", kb([("◀️ Bekor qilish", "adm:panel")]))
    await cb.answer()


@admin.message(Reply.text)
async def adm_reply_send(m: Message, state: FSMContext):
    target = (await state.get_data())["target"]
    try:
        head = await m.bot.send_message(target, "💬 <b>Admin javobi:</b>")
        copy = await m.copy_to(target, reply_markup=kb([("✍️ Adminga yozish", "contact")]))
    except TelegramAPIError:
        return await show(m, "⚠️ Yuborib bo'lmadi (foydalanuvchi botni bloklagan bo'lishi mumkin).",
                          kb([("◀️ Admin panel", "adm:panel")]))
    track(target, head.message_id, copy.message_id)
    await state.clear()
    await show(m, "✅ Javob yuborildi.", kb([("◀️ Admin panel", "adm:panel")]))


# ───────────────────────────── admin: statistika, kirish tarixi ─────────────────────────────

def stats_view():
    pr, en, fl = counts()
    per_sec = {r["section"]: r["c"] for r in q("SELECT section, COUNT(*) c FROM entries GROUP BY section")}
    lines = ["📊 <b>Statistika</b>", "",
             f"📁 Loyihalar: <b>{pr}</b> · 🗂 Ishlar: <b>{en}</b> · 📎 Fayllar: <b>{fl}</b>",
             " · ".join(f"{label} <b>{per_sec.get(s, 0)}</b>" for s, label in SECTIONS.items())]
    diffs = {r["difficulty"]: r["c"] for r in q(
        "SELECT difficulty, COUNT(*) c FROM entries WHERE difficulty IS NOT NULL GROUP BY difficulty")}
    if diffs:
        lines.append("🎯 " + " · ".join(f"{DIFFICULTY[k]} <b>{diffs[k]}</b>" for k in DIFFICULTY if k in diffs))
    methods = {r["method"]: r["c"] for r in q("SELECT method, COUNT(*) c FROM entries WHERE method IS NOT NULL GROUP BY method")}
    if methods:
        lines.append("🔧 " + " · ".join(f"{METHODS[k]} <b>{methods[k]}</b>" for k in METHODS if k in methods))
    tag_count = {}
    for r in q("SELECT tags FROM entries WHERE tags IS NOT NULL"):
        for t in tags_from_db(r["tags"]):
            tag_count[t] = tag_count.get(t, 0) + 1
    top = sorted(tag_count.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    if top:
        lines.append("🏷 " + ", ".join(f"#{esc(t)} ({n})" for t, n in top))
    grants = [_grant_dict(r) for r in q("SELECT * FROM grants")]
    expired = sum(1 for g in grants if is_expired(g["expires_at"]))
    lines.append(f"👥 Ruxsatlilar: <b>{len(grants) - expired}</b> faol" + (f", {expired} tugagan" if expired else ""))
    v = q1("SELECT COUNT(*) c, COUNT(DISTINCT user_id) u FROM access_log WHERE ts >= datetime('now', '-7 days')")
    lines.append(f"👁 Oxirgi 7 kun: <b>{v['c']}</b> ta ochish, <b>{v['u']}</b> foydalanuvchi")
    projects = all_projects()
    if projects:
        lines.append("")
        for pr_row in projects[:12]:
            cnt = {r["section"]: r["c"] for r in q(
                "SELECT section, COUNT(*) c FROM entries WHERE project_id=? GROUP BY section", pr_row["id"])}
            lines.append(f"📁 {esc(pr_row['name'])} — " + " · ".join(
                f"{label.split()[0]} {cnt.get(s, 0)}" for s, label in SECTIONS.items()))
    recent = q("SELECT title, project_id, created_at FROM entries ORDER BY id DESC LIMIT 5")
    if recent:
        lines += ["", "🆕 <b>Oxirgilari</b>"]
        lines += [f"• {r['created_at'][:10]} {esc(r['title'][:40])} ({esc(project_name(r['project_id']))})" for r in recent]
    return "\n".join(lines), kb([("📈 Grafik", "adm:stats")], [("◀️ Orqaga", "adm:panel")])


def activity_data(days):
    """Oxirgi `days` kun uchun har kuni nechta ish/loyiha qo'shilgani (bot vaqti bo'yicha)."""
    today = datetime.now().date()
    day_list = [today - timedelta(days=days - 1 - i) for i in range(days)]
    ent = {date.fromisoformat(r["d"]): r["c"] for r in q(
        "SELECT date(created_at, 'localtime') d, COUNT(*) c FROM entries GROUP BY d")}
    projects = [(r["name"], date.fromisoformat(r["d"])) for r in q(
        "SELECT name, date(created_at, 'localtime') d FROM projects WHERE created_at IS NOT NULL ORDER BY created_at")]
    per_project_day = {}
    for _, d in projects:
        per_project_day[d] = per_project_day.get(d, 0) + 1
    counts = [ent.get(d, 0) + per_project_day.get(d, 0) for d in day_list]
    return day_list, counts, projects, sum(ent.get(d, 0) for d in day_list)


def streaks(counts):
    longest = run_len = 0
    for c in counts:
        run_len = run_len + 1 if c else 0
        longest = max(longest, run_len)
    tail = counts[:-1] if counts and counts[-1] == 0 else counts  # bugun hali bo'sh bo'lsa seriya uzilmaydi
    current = 0
    for c in reversed(tail):
        if not c:
            break
        current += 1
    return current, longest


def render_activity_chart(day_list, counts, projects):
    """Faol kun (yashil, ustun balandligi = qo'shilgan soni) va nofaol kun (qizil, past chiziq) grafigi. PNG bayt."""
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    import matplotlib.dates as mdates
    from matplotlib.ticker import MaxNLocator

    green, red = "#0ca30c", "#d03b3b"  # dataviz: status good / critical
    surface, ink, ink2, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e3e2dd"
    days = len(day_list)
    start, today = day_list[0], day_list[-1]
    active = [(d, c) for d, c in zip(day_list, counts) if c]
    idle = [d for d, c in zip(day_list, counts) if not c]
    marks = [(n, d) for n, d in projects if start <= d <= today]

    fig = Figure(figsize=(9, 4.8), dpi=160, facecolor=surface)
    ax = fig.subplots()
    ax.set_facecolor(surface)
    ymax = max([c for _, c in active] + [3])
    top = ymax * (1.7 if marks else 1.25)
    ax.set_ylim(0, top)
    midnight = datetime.min.time()
    ax.set_xlim(datetime.combine(start, midnight) - timedelta(hours=14),
                datetime.combine(today, midnight) + timedelta(hours=14))
    ax.bar(idle, [0.3] * len(idle), width=0.72, color=red, edgecolor=surface, linewidth=0.8, zorder=3)
    ax.bar([d for d, _ in active], [c for _, c in active], width=0.72, color=green, edgecolor=surface,
           linewidth=0.8, zorder=3)
    for name, d in marks:
        ax.axvline(d, color=ink2, linestyle=(0, (3, 3)), linewidth=1, zorder=2)
        ax.text(d, top * 0.985, name[:14] + " ", rotation=90, ha="right", va="top", fontsize=8, color=ink2)
    ax.grid(axis="y", color=grid, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=1 if days <= 7 else 3 if days <= 30 else 10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.tick_params(colors=ink2, labelsize=8.5, length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(grid)
    ax.set_ylabel("Qo'shilgan (ish/loyiha)", color=ink2, fontsize=9)
    ax.set_title(f"Faollik: oxirgi {days} kun", loc="left", fontsize=13, fontweight="bold", color=ink, pad=26)
    ax.text(0, 1.03, f"Faol: {len(active)} kun · Nofaol: {len(idle)} kun", transform=ax.transAxes, fontsize=9,
            color=ink2)
    handles = [Patch(facecolor=green, label="Faol kun (ustun balandligi = qo'shilgan soni)"),
               Patch(facecolor=red, label="Nofaol kun (hech narsa qo'shilmagan)")]
    if marks:
        handles.append(Line2D([0], [0], color=ink2, linestyle=(0, (3, 3)), linewidth=1, label="Loyiha qo'shilgan sana"))
    ax.legend(handles=handles, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=len(handles),
              fontsize=8.5, labelcolor=ink2, handlelength=1.4, columnspacing=1.6)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=surface)
    return buf.getvalue()


async def show_photo(cb: CallbackQuery, photo: bytes, caption, markup):
    """Rasmli ekran: oldingi xabarlar o'chadi, bitta rasm qoladi."""
    uid = cb.from_user.id
    await purge(cb.bot, uid)
    await drop_message(cb)
    sent = await cb.bot.send_photo(uid, BufferedInputFile(photo, filename="faollik.png"), caption=caption,
                                   reply_markup=markup)
    track(uid, sent.message_id)


@admin.callback_query(F.data.startswith("adm:stats"))
async def adm_stats(cb: CallbackQuery):
    parts = cb.data.split(":")
    days = int(parts[2]) if len(parts) > 2 and parts[2] in ("7", "30", "90") else 30
    await cb.answer()
    day_list, counts, projects, added = activity_data(days)
    try:
        png = await asyncio.to_thread(render_activity_chart, day_list, counts, projects)
    except Exception:
        log.exception("grafik chizilmadi")
        return await show(cb, *stats_view())
    current, longest = streaks(counts)
    active = sum(1 for c in counts if c)
    lines = [f"📈 <b>Faollik: oxirgi {days} kun</b>",
             f"🟢 Faol kunlar: <b>{active}</b> · 🔴 Nofaol: <b>{days - active}</b>",
             f"🔥 Joriy seriya: <b>{current}</b> kun · Eng uzun: <b>{longest}</b> kun",
             f"➕ Qo'shilgan ishlar: <b>{added}</b>"]
    if projects:
        lines += ["", "📁 <b>Loyihalar (qo'shilgan sana)</b>"]
        lines += [f"• {esc(n[:30])}: {d:%d.%m.%Y}" for n, d in projects[:8]]
        if len(projects) > 8:
            lines.append(f"… va yana {len(projects) - 8} ta")
    markup = kb([(("• " if d == days else "") + f"{d} kun", f"adm:stats:{d}") for d in (7, 30, 90)],
                [("📋 Matnli statistika", "adm:stt")], [("◀️ Orqaga", "adm:panel")])
    await show_photo(cb, png, "\n".join(lines), markup)


@admin.callback_query(F.data == "adm:stt")
async def adm_stats_text(cb: CallbackQuery):
    await show(cb, *stats_view())
    await cb.answer()


def log_view(page):
    total = q1("SELECT COUNT(*) c FROM access_log")["c"]
    pages = max(1, -(-total // LOG_PAGE))
    page = min(max(page, 0), pages - 1)
    items = q("SELECT strftime('%d.%m %H:%M', ts, 'localtime') t, user_id, label FROM access_log "
              "ORDER BY id DESC LIMIT ? OFFSET ?", LOG_PAGE, page * LOG_PAGE)
    rows = []
    if pages > 1:
        rows.append([("⬅️", f"adm:log:{page - 1}"), (f"{page + 1}/{pages}", "noop"), ("➡️", f"adm:log:{page + 1}")])
    rows.append([("◀️ Orqaga", "adm:panel")])
    if not items:
        return "🛡 Hali hech kim biror ish ochmagan.", kb(*rows)
    body = "\n".join(f"<code>{r['t']}</code> {esc(who(r['user_id'], None))}\n   └ {esc((r['label'] or '')[:90])}"
                     for r in items)
    return f"🛡 <b>Kirish tarixi</b> ({total})\n\n{body}", kb(*rows)


@admin.callback_query(F.data.startswith("adm:log:"))
async def adm_log(cb: CallbackQuery):
    await show(cb, *log_view(int(cb.data.split(":")[2])))
    await cb.answer()


# ───────────────────────────── admin: backup / tiklash ─────────────────────────────

busy = asyncio.Lock()  # backup yoki tiklash paytida ikkinchisi boshlanmasligi uchun


def counts():
    return (q1("SELECT COUNT(*) c FROM projects")["c"], q1("SELECT COUNT(*) c FROM entries")["c"],
            q1("SELECT COUNT(*) c FROM materials WHERE file_id IS NOT NULL")["c"])


def size_str(n):
    return f"{n / 1024 / 1024:.1f} MB"


def local_backups():
    files = [f for f in BACKUP_DIR.glob("*.zip") if f.name.startswith(("backup_", "uploaded_"))] if BACKUP_DIR.exists() else []
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:8]


def read_backup_info(path):
    try:
        with zipfile.ZipFile(path) as zf:
            data = json.loads(zf.read("backup.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise ValueError("bu backup fayli emas") from e
    if data.get("version") != BACKUP_VERSION or not all(k in data for k in ("projects", "entries", "materials", "grants")):
        raise ValueError("backup formati mos emas")
    return {"created": data.get("created", "?"), "projects": len(data["projects"]), "entries": len(data["entries"]),
            "files": sum(1 for m in data["materials"] if m.get("file"))}


async def make_backup(bot: Bot, progress, prefix="backup"):
    """Bazani va barcha fayllarning o'zini ZIP ga yig'adi (yangi botda ham tiklash uchun)."""
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"{prefix}_{datetime.now():%Y-%m-%d_%H-%M-%S}.zip"
    mats = [dict(r) for r in q("SELECT * FROM materials ORDER BY id")]
    lost = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, mat in enumerate(mats, 1):
            file_id, file_name = mat.pop("file_id"), mat.pop("file_name")
            mat["file"], mat["name"] = None, file_name
            if file_id:
                try:
                    f = await bot.get_file(file_id)
                    buf = io.BytesIO()
                    await bot.download_file(f.file_path, destination=buf)
                    ext = Path(f.file_path or "").suffix or FILE_EXT.get(mat["kind"], "")
                    mat["name"] = file_name or f"file_{mat['id']}{ext}"
                    mat["file"] = f"files/{mat['id']}"
                    zf.writestr(mat["file"], buf.getvalue())
                except TelegramAPIError as e:
                    log.warning("backup: fayl olinmadi (material %s): %s", mat["id"], e)
                    lost += 1
            await progress(i, len(mats))
        data = {
            "version": BACKUP_VERSION, "created": datetime.now().isoformat(timespec="seconds"),
            "projects": [dict(r) for r in q("SELECT * FROM projects ORDER BY id")],
            "entries": [dict(r) for r in q("SELECT * FROM entries ORDER BY id")],
            "materials": mats,
            "grants": [dict(r) for r in q("SELECT * FROM grants ORDER BY id")],
            "users": [dict(r) for r in q("SELECT * FROM users")],
        }
        zf.writestr("backup.json", json.dumps(data, ensure_ascii=False, indent=1))
    return path, len(mats), lost


async def reupload(bot: Bot, kind, data, name):
    """Faylni yuklab, Telegram bergan yangi file_id ni oladi (xabar darhol o'chiriladi)."""
    for _ in range(3):
        try:
            msg = await getattr(bot, f"send_{kind}")(ADMIN_ID, **{kind: BufferedInputFile(data, filename=name)},
                                                     disable_notification=True)
            break
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
    else:
        raise TelegramAPIError(method=None, message="flood limit")
    obj = getattr(msg, kind)
    file_id = obj[-1].file_id if kind == "photo" else obj.file_id
    try:
        await bot.delete_message(ADMIN_ID, msg.message_id)
    except TelegramAPIError:
        pass
    return file_id


async def restore_backup(bot: Bot, path, progress):
    with zipfile.ZipFile(path) as zf:
        data = json.loads(zf.read("backup.json"))
        mats = data["materials"]
        new_ids, failed = {}, 0
        for i, mat in enumerate(mats, 1):
            if mat.get("file"):
                try:
                    new_ids[mat["id"]] = await reupload(bot, mat["kind"], zf.read(mat["file"]), mat.get("name") or "file")
                except TelegramAPIError as e:
                    log.warning("tiklash: fayl yuklanmadi (material %s): %s", mat["id"], e)
                    failed += 1
            await progress(i, len(mats))

    # hozirgi holatning xavfsizlik nusxasi, keyin butunlay almashtirish
    BACKUP_DIR.mkdir(exist_ok=True)
    safety = sqlite3.connect(BACKUP_DIR / f"pre_restore_{datetime.now():%Y-%m-%d_%H-%M-%S}.db")
    conn.backup(safety)
    safety.close()

    rows = []
    for mat in mats:
        if mat["id"] in new_ids:
            rows.append((mat["id"], mat["entry_id"], mat["kind"], new_ids[mat["id"]], mat["text"], mat.get("name")))
        elif mat["kind"] == "text":
            rows.append((mat["id"], mat["entry_id"], "text", None, mat["text"], None))
        else:
            label = mat.get("name") or mat["kind"]
            rows.append((mat["id"], mat["entry_id"], "text", None, f"⚠️ Fayl tiklanmadi: {label}", None))
            if not mat.get("file"):
                failed += 1
    with conn:
        conn.execute("DELETE FROM projects")
        conn.execute("DELETE FROM grants")
        conn.executemany("INSERT INTO projects(id, name, created_at) VALUES(:id, :name, :created_at)",
                         [{"created_at": None, **pr} for pr in data["projects"]])
        conn.executemany(
            "INSERT INTO entries(id, project_id, section, title, method, method_detail, flag, difficulty, tags, "
            "created_at) VALUES(:id, :project_id, :section, :title, :method, :method_detail, :flag, :difficulty, "
            ":tags, :created_at)", [{"difficulty": None, "tags": None, **e} for e in data["entries"]])
        conn.executemany("INSERT INTO materials(id, entry_id, kind, file_id, text, file_name) VALUES(?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO grants(user_id, username, sections, projects, expires_at) "
                         "VALUES(:user_id, :username, :sections, :projects, :expires_at)",
                         [{"expires_at": None, **g} for g in data["grants"]])
        conn.executemany(
            "INSERT INTO users(user_id, username, full_name) VALUES(:user_id, :username, :full_name) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
            data.get("users", []))
    return len(new_ids), failed


def progress_editor(cb: CallbackQuery, title):
    async def progress(i, n):
        if n and (i == n or i % 5 == 0):
            try:
                await cb.message.edit_text(f"{title}\n\n{i}/{n} fayl…")
            except TelegramAPIError:
                pass
    return progress


def backup_menu_view():
    pr, en, fl = counts()
    mode = get_setting("auto_backup", "daily")
    last = get_setting("auto_backup_last")
    last_txt = f"\n🕒 Oxirgi avto-backup: {datetime.fromisoformat(last):%d.%m.%Y %H:%M}" if last else ""
    text = (f"💾 <b>Backup</b>\n\nHozir: {pr} ta loyiha, {en} ta ish, {fl} ta fayl.\n\n"
            "📦 <b>Backup olish</b>: hamma narsa (fayllar bilan) bitta ZIP ga yig'iladi, sizga yuboriladi va "
            "kompyuterdagi <code>backups</code> papkasiga ham saqlanadi.\n"
            "♻️ <b>Tiklash</b>: ZIP ni botga yuborsangiz, hammasi qayta chiqadi. Yangi bot yoki tokenda ham ishlaydi.\n"
            f"🕒 <b>Avto-backup</b>: {AUTO_MODES[mode]} (bot ishlab turgan bo'lsa o'zi olib yuboradi, "
            f"oxirgi 7 tasi saqlanadi).{last_txt}")
    return text, kb([("📦 Backup olish", "bk:make")], [("♻️ Tiklash", "bk:restore")],
                    [(f"🕒 Avto-backup: {AUTO_MODES[mode]}", "bk:auto")], [("◀️ Orqaga", "adm:panel")])


@admin.callback_query(F.data == "bk:menu")
async def bk_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, *backup_menu_view())
    await cb.answer()


@admin.callback_query(F.data == "bk:auto")
async def bk_auto(cb: CallbackQuery):
    order = list(AUTO_MODES)
    mode = get_setting("auto_backup", "daily")
    set_setting("auto_backup", order[(order.index(mode) + 1) % len(order)])
    await show(cb, *backup_menu_view())
    await cb.answer()


@admin.callback_query(F.data == "bk:make")
async def bk_make(cb: CallbackQuery):
    if busy.locked():
        return await cb.answer("Boshqa jarayon davom etmoqda", show_alert=True)
    async with busy:
        await cb.answer()
        title = "⏳ Backup olinmoqda…"
        await show(cb, title)
        try:
            path, total, lost = await make_backup(cb.bot, progress_editor(cb, title))
        except Exception as e:
            log.exception("backup xatosi")
            return await show(cb.message, f"⚠️ Backup olinmadi: {esc(str(e))}", kb([("◀️ Orqaga", "bk:menu")]))
        size = path.stat().st_size
        note = f"\n⚠️ {lost} ta fayl Telegramdan olinmadi (20 MB dan katta yoki o'chgan)." if lost else ""
        if size <= MAX_SEND:
            try:
                await cb.bot.send_document(ADMIN_ID, FSInputFile(path), caption=f"💾 Backup · {size_str(size)}")
                sent = "Fayl yuqorida yuborildi."
            except TelegramAPIError as e:
                sent = f"⚠️ Telegramga yuborib bo'lmadi ({esc(str(e))})."
        else:
            sent = "Fayl Telegram uchun juda katta (50 MB dan ko'p), faqat kompyuterda saqlandi."
        await show(cb.message, f"✅ <b>Backup tayyor</b> ({size_str(size)}, {total} ta material){note}\n{sent}\n\n"
                               f"📁 <code>{esc(str(path))}</code>", kb([("◀️ Orqaga", "bk:menu")]))


def restore_menu():
    files = local_backups()
    rows = [[(f"♻️ {f.name[:-4]} · {size_str(f.stat().st_size)}", f"bk:c:{f.name}")] for f in files]
    rows.append([("◀️ Orqaga", "bk:menu")])
    text = ("♻️ <b>Tiklash</b>\n\n📎 Backup <b>ZIP</b> faylini shu chatga yuboring (20 MB gacha).\n"
            "Fayl kattaroq bo'lsa, uni kompyuterdagi <code>backups</code> papkaga tashlang, quyida chiqadi.")
    return text + ("\n\nKompyuterdagi backuplar:" if files else ""), kb(*rows)


@admin.callback_query(F.data == "bk:restore")
async def bk_restore(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Backup.wait)
    await show(cb, *restore_menu())
    await cb.answer()


async def confirm_restore(ev, name, keep_user=False):
    info = read_backup_info(BACKUP_DIR / name)
    await show(ev, f"♻️ <b>{esc(name)}</b>\n📅 {esc(str(info['created']))}\n"
                   f"📁 {info['projects']} loyiha · 🗂 {info['entries']} ish · 📎 {info['files']} fayl\n\n"
                   "⚠️ Hozirgi barcha loyihalar va ruxsatlar o'chib, shu backupdagilar bilan almashtiriladi "
                   "(hozirgi holat nusxasi <code>backups</code> papkaga saqlanadi). Davom etamizmi?",
               kb([("✅ Ha, tiklash", f"bk:go:{name}")], [("❌ Yo'q", "bk:restore")]), keep_user=keep_user)


def valid_backup_name(name):
    return bool(re.fullmatch(r"[\w.\-]+\.zip", name)) and (BACKUP_DIR / name).is_file()


@admin.callback_query(F.data.startswith("bk:c:"))
async def bk_confirm(cb: CallbackQuery, state: FSMContext):
    name = cb.data[5:]
    if not valid_backup_name(name):
        return await cb.answer("Fayl topilmadi", show_alert=True)
    await state.clear()
    try:
        await confirm_restore(cb, name)
    except ValueError as e:
        return await cb.answer(f"⚠️ {e}", show_alert=True)
    await cb.answer()


@admin.message(Backup.wait, F.document)
async def bk_upload(m: Message, state: FSMContext):
    back = kb([("◀️ Orqaga", "bk:menu")])
    doc = m.document
    if not (doc.file_name or "").lower().endswith(".zip"):
        return await show(m, "⚠️ ZIP fayl yuboring.", back, keep_user=True)
    if doc.file_size and doc.file_size > MAX_RECEIVE:
        return await show(m, "⚠️ Fayl 20 MB dan katta, bot uni Telegramdan ola olmaydi. Uni kompyuterdagi "
                             "<code>backups</code> papkaga tashlang va ♻️ Tiklash ni qayta oching.", back, keep_user=True)
    BACKUP_DIR.mkdir(exist_ok=True)
    dest = BACKUP_DIR / f"uploaded_{datetime.now():%Y-%m-%d_%H-%M-%S}.zip"
    try:
        await m.bot.download(doc, destination=dest)
        await state.clear()
        await confirm_restore(m, dest.name, keep_user=True)
    except (TelegramAPIError, ValueError) as e:
        dest.unlink(missing_ok=True)
        await state.set_state(Backup.wait)
        await show(m, f"⚠️ Bu yaroqli backup emas: {esc(str(e))}", back, keep_user=True)


@admin.message(Backup.wait)
async def bk_upload_other(m: Message):
    await show(m, "⚠️ Backup ZIP faylini yuboring.", kb([("◀️ Orqaga", "bk:menu")]))


@admin.callback_query(F.data.startswith("bk:go:"))
async def bk_go(cb: CallbackQuery):
    name = cb.data[6:]
    if not valid_backup_name(name):
        return await cb.answer("Fayl topilmadi", show_alert=True)
    if busy.locked():
        return await cb.answer("Boshqa jarayon davom etmoqda", show_alert=True)
    async with busy:
        await cb.answer()
        title = "⏳ Tiklanmoqda… (botni o'chirmang)"
        await show(cb, title)
        try:
            restored, failed = await restore_backup(cb.bot, BACKUP_DIR / name, progress_editor(cb, title))
        except Exception as e:
            log.exception("tiklash xatosi")
            return await show(cb.message, f"⚠️ Tiklab bo'lmadi, hozirgi ma'lumotlar o'zgarmadi: {esc(str(e))}",
                              kb([("◀️ Orqaga", "bk:menu")]))
        pr, en, fl = counts()
        note = f"\n⚠️ {failed} ta fayl tiklanmadi (o'rniga izoh qo'yildi)." if failed else ""
        await show(cb.message, f"✅ <b>Tiklandi</b>\n📁 {pr} loyiha · 🗂 {en} ish · 📎 {restored} fayl{note}",
                   kb([("📂 Ochish", "home")]))


async def _no_progress(i, n):
    pass


async def auto_backup_once(bot: Bot):
    mode = get_setting("auto_backup", "daily")
    if mode == "off" or busy.locked() or counts()[1] == 0:
        return
    last = get_setting("auto_backup_last")
    if last and datetime.now() - datetime.fromisoformat(last) < timedelta(days=1 if mode == "daily" else 7):
        return
    async with busy:
        path, total, lost = await make_backup(bot, _no_progress, prefix="backup_auto")
        set_setting("auto_backup_last", datetime.now().isoformat(timespec="seconds"))
        size = path.stat().st_size
        note = f"\n⚠️ {lost} ta fayl olinmadi (20 MB dan katta yoki o'chgan)." if lost else ""
        try:
            if size <= MAX_SEND:
                await bot.send_document(ADMIN_ID, FSInputFile(path), caption=f"🕒 Avtomatik backup · {size_str(size)}{note}")
            else:
                await bot.send_message(ADMIN_ID, f"🕒 Avtomatik backup tayyor ({size_str(size)}), lekin Telegram uchun "
                                                 f"katta. Kompyuterda: <code>{esc(str(path))}</code>{note}")
        except TelegramAPIError as e:
            log.warning("avto-backup yuborilmadi: %s", e)
    old = sorted(BACKUP_DIR.glob("backup_auto_*.zip"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in old[7:]:
        f.unlink(missing_ok=True)


async def auto_backup_loop(bot: Bot):
    await asyncio.sleep(60)
    while True:
        try:
            await auto_backup_once(bot)
        except Exception:
            log.exception("avto-backup xatosi")
        await asyncio.sleep(600)


# ───────────────────────────── fallback ─────────────────────────────

@fallback.message()
async def fb_message(m: Message):
    await show(m, *home_view(m.from_user.id))


@fallback.callback_query()
async def fb_callback(cb: CallbackQuery):
    await cb.answer("Bu tugma eskirgan yoki sizga ruxsat yo'q", show_alert=True)


# ───────────────────────────── main ─────────────────────────────

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not BOT_TOKEN or not ADMIN_ID:
        sys.exit("BOT_TOKEN va ADMIN_ID ni .env faylga yozing (README ga qarang).")
    init_db()
    bot = Bot(BOT_TOKEN, session=AiohttpSession(timeout=600),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(Touch())
    dp.include_routers(common, admin, fallback)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Bot ishga tushdi: @%s", (await bot.get_me()).username)
    auto_task = asyncio.create_task(auto_backup_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        auto_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
