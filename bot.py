import asyncio
import html
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup,
    InlineKeyboardButton, FSInputFile
)

# ========================= CONFIG =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", str(ADMIN_ID or 0)))
UPLOAD_OWNER_ONLY = False  # Public uploads are enabled; every upload still requires admin approval.
MAX_BOT_FILE_MB = int(os.getenv("MAX_BOT_FILE_MB", "2"))
BOT_CPU_LIMIT = int(os.getenv("BOT_CPU_LIMIT", "50"))
BOT_MEMORY_LIMIT_MB = int(os.getenv("BOT_MEMORY_LIMIT_MB", "256"))

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
BOTS_DIR = BASE / "hosted_bots"
LOG_DIR = BASE / "logs"
for folder in (DATA_DIR, BOTS_DIR, LOG_DIR):
    folder.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "hosting.sqlite3"
LOG_FILE = LOG_DIR / "hosting.log"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment Variables.")
if not ADMIN_ID or not OWNER_ID:
    raise RuntimeError("ADMIN_ID and OWNER_ID are required in Render Environment Variables.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("hosting")
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
STARTED_AT = time.time()
PROCESSES: dict[int, subprocess.Popen] = {}
USER_STATES: dict[int, str] = {}
USER_STATE_DATA: dict[int, str] = {}
MAINTENANCE = False

# ========================= DATABASE =========================
def db(query, params=(), fetch="none"):
    with sqlite3.connect(DB_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        cur = con.execute(query, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        con.commit()
        return cur.lastrowid


def init_db():
    db("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
        joined_at TEXT NOT NULL, last_seen TEXT NOT NULL,
        language TEXT DEFAULT 'en', banned INTEGER DEFAULT 0)""")
    db("""CREATE TABLE IF NOT EXISTS hosted_bots(
        user_id INTEGER PRIMARY KEY, file_path TEXT NOT NULL, file_name TEXT NOT NULL,
        file_size INTEGER DEFAULT 0, bot_username TEXT DEFAULT '', status TEXT DEFAULT 'PENDING',
        uploaded_at TEXT NOT NULL, started_at TEXT, pid INTEGER, last_error TEXT DEFAULT '')""")
    db("""CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    defaults = {
        "welcome_en": "🚀 Welcome to <b>XYZ BOT HOSTING</b>!\n\nUpload your <code>bot.py</code>, wait for admin approval, and manage your bot from one place.",
        "welcome_bn": "🚀 <b>XYZ BOT HOSTING</b>-এ স্বাগতম!\n\nতোমার <code>bot.py</code> আপলোড করো, Admin approval-এর জন্য অপেক্ষা করো এবং এখান থেকে বট পরিচালনা করো।",
        "update_channel": "https://t.me/",
        "owner_link": "https://t.me/",
        "maintenance": "0",
    }
    for k, v in defaults.items():
        db("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


def setting(key, default=""):
    row = db("SELECT value FROM settings WHERE key=?", (key,), "one")
    return row["value"] if row else default


def set_setting(key, value):
    db("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def touch_user(user: types.User):
    now = datetime.now(timezone.utc).isoformat()
    db("""INSERT INTO users(user_id,username,full_name,joined_at,last_seen)
       VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
       username=excluded.username, full_name=excluded.full_name, last_seen=excluded.last_seen""",
       (user.id, user.username or "", user.full_name or "", now, now))


def is_admin(uid: int) -> bool:
    return uid in {ADMIN_ID, OWNER_ID}


def user_lang(uid: int) -> str:
    row = db("SELECT language FROM users WHERE user_id=?", (uid,), "one")
    return row["language"] if row and row["language"] in {"en", "bn"} else "en"


TEXT = {
    "en": {
        "upload": "📤 𝐔𝐏𝐋𝐎𝐀𝐃 𝐅𝐈𝐋𝐄", "profile": "👤 𝐏𝐑𝐎𝐅𝐈𝐋𝐄", "mybot": "🤖 𝐌𝐘 𝐁𝐎𝐓",
        "speed": "⚡ 𝐁𝐎𝐓 𝐒𝐏𝐄𝐄𝐃", "stats": "📊 𝐁𝐎𝐓 𝐒𝐓𝐀𝐓𝐒", "channel": "📢 𝐔𝐏𝐃𝐀𝐓𝐄 𝐂𝐇𝐀𝐍𝐍𝐄𝐋",
        "owner": "👑 𝐎𝐖𝐍𝐄𝐑", "language": "🌐 𝐋𝐀𝐍𝐆𝐔𝐀𝐆𝐄", "admin": "🛠️ 𝐀𝐃𝐌𝐈𝐍 𝐏𝐀𝐍𝐄𝐋",
        "back": "⬅️ 𝐁𝐀𝐂𝐊", "send_file": "📨 Send your <code>bot.py</code> file as a Telegram document.",
        "only_py": "❌ Only a file named exactly <code>bot.py</code> is accepted.",
        "pending": "✅ Your file has been sent to the admin for review. You will be notified after approval.",
        "limit": "⚠️ You already have a bot. Delete it from MY BOT before uploading another one.",
        "no_bot": "You have not uploaded a bot yet.", "on": "🟢 BOT ON", "off": "🔴 BOT OFF", "delete": "🗑️ DELETE BOT",
        "lang_pick": "Choose your language:", "saved": "✅ Language updated.", "banned": "⛔ Your access is restricted. Contact the owner.",
        "not_admin": "⛔ Admin access required.", "error": "❌ Something went wrong. Please try again.",
    },
    "bn": {
        "upload": "📤 𝐔𝐏𝐋𝐎𝐀𝐃 𝐅𝐈𝐋𝐄", "profile": "👤 𝐏𝐑𝐎𝐅𝐈𝐋𝐄", "mybot": "🤖 𝐌𝐘 𝐁𝐎𝐓",
        "speed": "⚡ 𝐁𝐎𝐓 𝐒𝐏𝐄𝐄𝐃", "stats": "📊 𝐁𝐎𝐓 𝐒𝐓𝐀𝐓𝐒", "channel": "📢 𝐔𝐏𝐃𝐀𝐓𝐄 𝐂𝐇𝐀𝐍𝐍𝐄𝐋",
        "owner": "👑 𝐎𝐖𝐍𝐄𝐑", "language": "🌐 𝐋𝐀𝐍𝐆𝐔𝐀𝐆𝐄", "admin": "🛠️ 𝐀𝐃𝐌𝐈𝐍 𝐏𝐀𝐍𝐄𝐋",
        "back": "⬅️ 𝐁𝐀𝐂𝐊", "send_file": "📨 তোমার <code>bot.py</code> ফাইলটি Telegram document হিসেবে পাঠাও।",
        "only_py": "❌ শুধু <code>bot.py</code> নামের ফাইল গ্রহণ করা হবে।",
        "pending": "✅ তোমার ফাইলটি Admin-এর কাছে review-এর জন্য পাঠানো হয়েছে। সিদ্ধান্ত হলে জানানো হবে।",
        "limit": "⚠️ তোমার আগে থেকেই একটি বট আছে। নতুন বট আপলোডের আগে MY BOT থেকে সেটি ডিলিট করো।",
        "no_bot": "তুমি এখনো কোনো বট আপলোড করোনি।", "on": "🟢 BOT ON", "off": "🔴 BOT OFF", "delete": "🗑️ DELETE BOT",
        "lang_pick": "ভাষা নির্বাচন করো:", "saved": "✅ ভাষা পরিবর্তন হয়েছে।", "banned": "⛔ তোমার অ্যাক্সেস বন্ধ। Owner-এর সাথে যোগাযোগ করো।",
        "not_admin": "⛔ শুধু Admin এই কাজটি করতে পারবে।", "error": "❌ সমস্যা হয়েছে। আবার চেষ্টা করো।",
    },
}


def tr(uid, key):
    return TEXT[user_lang(uid)].get(key, TEXT["en"].get(key, key))


def main_keyboard(uid):
    t = TEXT[user_lang(uid)]
    rows = [
        [KeyboardButton(text=t["upload"], style="success")],
        [KeyboardButton(text=t["profile"], style="primary"), KeyboardButton(text=t["mybot"], style="success")],
        [KeyboardButton(text=t["speed"], style="primary"), KeyboardButton(text=t["stats"], style="danger")],
        [KeyboardButton(text=t["channel"], style="primary")],
        [KeyboardButton(text=t["owner"], style="success"), KeyboardButton(text=t["language"], style="danger")],
    ]
    if is_admin(uid):
        rows.append([KeyboardButton(text=t["admin"], style="danger")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="Choose an option…")


def back_kb(uid):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=tr(uid, "back"), style="primary")]], resize_keyboard=True)


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 All Users", callback_data="adm:users:0"), InlineKeyboardButton(text="📊 Overview", callback_data="adm:overview")],
        [InlineKeyboardButton(text="🤖 All Hosted Bots", callback_data="adm:bots"), InlineKeyboardButton(text="📨 Broadcast", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="📢 Set Update Channel", callback_data="adm:set:channel"), InlineKeyboardButton(text="👑 Set Owner Link", callback_data="adm:set:owner")],
        [InlineKeyboardButton(text="✍️ Welcome Message", callback_data="adm:set:welcome"), InlineKeyboardButton(text="🔒 Maintenance", callback_data="adm:maintenance")],
        [InlineKeyboardButton(text="📄 System Logs", callback_data="adm:logs"), InlineKeyboardButton(text="🔄 Refresh", callback_data="adm:home")],
    ])


def bot_control_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 BOT ON", callback_data=f"bot:on:{uid}"), InlineKeyboardButton(text="🔴 BOT OFF", callback_data=f"bot:off:{uid}")],
        [InlineKeyboardButton(text="🗑️ DELETE BOT", callback_data=f"bot:delete:{uid}")],
    ])

# ========================= PROCESS MANAGEMENT =========================
def extract_embedded_token(path: Path) -> str:
    """Read common hard-coded token variable names from the user's own bot.py."""
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
        patterns = [
            r"(?m)^\s*(?:BOT_TOKEN|TOKEN|API_TOKEN)\s*=\s*['\"]([0-9]{5,}:[A-Za-z0-9_-]{15,})['\"]",
        ]
        for pattern in patterns:
            match = re.search(pattern, source)
            if match:
                return match.group(1)
    except Exception:
        log.exception("Could not inspect uploaded bot token")
    return ""


async def refresh_bot_username(uid: int):
    path = bot_file(uid)
    token = extract_embedded_token(path)
    if not token:
        return
    try:
        timeout = ClientTimeout(total=8)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(f"https://api.telegram.org/bot{token}/getMe") as response:
                payload = await response.json(content_type=None)
        if payload.get("ok"):
            username = payload["result"].get("username", "")
            db("UPDATE hosted_bots SET bot_username=? WHERE user_id=?", (username, uid))
    except Exception:
        log.exception("Could not resolve hosted bot username for %s", uid)


def bot_file(uid):
    return BOTS_DIR / str(uid) / "bot.py"


def stop_process(uid):
    proc = PROCESSES.get(uid)
    if proc and proc.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            log.exception("Could not stop child process for %s", uid)
    PROCESSES.pop(uid, None)
    db("UPDATE hosted_bots SET status='OFF',pid=NULL,started_at=NULL WHERE user_id=?", (uid,))


def start_process(uid):
    path = bot_file(uid)
    if not path.exists():
        raise FileNotFoundError("bot.py was not found in the bot directory")
    current = PROCESSES.get(uid)
    if current and current.poll() is None:
        return current
    log_path = LOG_DIR / f"bot_{uid}.log"
    out = open(log_path, "ab", buffering=0)
    kwargs = {"cwd": str(path.parent), "stdin": subprocess.DEVNULL, "stdout": out,
              "stderr": subprocess.STDOUT, "close_fds": True}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "-u", "bot.py"], **kwargs)
    PROCESSES[uid] = proc
    db("UPDATE hosted_bots SET status='STARTING',pid=?,started_at=? WHERE user_id=?",
       (proc.pid, datetime.now(timezone.utc).isoformat(), uid))
    return proc


async def start_and_report(uid, notify=True):
    try:
        proc = start_process(uid)
        await asyncio.sleep(2.5)
        if proc.poll() is not None:
            log_text = read_bot_log(uid)
            db("UPDATE hosted_bots SET status='ERROR',pid=NULL,last_error=? WHERE user_id=?", (log_text[-3500:], uid))
            if notify:
                await bot.send_message(uid, "❌ <b>BOT START FAILED</b>\n\n<pre>" + html.escape(log_text[-3200:] or "Process exited without output") + "</pre>", reply_markup=main_keyboard(uid))
            return False
        db("UPDATE hosted_bots SET status='ON',pid=? WHERE user_id=?", (proc.pid, uid))
        await refresh_bot_username(uid)
        if notify:
            await bot.send_message(uid, "✅ <b>YOUR BOT IS NOW ONLINE</b>\n\nYour <code>bot.py</code> process started successfully.", reply_markup=main_keyboard(uid))
        return True
    except Exception as exc:
        log.exception("Bot start failed for %s", uid)
        db("UPDATE hosted_bots SET status='ERROR',last_error=? WHERE user_id=?", (str(exc), uid))
        if notify:
            await bot.send_message(uid, "❌ <b>BOT START ERROR</b>\n\n<pre>" + html.escape(str(exc)) + "</pre>", reply_markup=main_keyboard(uid))
        return False


def read_bot_log(uid):
    p = LOG_DIR / f"bot_{uid}.log"
    if not p.exists():
        return "No log output available."
    try:
        return p.read_text(encoding="utf-8", errors="replace")[-6000:]
    except Exception as exc:
        return str(exc)


async def monitor_processes():
    while True:
        await asyncio.sleep(5)
        for uid, proc in list(PROCESSES.items()):
            if proc.poll() is not None:
                PROCESSES.pop(uid, None)
                row = db("SELECT status FROM hosted_bots WHERE user_id=?", (uid,), "one")
                if row and row["status"] not in {"OFF", "DELETED"}:
                    log_text = read_bot_log(uid)
                    db("UPDATE hosted_bots SET status='ERROR',pid=NULL,last_error=? WHERE user_id=?", (log_text[-3500:], uid))
                    try:
                        await bot.send_message(uid, "⚠️ <b>YOUR BOT STOPPED</b>\n\nLatest error/output:\n<pre>" + html.escape(log_text[-3000:]) + "</pre>")
                    except Exception:
                        pass

# ========================= USER SCREENS =========================
@dp.message(CommandStart())
async def start(message: types.Message):
    uid = message.from_user.id
    touch_user(message.from_user)
    row = db("SELECT banned FROM users WHERE user_id=?", (uid,), "one")
    if row and row["banned"]:
        await message.answer(tr(uid, "banned"))
        return
    key = "welcome_bn" if user_lang(uid) == "bn" else "welcome_en"
    await message.answer(setting(key), reply_markup=main_keyboard(uid))


@dp.message(F.text)
async def text_router(message: types.Message):
    uid = message.from_user.id
    touch_user(message.from_user)
    text = (message.text or "").strip()
    row = db("SELECT banned FROM users WHERE user_id=?", (uid,), "one")
    if row and row["banned"]:
        await message.answer(tr(uid, "banned"))
        return

    state = USER_STATES.get(uid)
    if state:
        if state == "await_upload":
            if text == TEXT[user_lang(uid)]["back"]:
                USER_STATES.pop(uid, None)
                await message.answer("🏠 <b>MAIN MENU</b>", reply_markup=main_keyboard(uid))
            else:
                await message.answer("📨 Please attach your <code>bot.py</code> as a document.")
            return
        if state == "admin_broadcast":
            if not is_admin(uid):
                USER_STATES.pop(uid, None)
                return
            USER_STATES.pop(uid, None)
            users = db("SELECT user_id FROM users WHERE banned=0", fetch="all")
            ok = 0
            for item in users:
                try:
                    await bot.send_message(item["user_id"], text)
                    ok += 1
                    await asyncio.sleep(0.04)
                except Exception:
                    pass
            await message.answer(f"✅ Broadcast delivered to {ok}/{len(users)} users.", reply_markup=main_keyboard(uid))
            return
        if state.startswith("admin_set_"):
            if not is_admin(uid):
                USER_STATES.pop(uid, None)
                return
            kind = state.removeprefix("admin_set_")
            USER_STATES.pop(uid, None)
            if kind == "channel":
                set_setting("update_channel", text)
            elif kind == "owner":
                set_setting("owner_link", text)
            elif kind == "welcome":
                set_setting("welcome_en", text)
            await message.answer("✅ Setting saved.", reply_markup=main_keyboard(uid))
            return
        if state == "admin_user_lookup":
            USER_STATES.pop(uid, None)
            if text.isdigit():
                await show_user_detail(message, int(text))
            else:
                await message.answer("Send a numeric Telegram user ID.", reply_markup=main_keyboard(uid))
            return

    t = TEXT[user_lang(uid)]
    if text == t["upload"]:
        if MAINTENANCE and not is_admin(uid):
            await message.answer("🔒 Hosting is under maintenance.", reply_markup=main_keyboard(uid)); return
        existing = db("SELECT status FROM hosted_bots WHERE user_id=?", (uid,), "one")
        if existing:
            await message.answer(tr(uid, "limit"), reply_markup=main_keyboard(uid)); return
        USER_STATES[uid] = "await_upload"
        await message.answer(tr(uid, "send_file"), reply_markup=back_kb(uid))
        return
    if text == t["profile"]:
        await show_profile(message)
        return
    if text == t["mybot"]:
        await show_my_bot(message)
        return
    if text == t["speed"]:
        await show_speed(message)
        return
    if text == t["stats"]:
        await show_stats(message)
        return
    if text == t["channel"]:
        channel_url = html.escape(setting("update_channel"), quote=True)
        await message.answer(f'📢 <b>UPDATE CHANNEL</b>\n<a href="{channel_url}">Open channel</a>\n{html.escape(setting("update_channel"))}', reply_markup=main_keyboard(uid), disable_web_page_preview=True)
        return
    if text == t["owner"]:
        owner_url = html.escape(setting("owner_link"), quote=True)
        await message.answer(f'👑 <b>OWNER CONTACT</b>\n<a href="{owner_url}">Contact owner</a>\n{html.escape(setting("owner_link"))}', reply_markup=main_keyboard(uid), disable_web_page_preview=True)
        return
    if text == t["language"]:
        await message.answer(tr(uid, "lang_pick"), reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🇧🇩 বাংলা", style="success"), KeyboardButton(text="🇬🇧 ENGLISH", style="primary")],
            [KeyboardButton(text=tr(uid, "back"), style="danger")]], resize_keyboard=True))
        return
    if text in {"🇧🇩 বাংলা", "🇬🇧 ENGLISH"}:
        lang = "bn" if text == "🇧🇩 বাংলা" else "en"
        db("UPDATE users SET language=? WHERE user_id=?", (lang, uid))
        await message.answer(tr(uid, "saved"), reply_markup=main_keyboard(uid))
        return
    if text == tr(uid, "back"):
        USER_STATES.pop(uid, None)
        await message.answer("🏠 <b>MAIN MENU</b>", reply_markup=main_keyboard(uid))
        return
    if text == t["admin"] and is_admin(uid):
        await message.answer("🛠️ <b>ADMIN CONTROL CENTER</b>\nSelect a control below.", reply_markup=admin_kb())
        return
    if is_admin(uid) and text == "/admin":
        await message.answer("🛠️ <b>ADMIN CONTROL CENTER</b>", reply_markup=admin_kb())


@dp.message(F.document)
async def receive_bot_file(message: types.Message):
    uid = message.from_user.id
    touch_user(message.from_user)
    doc = message.document
    if USER_STATES.get(uid) != "await_upload":
        await message.answer("Tap UPLOAD FILE first, then send <code>bot.py</code>.", reply_markup=main_keyboard(uid)); return
    if doc.file_name != "bot.py":
        await message.answer(tr(uid, "only_py")); return
    if (doc.file_size or 0) > MAX_BOT_FILE_MB * 1024 * 1024:
        await message.answer(f"❌ File is too large. Maximum size: {MAX_BOT_FILE_MB} MB."); return
    existing = db("SELECT user_id FROM hosted_bots WHERE user_id=?", (uid,), "one")
    if existing:
        USER_STATES.pop(uid, None)
        await message.answer(tr(uid, "limit"), reply_markup=main_keyboard(uid)); return

    user_dir = BOTS_DIR / str(uid)
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / "bot.py"
    try:
        tg_file = await bot.get_file(doc.file_id)
        await bot.download_file(tg_file.file_path, destination=target)
        me = message.from_user
        now = datetime.now(timezone.utc).isoformat()
        db("INSERT INTO hosted_bots(user_id,file_path,file_name,file_size,status,uploaded_at) VALUES(?,?,?,?,?,?)",
           (uid, str(target), "bot.py", doc.file_size or 0, "PENDING", now))
        USER_STATES.pop(uid, None)
        username = f"@{me.username}" if me.username else "Not set"
        caption = (
            "📥 <b>NEW BOT APPROVAL REQUEST</b>\n\n"
            f"👤 User: {html.escape(me.full_name)}\n"
            f"🔗 Username: {html.escape(username)}\n"
            f"🆔 User ID: <code>{uid}</code>\n"
            f"📄 File: <code>bot.py</code>\n"
            f"💾 Size: {(doc.file_size or 0)/1024:.1f} KB\n"
            f"🕒 Uploaded: {html.escape(now)}\n\nReview and choose an action."
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ ACCEPT & RUN", callback_data=f"approve:{uid}", style="success"),
            InlineKeyboardButton(text="❌ REJECT", callback_data=f"reject:{uid}", style="danger")
        ]])
        await bot.send_document(ADMIN_ID, FSInputFile(target), caption=caption, reply_markup=markup)
        await message.answer(tr(uid, "pending"), reply_markup=main_keyboard(uid))
    except Exception as exc:
        log.exception("Upload failed")
        target.unlink(missing_ok=True)
        db("DELETE FROM hosted_bots WHERE user_id=?", (uid,))
        await message.answer("❌ Upload failed:\n<code>" + html.escape(str(exc)) + "</code>", reply_markup=main_keyboard(uid))


async def show_profile(message):
    u = message.from_user
    uid = u.id
    row = db("SELECT joined_at,language FROM users WHERE user_id=?", (uid,), "one")
    botrow = db("SELECT status FROM hosted_bots WHERE user_id=?", (uid,), "one")
    count = 1 if botrow else 0
    joined = row["joined_at"][:10] if row else "Unknown"
    text = ("👤 <b>USER PROFILE</b>\n\n"
            f"🧑 Name: {html.escape(u.full_name)}\n"
            f"🔗 Username: @{html.escape(u.username) if u.username else 'Not set'}\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"📅 Joined: <code>{joined}</code>\n"
            f"🤖 Hosted bots: <b>{count}/1</b>\n"
            f"📦 Files uploaded: <b>{count}</b>\n"
            f"🌐 Language: <b>{user_lang(uid).upper()}</b>")
    photos = await bot.get_user_profile_photos(uid, limit=1)
    if photos.total_count:
        await message.answer_photo(photos.photos[0][-1].file_id, caption=text, reply_markup=main_keyboard(uid))
    else:
        await message.answer(text, reply_markup=main_keyboard(uid))


async def show_my_bot(message):
    uid = message.from_user.id
    row = db("SELECT * FROM hosted_bots WHERE user_id=?", (uid,), "one")
    if not row:
        await message.answer(tr(uid, "no_bot"), reply_markup=main_keyboard(uid)); return
    started = row["started_at"] or "—"
    runtime = "—"
    if row["started_at"] and row["status"] in {"ON", "STARTING"}:
        try:
            started_dt = datetime.fromisoformat(row["started_at"])
            seconds = max(0, int((datetime.now(timezone.utc) - started_dt).total_seconds()))
            runtime = f"{seconds // 3600}h {(seconds % 3600) // 60}m"
        except Exception:
            pass
    text = ("🤖 <b>MY BOT</b>\n\n"
            f"📄 File: <code>{html.escape(row['file_name'])}</code>\n"
            f"👤 Bot owner: <code>{uid}</code>\n"
            f"🔗 Bot username: <b>@{html.escape(row['bot_username']) if row['bot_username'] else 'Not detected'}</b>\n"
            f"📦 Size: {row['file_size']/1024:.1f} KB\n"
            f"📌 Status: <b>{row['status']}</b>\n"
            f"🕒 Started: <code>{html.escape(started)}</code>\n"
            f"⏱️ Running time: <b>{runtime}</b>\n"
            f"🧾 Uploaded: <code>{html.escape(row['uploaded_at'][:19])}</code>")
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 BOT ON", callback_data=f"self:on:{uid}", style="success"), InlineKeyboardButton(text="🔴 BOT OFF", callback_data=f"self:off:{uid}", style="danger")],
        [InlineKeyboardButton(text="🗑️ DELETE BOT", callback_data=f"self:delete:{uid}", style="danger")],
        [InlineKeyboardButton(text="📜 VIEW ERROR LOG", callback_data=f"self:log:{uid}")]
    ]))


async def show_speed(message):
    uid = message.from_user.id
    t0 = time.perf_counter()
    await bot.get_me()
    latency = (time.perf_counter() - t0) * 1000
    vm = psutil.virtual_memory()
    row = db("SELECT status FROM hosted_bots WHERE user_id=?", (uid,), "one")
    text = ("⚡ <b>BOT SPEED</b>\n\n"
            f"🚀 Response: <b>{latency:.2f} ms</b>\n"
            f"🟢 Hosting service: <b>ONLINE</b>\n"
            f"⚙️ System: <b>PYTHON</b>\n"
            f"💻 CPU: <b>{psutil.cpu_percent(interval=0.1):.1f}%</b>\n"
            f"💾 RAM: <b>{vm.percent:.1f}%</b>\n"
            f"🖥️ Your bot: <b>{row['status'] if row else 'NOT UPLOADED'}</b>")
    await message.answer(text, reply_markup=main_keyboard(uid))


async def show_stats(message):
    uid = message.from_user.id
    total_users = db("SELECT COUNT(*) n FROM users", fetch="one")["n"]
    total_bots = db("SELECT COUNT(*) n FROM hosted_bots", fetch="one")["n"]
    online = db("SELECT COUNT(*) n FROM hosted_bots WHERE status='ON'", fetch="one")["n"]
    my = db("SELECT status FROM hosted_bots WHERE user_id=?", (uid,), "one")
    text = ("📊 <b>HOSTING STATISTICS</b>\n\n"
            f"👥 Total users: <b>{total_users}</b>\n"
            f"🤖 Hosted bots: <b>{total_bots}</b>\n"
            f"🟢 Bots online: <b>{online}</b>\n"
            f"🔴 Bots offline: <b>{total_bots-online}</b>\n"
            f"📌 Your bot: <b>{my['status'] if my else 'NONE'}</b>\n"
            f"⏱️ Service uptime: <b>{int((time.time()-STARTED_AT)//60)} minutes</b>")
    await message.answer(text, reply_markup=main_keyboard(uid))

# ========================= ADMIN CALLBACKS =========================
@dp.callback_query(F.data.startswith("approve:"))
async def approve_bot(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only", show_alert=True); return
    uid = int(call.data.split(":")[1])
    row = db("SELECT status FROM hosted_bots WHERE user_id=?", (uid,), "one")
    if not row or row["status"] != "PENDING":
        await call.answer("This request is no longer pending.", show_alert=True); return
    await call.answer("Approved. Starting bot…")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.reply("✅ <b>APPROVED</b> — launching bot process…")
    await start_and_report(uid, notify=True)


@dp.callback_query(F.data.startswith("reject:"))
async def reject_bot(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only", show_alert=True); return
    uid = int(call.data.split(":")[1])
    row = db("SELECT status FROM hosted_bots WHERE user_id=?", (uid,), "one")
    if not row or row["status"] != "PENDING":
        await call.answer("Request is no longer pending.", show_alert=True); return
    stop_process(uid)
    bot_file(uid).unlink(missing_ok=True)
    db("DELETE FROM hosted_bots WHERE user_id=?", (uid,))
    await call.answer("Rejected")
    await call.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(uid, "❌ <b>BOT UPLOAD REJECTED</b>\nYour upload was not approved. You can upload again.", reply_markup=main_keyboard(uid))
    except Exception:
        pass


async def show_user_detail(message, uid):
    row = db("SELECT * FROM users WHERE user_id=?", (uid,), "one")
    b = db("SELECT status,file_size,uploaded_at,bot_username FROM hosted_bots WHERE user_id=?", (uid,), "one")
    if not row:
        await message.answer("User not found.")
        return
    txt = (
        "👤 <b>USER DETAILS</b>\n\n"
        f"Name: {html.escape(row['full_name'])}\n"
        f"Username: @{html.escape(row['username']) if row['username'] else 'None'}\n"
        f"ID: <code>{uid}</code>\n"
        f"Joined: <code>{html.escape(row['joined_at'])}</code>\n"
        f"Last active: <code>{html.escape(row['last_seen'])}</code>\n"
        f"Language: <b>{row['language'].upper()}</b>\n"
        f"Banned: <b>{'YES' if row['banned'] else 'NO'}</b>\n"
    )
    buttons = [[InlineKeyboardButton(text="🚫 Ban" if not row["banned"] else "✅ Unban", callback_data=f"user:{'ban' if not row['banned'] else 'unban'}:{uid}")]]
    if b:
        txt += (
            f"Bot status: <b>{b['status']}</b>\n"
            f"Bot username: <b>@{html.escape(b['bot_username']) if b['bot_username'] else 'Not detected'}</b>\n"
            f"File size: <b>{b['file_size']/1024:.1f} KB</b>\n"
            f"Uploaded: <code>{html.escape(b['uploaded_at'])}</code>"
        )
        buttons += [
            [InlineKeyboardButton(text="🟢 ON", callback_data=f"bot:on:{uid}"), InlineKeyboardButton(text="🔴 OFF", callback_data=f"bot:off:{uid}")],
            [InlineKeyboardButton(text="🗑️ Delete bot", callback_data=f"bot:delete:{uid}")],
        ]
    else:
        txt += "Bot: <b>NONE</b>"
    await message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("adm:"))
async def admin_actions(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only", show_alert=True); return
    parts = call.data.split(":")
    action = parts[1]
    await call.answer()
    if action in {"home"}:
        await call.message.answer("🛠️ <b>ADMIN CONTROL CENTER</b>", reply_markup=admin_kb()); return
    if action == "overview":
        users = db("SELECT COUNT(*) n FROM users", fetch="one")["n"]
        bots = db("SELECT COUNT(*) n FROM hosted_bots", fetch="one")["n"]
        online = db("SELECT COUNT(*) n FROM hosted_bots WHERE status='ON'", fetch="one")["n"]
        await call.message.answer(f"📊 <b>ADMIN OVERVIEW</b>\nUsers: {users}\nHosted bots: {bots}\nOnline: {online}\nOffline/Pending: {bots-online}", reply_markup=admin_kb()); return
    if action == "users":
        page = int(parts[2]) if len(parts) > 2 else 0
        rows = db("SELECT user_id,full_name,username,banned FROM users ORDER BY joined_at DESC LIMIT 10 OFFSET ?", (page*10,), "all")
        buttons = []
        for u in rows:
            title = (u["full_name"] or str(u["user_id"]))[:22]
            buttons.append([InlineKeyboardButton(text=f"{'🚫' if u['banned'] else '👤'} {title} · {u['user_id']}", callback_data=f"adm:user:{u['user_id']}")])
        buttons.append([InlineKeyboardButton(text="🔎 Find user by ID", callback_data="adm:find")])
        buttons.append([InlineKeyboardButton(text="⬅️ Prev", callback_data=f"adm:users:{max(0,page-1)}"), InlineKeyboardButton(text="Next ➡️", callback_data=f"adm:users:{page+1}")])
        await call.message.answer(f"👥 <b>ALL USERS</b> · Page {page+1}", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); return
    if action == "user":
        await show_user_detail(call.message, int(parts[2])); return
    if action == "find":
        USER_STATES[call.from_user.id] = "admin_user_lookup"
        await call.message.answer("Send the Telegram user ID."); return
    if action == "bots":
        rows = db("SELECT user_id,status,file_size FROM hosted_bots ORDER BY uploaded_at DESC LIMIT 30", fetch="all")
        buttons = [[InlineKeyboardButton(text=f"{r['status']} · User {r['user_id']}", callback_data=f"adm:user:{r['user_id']}")] for r in rows]
        await call.message.answer("🤖 <b>HOSTED BOTS</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons or [[InlineKeyboardButton(text="No bots", callback_data="adm:home")]])); return
    if action == "broadcast":
        USER_STATES[call.from_user.id] = "admin_broadcast"
        await call.message.answer("📨 Send the broadcast message now."); return
    if action == "set":
        kind = parts[2]
        USER_STATES[call.from_user.id] = f"admin_set_{kind}"
        await call.message.answer(f"Send the new {kind} value:"); return
    if action == "maintenance":
        global MAINTENANCE
        MAINTENANCE = not MAINTENANCE
        set_setting("maintenance", "1" if MAINTENANCE else "0")
        await call.message.answer(f"Maintenance is now {'ON' if MAINTENANCE else 'OFF'}.", reply_markup=admin_kb()); return
    if action == "logs":
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-3500:] if LOG_FILE.exists() else "No logs yet."
        await call.message.answer("📄 <b>SYSTEM LOGS</b>\n<pre>" + html.escape(content) + "</pre>"); return


@dp.callback_query(F.data.startswith("user:"))
async def user_actions(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only", show_alert=True); return
    _, action, raw_uid = call.data.split(":")
    uid = int(raw_uid)
    if action == "ban":
        db("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
        try: await bot.send_message(uid, "⛔ Your account has been restricted by the admin.")
        except Exception: pass
    elif action == "unban":
        db("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await call.answer("User updated")
    await show_user_detail(call.message, uid)


@dp.callback_query(F.data.startswith("bot:"))
async def admin_bot_actions(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only", show_alert=True); return
    _, action, raw_uid = call.data.split(":")
    uid = int(raw_uid)
    row = db("SELECT user_id FROM hosted_bots WHERE user_id=?", (uid,), "one")
    if not row:
        await call.answer("Bot not found", show_alert=True); return
    if action == "on":
        await call.answer("Starting…")
        await start_and_report(uid, notify=True)
    elif action == "off":
        stop_process(uid)
        await call.answer("Bot stopped")
        try: await bot.send_message(uid, "🔴 <b>YOUR BOT WAS STOPPED BY ADMIN</b>")
        except Exception: pass
    elif action == "delete":
        stop_process(uid)
        bot_file(uid).unlink(missing_ok=True)
        db("DELETE FROM hosted_bots WHERE user_id=?", (uid,))
        await call.answer("Bot deleted")
        try: await bot.send_message(uid, "🗑️ <b>YOUR HOSTED BOT WAS DELETED BY ADMIN</b>", reply_markup=main_keyboard(uid))
        except Exception: pass
    await call.message.answer(f"Bot action completed for user <code>{uid}</code>.")


@dp.callback_query(F.data.startswith("self:"))
async def self_bot_actions(call: types.CallbackQuery):
    _, action, raw_uid = call.data.split(":")
    uid = int(raw_uid)
    if uid != call.from_user.id and not is_admin(call.from_user.id):
        await call.answer("Not allowed", show_alert=True); return
    row = db("SELECT user_id FROM hosted_bots WHERE user_id=?", (uid,), "one")
    if not row:
        await call.answer("Bot not found", show_alert=True); return
    if action == "on":
        await call.answer("Starting…")
        await start_and_report(uid, notify=True)
    elif action == "off":
        stop_process(uid)
        await call.answer("Bot stopped")
        await call.message.answer("🔴 <b>BOT STOPPED</b>")
    elif action == "delete":
        stop_process(uid)
        bot_file(uid).unlink(missing_ok=True)
        db("DELETE FROM hosted_bots WHERE user_id=?", (uid,))
        await call.answer("Deleted")
        await call.message.answer("🗑️ Bot deleted. You can upload a new bot.py now.", reply_markup=main_keyboard(uid))
    elif action == "log":
        log_text = read_bot_log(uid)
        await call.answer()
        await call.message.answer("📜 <b>BOT LOG</b>\n<pre>" + html.escape(log_text[-3500:]) + "</pre>")

# ========================= RENDER HEALTH SERVER =========================
async def health(request):
    return web.Response(text="ok")


async def start_web_server():
    from aiohttp import web, ClientSession, ClientTimeout
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()
    log.info("Health server started on port %s", os.getenv("PORT", "10000"))


async def restore_approved_bots():
    rows = db("SELECT user_id,status FROM hosted_bots WHERE status IN ('ON','STARTING')", fetch="all")
    for row in rows:
        uid = row["user_id"]
        if bot_file(uid).exists():
            await start_and_report(uid, notify=False)


async def main():
    init_db()
    global MAINTENANCE
    MAINTENANCE = setting("maintenance", "0") == "1"
    await start_web_server()
    asyncio.create_task(monitor_processes())
    await restore_approved_bots()
    log.info("Hosting manager is running")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
