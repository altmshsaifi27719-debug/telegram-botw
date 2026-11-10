import asyncio
import html
import io
import json
import logging
import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone

import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputMediaVideo
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden, RetryAfter
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters,
)

# ============================================================
# CONFIGURATION — preserved from the existing project
# ============================================================
BOT_TOKEN = "AAHDSAemV08jkAsTAXW3IQfsvigxvEWPLBU"  # Set your BotFather token here; no environment variable is used.
ADMIN_ID = 9291293881
# Multiple-admin support. Keep the existing admin ID and optionally add IDs via ADMIN_IDS=1,2,3.
ADMIN_IDS = {ADMIN_ID}
try:
    ADMIN_IDS.update(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
except ValueError:
    pass
DB_FILE = "bot.sqlite"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Conversation states. No deleted/stale plan states are referenced.
PLAN_NAME, PLAN_PRICE, UPI_ID, CHANNEL_LINK, DEMO_MEDIA, WELCOME_IMAGE, WELCOME_TEXT, BROADCAST_CONTENT, REJECTION_REASON, EDIT_PLAN_NAME, EDIT_PLAN_PRICE, DB_IMPORT, CHOOSE_PHOTO, PRO_PHOTO, USER_SEARCH, WELCOME_VIDEO, WELCOME_VIDEOS, PLAN_LINK = range(18)

DEFAULT_WELCOME = "💎 Welcome to our Premium Service!"
LIFETIME_DAYS = 36500

# Fallback conversation routing. This keeps admin setup buttons working even if
# a deployment has an older ConversationHandler callback dispatch behavior.
def set_manual_state(context, state):
    context.user_data["_manual_state"] = state

def clear_manual_state(context):
    context.user_data.pop("_manual_state", None)


def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def esc(value):
    return html.escape("" if value is None else str(value))


def bold(text):
    return f"<b>{text}</b>"


def styled_plan_name(text, style="normal"):
    """Render plan-button labels in a selectable Unicode style."""
    maps = {
        "bold": (0x1D400, 0x1D41A, 0x1D7CE),
        "italic": (0x1D434, 0x1D44E, None),
        "bold_italic": (0x1D468, 0x1D482, None),
        "script": (0x1D49C, 0x1D4B6, None),
        "double": (0x1D538, 0x1D552, 0x1D7D8),
        "mono": (0x1D5D4, 0x1D5EE, 0x1D7F6),
        "sans_bold": (0x1D5D4, 0x1D5EE, 0x1D7F6),
    }
    if style in (None, "normal") or style not in maps:
        return text
    upper, lower, digits = maps[style]
    out = []
    for ch in text:
        if "A" <= ch <= "Z": out.append(chr(upper + ord(ch) - ord("A")))
        elif "a" <= ch <= "z": out.append(chr(lower + ord(ch) - ord("a")))
        elif digits is not None and "0" <= ch <= "9": out.append(chr(digits + ord(ch) - ord("0")))
        else: out.append(ch)
    return "".join(out)


def font_choice_keyboard():
    options = [("normal", "Normal"), ("bold", "𝐁𝐨𝐥𝐝"), ("italic", "𝘐𝘵𝘢𝘭𝘪𝘤"),
               ("bold_italic", "𝑩𝒐𝒍𝒅 𝑰𝒕𝘢𝘭𝘪𝘤"), ("script", "𝓢𝓬𝓻𝓲𝓹𝓽"),
               ("double", "𝔻𝕠𝕦𝕓𝕝𝕖"), ("mono", "𝙼𝚘𝚗𝚘")]
    return InlineKeyboardMarkup([[style_button(label, f"admin:plan:font:{key}", style="primary")]
                                 for key, label in options])


def style_button(text, callback_data=None, url=None, style=None):
    """Create a Telegram inline button, compatible with older PTB installs.

    Telegram Bot API supports button styles (primary/blue, success/green,
    danger/red). PTB added the explicit ``style`` constructor argument in
    v22.7. If an older PTB is installed, pass the field through ``api_kwargs``
    when available instead of crashing the bot.
    """
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style not in {"primary", "success", "danger"}:
        return InlineKeyboardButton(**kwargs)

    try:
        # PTB >= 22.7
        return InlineKeyboardButton(**kwargs, style=style)
    except TypeError as exc:
        # Older PTB: style can still be forwarded as a raw Bot API field.
        if "unexpected keyword argument 'style'" not in str(exc):
            raise
        try:
            return InlineKeyboardButton(**kwargs, api_kwargs={"style": style})
        except TypeError:
            # Very old PTB: keep the button functional, but unstyled.
            return InlineKeyboardButton(**kwargs)


def reply_button(text, style=None):
    """Create a reply-keyboard button with native Telegram color styling.

    Uses the same compatibility strategy as inline buttons so an older PTB
    installation does not stop the bot with an unexpected ``style`` error.
    """
    kwargs = {"text": text}
    if style not in {"primary", "success", "danger"}:
        return KeyboardButton(**kwargs)

    try:
        return KeyboardButton(**kwargs, style=style)
    except TypeError as exc:
        if "unexpected keyword argument 'style'" not in str(exc):
            raise
        try:
            return KeyboardButton(**kwargs, api_kwargs={"style": style})
        except TypeError:
            return KeyboardButton(**kwargs)
def main_user_inline_keyboard():
    return InlineKeyboardMarkup([
        [style_button("💎 GET PREMIUM", "user:premium", style="success")],
        [style_button("🥵 DEMO", "user:demo", style="danger")],
        [style_button("🧾 PROOF", "user:proof", style="primary")],
        [style_button("✅ HOW TO GET PREMIUM", "user:tutorial", style="primary")],
    ])


def user_reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            [reply_button("💎 GET PREMIUM", "success")],
            [reply_button("🥵 DEMO", "danger")],
            [reply_button("🧾 PROOF", "primary")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [style_button("📊 DASHBOARD", "admin:dashboard", style="success"), style_button("💎 PLANS", "admin:plans", style="success")],
        [style_button("👥 USERS", "admin:users", style="primary"), style_button("💳 PAYMENTS", "admin:payments", style="primary")],
        [style_button("🥵 DEMO", "admin:demo", style="danger"), style_button("🏦 UPI SETTINGS", "admin:upi", style="primary")],
        [style_button("🖼️ WELCOME SETTINGS", "admin:welcome", style="primary")],
        [style_button("📢 BROADCAST", "admin:broadcast", style="primary")],
        [style_button("💾 BACKUP / RESTORE", "admin:db:backup", style="primary")],
        [style_button("👑 ADMIN MANAGEMENT", "admin:security", style="primary")],
        [style_button("📥 IMPORT DATABASE", "admin:db:import", style="primary"), style_button("📤 EXPORT DATABASE", "admin:db:export", style="primary")],
    ])


def back_keyboard(target="admin:menu"):
    return InlineKeyboardMarkup([[style_button("🔙 Back", target, style="primary")]])


def current_admin_ids():
    ids = set(ADMIN_IDS)
    try:
        raw = get_setting('extra_admins', '') or ''
        ids.update(int(x.strip()) for x in raw.split(',') if x.strip())
    except (ValueError, TypeError):
        pass
    return ids

def admin_only(obj):
    user = getattr(obj, "effective_user", None) or getattr(obj, "from_user", None)
    return bool(user and user.id in current_admin_ids())


def safe_name(user):
    return user.full_name or user.username or str(user.id)


def init_db():
    con = db()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            registered_at TEXT NOT NULL,
            premium_until TEXT,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            validity_days INTEGER NOT NULL,
            description TEXT,
            link TEXT,
            image_file_id TEXT,
            enabled INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS plan_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER,
            media_type TEXT,
            file_id TEXT,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS demo_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT,
            file_id TEXT,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tutorial(
            id INTEGER PRIMARY KEY CHECK(id=1),
            video_file_id TEXT,
            text TEXT
        );

        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            plan_id INTEGER NOT NULL,
            plan_name TEXT,
            amount REAL,
            validity INTEGER,
            screenshot_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            rejection_reason TEXT,
            created_at TEXT NOT NULL,
            approved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS broadcasts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT,
            file_id TEXT,
            text TEXT,
            target TEXT DEFAULT 'all',
            created_at TEXT NOT NULL,
            total INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        );
        """)

        cols = {r["name"] for r in con.execute("PRAGMA table_info(plans)").fetchall()}
        if "category" not in cols:
            con.execute("ALTER TABLE plans ADD COLUMN category TEXT NOT NULL DEFAULT 'choose'")
        if "font_style" not in cols:
            con.execute("ALTER TABLE plans ADD COLUMN font_style TEXT NOT NULL DEFAULT 'normal'")
        con.execute("UPDATE plans SET category='choose' WHERE category IS NULL OR category=''")
        bcols = {r["name"] for r in con.execute("PRAGMA table_info(broadcasts)").fetchall()}
        if "target" not in bcols:
            con.execute("ALTER TABLE broadcasts ADD COLUMN target TEXT NOT NULL DEFAULT 'all'")

        con.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_text',?)",
            (DEFAULT_WELCOME,),
        )
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('choose_photo','')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('pro_photo','')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('extra_admins','')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_videos','')")
        con.commit()
    finally:
        con.close()


def log_event(event, details=""):
    try:
        con = db()
        con.execute(
            "INSERT INTO logs(event,details,created_at) VALUES(?,?,?)",
            (event, str(details)[:1000], now_iso()),
        )
        con.commit()
        con.close()
    except Exception:
        logger.exception("log_event failed")


def set_setting(key, value):
    con = db()
    try:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()
    finally:
        con.close()


def get_setting(key, default=None):
    con = db()
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        con.close()


async def answer(q, text=None, alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError:
        pass


def register_user(user):
    con = db()
    try:
        con.execute("""
            INSERT INTO users(id,username,first_name,last_name,registered_at,status)
            VALUES(?,?,?,?,?,'active')
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name
        """, (
            user.id, user.username, user.first_name, user.last_name, now_iso()
        ))
        con.commit()
    finally:
        con.close()


async def send_start_screen(bot, chat_id):
    text = get_setting("welcome_text", DEFAULT_WELCOME) or DEFAULT_WELCOME
    image = get_setting("welcome_image")
    inline_kb = main_user_inline_keyboard()

    # Up to five welcome videos are stored together and sent as one Telegram
    # media group (album), so users do not receive five separate messages.
    raw_videos = get_setting("welcome_videos", "") or ""
    videos = []
    try:
        parsed = json.loads(raw_videos)
        if isinstance(parsed, list):
            videos = [str(x) for x in parsed if x]
    except (ValueError, TypeError):
        videos = []

    # Backward compatibility with the older single-video setting.
    if not videos:
        legacy_video = get_setting("welcome_video")
        if legacy_video:
            videos = [legacy_video]

    if videos:
        try:
            media = []
            for i, file_id in enumerate(videos[:5]):
                kwargs = {"media": file_id}
                if i == 0:
                    kwargs["caption"] = bold(esc(text))
                    kwargs["parse_mode"] = ParseMode.HTML
                media.append(InputMediaVideo(**kwargs))
            await bot.send_media_group(chat_id, media)
            await bot.send_message(
                chat_id,
                bold("👇 Choose an option:"),
                parse_mode=ParseMode.HTML,
                reply_markup=inline_kb,
            )
            await bot.send_message(
                chat_id,
                "👇",
                reply_markup=user_reply_keyboard(),
            )
            return
        except TelegramError:
            pass

    if image:
        try:
            await bot.send_photo(
                chat_id,
                image,
                caption=bold(esc(text)),
                parse_mode=ParseMode.HTML,
                reply_markup=inline_kb,
            )
            await bot.send_message(
                chat_id,
                bold("👇 Choose an option:"),
                parse_mode=ParseMode.HTML,
                reply_markup=user_reply_keyboard(),
            )
            return
        except TelegramError:
            pass

    await bot.send_message(
        chat_id,
        bold(esc(text)),
        parse_mode=ParseMode.HTML,
        reply_markup=inline_kb,
    )
    await bot.send_message(chat_id, bold("👇 Choose an option:"), parse_mode=ParseMode.HTML, reply_markup=user_reply_keyboard())

async def tutorial_user(q, context):
    await answer(q)
    con = db()
    try:
        row = con.execute("SELECT * FROM tutorial WHERE id=1").fetchone()
    finally:
        con.close()

    if not row or (not row["video_file_id"] and not row["text"]):
        await q.message.reply_text(
            bold("✅ How to Get Premium is not configured yet."),
            parse_mode=ParseMode.HTML,
        )
        return

    if row["video_file_id"]:
        try:
            await context.bot.send_video(q.message.chat_id, row["video_file_id"])
        except TelegramError:
            pass
    if row["text"]:
        await context.bot.send_message(
            q.message.chat_id,
            bold(esc(row["text"])),
            parse_mode=ParseMode.HTML,
        )
    await context.bot.send_message(
        q.message.chat_id,
        bold("✅ HOW TO GET PREMIUM"),
        parse_mode=ParseMode.HTML,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    register_user(update.effective_user)
    await send_start_screen(context.bot, update.effective_chat.id)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text(bold("⛔ Admin access required"), parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        bold("🔐 Admin Panel"),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_main_keyboard(),
    )


async def edit_or_send(q, text, keyboard=None):
    try:
        await q.edit_message_text(
            bold(text), parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except TelegramError:
        await q.message.reply_text(
            bold(text), parse_mode=ParseMode.HTML, reply_markup=keyboard
        )


def plan_heading(name):
    value = (name or "").strip().upper()
    return value if value.endswith(" PLAN") else f"{value} PLAN"


def get_plans(category):
    con = db()
    try:
        return con.execute(
            "SELECT * FROM plans WHERE enabled=1 AND category=? ORDER BY id",
            (category,),
        ).fetchall()
    finally:
        con.close()


async def send_purchase_broadcast(bot, payment_id, plan_name, screenshot_file_id):
    """Broadcast an approved purchase to all registered users.

    Uses the verified payment screenshot and live plan buttons. Errors are
    isolated per user so one blocked/deleted chat cannot stop the broadcast.
    """
    try:
        con = db()
        try:
            users = con.execute("SELECT id FROM users").fetchall()
        finally:
            con.close()

        plans = get_plans("choose") + get_plans("pro")
        rows = [[style_button(f"💎 {styled_plan_name(plan['name'], plan['font_style'] if 'font_style' in plan.keys() else 'normal')}", f"user:plan:{plan['id']}", style=["success", "danger", "primary"][i % 3])] for i, plan in enumerate(plans)]
        keyboard = InlineKeyboardMarkup(rows) if rows else None

        caption = (
            "🎉 <b>NEW PURCHASE SUCCESS</b>\n\n"
            f"💎 <b>PLAN NAME:</b> {esc(plan_name)}\n"
            f"🧾 <b>PAYMENT ID:</b> #{payment_id}\n\n"
            "✅ <b>Payment verified by admin.</b>"
        )

        success = 0
        failed = 0
        for row in users:
            chat_id = row["id"]
            try:
                if screenshot_file_id:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=screenshot_file_id,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                success += 1
            except RetryAfter as exc:
                try:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
                    if screenshot_file_id:
                        await bot.send_photo(chat_id=chat_id, photo=screenshot_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
                    else:
                        await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML, reply_mar
