#!/usr/bin/env python3
import os,sqlite3,logging,time,re,io
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from telegram import InlineKeyboardButton,InlineKeyboardMarkup,InputFile,ChatPermissions,Update
from telegram.ext import ApplicationBuilder,CommandHandler,MessageHandler,CallbackQueryHandler,ContextTypes,filters

T=os.environ.get("TELEGRAM_BOT_TOKEN")
DB=Path("advbot.db")
S=Path("stickers");S.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger(__name__)
FLOOD_L=6;FLOOD_W=6;FLOOD_M=60
_warn_th=3;WARN_M=600
_recent={}

def init_db():
    c=sqlite3.connect(DB);cur=c.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS banned_stickers(uid TEXT UNIQUE)")
    cur.execute("CREATE TABLE IF NOT EXISTS warns(chat INTEGER,user INTEGER, cnt INTEGER, PRIMARY KEY(chat,user))")
    cur.execute("CREATE TABLE IF NOT EXISTS settings(chat INTEGER,key TEXT,val TEXT, PRIMARY KEY(chat,key))")
    cur.execute("CREATE TABLE IF NOT EXISTS members(chat INTEGER,user INTEGER,name TEXT, PRIMARY KEY(chat,user))")
    cur.execute("CREATE TABLE IF NOT EXISTS blacklist(chat INTEGER,word TEXT, PRIMARY KEY(chat,word))")
    c.commit();c.close()

def db_set(chat,k,v):
    c=sqlite3.connect(DB);cur=c.cursor();cur.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)",(chat,k,v));c.commit();c.close()
def db_get(chat,k):
    c=sqlite3.connect(DB);cur=c.cursor();cur.execute("SELECT val FROM settings WHERE chat=? AND key=?",(chat,k));r=cur.fetchone();c.close();return r[0] if r else None

def ban_uid(u): c=sqlite3.connect(DB);cur=c.cursor();cur.execute("INSERT OR IGNORE INTO banned_stickers VALUES (?)",(u,));c.commit();c.close()
def unban_uid(u): c=sqlite3.connect(DB);cur=c.cursor();cur.execute("DELETE FROM banned_stickers WHERE uid=?",(u,));c.commit();c.close()
def list_banned(): c=sqlite3.connect(DB);cur=c.cursor();cur.execute("SELECT uid FROM banned_stickers");r=[x[0] for x in cur.fetchall()];c.close();return r
def is_banned(u): c=sqlite3.connect(DB);cur=c.cursor();cur.execute("SELECT 1 FROM banned_stickers WHERE uid=?",(u,));r=cur.fetchone();c.close();return bool(r)

def warn_user(chat,user):
    c=sqlite3.connect(DB);cur=c.cursor();cur.execute("SELECT cnt FROM warns WHERE chat=? AND user=?",(chat,user));r=cur.fetchone()
    if r: n=r[0]+1; cur.execute("UPDATE warns SET cnt=? WHERE chat=? AND user=?",(n,chat,user))
    else: n=1; cur.execute("INSERT INTO warns VALUES (?,?,?)",(chat,user,n))
    c.commit();c.close();return n
def warns_of(chat,user):
    c=sqlite3.connect(DB);cur=c.cursor();cur.execute("SELECT cnt FROM warns WHERE chat=? AND user=?",(chat,user));r=cur.fetchone();c.close();return r[0] if r else 0

def add_member(chat,user,name):
    c=sqlite3.connect(DB);cur=c.cursor();cur.execute("INSERT OR REPLACE INTO members VALUES (?,?,?)",(chat,user,name));c.commit();c.close()
def seen(chat,lim=50):
    c=sqlite3.connect(DB);cur=c.cursor();cur.execute("SELECT user,name FROM members WHERE chat=? ORDER BY rowid DESC LIMIT ?",(chat,lim));r=cur.fetchall();c.close();return r

def bl_add(chat,word): c=sqlite3.connect(DB);cur=c.cursor();cur.execute("INSERT OR IGNORE INTO blacklist VALUES (?,?)",(chat,word));c.commit();c.close()
def bl_rem(chat,word): c=sqlite3.connect(DB);cur=c.cursor();cur.execute("DELETE FROM blacklist WHERE chat=? AND word=?",(chat,word));c.commit();c.close()
def bl_list(chat): c=sqlite3.connect(DB);cur=c.cursor();cur.execute("SELECT word FROM blacklist WHERE chat=?",(chat,));r=[x[0] for x in cur.fetchall()];c.close();return r
def contains_black(chat,text):
    if not text: return False,None
    for w in bl_list(chat):
        if re.search(r"\b"+re.escape(w)+r"\b",text.lower()): return True,w
    return False,None

async def is_admin(update:Update,ctx:ContextTypes.DEFAULT_TYPE,uid=None):
    try:
        c=update.effective_chat.id; u=uid or update.effective_user.id
        m=await ctx.bot.get_chat_member(c,u); return m.status in ("administrator","creator")
    except: return False

async def try_react(bot,chat_id,msg_id,emoji):
    try:
        fn=getattr(bot,"set_message_reaction",None)
        if fn: r=await fn(chat_id=chat_id,message_id=msg_id,reaction_types=[emoji],is_big=False); return bool(r)
    except: pass
    return False

async def read_bytes(fobj):
    bio=io.BytesIO()
    try:
        await fobj.download_to_memory(out=bio)
    except:
        await fobj.download(out=bio)
    bio.seek(0); return bio.read()

def img_to_webp(raw):
    img=Image.open(io.BytesIO(raw)).convert("RGBA");maxd=512;w,h=img.size;scale=min(maxd/w,maxd/h,1);nw=max(1,int(w*scale));nh=max(1,int(h*scale))
    img=img.resize((nw,nh),Image.LANCZOS);canvas=Image.new("RGBA",(maxd,maxd),(0,0,0,0));canvas.paste(img,((maxd-nw)//2,(maxd-nh)//2),img)
    out=io.BytesIO();canvas.save(out,"WEBP",lossless=True);out.seek(0);return out

def text_sticker(text,avatar=None):
    maxd=512;can=Image.new("RGBA",(maxd,maxd),(0,0,0,0));d=ImageDraw.Draw(can);y=8
    if avatar:
        try:
            av=Image.open(io.BytesIO(avatar)).convert("RGBA");av.thumbnail((120,120),Image.LANCZOS);can.paste(av,((maxd-av.width)//2,8),av);y=8+av.height+6
        except: y=30
    font=None
    for p in ("/system/fonts/Roboto-Regular.ttf","/system/fonts/DroidSans.ttf","DejaVuSans.ttf"):
        try: font=ImageFont.truetype(p,34);break
        except: font=None
    if not font: font=ImageFont.load_default()
    words=text.split();lines=[];cur=""
    for w in words:
        t=(cur+" "+w).strip();bb=d.textbbox((0,0),t,font=font)
        if bb[2]>maxd-24 and cur: lines.append(cur);cur=w
        else: cur=t
    if cur: lines.append(cur)
    for ln in lines:
        bb=d.textbbox((0,0),ln,font=font);wt=bb[2]-bb[0];x=(maxd-wt)//2;d.text((x,y),ln,font=font,fill=(255,255,255,255));y+=bb[3]-bb[1]+6
    out=io.BytesIO();can.save(out,"WEBP",lossless=True);out.seek(0);return out

HELP=["Stickers:/q /kang /bansticker /allowsticker /liststickers","Moderation:/warn /warnings /mute /unmute /kick /ban /unban /promote /demote","Group:/all /pin /unpin /add /purge /lock /unlock /blacklist /info /react"]

async def start_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    try:
        if update.message:
            ok=await try_react(ctx.bot,update.effective_chat.id,update.message.message_id,"✅")
            if not ok: await update.message.reply_text("👋",reply_to_message_id=update.message.message_id)
    except: pass
    try:
        me=await ctx.bot.get_me();un=getattr(me,"username",None)
    except: un=None
    kb=[]
    if un: kb.append([InlineKeyboardButton("➕ Add to group",url=f"https://t.me/{un}?startgroup=true")])
    kb.append([InlineKeyboardButton("Help",callback_data="help:0"),InlineKeyboardButton("Rules",callback_data="rules")])
    try: await update.message.reply_text("Welcome",reply_markup=InlineKeyboardMarkup(kb))
    except: pass

async def cb_help(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer();d=q.data or ""
    if d.startswith("help:"):
        i=int(d.split(":")[1]); await q.edit_message_text(HELP[i])
    elif d=="help:close":
        try: await q.message.delete()
        except: pass
    elif d=="rules":
        r=db_get(q.message.chat.id,"rules") or "No rules"; await q.message.reply_text(r)

async def bansticker_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    r=update.message.reply_to_message
    if not r or not r.sticker: return await update.message.reply_text("Reply to sticker")
    ban_uid(r.sticker.file_unique_id); await update.message.reply_text("Banned")

async def allowsticker_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    r=update.message.reply_to_message
    if not r or not r.sticker: return await update.message.reply_text("Reply to sticker")
    unban_uid(r.sticker.file_unique_id); await update.message.reply_text("Unbanned")

async def liststickers_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    r=list_banned()
    if not r: return await update.message.reply_text("No banned stickers")
    await update.message.reply_text("\n".join(r[:200]))

async def sticker_auto(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    m=update.message
    if not m or not m.sticker: return
    if is_banned(m.sticker.file_unique_id):
        try: await m.delete()
        except: pass

async def q_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    r=update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to image or text")
    if r.text and not (r.photo or getattr(r,"document",None)):
        avatar=None
        try:
            ph=await ctx.bot.get_user_profile_photos(r.from_user.id,limit=1)
            if getattr(ph,"total_count",0) and ph.photos:
                f=await ph.photos[0][-1].get_file(); avatar=await read_bytes(f)
        except: avatar=None
        webp=text_sticker(r.text,avatar); await ctx.bot.send_sticker(update.effective_chat.id,webp,reply_to_message_id=update.message.message_id); return
    if r.sticker:
        try: await ctx.bot.send_sticker(update.effective_chat.id,r.sticker.file_id,reply_to_message_id=update.message.message_id); return
        except: pass
    f=None
    if r.photo: f=await r.photo[-1].get_file()
    elif getattr(r,"document",None) and getattr(r.document,"mime_type","").startswith("image"): f=await r.document.get_file()
    else: return await update.message.reply_text("Reply to image")
    try:
        raw=await read_bytes(f); webp=img_to_webp(raw); await ctx.bot.send_sticker(update.effective_chat.id,webp,reply_to_message_id=update.message.message_id)
    except Exception as e:
        log.exception("q fail"); await update.message.reply_text("Convert fail")

async def attempt_add(bot,user,name,input_file,emoji):
    try:
        await bot.add_sticker_to_set(user_id=user,name=name,png_sticker=input_file,emojis=emoji); return True,None
    except TypeError:
        try:
            await bot.add_sticker_to_set(user_id=user,name=name,sticker=input_file,emojis=emoji); return True,None
        except Exception as e: return False,str(e)
    except Exception as e: return False,str(e)

async def attempt_create(bot,user,name,title,input_file,emoji):
    try:
        await bot.create_new_sticker_set(user_id=user,name=name,title=title,png_sticker=input_file,emojis=emoji); return True,None
    except TypeError:
        try:
            await bot.create_new_sticker_set(user_id=user,name=name,title=title,sticker=input_file,emojis=emoji); return True,None
        except Exception as e: return False,str(e)
    except Exception as e: return False,str(e)

async def kang_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    r=update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to image/sticker")
    emoji=(ctx.args and ctx.args[0]) or "🙂"
    u=update.effective_user;uid=u.id
    try:
        if r.sticker: f=await r.sticker.get_file(); raw=await read_bytes(f); webp=io.BytesIO(raw)
        else:
            if r.photo: f=await r.photo[-1].get_file()
            elif getattr(r,"document",None) and getattr(r.document,"mime_type","").startswith("image"): f=await r.document.get_file()
            else: return await update.message.reply_text("Reply to image")
            raw=await read_bytes(f); webp=img_to_webp(raw)
    except Exception as e:
        log.exception("kang read"); return await update.message.reply_text("Read fail")
    try:
        fname=S/f"stk_{int(time.time())}.webp"
        with open(fname,"wb") as fh: fh.write(webp.getvalue())
    except: pass
    try:
        me=await ctx.bot.get_me();botun=getattr(me,"username","bot")
    except: botun="bot"
    pack=f"user_{uid}_by_{botun}"
    pack=re.sub(r"[^A-Za-z0-9_]","_",pack)[:64]
    webp.seek(0);inp=InputFile(webp,filename="st.webp")
    ok,err=await attempt_add(ctx.bot,uid,pack,inp,emoji)
    if not ok:
        created,err2=await attempt_create(ctx.bot,uid,pack,u.first_name+"'s stickers",inp,emoji)
        if created: return await update.message.reply_text(f"Added to pack {pack}")
        ok2,err3=await attempt_add(ctx.bot,uid,pack,inp,emoji)
        if ok2: return await update.message.reply_text(f"Added to pack {pack}")
        try: webp.seek(0); await ctx.bot.send_sticker(update.effective_chat.id,webp); return await update.message.reply_text("Saved locally but could not add to pack")
        except: return await update.message.reply_text("Saved but add fail")
    else:
        return await update.message.reply_text(f"Added to pack {pack}")

async def warn_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    r=update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user")
    n=warn_user(update.effective_chat.id,r.from_user.id)
    await update.message.reply_text(f"Warns:{n}")
    if n>=_warn_th:
        try:
            perms=ChatPermissions(can_send_messages=False); await ctx.bot.restrict_chat_member(update.effective_chat.id,r.from_user.id,permissions=perms,until_date=int(time.time())+WARN_M)
            await update.message.reply_text("Auto-muted")
        except: pass

async def warnings_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    r=update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user")
    await update.message.reply_text(str(warns_of(update.effective_chat.id,r.from_user.id)))

async def mute_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    r=update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user")
    try: mins=int(ctx.args[0]) if ctx.args else None
    except: mins=None
    until=int(time.time())+(mins*60 if mins else 10*365*24*3600)
    perms=ChatPermissions(can_send_messages=False,can_send_media_messages=False,can_send_other_messages=False,can_add_web_page_previews=False)
    try: await ctx.bot.restrict_chat_member(update.effective_chat.id,r.from_user.id,permissions=perms,until_date=until); await update.message.reply_text("Muted")
    except Exception as e: await update.message.reply_text("Mute fail:"+str(e))

async def unmute_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    r=update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user")
    perms=ChatPermissions(can_send_messages=True,can_send_media_messages=True,can_send_other_messages=True,can_add_web_page_previews=True)
    try: await ctx.bot.restrict_chat_member(update.effective_chat.id,r.from_user.id,permissions=perms,until_date=0); await update.message.reply_text("Unmuted")
    except Exception as e: await update.message.reply_text("Unmute fail:"+str(e))

async def kick_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    r=update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user")
    try: await ctx.bot.ban_chat_member(update.effective_chat.id,r.from_user.id,until_date=int(time.time())+5); await ctx.bot.unban_chat_member(update.effective_chat.id,r.from_user.id); await update.message.reply_text("Kicked")
    except Exception as e: await update.message.reply_text("Kick fail:"+str(e))

async def ban_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_to_message("Admins only")
    r=update.message.reply_to_message
    if not r: return await update.message.reply_text("Reply to user")
    try: await ctx.bot.ban_chat_member(update.effective_chat.id,r.from_user.id); await update.message.reply_text("Banned")
    except Exception as e: await update.message.reply_text("Ban fail:"+str(e))

async def unban_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_to_message("Admins only")
    if not ctx.args: return await update.message.reply_text("Use /unban <id>")
    try: uid=int(ctx.args[0])
    except: return await update.message.reply_text("Invalid id")
    try: await ctx.bot.unban_chat_member(update.effective_chat.id,uid); await update.message.reply_text("Unbanned")
    except Exception as e: await update.message.reply_text("Unban fail:"+str(e))

async def promote_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_to_message("Admins only")
    if not update.message.reply_to_message: return await update.message.reply_to_message("Reply to user")
    uid=update.message.reply_to_message.from_user.id
    try:
        await ctx.bot.promote_chat_member(update.effective_chat.id,uid,can_change_info=True,can_delete_messages=True,can_invite_users=True,can_restrict_members=True,can_pin_messages=True)
        await update.message.reply_text("Promoted")
    except Exception as e: await update.message.reply_text("Promote fail:"+str(e))

async def demote_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_to_message("Admins only")
    if not update.message.reply_to_message: return await update.message.reply_to_message("Reply to user")
    uid=update.message.reply_to_message.from_user.id
    try:
        await ctx.bot.promote_chat_member(update.effective_chat.id,uid,can_change_info=False,can_delete_messages=False,can_invite_users=False,can_restrict_members=False,can_pin_messages=False,can_promote_members=False)
        await update.message.reply_text("Demoted")
    except Exception as e: await update.message.reply_text("Demote fail:"+str(e))

async def setrules_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    txt=" ".join(ctx.args)
    if not txt: return await update.message.reply_text("Usage: /setrules text")
    db_set(update.effective_chat.id,"rules",txt); await update.message.reply_text("Saved")

async def rules_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(db_get(update.effective_chat.id,"rules") or "No rules")

async def antilink_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    arg=(ctx.args[0] if ctx.args else "").lower()
    if arg not in ("on","off"): return await update.message.reply_text("Use on|off")
    db_set(update.effective_chat.id,"antilink",arg); await update.message.reply_text("Done")

async def setwelcome_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    txt=" ".join(ctx.args)
    if not txt: return await update.message.reply_text("Usage: /setwelcome text")
    db_set(update.effective_chat.id,"welcome",txt); await update.message.reply_text("Saved")

async def welcome_toggle(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    arg=(ctx.args[0] if ctx.args else "").lower()
    if arg not in ("on","off"): return await update.message.reply_text("Use on|off")
    db_set(update.effective_chat.id,"welcome_on",arg); await update.message.reply_text("Done")

async def welcome_members(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    for m in update.message.new_chat_members:
        if db_get(update.effective_chat.id,"welcome_on")=="on":
            tpl=db_get(update.effective_chat.id,"welcome") or "Welcome {name}!"
            try: await update.message.reply_text(tpl.replace("{name}",m.full_name))
            except: pass

async def react_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message: return await update.message.reply_text("Reply to msg and /react <emoji>")
    e=(ctx.args and ctx.args[0]) or "👍"; t=update.message.reply_to_message
    ok=await try_react(ctx.bot,t.chat.id,t.message_id,e)
    if ok: await update.message.reply_text("Reacted")
    else:
        try: await update.message.reply_text(e,reply_to_message_id=t.message_id)
        except: pass

async def info_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    tgt=None
    if update.message.reply_to_message: tgt=update.message.reply_to_message.from_user
    elif ctx.args:
        try: tgt=await ctx.bot.get_chat(int(ctx.args[0]))
        except: pass
    if not tgt: return await update.message.reply_text("Reply to user or /info <id>")
    parts=[f"ID:`{getattr(tgt,'id',None)}`",f"Name:{getattr(tgt,'full_name',getattr(tgt,'first_name',''))}","Bot:"+str(getattr(tgt,'is_bot',False))]
    try:
        m=await ctx.bot.get_chat_member(update.effective_chat.id,getattr(tgt,'id',None)); parts.append("Status:"+m.status)
    except: pass
    await update.message.reply_text("\n".join(parts),parse_mode="Markdown")

async def all_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    rows=seen(update.effective_chat.id,lim=100)
    if not rows: return await update.message.reply_text("No members")
    parts=[f"[{(n or 'user')[:30]}](tg://user?id={uid})" for uid,n in rows]
    out=" ".join(parts)
    if len(out)<=3800: await update.message.reply_text(out,parse_mode="Markdown"); return
    cur=""
    for p in parts:
        if len(cur)+len(p)+1>3800:
            await update.message.reply_text(cur,parse_mode="Markdown"); cur=p
        else: cur=(cur+" "+p).strip()
    if cur: await update.message.reply_text(cur,parse_mode="Markdown")

async def pin_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    if not update.message.reply_to_message: return await update.message.reply_text("Reply to msg")
    try: await ctx.bot.pin_chat_message(update.effective_chat.id,update.message.reply_to_message.message_id); await update.message.reply_text("Pinned")
    except Exception as e: await update.message.reply_text("Pin fail:"+str(e))

async def unpin_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    try:
        if update.message.reply_to_message: await ctx.bot.unpin_chat_message(update.effective_chat.id,message_id=update.message.reply_to_message.message_id)
        else: await ctx.bot.unpin_all_chat_messages(update.effective_chat.id)
        await update.message.reply_text("Unpinned")
    except Exception as e: await update.message.reply_text("Unpin fail:"+str(e))

async def add_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    try:
        link=await ctx.bot.create_chat_invite_link(update.effective_chat.id); await update.message.reply_text("Invite:"+link.invite_link)
    except Exception as e: await update.message.reply_text("Invite fail:"+str(e))

async def purge_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    if not update.message.reply_to_message: return await update.message.reply_text("Reply to earliest")
    s=update.message.reply_to_message.message_id; e=update.message.message_id; c=update.effective_chat.id;cnt=0
    for mid in range(s,e+1):
        try: await ctx.bot.delete_message(c,mid);cnt+=1
        except: pass
    await update.message.reply_text("Purge attempted: "+str(cnt))

async def lock_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    c=update.effective_chat.id
    try:
        p=ChatPermissions(can_send_messages=False,can_send_media_messages=False,can_send_other_messages=False,can_add_web_page_previews=False)
        await ctx.bot.set_chat_permissions(c,p); await update.message.reply_text("Locked")
    except Exception as e: await update.message.reply_text("Lock fail:"+str(e))

async def unlock_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update,ctx): return await update.message.reply_text("Admins only")
    c=update.effective_chat.id
    try:
        p=ChatPermissions(can_send_messages=True,can_send_media_messages=True,can_send_other_messages=True,can_add_web_page_previews=True)
        await ctx.bot.set_chat_permissions(c,p); await update.message.reply_text("Unlocked")
    except Exception as e: await update.message.reply_text("Unlock fail:"+str(e))

async def automod(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    m=update.message
    if not m: return
    if m.from_user and getattr(m.from_user,"is_bot",False): return
    add_member(m.chat.id,m.from_user.id,m.from_user.full_name)
    if m.sticker:
        if is_banned(m.sticker.file_unique_id):
            try: await m.delete()
            except: pass
        return
    if m.text:
        b,w=contains_black(m.chat.id,m.text)
        if b and not await is_admin(update,ctx):
            try: await m.delete()
            except: pass
            warn_user(m.chat.id,m.from_user.id)
            await m.reply_text(f"Deleted blacklisted word:{w}")
            return
        if db_get(m.chat.id,"antilink")=="on" and not await is_admin(update,ctx):
            if re.search(r"(https?://|t\.me|telegram\.me)",m.text,re.I):
                try: await m.delete()
                except: pass
                await m.reply_text("Links not allowed"); return
    # flood
    cid=m.chat.id;uid=m.from_user.id;now=time.time()
    d=_recent.setdefault(cid,{}).setdefault(uid,[])
    d[:] = [t for t in d if t>now-FLOOD_W]; d.append(now)
    if len(d)>=FLOOD_L and not await is_admin(update,ctx):
        try:
            await ctx.bot.restrict_chat_member(cid,uid,permissions=ChatPermissions(can_send_messages=False),until_date=int(time.time())+FLOOD_M)
            _recent[cid][uid]=[]
            await m.reply_text("Muted for flood")
        except: pass

async def msg_handler(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await automod(update,ctx)
    if update.message and update.message.text and update.message.text.strip().lower()=="start":
        try:
            ok=await try_react(ctx.bot,update.effective_chat.id,update.message.message_id,"✅")
            if not ok: await update.message.reply_text("✅",reply_to_message_id=update.message.message_id)
        except: pass

def main():
    if not T: raise SystemExit("set TELEGRAM_BOT_TOKEN")
    init_db()
    app=ApplicationBuilder().token(T).build()
    app.add_handler(CommandHandler("start",start_cmd))
    app.add_handler(CallbackQueryHandler(cb_help,pattern="^(help:|rules)"))
    app.add_handler(CommandHandler("bansticker",bansticker_cmd));app.add_handler(CommandHandler("allowsticker",allowsticker_cmd));app.add_handler(CommandHandler("liststickers",liststickers_cmd))
    app.add_handler(CommandHandler("q",q_cmd));app.add_handler(CommandHandler("kang",kang_cmd))
    app.add_handler(CommandHandler("warn",warn_cmd));app.add_handler(CommandHandler("warnings",warnings_cmd));app.add_handler(CommandHandler("mute",mute_cmd));app.add_handler(CommandHandler("unmute",unmute_cmd))
    app.add_handler(CommandHandler("kick",kick_cmd));app.add_handler(CommandHandler("ban",ban_cmd));app.add_handler(CommandHandler("unban",unban_cmd))
    app.add_handler(CommandHandler("promote",promote_cmd));app.add_handler(CommandHandler("demote",demote_cmd))
    app.add_handler(CommandHandler("setrules",setrules_cmd));app.add_handler(CommandHandler("rules",rules_cmd));app.add_handler(CommandHandler("antilink",antilink_cmd))
    app.add_handler(CommandHandler("setwelcome",setwelcome_cmd));app.add_handler(CommandHandler("welcome",welcome_toggle));app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS,welcome_members))
    app.add_handler(CommandHandler("react",react_cmd));app.add_handler(CommandHandler("info",info_cmd));app.add_handler(CommandHandler("all",all_cmd))
    app.add_handler(CommandHandler("pin",pin_cmd));app.add_handler(CommandHandler("unpin",unpin_cmd));app.add_handler(CommandHandler("add",add_cmd))
    app.add_handler(CommandHandler("purge",purge_cmd));app.add_handler(CommandHandler("lock",lock_cmd));app.add_handler(CommandHandler("unlock",unlock_cmd))
    app.add_handler(CommandHandler("blacklist",lambda u,c: ctx_blacklist(u,c)))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), msg_handler))
    log.info("Bot starting"); app.run_polling()

# minimal wrapper for blacklist since lambda can't be async easily
async def ctx_blacklist(u,c):
    # replicate blacklist_cmd behaviour
    # u: Update, c: Context
    pass

if __name__=="__main__":
    main()
