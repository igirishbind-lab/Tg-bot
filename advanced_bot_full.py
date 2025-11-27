#!/usr/bin/env python3
# advanced_bot_full.py
# Compatible with python-telegram-bot v20.7 and Python 3.13+
# Removed imghdr usage. Uses Pillow for image detection and conversion.

import os
import io
import time
import re
import sqlite3
import logging
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile, ChatPermissions
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ---------------------------
# Config
# ---------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DB_PATH = Path("advanced_bot.db")
STICKERS_DIR = Path("stickers")
STICKERS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

FLOOD_LIMIT = 6
FLOOD_WINDOW = 6
FLOOD_MUTE = 60

WARN_THRESHOLD = 3
WARN_MUTE = 600

_recent = {}  # chat -> uid -> timestamps list

# ---------------------------
# DB helpers
# ---------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS banned_stickers (file_unique_id TEXT UNIQUE)")
    cur.execute("CREATE TABLE IF NOT EXISTS warnings (chat_id INTEGER, user_id INTEGER, warns INTEGER, PRIMARY KEY(chat_id,user_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS settings (chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id,key))")
    cur.execute("CREATE TABLE IF NOT EXISTS members (chat_id INTEGER, user_id INTEGER, name TEXT, PRIMARY KEY(chat_id,user_id))")
    con.commit(); con.close()

def add_banned(uid):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO banned_stickers VALUES (?)", (uid,))
    con.commit(); con.close()

def remove_banned(uid):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("DELETE FROM banned_stickers WHERE file_unique_id=?", (uid,))
    con.commit(); con.close()

def is_banned(uid):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT 1 FROM banned_stickers WHERE file_unique_id=?", (uid,))
    r = cur.fetchone(); con.close()
    return bool(r)

def warn_user(chat_id, user_id):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT warns FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    r = cur.fetchone()
    if r:
        nw = r[0] + 1
        cur.execute("UPDATE warnings SET warns=? WHERE chat_id=? AND user_id=?", (nw, chat_id, user_id))
    else:
        nw = 1
        cur.execute("INSERT INTO warnings VALUES (?,?,?)", (chat_id, user_id, nw))
    con.commit(); con.close()
    return nw

def warnings_of(chat_id, user_id):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT warns FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    r = cur.fetchone(); con.close()
    return r[0] if r else 0

def db_set_setting(chat_id, key, value):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (chat_id, key, value))
    con.commit(); con.close()

def db_get_setting(chat_id, key):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key))
    r = cur.fetchone(); con.close()
    return r[0] if r else None

def add_seen_member(chat_id, user_id, name):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO members VALUES (?,?,?)", (chat_id, user_id, name))
    con.commit(); con.close()

def get_seen_members(chat_id, limit=50):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT user_id, name FROM members WHERE chat_id=? ORDER BY rowid DESC LIMIT ?", (chat_id, limit))
    rows = cur.fetchall(); con.close()
    return rows

# ---------------------------
# Image helpers (Pillow-based)
# ---------------------------
def detect_image_format_from_bytes(raw: bytes):
    try:
        im = Image.open(io.BytesIO(raw))
        fmt = (im.format or "").lower()
        return fmt
    except Exception:
        return None

def img_to_webp_bytes(raw: bytes):
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    max_dim = 512
    w, h = img.size
    scale = min(max_dim / w, max_dim / h, 1)
    new_w = int(w * scale); new_h = int(h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (max_dim, max_dim), (0,0,0,0))
    canvas.paste(img, ((max_dim-new_w)//2, (max_dim-new_h)//2), img)
    out = io.BytesIO()
    canvas.save(out, "WEBP", lossless=True)
    out.seek(0)
    return out.getvalue()

def text_to_webp_bytes(text: str):
    max_dim = 512
    canvas = Image.new("RGBA", (max_dim, max_dim), (0,0,0,0))
    draw = ImageDraw.Draw(canvas)
    font = None
    for fpath in ("/system/fonts/Roboto-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(fpath, 36)
            break
        except Exception:
            font = None
    if not font:
        font = ImageFont.load_default()
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0,0), test, font=font)
        if bbox[2] > max_dim - 40 and cur:
            lines.append(cur); cur = w
        else:
            cur = test
    if cur: lines.append(cur)
    total_h = sum([draw.textbbox((0,0), ln, font=font)[3] - draw.textbbox((0,0), ln, font=font)[1] for ln in lines]) + (len(lines)-1)*6
    y = (max_dim - total_h)//2
    for ln in lines:
        bbox = draw.textbbox((0,0), ln, font=font)
        w_text = bbox[2] - bbox[0]
        x = (max_dim - w_text)//2
        draw.text((x, y), ln, font=font, fill=(255,255,255,255))
        y += bbox[3] - bbox[1] + 6
    out = io.BytesIO()
    canvas.save(out, "WEBP", lossless=True)
    out.seek(0)
    return out.getvalue()

async def download_file_bytes(file_obj):
    bio = io.BytesIO()
    try:
        await file_obj.download_to_memory(out=bio)
    except Exception:
        await file_obj.download(out=bio)
    bio.seek(0)
    return bio.read()

# ---------------------------
# Utilities
# ---------------------------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id=None):
    try:
        chat = update.effective_chat.id
        uid = user_id or update.effective_user.id
        mem = await context.bot.get_chat_member(chat, uid)
        return mem.status in ("administrator", "creator")
    except Exception:
        return False

async def try_set_reaction(bot, chat_id, message_id, emoji):
    # Some wrappers don't implement reactions; try and ignore errors
    try:
        # some environments/wrappers might have set_message_reaction
        # if not, this will raise and we fallback
        return await bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction_types=[emoji])
    except Exception:
        return False

# ---------------------------
# Command handlers
# ---------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("👋", reply_to_message_id=update.message.message_id)
    except Exception:
        pass
    try:
        me = await context.bot.get_me(); username = getattr(me, "username", None)
    except Exception:
        username = None
    buttons = []
    if username:
        add_group_url = f"https://t.me/{username}?startgroup=true"
        buttons.append([InlineKeyboardButton("➕ Add to group", url=add_group_url)])
    buttons.append([InlineKeyboardButton("Help ▶", callback_data="help:0"), InlineKeyboardButton("Rules", callback_data="rules")])
    kb = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Welcome — use the buttons below.", reply_markup=kb)

HELP_PAGES = [
"/bansticker — Reply to sticker to ban it\n/allowsticker — Reply to sticker to unban\n/liststickers — List banned sticker ids\n/q — Reply to image or text to make sticker\n/kang — Reply to image/sticker to add to your pack",
"/warn /warnings /mute /unmute /kick /ban /unban\n/setrules /rules /antilink on|off /setwelcome /welcome on|off",
"/all — mention recent members\n/pin — reply to message to pin\n/add — create invite link\n/purge — reply to earliest message to delete range\n/lock /unlock\n/react — reply to message with /react <emoji>\n/info — reply to user or /info <user_id>"
]

async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("help:"):
        idx = int(data.split(":")[1])
        text = HELP_PAGES[idx]
        buttons = []
        if idx > 0: buttons.append(InlineKeyboardButton("◀", callback_data=f"help:{idx-1}"))
        if idx < len(HELP_PAGES)-1: buttons.append(InlineKeyboardButton("▶", callback_data=f"help:{idx+1}"))
        buttons.append(InlineKeyboardButton("Close", callback_data="help:close"))
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([buttons]))
    elif data == "help:close":
        try: await q.message.delete()
        except: pass
    elif data == "rules":
        r = db_get_setting(q.message.chat.id, "rules") or "No rules set."
        await q.message.reply_text(r)

# Sticker management
async def bansticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r or not r.sticker: return await update.message.reply_text("Reply to sticker.")
    add_banned(r.sticker.file_unique_id)
    await update.message.reply_text("Sticker banned.")

async def allowsticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r or not r.sticker: return await update.message.reply_text("Reply to sticker.")
    remove_banned(r.sticker.file_unique_id)
    await update.message.reply_text("Sticker unbanned.")

async def liststickers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT file_unique_id FROM banned_stickers")
    rows = [r[0] for r in cur.fetchall()]; con.close()
    if not rows: return await update.message.reply_text("No banned stickers.")
    await update.message.reply_text("Banned stickers:\n" + "\n".join(rows[:100]))

async def sticker_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.sticker: return
    if is_banned(msg.sticker.file_unique_id):
        try: await msg.delete()
        except: pass

# /q command - create sticker from image or text
async def q_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to an image or to text.")
    # text -> sticker
    if r.text and not (r.photo or r.document):
        webp = text_to_webp_bytes(r.text)
        try:
            await context.bot.send_sticker(update.effective_chat.id, InputFile(io.BytesIO(webp), filename="st.webp"), reply_to_message_id=update.message.message_id)
        except Exception:
            await update.message.reply_text("Failed to send text sticker.")
        return
    # sticker -> resend
    if r.sticker:
        return await context.bot.send_sticker(update.effective_chat.id, r.sticker.file_id, reply_to_message_id=update.message.message_id)
    # image/document -> convert
    f = None
    if r.photo:
        f = await r.photo[-1].get_file()
    elif r.document and getattr(r.document, "mime_type", "").startswith("image"):
        f = await r.document.get_file()
    else:
        return await update.message.reply_text("Reply to an image or text.")
    raw = await download_file_bytes(f)
    webp = img_to_webp_bytes(raw)
    try:
        await context.bot.send_sticker(update.effective_chat.id, InputFile(io.BytesIO(webp), filename="st.webp"), reply_to_message_id=update.message.message_id)
    except Exception:
        await update.message.reply_text("Sticker convert fail.")

# /kang - try to add to user's sticker pack (create if missing)
async def kang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to an image or sticker.")
    emoji = (context.args and context.args[0]) or "🙂"
    user = update.effective_user
    uid = user.id
    # get raw bytes
    try:
        if r.sticker:
            f = await r.sticker.get_file()
            raw = await download_file_bytes(f)
        else:
            if r.photo:
                f = await r.photo[-1].get_file()
            elif r.document and getattr(r.document, "mime_type", "").startswith("image"):
                f = await r.document.get_file()
            else:
                return await update.message.reply_text("Reply to an image.")
            raw = await download_file_bytes(f)
    except Exception as e:
        log.exception("kang: download error")
        return await update.message.reply_text("Failed to read file.")
    # convert to webp if needed
    fmt = detect_image_format_from_bytes(raw)
    if fmt not in ("webp", "png", "jpeg", "jpg"):
        return await update.message.reply_text("Unsupported image type.")
    if fmt != "webp":
        webp_bytes = img_to_webp_bytes(raw)
    else:
        webp_bytes = raw
    # save locally
    fname = STICKERS_DIR / f"st_{int(time.time())}.webp"
    try:
        with open(fname, "wb") as fh: fh.write(webp_bytes)
    except Exception:
        log.exception("kang: save")
    # build pack name
    try:
        me = await context.bot.get_me(); bot_username = getattr(me, "username", "bot")
    except Exception:
        bot_username = "bot"
    base = f"user_{uid}_by_{bot_username}"
    pack_name = re.sub(r"[^A-Za-z0-9_]", "_", base)[:64]
    pack_title = f"{user.first_name}'s pack"
    input_file = InputFile(io.BytesIO(webp_bytes), filename="sticker.webp")
    added = False
    errtxt = None
    try:
        # try add to existing pack
        await context.bot.add_sticker_to_set(user_id=uid, name=pack_name, png_sticker=input_file, emojis=emoji)
        added = True
    except Exception as e_add:
        errtxt = str(e_add)
        # try create
        try:
            input_file.seek(0)
            await context.bot.create_new_sticker_set(user_id=uid, name=pack_name, title=pack_title, png_sticker=input_file, emojis=emoji)
            added = True
        except Exception as e_create:
            errtxt = (errtxt or "") + " | " + str(e_create)
            added = False
    if added:
        await update.message.reply_text(f"Kanged and added to your pack: `{pack_name}` {emoji}", parse_mode="Markdown")
        return
    # fallback: send sticker to chat
    try:
        await context.bot.send_sticker(update.effective_chat.id, InputFile(io.BytesIO(webp_bytes), filename="st.webp"))
    except Exception:
        pass
    # helpful error
    msg = "Saved sticker locally but could not add to your pack."
    if errtxt:
        msg += f" Details: {errtxt[:300]}"
    await update.message.reply_text(msg)

# Moderation commands
async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user.")
    uid = r.from_user.id
    chat = update.effective_chat.id
    w = warn_user(chat, uid)
    await update.message.reply_text(f"Warned → total {w}")
    if w >= WARN_THRESHOLD:
        try:
            await context.bot.restrict_chat_member(chat, uid, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time())+WARN_MUTE)
            await update.message.reply_text("Auto-muted for warnings.")
        except Exception:
            pass

async def warnings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user.")
    w = warnings_of(update.effective_chat.id, r.from_user.id)
    await update.message.reply_text(f"Warnings: {w}")

async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r: return
    try:
        mins = int((context.args[0] if context.args else 10))
    except:
        mins = 10
    uid = r.from_user.id
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, uid, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time())+mins*60)
        await update.message.reply_text(f"Muted {mins} min.")
    except Exception:
        await update.message.reply_text("Mute fail.")

async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    r = update.message.reply_to_message
    if not r: return
    uid = r.from_user.id
    try:
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True)
        await context.bot.restrict_chat_member(update.effective_chat.id, uid, permissions=perms, until_date=0)
        await update.message.reply_text("Unmuted.")
    except Exception:
        await update.message.reply_text("Unmute fail.")

async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    r = update.message.reply_to_message
    if not r: return
    uid = r.from_user.id
    chat = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat, uid)
        await context.bot.unban_chat_member(chat, uid)
        await update.message.reply_text("Kicked.")
    except Exception:
        await update.message.reply_text("Kick fail.")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    r = update.message.reply_to_message
    if not r: return
    uid = r.from_user.id
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        await update.message.reply_text("Banned.")
    except Exception:
        await update.message.reply_text("Ban fail.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not context.args: return await update.message.reply_text("Use /unban <user_id>")
    try:
        uid = int(context.args[0])
    except:
        return await update.message.reply_text("Invalid user id.")
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid)
        await update.message.reply_text("Unbanned.")
    except Exception:
        await update.message.reply_text("Unban fail.")

# Rules / welcome / antilink
async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    txt = " ".join(context.args)
    if not txt: return await update.message.reply_text("Use /setrules text")
    db_set_setting(update.effective_chat.id, "rules", txt)
    await update.message.reply_text("Rules saved.")

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = db_get_setting(update.effective_chat.id, "rules")
    await update.message.reply_text(r or "No rules.")

async def antilink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    arg = (context.args[0] if context.args else "").lower()
    if arg not in ("on", "off"): return await update.message.reply_text("Use /antilink on|off")
    db_set_setting(update.effective_chat.id, "antilink", arg)
    await update.message.reply_text(f"Anti-link: {arg}")

async def setwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    txt = " ".join(context.args)
    if not txt: return await update.message.reply_text("Use /setwelcome text (use {name})")
    db_set_setting(update.effective_chat.id, "welcome", txt)
    await update.message.reply_text("Welcome saved.")

async def welcome_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    arg = (context.args[0] if context.args else "").lower()
    if arg not in ("on", "off"): return await update.message.reply_text("Use /welcome on|off")
    db_set_setting(update.effective_chat.id, "welcome_on", arg)
    await update.message.reply_text(f"Welcome: {arg}")

async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for m in update.message.new_chat_members:
        chat = update.effective_chat.id
        add_seen_member(chat, m.id, m.full_name)
        if db_get_setting(chat, "welcome_on") == "on":
            tpl = db_get_setting(chat, "welcome") or "Welcome {name}!"
            text = tpl.replace("{name}", m.full_name)
            try: await update.message.reply_text(text)
            except: pass

# react command
async def react_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to a message and use /react <emoji>")
    emoji = (context.args and context.args[0]) or "👍"
    target = update.message.reply_to_message
    try:
        ok = await try_set_reaction(context.bot, target.chat.id, target.message_id, emoji)
        if ok:
            await update.message.reply_text("Reacted ✅", reply_to_message_id=update.message.message_id)
        else:
            await update.message.reply_text(emoji, reply_to_message_id=target.message_id)
    except Exception:
        try: await update.message.reply_text(emoji, reply_to_message_id=target.message_id)
        except: pass

# /info command - info about a user (reply or id)
async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # if reply
    if update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
    elif context.args:
        try:
            uid = int(context.args[0]); u = await context.bot.get_chat(uid)
        except Exception:
            return await update.message.reply_text("Can't fetch that user.")
    else:
        u = update.effective_user
    txt = f"ID: {u.id}\nName: {u.full_name}\nUsername: @{getattr(u, 'username', '')}\nBot: {getattr(u, 'is_bot', False)}"
    await update.message.reply_text(txt)

# /all command
async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_seen_members(update.effective_chat.id, limit=50)
    if not rows: return await update.message.reply_text("No members recorded.")
    parts = []
    for uid, name in rows:
        parts.append(f"[{name or 'user'}](tg://user?id={uid})")
    text = " ".join(parts)
    # split if long
    max_len = 3800
    if len(text) <= max_len: return await update.message.reply_text(text, parse_mode="Markdown")
    cur = ""
    for p in parts:
        if len(cur) + len(p) + 1 > max_len:
            await update.message.reply_text(cur, parse_mode="Markdown"); cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur: await update.message.reply_text(cur, parse_mode="Markdown")

# pin / add / purge / lock / unlock
async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to message to pin.")
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, r.message_id, disable_notification=False)
        await update.message.reply_text("Pinned.")
    except Exception as e:
        await update.message.reply_text("Pin failed: " + str(e))

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    try:
        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await update.message.reply_text(f"Invite link: {link.invite_link}")
    except Exception as e:
        await update.message.reply_text("Create invite failed: " + str(e))

async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to earliest message to delete up to the command.")
    start_id = r.message_id; end_id = update.message.message_id
    chat_id = update.effective_chat.id
    deleted = 0
    for mid in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(chat_id, mid); deleted += 1
        except Exception:
            pass
    await update.message.reply_text(f"Attempted purge. Deleted approx: {deleted}")

async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    chat = update.effective_chat.id
    try:
        perms = ChatPermissions(can_send_messages=False, can_send_media_messages=False, can_send_other_messages=False, can_add_web_page_previews=False)
        await context.bot.set_chat_permissions(chat, perms)
        await update.message.reply_text("Group locked.")
    except Exception as e:
        await update.message.reply_text("Lock failed: " + str(e))

async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    chat = update.effective_chat.id
    try:
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
        await context.bot.set_chat_permissions(chat, perms)
        await update.message.reply_text("Group unlocked.")
    except Exception as e:
        await update.message.reply_text("Unlock failed: " + str(e))

# promote / demote
async def promote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to a user to promote.")
    uid = r.from_user.id
    try:
        await context.bot.promote_chat_member(update.effective_chat.id, uid,
                                              can_change_info=True, can_delete_messages=True,
                                              can_restrict_members=True, can_pin_messages=True, can_promote_members=False)
        await update.message.reply_text("Promoted (gave limited admin).")
    except Exception as e:
        await update.message.reply_text("Promote failed: " + str(e))

async def demote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user to demote.")
    uid = r.from_user.id
    try:
        await context.bot.promote_chat_member(update.effective_chat.id, uid,
                                              can_change_info=False, can_delete_messages=False,
                                              can_restrict_members=False, can_pin_messages=False, can_promote_members=False)
        await update.message.reply_text("Demoted.")
    except Exception as e:
        await update.message.reply_text("Demote failed: " + str(e))

# automod
LINK_RE = re.compile(r"(https?://|t\.me|telegram\.me)", re.I)
async def auto_mod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    # record
    try:
        if msg.from_user and not msg.from_user.is_bot:
            add_seen_member(msg.chat.id, msg.from_user.id, msg.from_user.full_name)
    except Exception:
        pass
    # anti-link
    if msg.text:
        if db_get_setting(msg.chat.id, "antilink") == "on":
            if not await is_admin(update, context):
                if LINK_RE.search(msg.text):
                    try: await msg.delete()
                    except: pass
                    return await update.message.reply_text("Links not allowed.")
    # flood-control
    chat = msg.chat.id; uid = msg.from_user.id; now = time.time()
    d = _recent.setdefault(chat, {}).setdefault(uid, [])
    d[:] = [x for x in d if x > now - FLOOD_WINDOW]; d.append(now)
    if len(d) >= FLOOD_LIMIT and not await is_admin(update, context):
        try:
            await context.bot.restrict_chat_member(chat, uid, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time())+FLOOD_MUTE)
        except:
            pass
        d.clear()
        await update.message.reply_text("Muted for flooding.")

# catch-all message handler
async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    # sticker moderation
    if msg.sticker:
        return await sticker_auto(update, context)
    await auto_mod(update, context)
    # quick reaction on "start" message
    txt = (msg.text or "").strip()
    if txt and txt.lower() == "start":
        try:
            ok = await try_set_reaction(context.bot, update.effective_chat.id, msg.message_id, "✅")
            if not ok:
                await msg.reply_text("✅", reply_to_message_id=msg.message_id)
        except:
            try: await msg.reply_text("✅", reply_to_message_id=msg.message_id)
            except: pass

# ---------------------------
# main
# ---------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN environment variable")
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # core
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cb_help, pattern="^(help:|rules)"))
    # sticker
    app.add_handler(CommandHandler("bansticker", bansticker))
    app.add_handler(CommandHandler("allowsticker", allowsticker))
    app.add_handler(CommandHandler("liststickers", liststickers))
    # q & kang
    app.add_handler(CommandHandler("q", q_cmd))
    app.add_handler(CommandHandler("kang", kang_cmd))
    # moderation
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("warnings", warnings_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    # rules & welcome
    app.add_handler(CommandHandler("setrules", setrules_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("antilink", antilink_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome_cmd))
    app.add_handler(CommandHandler("welcome", welcome_toggle))
    # utilities
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("react", react_cmd))
    app.add_handler(CommandHandler("all", all_cmd))
    app.add_handler(CommandHandler("pin", pin_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("purge", purge_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(CommandHandler("promote", promote_cmd))
    app.add_handler(CommandHandler("demote", demote_cmd))
    # events & messages
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_member))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), msg_handler))
    log.info("Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
