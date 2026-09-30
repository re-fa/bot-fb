import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import dotenv_values
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ═══════════════════════════════════════
# Settings
# ═══════════════════════════════════════
PROJECT_DIR = Path(__file__).resolve().parent
ROOT_DIR = PROJECT_DIR / "start"
DB_PATH = PROJECT_DIR / "database.sqlite3"
LEGACY_USERS = PROJECT_DIR / "users.json"

_env = dotenv_values(PROJECT_DIR / ".env")
BOT_TOKEN = _env.get("BOT_TOKEN")
ADMIN_ID = int(_env.get("ADMIN_ID") or 0)
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN غير موجود في ملف .env")

DEFAULT_MENU_TEXT = "اختر من القائمة:"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
AUDIO_EXTS = {".mp3", ".ogg", ".wav", ".m4a", ".opus"}
MAX_UPLOAD = 50 * 1024 * 1024
MAX_PHOTO = 10 * 1024 * 1024
CAPTION_LIMIT = 1000
TEXT_LIMIT = 4000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

user_locks = defaultdict(asyncio.Lock)


# ═══════════════════════════════════════
# Database (SQLite)
# ═══════════════════════════════════════
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                joined TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                interactions INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS interactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                at TEXT NOT NULL,
                kind TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_interactions_at ON interactions(at);
            CREATE TABLE IF NOT EXISTS file_cache(
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                file_id TEXT NOT NULL
            );
            """
        )
        empty = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if empty and LEGACY_USERS.exists():
            try:
                old = json.loads(LEGACY_USERS.read_text(encoding="utf-8"))
                for u in old:
                    conn.execute(
                        "INSERT OR IGNORE INTO users(id, username, first_name, joined, last_seen, interactions) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            u["id"],
                            u.get("username", ""),
                            u.get("first_name", ""),
                            u.get("joined", datetime.now().isoformat()),
                            u.get("last_seen", datetime.now().isoformat()),
                            u.get("requests", 0),
                        ),
                    )
                log.info("تم استيراد %d مستخدم من users.json", len(old))
            except Exception as e:
                log.warning("تعذر استيراد users.json: %s", e)


def register_user(user, kind):
    now = datetime.now().isoformat(timespec="seconds")
    with closing(db()) as conn, conn:
        conn.execute(
            """
            INSERT INTO users(id, username, first_name, joined, last_seen, interactions, blocked)
            VALUES(?,?,?,?,?,1,0)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen,
                interactions=interactions+1,
                blocked=0
            """,
            (user.id, user.username or "", user.first_name or "", now, now),
        )
        conn.execute(
            "INSERT INTO interactions(user_id, at, kind) VALUES(?,?,?)",
            (user.id, now, kind),
        )


def get_stats():
    now = datetime.now()
    today = now.date().isoformat()
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    with closing(db()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_today = conn.execute(
            "SELECT COUNT(*) FROM users WHERE last_seen LIKE ?", (today + "%",)
        ).fetchone()[0]
        today_i = conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE at LIKE ?", (today + "%",)
        ).fetchone()[0]
        week_i = conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE at >= ?", (week_ago,)
        ).fetchone()[0]
        last = conn.execute(
            "SELECT username, first_name FROM users ORDER BY joined DESC LIMIT 1"
        ).fetchone()
    if last:
        last_name = f"@{last['username']}" if last["username"] else (last["first_name"] or "—")
    else:
        last_name = "لا يوجد"
    return total, active_today, today_i, week_i, last_name


def all_user_ids():
    with closing(db()) as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM users")]


def mark_blocked(user_id):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE users SET blocked=1 WHERE id=?", (user_id,))


# ═══════════════════════════════════════
# file_id cache
# ═══════════════════════════════════════
def cache_key(path):
    return path.relative_to(ROOT_DIR).as_posix()


def cache_get(path):
    try:
        st = path.stat()
        with closing(db()) as conn:
            row = conn.execute(
                "SELECT file_id, size, mtime_ns FROM file_cache WHERE path=?",
                (cache_key(path),),
            ).fetchone()
        if row and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns:
            return row["file_id"]
    except OSError:
        pass
    return None


def cache_put(path, file_id):
    if not file_id:
        return
    try:
        st = path.stat()
        with closing(db()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO file_cache(path, size, mtime_ns, file_id) VALUES(?,?,?,?)",
                (cache_key(path), st.st_size, st.st_mtime_ns, file_id),
            )
    except OSError:
        pass


def cache_del(path):
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM file_cache WHERE path=?", (cache_key(path),))


# ═══════════════════════════════════════
# Paths and navigation
# ═══════════════════════════════════════
_token_index = {}


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def token_of(path):
    rel = path.relative_to(ROOT_DIR).as_posix()
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]


def inside_root(path):
    try:
        root = ROOT_DIR.resolve()
        return path.resolve() == root or root in path.resolve().parents
    except OSError:
        return False


def rebuild_index():
    _token_index.clear()
    for dirpath, dirnames, _ in os.walk(ROOT_DIR):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for d in dirnames:
            p = Path(dirpath) / d
            _token_index[token_of(p)] = p


def resolve_token(token):
    path = _token_index.get(token)
    if path is None or not path.is_dir():
        rebuild_index()
        path = _token_index.get(token)
    if path is None or path.is_symlink() or not path.is_dir() or not inside_root(path):
        return None
    return path


def list_subfolders(folder):
    try:
        subs = [
            Path(e.path)
            for e in os.scandir(folder)
            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
        ]
    except OSError:
        return []
    return sorted(subs, key=lambda p: natural_key(p.name))


def list_files(folder):
    out = []
    try:
        entries = sorted(folder.iterdir(), key=lambda p: natural_key(p.name))
    except OSError:
        return out
    for p in entries:
        name = p.name.lower()
        if not p.is_file() or name.startswith("."):
            continue
        if name in ("main.md", "link.txt"):
            continue
        if p.stem.lower() == "main" and p.suffix.lower() in IMAGE_EXTS:
            continue
        out.append(p)
    return out


def find_main_image(folder):
    for ext in IMAGE_EXTS:
        p = folder / f"main{ext}"
        if p.is_file():
            return p
    return None


def read_text(path):
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def folder_link(folder):
    f = folder / "link.txt"
    if not f.is_file():
        return None
    lines = read_text(f).splitlines()
    url = lines[0].strip() if lines else ""
    return url if re.match(r"^https?://\S+$", url, re.IGNORECASE) else None


def build_keyboard(folder, subs, is_root):
    rows = []
    for sub in subs:
        url = folder_link(sub)
        if url:
            rows.append([InlineKeyboardButton(sub.name, url=url)])
        else:
            rows.append([InlineKeyboardButton(sub.name, callback_data=f"p:{token_of(sub)}")])
    if not is_root:
        parent = folder.parent
        data = "home" if parent == ROOT_DIR else f"p:{token_of(parent)}"
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=data)])
    return InlineKeyboardMarkup(rows) if rows else None


# ═══════════════════════════════════════
# Text
# ═══════════════════════════════════════
def to_html(raw, user):
    label = f"@{user.username}" if user.username else (user.first_name or "صديقي")
    mention = f'<a href="tg://user?id={user.id}">{html.escape(label)}</a>'
    return html.escape(raw, quote=False).replace("{name}", mention)


# ═══════════════════════════════════════
# Sending pages and files
# ═══════════════════════════════════════
async def delete_quietly(msg):
    if msg is None:
        return
    try:
        await msg.delete()
    except TelegramError:
        pass


async def send_photo(context, chat_id, path, caption, keyboard):
    kwargs = dict(chat_id=chat_id, reply_markup=keyboard)
    if caption:
        kwargs.update(caption=caption, parse_mode=ParseMode.HTML)
    cached = cache_get(path)
    if cached:
        try:
            await context.bot.send_photo(photo=cached, **kwargs)
            return
        except TelegramError:
            cache_del(path)
    with path.open("rb") as f:
        msg = await context.bot.send_photo(photo=InputFile(f, filename=path.name), **kwargs)
    if msg.photo:
        cache_put(path, msg.photo[-1].file_id)


async def deliver(context, chat_id, raw, user, image, keyboard, query):
    msg = query.message if query else None
    msg_is_photo = bool(msg is not None and getattr(msg, "photo", None))
    text_html = to_html(raw[:TEXT_LIMIT], user) if raw else ""

    if image:
        await delete_quietly(msg)
        if raw and len(raw) <= CAPTION_LIMIT:
            await send_photo(context, chat_id, image, text_html, keyboard)
        elif raw:
            await send_photo(context, chat_id, image, None, None)
            await context.bot.send_message(
                chat_id, text_html, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        else:
            await send_photo(context, chat_id, image, None, keyboard)
        return

    if msg is not None and not msg_is_photo:
        try:
            await query.edit_message_text(
                text_html, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            log.warning("edit_message_text: %s", e)
    await delete_quietly(msg)
    await context.bot.send_message(
        chat_id, text_html, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


def media_id(msg):
    if msg.photo:
        return msg.photo[-1].file_id
    media = msg.video or msg.audio or msg.document
    return media.file_id if media else None


def pick_kinds(bot, path, size):
    ext = path.suffix.lower()
    doc = ("document", bot.send_document)
    if ext in VIDEO_EXTS:
        return [("video", bot.send_video), doc]
    if ext in AUDIO_EXTS:
        return [("audio", bot.send_audio), doc]
    if ext in PHOTO_EXTS and size <= MAX_PHOTO:
        return [("photo", bot.send_photo), doc]
    return [doc]


async def send_file(context, chat_id, path):
    try:
        size = path.stat().st_size
    except OSError:
        return
    kinds = pick_kinds(context.bot, path, size)

    cached = cache_get(path)
    if cached:
        arg, method = kinds[0]
        try:
            await method(chat_id=chat_id, **{arg: cached})
            return
        except TelegramError:
            cache_del(path)

    if size > MAX_UPLOAD:
        await context.bot.send_message(chat_id, f"⚠️ الملف «{path.name}» أكبر من 50MB ولا يمكن إرساله.")
        return

    for i, (arg, method) in enumerate(kinds):
        try:
            with path.open("rb") as f:
                msg = await method(chat_id=chat_id, **{arg: InputFile(f, filename=path.name)})
        except BadRequest as e:
            if i < len(kinds) - 1:
                continue
            log.error("failed to send %s: %s", path, e)
            await context.bot.send_message(chat_id, f"⚠️ تعذر إرسال «{path.name}».")
            return
        except (TelegramError, OSError) as e:
            log.error("failed to send %s: %s", path, e)
            await context.bot.send_message(chat_id, f"⚠️ تعذر إرسال «{path.name}».")
            return
        if i == 0:
            cache_put(path, media_id(msg))
        return


async def show_page(context, chat_id, folder, user, query=None, is_root=False):
    raw = read_text(folder / "main.md")
    image = find_main_image(folder)
    subs = list_subfolders(folder)
    if not raw and subs:
        raw = DEFAULT_MENU_TEXT

    if raw or image:
        keyboard = build_keyboard(folder, subs, is_root)
        await deliver(context, chat_id, raw, user, image, keyboard, query)

    for f in list_files(folder):
        await send_file(context, chat_id, f)


# ═══════════════════════════════════════
# Commands
# ═══════════════════════════════════════
def is_admin(update):
    return bool(ADMIN_ID) and update.effective_user and update.effective_user.id == ADMIN_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with user_locks[user.id]:
        register_user(user, "start")
        await show_page(context, update.effective_chat.id, ROOT_DIR, user, None, is_root=True)


async def nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    async with user_locks[user.id]:
        register_user(user, "nav")
        if query.data == "home":
            folder, is_root = ROOT_DIR, True
        else:
            folder, is_root = resolve_token(query.data[2:]), False
        if folder is None:
            try:
                await query.edit_message_text("⚠️ هذا القسم لم يعد موجودًا، أرسل /start من جديد.")
            except TelegramError:
                pass
            return
        await show_page(context, query.message.chat.id, folder, user, query, is_root)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    total, active_today, today_i, week_i, last_name = get_stats()
    await update.message.reply_text(
        "📊 احصائيات البوت\n\n"
        f"👥 المستخدمون: {total}\n"
        f"🟢 نشطون اليوم: {active_today}\n"
        f"📅 تفاعلات اليوم: {today_i}\n"
        f"🗓 تفاعلات آخر 7 أيام: {week_i}\n"
        f"🕐 آخر مستخدم: {last_name}"
    )


# ═══════════════════════════════════════
# Broadcast (/all)
# ═══════════════════════════════════════
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data["bc_state"] = "waiting"
    await update.message.reply_text("📢 أرسل الرسالة التي تريد إذاعتها (أو /cancel للإلغاء):")


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.pop("bc_state", None)
    context.user_data.pop("bc_msg", None)
    await update.message.reply_text("❎ تم الإلغاء.")


async def broadcast_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("bc_state") != "waiting":
        return
    msg = update.effective_message
    context.user_data["bc_state"] = "confirm"
    context.user_data["bc_msg"] = (msg.chat_id, msg.message_id)
    await context.bot.copy_message(msg.chat_id, msg.chat_id, msg.message_id)
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ إرسال", callback_data="bc:yes"),
            InlineKeyboardButton("❌ إلغاء", callback_data="bc:no"),
        ]]
    )
    await context.bot.send_message(
        msg.chat_id,
        f"👆 هذه معاينة الرسالة.\nسيتم إرسالها إلى {len(all_user_ids())} مستخدم. تأكيد؟",
        reply_markup=kb,
    )


async def do_broadcast(context, from_chat, message_id):
    ok = blocked = failed = 0
    for uid in all_user_ids():
        for attempt in range(2):
            try:
                await context.bot.copy_message(uid, from_chat, message_id)
                ok += 1
            except Forbidden:
                mark_blocked(uid)
                blocked += 1
            except RetryAfter as e:
                wait = e.retry_after
                wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
                await asyncio.sleep(wait + 1)
                continue
            except TelegramError as e:
                log.warning("broadcast to %s: %s", uid, e)
                failed += 1
            break
        await asyncio.sleep(0.05)
    return ok, blocked, failed


async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    action = query.data.split(":", 1)[1]
    payload = context.user_data.get("bc_msg")
    if context.user_data.get("bc_state") != "confirm" or not payload:
        await query.edit_message_text("انتهت هذه العملية. أرسل /all من جديد.")
        return
    context.user_data.pop("bc_state", None)
    context.user_data.pop("bc_msg", None)
    if action == "no":
        await query.edit_message_text("❎ تم الإلغاء.")
        return
    await query.edit_message_text("⏳ جارٍ الإرسال...")
    ok, blocked, failed = await do_broadcast(context, *payload)
    await query.edit_message_text(
        f"✅ وصلت إلى: {ok}\n🚫 حظروا البوت: {blocked}\n⚠️ فشل: {failed}"
    )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("خطأ غير متوقع", exc_info=context.error)


# ═══════════════════════════════════════
# Startup
# ═══════════════════════════════════════
def main():
    ROOT_DIR.mkdir(exist_ok=True)
    init_db()
    rebuild_index()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(120)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("all", broadcast_start))
    app.add_handler(CommandHandler("cancel", broadcast_cancel))
    app.add_handler(CallbackQueryHandler(nav, pattern=r"^(p:[0-9a-f]+|home)$"))
    app.add_handler(CallbackQueryHandler(broadcast_confirm, pattern=r"^bc:(yes|no)$"))
    if ADMIN_ID:
        app.add_handler(
            MessageHandler(
                filters.User(user_id=ADMIN_ID) & filters.ChatType.PRIVATE & ~filters.COMMAND,
                broadcast_capture,
            )
        )
    app.add_error_handler(on_error)

    print("البوت يعمل...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
