#!/usr/bin/env python3
"""
Advanced Telegram group bot (PTB v20 compatible)

Features:
- /q -> convert image or text to sticker
- /kang -> try to add to user's sticker pack (create if needed), fallback save/send
- /bansticker, /allowsticker, /liststickers
- blacklist add/remove/list and auto-delete
- anti-link toggle
- flood control, warns, mute on threshold
- /all -> mention recent members bot has seen
- /pin, /unpin, /purge, /add (invite), /lock, /unlock
- /promote, /demote
- /info -> info about user
- react-on-start (best effort)
- SQLite DB in repo dir
"""
import os
import io
import re
import time
import sqlite3
import logging
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ChatPermissions,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# CONFIG
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = Path("advanced_bot.db")
STICKERS_DIR = Path("stickers")
STICKERS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# moderation config
FLOOD_LIMIT = 6
FLOOD_WINDOW = 6  # seconds
FLOOD_MUTE_SECS = 60
WARN_THRESHOLD = 3
WARN_MUTE_SECS = 600

_recent = {}  # chat_id -> user_id -> [timestamps]


# -------------------------
# Database helpers
# -------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS banned_stickers (file_unique_id TEXT UNIQUE)")
    cur.execute("CREATE TABLE IF NOT EXISTS warnings (chat_id INTEGER, user_id INTEGER, warns INTEGER, PRIMARY KEY(chat_id,user_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS settings (chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id,key))")
    cur.execute("CREATE TABLE IF NOT EXISTS members (chat_id INTEGER, user_id INTEGER, name TEXT, PRIMARY KEY(chat_id,user_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS blacklist (chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id,word))")
    con.commit()
    con.close()


def db_set(chat_id: int, key: str, value: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (chat_id, key, value))
    con.commit()
    con.close()


def db_get(chat_id: int, key: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key))
    r = cur.fetchone()
    con.close()
    return r[0] if r else None


def banned_add(uid: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO banned_stickers VALUES (?)", (uid,))
    con.commit()
    con.close()


def banned_remove(uid: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM banned_stickers WHERE file_unique_id=?", (uid,))
    con.commit()
    con.close()


def banned_list():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT file_unique_id FROM banned_stickers")
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return rows


def banned_check(uid: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM banned_stickers WHERE file_unique_id=?", (uid,))
    r = cur.fetchone()
    con.close()
    return bool(r)


def warn_user(chat_id: int, user_id: int) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT warns FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    r = cur.fetchone()
    if r:
        nw = r[0] + 1
        cur.execute("UPDATE warnings SET warns=? WHERE chat_id=? AND user_id=?", (nw, chat_id, user_id))
    else:
        nw = 1
        cur.execute("INSERT INTO warnings VALUES (?,?,?)", (chat_id, user_id, nw))
    con.commit()
    con.close()
    return nw


def warnings_of(chat_id: int, user_id: int) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT warns FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    r = cur.fetchone()
    con.close()
    return r[0] if r else 0


def members_add(chat_id: int, user_id: int, name: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO members VALUES (?,?,?)", (chat_id, user_id, name))
    con.commit()
    con.close()


def members_recent(chat_id: int, limit: int = 50):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT user_id, name FROM members WHERE chat_id=? ORDER BY rowid DESC LIMIT ?", (chat_id, limit))
    rows = cur.fetchall()
    con.close()
    return rows


def blacklist_add(chat_id: int, word: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO blacklist VALUES (?,?)", (chat_id, word.lower()))
    con.commit()
    con.close()


def blacklist_remove(chat_id: int, word: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM blacklist WHERE chat_id=? AND word=?", (chat_id, word.lower()))
    con.commit()
    con.close()


def blacklist_list(chat_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT word FROM blacklist WHERE chat_id=?", (chat_id,))
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return rows


def contains_black(chat_id: int, text: str):
    if not text:
        return False, None
    words = blacklist_list(chat_id)
    if not words:
        return False, None
    t = text.lower()
    for w in words:
        if re.search(r"\b" + re.escape(w) + r"\b", t):
            return True, w
    return False, None


# -------------------------
# File helpers (PIL)
# -------------------------
async def file_bytes(file_obj):
    bio = io.BytesIO()
    try:
        await file_obj.download_to_memory(out=bio)
    except Exception:
        await file_obj.download(out=bio)
    bio.seek(0)
    return bio.read()


def img_to_webp(raw: bytes):
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    max_dim = 512
    w, h = img.size
    scale = min(max_dim / w, max_dim / h, 1)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (max_dim, max_dim), (0, 0, 0, 0))
    canvas.paste(img, ((max_dim - new_w) // 2, (max_dim - new_h) // 2), img)
    out = io.BytesIO()
    canvas.save(out, "WEBP", lossless=True)
    out.seek(0)
    return out


def text_to_webp_image(text: str, avatar_bytes: bytes | None = None):
    max_dim = 512
    canvas = Image.new("RGBA", (max_dim, max_dim), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font = None
    for p in ("/system/fonts/Roboto-Regular.ttf", "/system/fonts/DroidSans.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(p, 36)
            break
        except Exception:
            font = None
    if not font:
        font = ImageFont.load_default()

    y = 8
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            av.thumbnail((110, 110), Image.LANCZOS)
            canvas.paste(av, ((max_dim - av.width) // 2, 8), av)
            y = 8 + av.height + 6
        except Exception:
            y = 30

    words = text.split()
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] > max_dim - 40 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)

    line_heights = [draw.textbbox((0, 0), ln, font=font)[3] - draw.textbbox((0, 0), ln, font=font)[1] for ln in lines]
    total_h = sum(line_heights) + (len(lines) - 1) * 6
    start_y = y + max(0, (max_dim - y - total_h) // 2)
    yy = start_y
    for ln, lh in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), ln, font=font)
        w_text = bbox[2] - bbox[0]
        x = (max_dim - w_text) // 2
        draw.text((x, yy), ln, font=font, fill=(255, 255, 255, 255))
        yy += lh + 6

    out = io.BytesIO()
    canvas.save(out, "WEBP", lossless=True)
    out.seek(0)
    return out


# -------------------------
# Utility / permission checks
# -------------------------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int | None = None) -> bool:
    try:
        chat_id = update.effective_chat.id
        uid = user_id or update.effective_user.id
        member = await context.bot.get_chat_member(chat_id, uid)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def try_set_reaction(bot, chat_id: int, message_id: int, emoji: str) -> bool:
    try:
        fn = getattr(bot, "set_message_reaction", None)
        if fn:
            try:
                res = await fn(chat_id=chat_id, message_id=message_id, reaction_types=[emoji], is_big=False)
                return bool(res)
            except TypeError:
                res = await fn(chat_id, message_id, [emoji], False)
                return bool(res)
    except Exception:
        log.debug("native reaction failed", exc_info=True)
    return False


# -------------------------
# Handlers
# -------------------------
HELP_PAGES = [
    "Stickers: /q /kang /bansticker /allowsticker /liststickers",
    "Moderation: /warn /warnings /mute /unmute /kick /ban /unban /promote /demote",
    "Group: /all /pin /unpin /add /purge /lock /unlock /blacklist /info /react",
]


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    try:
        if msg:
            ok = await try_set_reaction(context.bot, update.effective_chat.id, msg.message_id, "✅")
            if not ok:
                await msg.reply_text("👋", reply_to_message_id=msg.message_id)
    except Exception:
        pass

    try:
        me = await context.bot.get_me()
        username = getattr(me, "username", None)
    except Exception:
        username = None

    buttons = []
    if username:
        buttons.append([InlineKeyboardButton("➕ Add to group", url=f"https://t.me/{username}?startgroup=true")])
    buttons.append([InlineKeyboardButton("Help ▶", callback_data="help:0"), InlineKeyboardButton("Rules", callback_data="rules")])
    kb = InlineKeyboardMarkup(buttons)
    try:
        await msg.reply_text("Welcome — use the buttons.", reply_markup=kb)
    except Exception:
        pass


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("help:"):
        idx = int(data.split(":", 1)[1])
        if 0 <= idx < len(HELP_PAGES):
            nav = []
            if idx > 0:
                nav.append(InlineKeyboardButton("◀", callback_data=f"help:{idx-1}"))
            if idx < len(HELP_PAGES) - 1:
                nav.append(InlineKeyboardButton("▶", callback_data=f"help:{idx+1}"))
            nav.append(InlineKeyboardButton("Close", callback_data="help:close"))
            await q.edit_message_text(HELP_PAGES[idx], reply_markup=InlineKeyboardMarkup([nav]))
    elif data == "help:close":
        try:
            await q.message.delete()
        except Exception:
            pass
    elif data == "rules":
        rules = db_get(q.message.chat.id, "rules") or "No rules set."
        await q.message.reply_text(rules)


# --- sticker management
async def bansticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    r = update.effective_message.reply_to_message
    if not r or not r.sticker:
        return await update.effective_message.reply_text("Reply to a sticker.")
    banned_add(r.sticker.file_unique_id)
    await update.effective_message.reply_text("Sticker banned.")


async def allowsticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    r = update.effective_message.reply_to_message
    if not r or not r.sticker:
        return await update.effective_message.reply_text("Reply to a sticker.")
    banned_remove(r.sticker.file_unique_id)
    await update.effective_message.reply_text("Sticker unbanned.")


async def liststickers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = banned_list()
    if not rows:
        return await update.effective_message.reply_text("No banned stickers.")
    txt = "Banned stickers:\n" + "\n".join(rows[:200])
    await update.effective_message.reply_text(txt)


async def sticker_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.sticker:
        return
    if banned_check(msg.sticker.file_unique_id):
        try:
            await msg.delete()
        except Exception:
            pass


# --- /q: convert image or text to sticker
async def q_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    r = msg.reply_to_message if msg else None
    if not r:
        return await msg.reply_text("Reply to an image (or reply to text to create a text-sticker).")
    if r.text and not (r.photo or getattr(r, "document", None)):
        avatar = None
        try:
            ph = await context.bot.get_user_profile_photos(r.from_user.id, limit=1)
            if getattr(ph, "total_count", 0) and ph.photos:
                f = await ph.photos[0][-1].get_file()
                avatar = await file_bytes(f)
        except Exception:
            avatar = None
        webp = text_to_webp_image(r.text, avatar)
        try:
            await context.bot.send_sticker(update.effective_chat.id, webp, reply_to_message_id=msg.message_id)
        except Exception:
            await msg.reply_text("Failed to send text-sticker.")
        return
    if r.sticker:
        try:
            await context.bot.send_sticker(update.effective_chat.id, r.sticker.file_id, reply_to_message_id=msg.message_id)
            return
        except Exception:
            pass
    file_obj = None
    if r.photo:
        file_obj = await r.photo[-1].get_file()
    elif getattr(r, "document", None) and getattr(r.document, "mime_type", "").startswith("image"):
        file_obj = await r.document.get_file()
    else:
        return await msg.reply_text("Reply to an image or text.")
    try:
        raw = await file_bytes(file_obj)
        webp = img_to_webp(raw)
        await context.bot.send_sticker(update.effective_chat.id, webp, reply_to_message_id=msg.message_id)
    except Exception:
        await msg.reply_text("Sticker convert failed.")


# --- /kang: try add sticker to user's pack (create if needed). robustly handles API variants.
async def _try_add_sticker_set(bot, user_id: int, set_name: str, input_file: InputFile, emojis: str):
    try:
        await bot.add_sticker_to_set(user_id=user_id, name=set_name, png_sticker=input_file, emojis=emojis)
        return True, None
    except TypeError:
        try:
            await bot.add_sticker_to_set(user_id=user_id, name=set_name, sticker=input_file, emojis=emojis)
            return True, None
        except Exception as e:
            return False, str(e)
    except Exception as e:
        return False, str(e)


async def _try_create_sticker_set(bot, user_id: int, set_name: str, title: str, input_file: InputFile, emojis: str):
    try:
        await bot.create_new_sticker_set(user_id=user_id, name=set_name, title=title, png_sticker=input_file, emojis=emojis)
        return True, None
    except TypeError:
        try:
            await bot.create_new_sticker_set(user_id=user_id, name=set_name, title=title, sticker=input_file, emojis=emojis)
            return True, None
        except Exception as e:
            return False, str(e)
    except Exception as e:
        return False, str(e)


async def kang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    r = msg.reply_to_message if msg else None
    if not r:
        return await msg.reply_text("Reply to an image or sticker to kang.")
    emojis = (context.args and context.args[0]) or "🙂"
    user = update.effective_user
    uid = user.id
    try:
        if r.sticker:
            f = await r.sticker.get_file()
            raw = await file_bytes(f)
            webp_io = io.BytesIO(raw)
        else:
            if r.photo:
                f = await r.photo[-1].get_file()
            elif getattr(r, "document", None) and getattr(r.document, "mime_type", "").startswith("image"):
                f = await r.document.get_file()
            else:
                return await msg.reply_text("Reply to an image.")
            raw = await file_bytes(f)
            webp_io = img_to_webp(raw)
    except Exception as e:
        log.exception("kang: read file")
        return await msg.reply_text("Failed to read the image.")
    try:
        fname = STICKERS_DIR / f"sticker_{int(time.time())}.webp"
        with open(fname, "wb") as fh:
            fh.write(webp_io.getvalue())
    except Exception:
        log.exception("kang: save local")
    webp_io.seek(0)
    input_file = InputFile(webp_io, filename="sticker.webp")
    try:
        me = await context.bot.get_me()
        bot_username = getattr(me, "username", "bot")
    except Exception:
        bot_username = "bot"
    base = f"user_{uid}_by_{bot_username}"
    pack_name = re.sub(r"[^A-Za-z0-9_]", "_", base)[:64]
    pack_title = f"{user.first_name}'s stickers"
    ok, err = await _try_add_sticker_set(context.bot, uid, pack_name, input_file, emojis)
    if ok:
        return await msg.reply_text(f"Added to your pack: `{pack_name}`", parse_mode="Markdown")
    created, errc = await _try_create_sticker_set(context.bot, uid, pack_name, pack_title, input_file, emojis)
    if created:
        return await msg.reply_text(f"Created pack and added: `{pack_name}`", parse_mode="Markdown")
    ok2, err2 = await _try_add_sticker_set(context.bot, uid, pack_name, input_file, emojis)
    if ok2:
        return await msg.reply_text(f"Added to your pack: `{pack_name}`", parse_mode="Markdown")
    try:
        webp_io.seek(0)
        await context.bot.send_sticker(update.effective_chat.id, webp_io)
    except Exception:
        pass
    err_msg = err or errc or err2 or "unknown"
    await msg.reply_text("Saved locally but could not add to your pack. Details: " + str(err_msg))


# -------------------------
# Moderation & utilities
# -------------------------
async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    r = update.effective_message.reply_to_message
    if not r:
        return await update.effective_message.reply_text("Reply to a user.")
    uid = r.from_user.id
    chat = update.effective_chat.id
    w = warn_user(chat, uid)
    await update.effective_message.reply_text(f"Warned — total {w}")
    if w >= WARN_THRESHOLD:
        try:
            await context.bot.restrict_chat_member(chat, uid, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + WARN_MUTE_SECS)
            await update.effective_message.reply_text("Auto-muted for warnings.")
        except Exception:
            pass


async def warnings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.effective_message.reply_to_message
    if not r:
        return await update.effective_message.reply_to_message("Reply to a user.")
    w = warnings_of(update.effective_chat.id, r.from_user.id)
    await update.effective_message.reply_text(f"Warnings: {w}")


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    r = update.effective_message.reply_to_message
    if not r:
        return
    try:
        mins = int(context.args[0]) if context.args else 10
    except Exception:
        mins = 10
    uid = r.from_user.id
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, uid, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + mins * 60)
        await update.effective_message.reply_text(f"Muted {mins} min.")
    except Exception as e:
        await update.effective_message.reply_text("Mute failed: " + str(e))


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    r = update.effective_message.reply_to_message
    if not r:
        return
    uid = r.from_user.id
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, uid, permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True), until_date=0)
        await update.effective_message.reply_text("Unmuted.")
    except Exception as e:
        await update.effective_message.reply_text("Unmute failed: " + str(e))


async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    r = update.effective_message.reply_to_message
    if not r:
        return
    uid = r.from_user.id
    chat = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat, uid, until_date=int(time.time()) + 5)
        await context.bot.unban_chat_member(chat, uid)
        await update.effective_message.reply_text("Kicked.")
    except Exception as e:
        await update.effective_message.reply_text("Kick failed: " + str(e))


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    r = update.effective_message.reply_to_message
    if not r:
        return
    uid = r.from_user.id
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        await update.effective_message.reply_text("Banned.")
    except Exception as e:
        await update.effective_message.reply_text("Ban failed: " + str(e))


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_to_message("Admins only.")
    if not context.args:
        return await update.effective_message.reply_text("Use /unban <user_id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.effective_message.reply_text("Invalid id.")
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid)
        await update.effective_message.reply_text("Unbanned.")
    except Exception as e:
        await update.effective_message.reply_text("Unban failed: " + str(e))


async def promote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    r = update.effective_message.reply_to_message
    if not r:
        return
    uid = r.from_user.id
    try:
        await context.bot.promote_chat_member(update.effective_chat.id, uid, can_change_info=True, can_delete_messages=True, can_invite_users=True, can_restrict_members=True, can_pin_messages=True, is_anonymous=False)
        await update.effective_message.reply_text("Promoted.")
    except Exception as e:
        await update.effective_message.reply_text("Promote failed: " + str(e))


async def demote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    r = update.effective_message.reply_to_message
    if not r:
        return
    uid = r.from_user.id
    try:
        await context.bot.promote_chat_member(update.effective_chat.id, uid, can_change_info=False, can_delete_messages=False, can_invite_users=False, can_restrict_members=False, can_pin_messages=False, is_anonymous=False, can_promote_members=False)
        await update.effective_message.reply_text("Demoted.")
    except Exception as e:
        await update.effective_message.reply_text("Demote failed: " + str(e))


# rules / welcome / antilink
async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    txt = " ".join(context.args)
    if not txt:
        return await update.effective_message.reply_text("Usage: /setrules <text>")
    db_set(update.effective_chat.id, "rules", txt)
    await update.effective_message.reply_text("Rules saved.")


async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = db_get(update.effective_chat.id, "rules")
    await update.effective_message.reply_text(r or "No rules set.")


async def antilink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    arg = (context.args[0] if context.args else "").lower()
    if arg not in ("on", "off"):
        return await update.effective_message.reply_text("Usage: /antilink on|off")
    db_set(update.effective_chat.id, "antilink", arg)
    await update.effective_message.reply_text(f"Anti-link: {arg}")


async def setwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_to_user("Admins only.")
    txt = " ".join(context.args)
    if not txt:
        return await update.effective_message.reply_text("Use /setwelcome text (use {name})")
    db_set(update.effective_chat.id, "welcome", txt)
    await update.effective_message.reply_text("Welcome message saved.")


async def welcome_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    arg = (context.args[0] if context.args else "").lower()
    if arg not in ("on", "off"):
        return await update.effective_message.reply_text("Use /welcome on|off")
    db_set(update.effective_chat.id, "welcome_on", arg)
    await update.effective_message.reply_text(f"Welcome: {arg}")


async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for m in update.effective_message.new_chat_members:
        chat = update.effective_chat.id
        if db_get(chat, "welcome_on") == "on":
            tpl = db_get(chat, "welcome") or "Welcome {name}!"
            text = tpl.replace("{name}", m.full_name)
            try:
                await update.effective_message.reply_text(text)
            except Exception:
                pass


# react command
async def react_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message.reply_to_message:
        return await update.effective_message.reply_text("Reply to a message and use /react <emoji>")
    emoji = (context.args and context.args[0]) or "👍"
    target = update.effective_message.reply_to_message
    ok = await try_set_reaction(context.bot, target.chat.id, target.message_id, emoji)
    if ok:
        await update.effective_message.reply_text("Reacted ✅", reply_to_message_id=update.effective_message.message_id)
    else:
        try:
            await update.effective_message.reply_text(emoji, reply_to_message_id=target.message_id)
        except Exception:
            pass


# info
async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = None
    if update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user
    elif context.args:
        try:
            uid = int(context.args[0])
            target = await context.bot.get_chat(uid)
        except Exception:
            pass
    if not target:
        return await update.effective_message.reply_text("Reply to a user or /info <id>")

    info_lines = [f"ID: `{getattr(target, 'id', '')}`", f"Name: {getattr(target, 'full_name', getattr(target, 'first_name', ''))}", f"Bot: {getattr(target, 'is_bot', False)}"]
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, getattr(target, "id", None))
        info_lines.append(f"Status: {member.status}")
    except Exception:
        pass
    await update.effective_message.reply_text("\n".join(info_lines), parse_mode="Markdown")


# group utilities
async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = members_recent(update.effective_chat.id, limit=100)
    if not rows:
        return await update.effective_message.reply_text("No members recorded yet.")
    parts = [f"[{(n or 'user')[:30]}](tg://user?id={uid})" for uid, n in rows]
    text = " ".join(parts)
    MAX = 3800
    if len(text) <= MAX:
        await update.effective_message.reply_text(text, parse_mode="Markdown")
        return
    cur = ""
    for p in parts:
        if len(cur) + len(p) + 1 > MAX:
            await update.effective_message.reply_text(cur, parse_mode="Markdown")
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        await update.effective_message.reply_text(cur, parse_mode="Markdown")


async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    if not update.effective_message.reply_to_message:
        return await update.effective_message.reply_to_message("Reply to a message to pin it.")
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.effective_message.reply_to_message.message_id, disable_notification=False)
        await update.effective_message.reply_text("Pinned.")
    except Exception as e:
        await update.effective_message.reply_to_user("Pin failed: " + str(e))


async def unpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_to_text("Admins only.")
    try:
        if update.effective_message.reply_to_message:
            await context.bot.unpin_chat_message(update.effective_chat.id, message_id=update.effective_message.reply_to_message.message_id)
        else:
            await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        await update.effective_message.reply_text("Unpinned.")
    except Exception as e:
        await update.effective_message.reply_text("Unpin failed: " + str(e))


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_text("Admins only.")
    try:
        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await update.effective_message.reply_text("Invite: " + link.invite_link)
    except Exception as e:
        await update.effective_message.reply_text("Invite failed: " + str(e))


async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_to_text("Admins only.")
    if not update.effective_message.reply_to_message:
        return await update.effective_message.reply_to_text("Reply to the oldest message to delete up to this command.")
    start_id = update.effective_message.reply_to_message.message_id
    end_id = update.effective_message.message_id
    chat_id = update.effective_chat.id
    deleted = 0
    for mid in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass
    await update.effective_message.reply_text(f"Purge attempted. Deleted approx: {deleted}")


async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_to_text("Admins only.")
    try:
        perms = ChatPermissions(can_send_messages=False, can_send_media_messages=False, can_send_other_messages=False, can_add_web_page_previews=False)
        await context.bot.set_chat_permissions(update.effective_chat.id, perms)
        await update.effective_message.reply_text("Group locked for non-admins.")
    except Exception as e:
        await update.effective_message.reply_text("Lock failed: " + str(e))


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return await update.effective_message.reply_to_text("Admins only.")
    try:
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
        await context.bot.set_chat_permissions(update.effective_chat.id, perms)
        await update.effective_message.reply_text("Unlock failed: " + str(e))
    except Exception:
        await update.effective_message.reply_text("Group unlocked for all members.")


# automod: anti-link, blacklist, flood
LINK_RE = re.compile(r"(https?://|t\.me|telegram\.me)", re.I)


async def auto_mod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    if msg.from_user and getattr(msg.from_user, "is_bot", False):
        return
    try:
        members_add(msg.chat.id, msg.from_user.id, msg.from_user.full_name)
    except Exception:
        pass
    if msg.sticker:
        await sticker_auto(update, context)
        return
    if msg.text:
        b, word = contains_black(msg.chat.id, msg.text)
        if b and not await is_admin(update, context):
            try:
                await msg.delete()
            except Exception:
                pass
            warn_user(msg.chat.id, msg.from_user.id)
            await msg.reply_text(f"Deleted blacklisted word: {word}")
            return
        if db_get(msg.chat.id, "antilink") == "on" and not await is_admin(update, context):
            if LINK_RE.search(msg.text):
                try:
                    await msg.delete()
                except Exception:
                    pass
                await msg.reply_text("Links not allowed.")
                return
    chat = msg.chat.id
    uid = msg.from_user.id
    now = time.time()
    d = _recent.setdefault(chat, {}).setdefault(uid, [])
    d[:] = [x for x in d if x > now - FLOOD_WINDOW]
    d.append(now)
    if len(d) >= FLOOD_LIMIT and not await is_admin(update, context):
        try:
            await context.bot.restrict_chat_member(chat, uid, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + FLOOD_MUTE_SECS)
        except Exception:
            pass
        d.clear()
        await msg.reply_text("Muted for flooding.")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await auto_mod(update, context)
    msg = update.effective_message
    if msg and msg.text and msg.text.strip().lower() == "start":
        try:
            ok = await try_set_reaction(context.bot, update.effective_chat.id, msg.message_id, "✅")
            if not ok:
                await msg.reply_text("✅", reply_to_message_id=msg.message_id)
        except Exception:
            pass


# -------------------------
# Main
# -------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN environment variable")
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cb_help, pattern="^(help:|rules)"))

    app.add_handler(CommandHandler("bansticker", bansticker_cmd))
    app.add_handler(CommandHandler("allowsticker", allowsticker_cmd))
    app.add_handler(CommandHandler("liststickers", liststickers_cmd))

    app.add_handler(CommandHandler("q", q_cmd))
    app.add_handler(CommandHandler("kang", kang_cmd))

    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("warnings", warnings_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("promote", promote_cmd))
    app.add_handler(CommandHandler("demote", demote_cmd))

    app.add_handler(CommandHandler("setrules", setrules_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("antilink", antilink_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome_cmd))
    app.add_handler(CommandHandler("welcome", welcome_toggle))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_member))

    app.add_handler(CommandHandler("react", react_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("all", all_cmd))
    app.add_handler(CommandHandler("pin", pin_cmd))
    app.add_handler(CommandHandler("unpin", unpin_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("purge", purge_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))

    async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            return await update.effective_message.reply_text("Usage: /blacklist add|remove|list <word>")
        sub = context.args[0].lower()
        if sub == "add" and len(context.args) >= 2:
            w = context.args[1].lower()
            if not await is_admin(update, context):
                return await update.effective_message.reply_text("Admins only.")
            blacklist_add(update.effective_chat.id, w)
            await update.effective_message.reply_text(f"Added blacklisted word: {w}")
        elif sub == "remove" and len(context.args) >= 2:
            w = context.args[1].lower()
            if not await is_admin(update, context):
                return await update.effective_message.reply_text("Admins only.")
            blacklist_remove(update.effective_chat.id, w)
            await update.effective_message.reply_text(f"Removed blacklisted word: {w}")
        elif sub == "list":
            items = blacklist_list(update.effective_chat.id)
            await update.effective_message.reply_text("Blacklisted: " + (", ".join(items) if items else "none"))
        else:
            await update.effective_message.reply_text("Usage: /blacklist add|remove|list <word>")

    app.add_handler(CommandHandler("blacklist", blacklist_cmd))

    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_handler))

    log.info("Bot running (PTB v20 compatible)")
    app.run_polling()


if __name__ == "__main__":
    main()
