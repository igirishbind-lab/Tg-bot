#!/usr/bin/env python3
# advanced_bot_full.py
# Webhook-ready Telegram bot (Flask + python-telegram-bot)
# Features: webhook receiver, sticker ban, /q, /kang, moderation, /info, /all, pin/purge/lock, notes, welcome, antilink, flood-control

import os
import io
import time
import sqlite3
import logging
import asyncio
from pathlib import Path
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ChatPermissions
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ------------- CONFIG -------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# Render provides a public url in RENDER_EXTERNAL_URL env var usually. You may set WEBHOOK_URL manually if needed.
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL") or ""
DB_PATH = Path("bot_data.db")
STICKERS_DIR = Path("stickers")
STICKERS_DIR.mkdir(parents=True, exist_ok=True)

# moderation configs
FLOOD_LIMIT = 6
FLOOD_WINDOW = 6  # seconds
FLOOD_MUTE = 60   # seconds
WARN_THRESHOLD = 3
WARN_MUTE = 600

# setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# keep recent message timestamps for flood control
_recent = {}

# ------------- DB -------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS banned_stickers (file_unique_id TEXT UNIQUE)")
    cur.execute("CREATE TABLE IF NOT EXISTS warnings (chat_id INTEGER, user_id INTEGER, warns INTEGER, PRIMARY KEY(chat_id,user_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS settings (chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id,key))")
    cur.execute("CREATE TABLE IF NOT EXISTS notes (chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id,key))")
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
    r = cur.fetchone()
    con.close()
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
    r = cur.fetchone()
    con.close()
    return r[0] if r else 0

def db_set_setting(chat_id, key, value):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (chat_id, key, value))
    con.commit(); con.close()

def db_get_setting(chat_id, key):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key))
    r = cur.fetchone()
    con.close()
    return r[0] if r else None

def db_set_note(chat_id, key, value):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?)", (chat_id, key, value))
    con.commit(); con.close()

def db_get_note(chat_id, key):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT value FROM notes WHERE chat_id=? AND key=?", (chat_id, key))
    r = cur.fetchone()
    con.close()
    return r[0] if r else None

def db_del_note(chat_id, key):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("DELETE FROM notes WHERE chat_id=? AND key=?", (chat_id, key))
    con.commit(); con.close()

def db_list_notes(chat_id):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT key FROM notes WHERE chat_id=?", (chat_id,))
    out = [r[0] for r in cur.fetchall()]
    con.close()
    return out

def add_seen_member(chat_id, user_id, name):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO members VALUES (?,?,?)", (chat_id, user_id, name))
    con.commit(); con.close()

def get_seen_members(chat_id, limit=50):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT user_id, name FROM members WHERE chat_id=? ORDER BY rowid DESC LIMIT ?", (chat_id, limit))
    rows = cur.fetchall()
    con.close()
    return rows

# ------------- UTIL -------------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id=None):
    try:
        chat = update.effective_chat.id
        uid = user_id or update.effective_user.id
        mem = await context.bot.get_chat_member(chat, uid)
        return mem.status in ("administrator", "creator")
    except Exception:
        return False

async def file_bytes(bot_file):
    bio = io.BytesIO()
    try:
        await bot_file.download_to_memory(out=bio)
    except Exception:
        await bot_file.download(out=bio)
    bio.seek(0)
    return bio.read()

def img_to_webp(raw):
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
    return out

def text_to_webp_image(text):
    max_dim = 512
    canvas = Image.new("RGBA", (max_dim, max_dim), (0,0,0,0))
    draw = ImageDraw.Draw(canvas)
    font = None
    for fpath in ("/system/fonts/DroidSans.ttf", "/system/fonts/Roboto-Regular.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(fpath, 36); break
        except Exception:
            font = None
    if not font:
        font = ImageFont.load_default()

    words = text.split()
    lines = []; cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0,0), test, font=font)
        if bbox[2] > max_dim - 40 and cur:
            lines.append(cur); cur = w
        else:
            cur = test
    if cur: lines.append(cur)

    line_heights = [draw.textbbox((0,0), ln, font=font)[3] - draw.textbbox((0,0), ln, font=font)[1] for ln in lines]
    total_h = sum(line_heights) + (len(lines)-1)*6
    y = (max_dim - total_h)//2
    for ln, lh in zip(lines, line_heights):
        bbox = draw.textbbox((0,0), ln, font=font)
        w_text = bbox[2] - bbox[0]
        x = (max_dim - w_text)//2
        draw.text((x, y), ln, font=font, fill=(255,255,255,255))
        y += lh + 6

    out = io.BytesIO()
    canvas.save(out, "WEBP", lossless=True)
    out.seek(0)
    return out

# ------------- BOT HANDLERS -------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        me = await context.bot.get_me()
        username = getattr(me, "username", None)
    except:
        username = None

    buttons = []
    if username:
        buttons.append([InlineKeyboardButton("➕ Add to group", url=f"https://t.me/{username}?startgroup=true")])
    buttons.append([InlineKeyboardButton("Help ▶", callback_data="help:0"), InlineKeyboardButton("Rules", callback_data="rules")])
    kb = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Welcome — Bot running on webhook. Use buttons below.", reply_markup=kb)

HELP = [
"/bansticker (reply) — ban sticker\n/allowsticker (reply) — unban\n/liststickers — list banned\n/q (reply to text/image) — make sticker\n/kang (reply to image/sticker) — add to your pack",
"/warn (reply) — warn user\n/warnings (reply) — show warns\n/mute (reply) — mute user\n/unmute (reply)\n/kick (reply)\n/ban (reply)\n/unban <id>",
"/all — mention recent seen members\n/pin (reply) — pin\n/add — create invite link\n/purge (reply earliest) — delete range\n/lock /unlock — lock group"
]

async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data or ""
    if d.startswith("help:"):
        idx = int(d.split(":")[1])
        text = HELP[idx]
        btn = []
        if idx>0: btn.append(InlineKeyboardButton("◀", callback_data=f"help:{idx-1}"))
        if idx < len(HELP)-1: btn.append(InlineKeyboardButton("▶", callback_data=f"help:{idx+1}"))
        btn.append(InlineKeyboardButton("Close", callback_data="help:close"))
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([btn]))
    elif d == "help:close":
        try: await q.message.delete()
        except: pass
    elif d == "rules":
        r = db_get_setting(q.message.chat.id, "rules") or "No rules set."
        await q.message.reply_text(r)

# sticker management
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
    txt = "Banned stickers:\n" + "\n".join(rows[:50])
    await update.message.reply_text(txt)

async def sticker_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.sticker: return
    if is_banned(msg.sticker.file_unique_id):
        try: await msg.delete()
        except: pass

# /q (text or image -> sticker)
async def q_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Reply to text or image.")
    # text -> sticker
    if r.text and not (r.photo or r.document):
        webp = text_to_webp_image(r.text)
        try:
            webp.seek(0)
            await context.bot.send_sticker(update.effective_chat.id, webp, reply_to_message_id=update.message.message_id)
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
    raw = await file_bytes(f)
    webp = img_to_webp(raw)
    try:
        webp.seek(0)
        await context.bot.send_sticker(update.effective_chat.id, webp, reply_to_message_id=update.message.message_id)
    except Exception:
        await update.message.reply_text("Sticker convert fail.")

# /kang attempt
async def kang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to image/sticker to kang.")
    emoji = (context.args and context.args[0]) or "🙂"
    user = update.effective_user; uid = user.id
    try:
        if r.sticker:
            f = await r.sticker.get_file()
            raw = await file_bytes(f); webp = io.BytesIO(raw)
        else:
            if r.photo:
                f = await r.photo[-1].get_file()
            elif r.document and getattr(r.document, "mime_type", "").startswith("image"):
                f = await r.document.get_file()
            else:
                return await update.message.reply_text("Reply to an image.")
            raw = await file_bytes(f); webp = img_to_webp(raw)
    except Exception:
        return await update.message.reply_text("Failed to read the image. Try again.")
    # save locally
    fname = STICKERS_DIR / f"sticker_{int(time.time())}.webp"
    try:
        with open(fname, "wb") as fh:
            fh.write(webp.getvalue())
    except Exception:
        return await update.message.reply_text("Failed to save sticker locally.")
    # try add to user's pack
    try:
        me = await context.bot.get_me(); bot_username = getattr(me, "username", "bot")
    except:
        bot_username = "bot"
    base = f"user_{uid}"
    suffix = f"_by_{bot_username}"
    pack_name_raw = (base + suffix)[:64]
    pack_name = "".join(ch if ch.isalnum() or ch=="_" else "_" for ch in pack_name_raw)
    pack_title = f"{user.first_name}'s stickers"
    webp.seek(0)
    inp = InputFile(webp, filename="sticker.webp")
    added = False; error_msg = None
    try:
        await context.bot.add_sticker_to_set(user_id=uid, name=pack_name, png_sticker=inp, emojis=emoji)
        added = True
    except Exception as e_add:
        error_msg = str(e_add)
        try:
            webp.seek(0)
            await context.bot.create_new_sticker_set(user_id=uid, name=pack_name, title=pack_title, png_sticker=inp, emojis=emoji)
            added = True
        except Exception as e_create:
            error_msg = (error_msg or "") + " | " + str(e_create)
            added = False
    if added:
        await update.message.reply_text(f"Kanged and added to your pack: `{pack_name}` {emoji}", parse_mode="Markdown")
        return
    # fallback: send sticker in chat
    try:
        webp.seek(0)
        await context.bot.send_sticker(update.effective_chat.id, webp)
    except:
        pass
    msg = "Saved sticker locally but could not add to your pack."
    if error_msg:
        msg += f" Details: {error_msg[:300]}"
    await update.message.reply_text(msg)

# moderation cmds
async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user.")
    uid = r.from_user.id; chat = update.effective_chat.id
    w = warn_user(chat, uid)
    await update.message.reply_text(f"Warned → total {w}")
    if w >= WARN_THRESHOLD:
        try:
            await context.bot.restrict_chat_member(chat, uid, ChatPermissions(can_send_messages=False), until_date=int(time.time())+WARN_MUTE)
            await update.message.reply_text("Auto-muted for warnings.")
        except:
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
    try: mins = int((context.args[0] if context.args else 10))
    except: mins = 10
    uid = r.from_user.id
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, uid, ChatPermissions(can_send_messages=False), until_date=int(time.time())+mins*60)
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
        await context.bot.restrict_chat_member(update.effective_chat.id, uid, perms, until_date=0)
        await update.message.reply_text("Unmuted.")
    except:
        await update.message.reply_text("Unmute fail.")

async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    r = update.message.reply_to_message
    if not r: return
    uid = r.from_user.id; chat = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat, uid, until_date=int(time.time())+5)
        await context.bot.unban_chat_member(chat, uid)
        await update.message.reply_text("Kicked.")
    except:
        await update.message.reply_text("Kick fail.")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    r = update.message.reply_to_message
    if not r: return
    uid = r.from_user.id
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        await update.message.reply_text("Banned.")
    except:
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
    except:
        await update.message.reply_text("Unban fail.")

# rules / welcome / antilink / notes
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
    if arg not in ("on","off"): return await update.message.reply_text("Use /antilink on|off")
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
    if arg not in ("on","off"): return await update.message.reply_text("Use /welcome on|off")
    db_set_setting(update.effective_chat.id, "welcome_on", arg)
    await update.message.reply_text(f"Welcome: {arg}")

async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for m in update.message.new_chat_members:
        chat = update.effective_chat.id
        if db_get_setting(chat, "welcome_on") == "on":
            tpl = db_get_setting(chat, "welcome") or "Welcome {name}!"
            text = tpl.replace("{name}", m.full_name)
            try:
                await update.message.reply_text(text)
            except:
                pass

# notes
async def setnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    if len(context.args)<2: return await update.message.reply_text("Use /setnote key value")
    key = context.args[0].lower()
    val = " ".join(context.args[1:])
    db_set_note(update.effective_chat.id, key, val)
    await update.message.reply_text("Note saved.")

async def note_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Use /note key")
    key = context.args[0].lower()
    val = db_get_note(update.effective_chat.id, key)
    if not val: return await update.message.reply_text("Not found.")
    await update.message.reply_text(val)

async def delnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    if not context.args: return await update.message.reply_text("Use /delnote key")
    db_del_note(update.effective_chat.id, context.args[0].lower())
    await update.message.reply_text("Note deleted.")

async def listnotes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out = db_list_notes(update.effective_chat.id)
    if not out: return await update.message.reply_text("No notes.")
    await update.message.reply_text("Notes: " + ", ".join(out))

# utils: react and info
async def react_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to a message and use /react <emoji>")
    emoji = (context.args and context.args[0]) or "👍"
    target = update.message.reply_to_message
    try:
        # fallback: reply with emoji
        await update.message.reply_text(emoji, reply_to_message_id=target.message_id)
    except:
        pass

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Reply to a user.")
    u = r.from_user
    text = f"User info:\nName: {u.full_name}\nID: {u.id}\nUsername: @{u.username if u.username else 'none'}\nIs bot: {u.is_bot}"
    await update.message.reply_text(text)

# group utilities
async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    rows = get_seen_members(chat.id, limit=50)
    if not rows:
        return await update.message.reply_text("No members recorded yet.")
    parts = []
    for uid, name in rows:
        safe_name = (name or "user").replace("`", "")[:50]
        parts.append(f"[{safe_name}](tg://user?id={uid})")
    text = " ".join(parts)
    max_len = 3800
    if len(text) <= max_len:
        await update.message.reply_text(text, parse_mode="Markdown")
        return
    cur = ""
    for p in parts:
        if len(cur) + len(p) + 1 > max_len:
            await update.message.reply_text(cur, parse_mode="Markdown")
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        await update.message.reply_text(cur, parse_mode="Markdown")

async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to the message you want to pin.")
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
        await update.message.reply_text("Could not create invite link: " + str(e))

async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    r = update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to the oldest message you want to delete up to the command message.")
    start_id = r.message_id; end_id = update.message.message_id; chat_id = update.effective_chat.id
    deleted = 0
    for mid in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(chat_id, mid)
            deleted += 1
        except:
            pass
    await update.message.reply_text(f"Attempted purge. Deleted approx: {deleted}")

async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    chat = update.effective_chat.id
    try:
        perms = ChatPermissions(can_send_messages=False, can_send_media_messages=False, can_send_other_messages=False, can_add_web_page_previews=False)
        await context.bot.set_chat_permissions(chat, perms)
        await update.message.reply_text("Group locked for non-admins.")
    except Exception as e:
        await update.message.reply_text("Lock failed: " + str(e))

async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return await update.message.reply_text("Admins only.")
    chat = update.effective_chat.id
    try:
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
        await context.bot.set_chat_permissions(chat, perms)
        await update.message.reply_text("Group unlocked for all members.")
    except Exception as e:
        await update.message.reply_text("Unlock failed: " + str(e))

# automod
LINK_RE = __import__("re").compile(r"(https?://|t\.me|telegram\.me)", __import__("re").I)

async def auto_mod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
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
    d[:] = [x for x in d if x > now - FLOOD_WINDOW]
    d.append(now)
    if len(d) >= FLOOD_LIMIT and not await is_admin(update, context):
        try:
            await context.bot.restrict_chat_member(chat, uid, ChatPermissions(can_send_messages=False), until_date=int(time.time())+FLOOD_MUTE)
        except:
            pass
        d.clear()
        await update.message.reply_text("Muted for flooding.")

# catch-all message handler
async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    # record user
    try:
        if msg.from_user and not msg.from_user.is_bot:
            add_seen_member(msg.chat.id, msg.from_user.id, msg.from_user.full_name)
    except: pass
    # sticker moderation
    if msg.sticker:
        return await sticker_auto(update, context)
    await auto_mod(update, context)
    # quick reaction to literal "start"
    txt = (msg.text or "").strip()
    if txt and txt.lower() == "start":
        try:
            await msg.reply_text("✅", reply_to_message_id=msg.message_id)
        except:
            pass
    # notes quick .key
    if txt.startswith(".") and len(txt)>1:
        key = txt[1:].split()[0].lower()
        val = db_get_note(update.effective_chat.id, key)
        if val:
            await update.message.reply_text(val)

# ------------- SETUP APPLICATION (handlers) -------------
# Build the Application once (no polling)
application = ApplicationBuilder().token(TOKEN).build()

# register handlers
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(cb_help, pattern="^(help:|rules)"))

# stickers
application.add_handler(CommandHandler("bansticker", bansticker))
application.add_handler(CommandHandler("allowsticker", allowsticker))
application.add_handler(CommandHandler("liststickers", liststickers))

# q & kang
application.add_handler(CommandHandler("q", q_cmd))
application.add_handler(CommandHandler("kang", kang_cmd))

# moderation
application.add_handler(CommandHandler("warn", warn_cmd))
application.add_handler(CommandHandler("warnings", warnings_cmd))
application.add_handler(CommandHandler("mute", mute_cmd))
application.add_handler(CommandHandler("unmute", unmute_cmd))
application.add_handler(CommandHandler("kick", kick_cmd))
application.add_handler(CommandHandler("ban", ban_cmd))
application.add_handler(CommandHandler("unban", unban_cmd))

# rules & welcome
application.add_handler(CommandHandler("setrules", setrules_cmd))
application.add_handler(CommandHandler("rules", rules_cmd))
application.add_handler(CommandHandler("antilink", antilink_cmd))
application.add_handler(CommandHandler("setwelcome", setwelcome_cmd))
application.add_handler(CommandHandler("welcome", welcome_toggle))

# notes
application.add_handler(CommandHandler("setnote", setnote_cmd))
application.add_handler(CommandHandler("note", note_cmd))
application.add_handler(CommandHandler("delnote", delnote_cmd))
application.add_handler(CommandHandler("listnotes", listnotes_cmd))

# utilities
application.add_handler(CommandHandler("react", react_cmd))
application.add_handler(CommandHandler("info", info_cmd))

# group utilities
application.add_handler(CommandHandler("all", all_cmd))
application.add_handler(CommandHandler("pin", pin_cmd))
application.add_handler(CommandHandler("add", add_cmd))
application.add_handler(CommandHandler("purge", purge_cmd))
application.add_handler(CommandHandler("lock", lock_cmd))
application.add_handler(CommandHandler("unlock", unlock_cmd))

# events
application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_member))

# catch-all
application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), msg_handler))

# initialize DB
init_db()

# ------------- FLASK APP (webhook receiver) -------------
flask_app = Flask(__name__)

@flask_app.post("/")
def receive_update():
    if application is None or TOKEN is None:
        return "Bot not ready", 503
    data = request.get_json(force=True)
    if not data:
        return "no data", 400
    upd = Update.de_json(data, application.bot)
    # process in background
    try:
        asyncio.create_task(application.process_update(upd))
    except RuntimeError:
        # no running loop (gunicorn sync worker) -> run new loop briefly
        asyncio.run(application.process_update(upd))
    return "OK"

@flask_app.get("/")
def home():
    return "Webhook bot running."

# a helper to set webhook once at startup (synchronous call to Telegram API)
def set_webhook():
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set. Webhook won't be registered.")
        return False
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL (RENDER_EXTERNAL_URL or WEBHOOK_URL) not set. Please set WEBHOOK_URL env or rely on Render's external url.")
        # still try to set to empty -> will fail
    hook = (PUBLIC_URL.rstrip("/") if PUBLIC_URL else "") + "/"
    try:
        # set webhook using the bot's API method (async) by using requests to avoid async complexity
        import requests
        url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
        resp = requests.post(url, json={"url": hook, "allowed_updates": []}, timeout=15)
        if resp.ok:
            log.info("Webhook set to %s", hook)
            return True
        else:
            log.error("Failed to set webhook: %s %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        log.exception("Exception setting webhook: %s", e)
        return False

# set webhook when module is loaded (Render import)
try:
    ok = set_webhook()
    if not ok:
        log.warning("Webhook registration did not succeed. Check WEBHOOK_URL / PUBLIC_URL env.")
except Exception:
    log.exception("Webhook registration failed.")

# expose Flask app variable for gunicorn
app = flask_app
