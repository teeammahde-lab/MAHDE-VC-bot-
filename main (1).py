"""
Telegram Multi-Client Voice Chat Bot
=====================================
REQUIREMENTS (Wispbyte-এ requirements.txt ফাইলে এগুলো বসান):
    aiogram>=3.25
    kurigram
    tgcrypto
    py-tgcalls
    cryptography
    python-dotenv

RUN: python main.py   (Startup file = main.py)
"""
import asyncio, html, logging, os, re, sqlite3, time
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
                           InlineKeyboardMarkup, InlineKeyboardButton)
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.errors import (FloodWait, SessionPasswordNeeded, PhoneCodeInvalid,
                             PhoneCodeExpired, PasswordHashInvalid, UserAlreadyParticipant)
from pyrogram.raw.functions.messages import GetMessagesViews
from pytgcalls import PyTgCalls

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ====== আপনার কনফিগ (এখানেই বসানো আছে) ======
BOT_TOKEN = "8925308973:AAGlr9DM_HlHNPzn90qvV66GgxquREtnIyo"
API_ID = 33680093
API_HASH = "c6c802e0811240f1d1b46c730ba0a856"
OWNER_ID = 8906782967
DELAY = float(os.environ.get("DELAY", "1"))  # seconds between accounts (FloodWait protection)

# ====== Telegram Premium emoji (optional) ======
# Format:  "normal emoji": "custom_emoji_id"
# Every place the bot uses that emoji (messages + button icons) becomes a premium emoji.
# To get an ID: as admin, send any premium emoji to the bot -> it replies with the ID.
# Works only if the BOT OWNER account has Telegram Premium. Leave empty to use normal emoji.
PREMIUM = {
    # "📋": "5368324170671202286",
    # "🎙": "5310169226856644648",
}


def h(x):
    return html.escape(str(x))


# ---------- encryption + db ----------
KEY_FILE = "secret.key"
if os.environ.get("ENC_KEY"):
    KEY = os.environ["ENC_KEY"].encode()
elif os.path.exists(KEY_FILE):
    KEY = open(KEY_FILE, "rb").read()
else:
    KEY = Fernet.generate_key()
    open(KEY_FILE, "wb").write(KEY)
fernet = Fernet(KEY)

db = sqlite3.connect("data.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY, name TEXT, phone TEXT, session BLOB, owner INTEGER);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, name TEXT, username TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS auto2(owner INTEGER, kind TEXT, chat_id TEXT, emoji TEXT, PRIMARY KEY(owner, kind, chat_id));
""")
try:
    db.execute("ALTER TABLE clients ADD COLUMN owner INTEGER")
except sqlite3.OperationalError:
    pass
db.execute("UPDATE clients SET owner=? WHERE owner IS NULL", (OWNER_ID,))
db.commit()

# ---------- runtime state ----------
RUNNING = {}     # tg_id -> (Client, PyTgCalls, owner_user_id)
PENDING = {}     # bot user id -> dict(app, phone, hash)
AUTO_REACT = {}  # (owner, chat_id) -> emoji
AUTO_VIEW = set()  # (owner, chat_id)
SEEN = {}        # (tg_id, chat, kind) -> set(message ids)


def load_auto():
    AUTO_REACT.clear(); AUTO_VIEW.clear()
    for owner, kind, chat, emoji in db.execute("SELECT owner, kind, chat_id, emoji FROM auto2"):
        if kind == "react":
            AUTO_REACT[(owner, int(chat))] = emoji
        else:
            AUTO_VIEW.add((owner, int(chat)))


def user_clients(uid):
    """Admin controls ALL accounts in the bot; everyone else only their own."""
    if uid == OWNER_ID:
        return list(RUNNING.items())
    return [(tid, v) for tid, v in RUNNING.items() if v[2] == uid]


def owner_name(oid):
    if oid == OWNER_ID:
        return "You"
    r = db.execute("SELECT name FROM users WHERE user_id=?", (oid,)).fetchone()
    return r[0] if r and r[0] else str(oid)


# ---------- helpers ----------
def parse_target(t):
    t = t.strip()
    m = re.search(r"t\.me/c/(\d+)", t)
    if m: return int("-100" + m.group(1))
    if re.fullmatch(r"-?\d+", t): return int(t)
    m = re.search(r"t\.me/(\+[\w-]+|joinchat/[\w-]+)", t)
    if m: return "https://t.me/" + m.group(1)
    m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{4,})", t)
    if m: return m.group(1)
    return t


def parse_post(t):
    m = re.search(r"t\.me/c/(\d+)/(\d+)", t)
    if m: return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", t)
    if m: return m.group(1), int(m.group(2))
    return None


async def for_all(uid, fn):
    ok = fail = 0
    t0 = time.time()
    for _, (app, calls, _o) in user_clients(uid):
        try:
            try:
                await fn(app, calls)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                await fn(app, calls)
            ok += 1
        except UserAlreadyParticipant:
            ok += 1
        except Exception as e:
            fail += 1
            log.warning("client failed: %r", e)
        await asyncio.sleep(DELAY)
    return ok, fail, max(1, int(time.time() - t0))


def report(ok, fail, secs, label="Done"):
    return f"✅ Done!\n\n🎙 {label}: {ok} client(s)\n❌ Failed: {fail} client(s)\n⏱ Duration: {secs} second(s)"


def make_handler(owner):
    async def on_post(client, m):
        chat = m.chat.id
        emoji = AUTO_REACT.get((owner, chat)) or AUTO_REACT.get((OWNER_ID, chat))
        if emoji:
            try:
                await client.send_reaction(chat, m.id, emoji)
            except Exception as e:
                log.debug("react: %r", e)
        if (owner, chat) in AUTO_VIEW or (OWNER_ID, chat) in AUTO_VIEW:
            try:
                await client.invoke(GetMessagesViews(peer=await client.resolve_peer(chat), id=[m.id], increment=True))
            except Exception as e:
                log.debug("view: %r", e)
    return on_post


async def start_client(session_string, tg_id, owner):
    await stop_client(tg_id)
    app = Client(f"c{tg_id}", api_id=API_ID, api_hash=API_HASH,
                 session_string=session_string, in_memory=True)
    await app.start()
    try:
        async for _ in app.get_dialogs():   # fills peer cache so chat IDs resolve after restart
            pass
    except Exception as e:
        log.warning("dialogs %s: %r", tg_id, e)
    calls = PyTgCalls(app)
    await calls.start()
    app.add_handler(MessageHandler(make_handler(owner), filters.channel))
    RUNNING[tg_id] = (app, calls, owner)


async def stop_client(tg_id):
    pair = RUNNING.pop(tg_id, None)
    if pair:
        try: await pair[0].stop()
        except Exception: pass


async def recent_ids(app, chat, n=10):
    return [x.id async for x in app.get_chat_history(chat, limit=n)]


async def view_posts(app, chat, ids):
    await app.invoke(GetMessagesViews(peer=await app.resolve_peer(chat), id=ids, increment=True))


async def watcher():
    """Backup for Auto View / Auto React: checks recent posts every 10s so nothing is missed."""
    await asyncio.sleep(20)
    while True:
        try:
            for owner, chat in set(AUTO_VIEW) | set(AUTO_REACT):
                clients = user_clients(owner)
                if not clients:
                    continue
                try:
                    ids = await recent_ids(clients[0][1][0], chat)
                except Exception as e:
                    log.warning("watch %s: %r", chat, e)
                    continue
                for tg_id, (app, _c, _o) in clients:
                    if (owner, chat) in AUTO_VIEW:
                        vkey = (tg_id, chat, "v")
                        if vkey not in SEEN:
                            SEEN[vkey] = set(ids)   # baseline: old posts are not viewed
                        else:
                            new = [i for i in ids if i not in SEEN[vkey]]
                            if new:
                                try:
                                    await view_posts(app, chat, new)
                                    SEEN[vkey].update(new)
                                except FloodWait as e:
                                    await asyncio.sleep(e.value + 1)
                                except Exception as e:
                                    log.warning("view %s: %r", tg_id, e)
                    emoji = AUTO_REACT.get((owner, chat))
                    if emoji:
                        key = (tg_id, chat, "r")
                        if key not in SEEN:
                            SEEN[key] = set(ids)   # baseline: don't react to old posts
                        else:
                            for i in [i for i in ids if i not in SEEN[key]]:
                                try:
                                    await app.send_reaction(chat, i, emoji)
                                except Exception as e:
                                    log.debug("react %s: %r", tg_id, e)
                                SEEN[key].add(i)
                    await asyncio.sleep(0.5)
        except Exception as e:
            log.warning("watcher: %r", e)
        await asyncio.sleep(10)


# ---------- buttons: colors + premium emoji ----------
def L(emoji, text):
    """Label: if the emoji is premium it becomes the button icon, so drop it from the text."""
    return text if emoji in PREMIUM else f"{emoji} {text}"


def split_icon(label):
    for e, cid in PREMIUM.items():
        if label.startswith(e):
            return label[len(e):].strip(), cid
    return label, None


def ibtn(label, data, style=None):
    text, icon = split_icon(label)
    return InlineKeyboardButton(text=text, callback_data=data, style=style, icon_custom_emoji_id=icon)


def ikb(*rows):
    """rows of (label, callback_data[, style]); style = primary | success | danger"""
    return InlineKeyboardMarkup(inline_keyboard=[[ibtn(*b) for b in r] for r in rows])


B_CLIENTS, B_REACT, B_JOIN = L("📋", "My Clients"), L("❤️", "Send Reaction"), L("🔗", "Join Channel")
B_JVC, B_LVC = L("🎙", "Join Voice Chat"), L("🔇", "Leave Voice Chat")
B_AR, B_AV = L("🔁", "Auto React"), L("👁", "Auto View")
B_ACC, B_ADMIN = L("🔑", "Access"), L("🛠", "Admin Panel")
B_ADD = L("➕", "Add Client")
B_BACK = L("🔙", "Back")
BTN_EMOJI = {B_CLIENTS: "📋", B_REACT: "❤️", B_JOIN: "🔗", B_JVC: "🎙", B_LVC: "🔇",
             B_AR: "🔁", B_AV: "👁", B_ACC: "🔑", B_ADMIN: "🛠", B_BACK: "🔙", B_ADD: "➕"}


def rbtn(text, style=None):
    return KeyboardButton(text=text, style=style, icon_custom_emoji_id=PREMIUM.get(BTN_EMOJI.get(text)))


def build_kb(admin):
    rows = [
        [rbtn(B_CLIENTS, "primary"), rbtn(B_ADD, "success")],
        [rbtn(B_REACT, "danger"), rbtn(B_JOIN, "primary")],
        [rbtn(B_JVC, "success"), rbtn(B_LVC, "danger")],
        [rbtn(B_AR, "primary"), rbtn(B_AV, "success")],
        [rbtn(B_ACC, "primary")] + ([rbtn(B_ADMIN, "success")] if admin else []),
    ]
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=rows)


MAIN_KB = build_kb(True)
USER_KB = build_kb(False)
BACK_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[rbtn(B_BACK, "danger")]])


def kb(uid):
    return MAIN_KB if uid == OWNER_ID else USER_KB


class PremiumMW(BaseRequestMiddleware):
    """Turns plain emoji in outgoing messages into premium (custom) emoji."""
    async def __call__(self, make_request, bot, method):
        if type(method).__name__ in ("SendMessage", "EditMessageText") and isinstance(method.text, str):
            t = method.text
            for e, cid in PREMIUM.items():
                t = t.replace(e, f'<tg-emoji emoji-id="{cid}">{e}</tg-emoji>')
            method.text = t
        return await make_request(bot, method)


class S(StatesGroup):
    phone = State(); otp = State(); pwd = State()
    join = State(); jvc = State(); lvc = State()
    react_link = State(); react_emoji = State()
    ar_chat = State(); ar_emoji = State(); av_chat = State()


# ---------- access: request -> admin accepts ----------
ADMIN_ONLY_TEXT = {B_ADMIN}


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = event.from_user
        if not u:
            return
        is_msg = isinstance(event, Message)

        async def say(t):
            if is_msg:
                await event.answer(t)
            else:
                await event.answer(re.sub(r"<[^>]+>", "", t)[:190], show_alert=True)

        if u.id == OWNER_ID:
            return await handler(event, data)
        row = db.execute("SELECT status FROM users WHERE user_id=?", (u.id,)).fetchone()
        st = row[0] if row else None
        if st == "approved":
            if is_msg and event.text in ADMIN_ONLY_TEXT:
                return await say("⛔ Admin only.")
            if not is_msg and (event.data or "").startswith("adm"):
                return await say("⛔ Admin only.")
            return await handler(event, data)
        if st == "rejected":
            return await say("❌ Your access request was rejected by the bot owner.")
        if st == "revoked":
            return await say("🚫 Your access has been revoked by the bot owner.")
        if st == "pending":
            return await say("⏳ Your request is still waiting for the owner's approval.")
        # new user -> send request to admin
        db.execute("INSERT OR REPLACE INTO users(user_id,name,username,status) VALUES(?,?,?,?)",
                   (u.id, u.full_name, u.username or "", "pending"))
        db.commit()
        await say("🔐 Bot authorization request sent!\n\nWaiting for the bot owner to approve you. You'll be notified here.")
        try:
            uname = f"@{h(u.username)}" if u.username else "—"
            await data["bot"].send_message(
                OWNER_ID,
                f"🔔 New access request\n\n👤 Name: {h(u.full_name)}\n🔗 Username: {uname}\n🆔 ID: {u.id}",
                reply_markup=ikb([("✅ Accept", f"adm:ap:{u.id}", "success"), ("❌ Reject", f"adm:rj:{u.id}", "danger")]))
        except Exception as e:
            log.warning("owner notify failed: %r", e)


dp = Dispatcher(storage=MemoryStorage())
dp.message.outer_middleware(Guard())
dp.callback_query.outer_middleware(Guard())


# ---------- basic ----------
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 Welcome! Choose an option below.", reply_markup=kb(m.from_user.id))


@dp.message(Command("cancel"))
@dp.message(F.text == B_BACK)
async def cancel(m: Message, state: FSMContext):
    p = PENDING.pop(m.from_user.id, None)
    if p:
        try: await p["app"].disconnect()
        except Exception: pass
    await state.clear()
    await m.answer("❎ Cancelled.", reply_markup=kb(m.from_user.id))


@dp.message(Command("emojiid"))
async def emojiid_help(m: Message):
    if m.from_user.id == OWNER_ID:
        await m.answer("Send me any premium emoji and I'll reply with its ID.")


def has_custom_emoji(m: Message):
    return m.from_user.id == OWNER_ID and any(e.type == "custom_emoji" for e in (m.entities or []))


@dp.message(StateFilter(None), has_custom_emoji)
async def emoji_ids(m: Message):
    lines = []
    for e in m.entities:
        if e.type == "custom_emoji":
            ch = e.extract_from(m.text)
            lines.append(f'{h(ch)} → <code>{e.custom_emoji_id}</code>')
    await m.answer("\n".join(lines) + '\n\nPaste into PREMIUM in main.py like:\n<code>"📋": "ID"</code>')


# ---------- My Clients / Add Client ----------
def client_rows(uid):
    if uid == OWNER_ID:
        return db.execute("SELECT id, name, phone, owner FROM clients ORDER BY id").fetchall()
    return db.execute("SELECT id, name, phone, owner FROM clients WHERE owner=? ORDER BY id", (uid,)).fetchall()


@dp.message(F.text == B_CLIENTS)
async def my_clients(m: Message):
    uid = m.from_user.id
    rows = client_rows(uid)
    if not rows:
        return await m.answer("📋 My Clients\n\nNo clients yet. Tap ➕ Add Client to log in an account.")
    lines = []
    for i, (c, n, p, o) in enumerate(rows, 1):
        l = f"{i}. {'🟢' if c in RUNNING else '🔴'} {h(n)} | 📞 {h(p)} | 🆔 {c}"
        if uid == OWNER_ID:
            l += f" | 👥 {h(owner_name(o))}"
        lines.append(l)
    body = "\n".join(lines)
    if len(body) > 3500:
        body = body[:3500] + "\n…"
    await m.answer(f"📋 My Clients ({len(rows)})\n\n{body}\n\nTo remove: /remove NUMBER  (e.g. /remove 3)")


@dp.message(F.text == B_ADD)
async def add_client_btn(m: Message):
    await m.answer(
        "➕ Add Clients\n\nChoose how to add a Telegram account:\n\n• 📱 Add by Phone Number — log in with phone + OTP",
        reply_markup=ikb([("📱 Add by Phone Number", "add_phone", "primary")]))


@dp.callback_query(F.data == "add_phone")
async def add_phone(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.phone)
    await c.message.answer(
        "📱 Add by Phone Number\n\nSend the Telegram account phone number with country code, e.g. +1234567890.\n\n"
        "Tap 🔙 Back to cancel, or send /cancel to abort.", reply_markup=BACK_KB)
    await c.answer()


@dp.message(Command("remove"))
async def remove_cmd(m: Message):
    rows = client_rows(m.from_user.id)
    nums = [int(x) for x in (m.text or "").split()[1:] if x.isdigit()]
    nums = [n for n in nums if 1 <= n <= len(rows)]
    if not nums:
        return await m.answer("Usage: /remove NUMBER\nUse the number from the 📋 My Clients list. Several at once: /remove 2 5 7")
    names = []
    for n in sorted(set(nums)):
        cid, name = rows[n - 1][0], rows[n - 1][1]
        await stop_client(cid)
        db.execute("DELETE FROM clients WHERE id=?", (cid,))
        names.append(h(name))
    db.commit()
    await m.answer("🗑 Removed: " + ", ".join(names))


@dp.message(S.phone)
async def got_phone(m: Message, state: FSMContext):
    phone = m.text.strip().replace(" ", "")
    app = Client(f"login{m.from_user.id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await app.connect()
        sent = await app.send_code(phone)
    except Exception as e:
        try: await app.disconnect()
        except Exception: pass
        return await m.answer(f"❌ Could not send code: {h(e)}\nTry again or tap 🔙 Back.")
    PENDING[m.from_user.id] = dict(app=app, phone=phone, hash=sent.phone_code_hash)
    await state.set_state(S.otp)
    await m.answer(
        f"🔑 OTP sent to {h(phone)}\n\nSend the login code. You can paste the full message — I'll extract the digits automatically.\n\n"
        "Tap 🔙 Back to cancel, or send /cancel to abort.")


async def finish_login(m: Message, state: FSMContext, p):
    app = p["app"]
    session = await app.export_session_string()
    me = await app.get_me()
    await app.disconnect()
    name = " ".join(x for x in [me.first_name, me.last_name] if x) or "NoName"
    db.execute("INSERT OR REPLACE INTO clients(id,name,phone,session,owner) VALUES(?,?,?,?,?)",
               (me.id, name, p["phone"], fernet.encrypt(session.encode()), m.from_user.id))
    db.commit()
    await start_client(session, me.id, m.from_user.id)
    PENDING.pop(m.from_user.id, None)
    await state.clear()
    await m.answer(f"✅ Account added via Phone Number!\n\n👤 Name: {h(name)}\n📞 Phone: {h(p['phone'])}\n🆔 ID: {me.id}",
                   reply_markup=kb(m.from_user.id))


@dp.message(S.otp)
async def got_otp(m: Message, state: FSMContext):
    p = PENDING.get(m.from_user.id)
    code = "".join(re.findall(r"\d", m.text))
    if not p or len(code) < 5:
        return await m.answer("❌ Couldn't find a valid code. Send it again.")
    try:
        await p["app"].sign_in(p["phone"], p["hash"], code)
    except SessionPasswordNeeded:
        await state.set_state(S.pwd)
        return await m.answer("🔐 2-Step Verification is enabled.\n\nSend your account password.")
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        return await m.answer(f"❌ {e.__class__.__name__}. Send the correct code, or /cancel and retry.")
    except Exception as e:
        return await m.answer(f"❌ Login failed: {h(e)}")
    await finish_login(m, state, p)


@dp.message(S.pwd)
async def got_pwd(m: Message, state: FSMContext):
    p = PENDING.get(m.from_user.id)
    try:
        await p["app"].check_password(m.text.strip())
    except PasswordHashInvalid:
        return await m.answer("❌ Wrong password. Try again.")
    except Exception as e:
        return await m.answer(f"❌ Login failed: {h(e)}")
    try: await m.delete()  # remove password from chat
    except Exception: pass
    await finish_login(m, state, p)


# ---------- Join Channel ----------
@dp.message(F.text == B_JOIN)
async def join_ask(m: Message, state: FSMContext):
    await state.set_state(S.join)
    await m.answer(
        "🔗 Join Channel / Group\n\nSend the username or invite link.\n\n"
        "• Public: @username or https://t.me/username\n• Private: https://t.me/+InviteHash\n\n"
        "All your clients will attempt to join.\n\nSend /cancel to abort.", reply_markup=BACK_KB)


@dp.message(S.join)
async def join_do(m: Message, state: FSMContext):
    target = parse_target(m.text)
    await state.clear()
    await m.answer("⏳ Joining...")
    ok, fail, secs = await for_all(m.from_user.id, lambda app, _: app.join_chat(target))
    await m.answer(report(ok, fail, secs, "Joined"), reply_markup=kb(m.from_user.id))


# ---------- Voice chat ----------
@dp.message(F.text == B_JVC)
async def jvc_ask(m: Message, state: FSMContext):
    await state.set_state(S.jvc)
    await m.answer(
        "🎙 Join Voice Chat\n\nSend the username or Chat ID.\n\n"
        "• Public: @username or https://t.me/username\n"
        "• Private: Chat ID (e.g. -1001234567890) or https://t.me/c/1234567890\n\n"
        "⚠️ For private groups/channels: use 🔗 Join Channel first, then come here with the Chat ID.\n"
        "⚠️ Invite links (t.me/+...) are not supported here — use the Chat ID instead.\n\nSend /cancel to abort.",
        reply_markup=BACK_KB)


@dp.message(S.jvc)
async def jvc_do(m: Message, state: FSMContext):
    chat = parse_target(m.text)
    if isinstance(chat, str) and chat.startswith("https://t.me/+"):
        return await m.answer("⚠️ Invite links aren't supported here. Send the Chat ID.")
    await state.clear()
    await m.answer("⏳ Joining voice chat...")

    async def fn(app, calls):
        await app.get_chat(chat)   # cache peer
        await calls.play(chat)     # no stream = silent listener

    ok, fail, secs = await for_all(m.from_user.id, fn)
    await m.answer(report(ok, fail, secs, "Joined VC"), reply_markup=kb(m.from_user.id))


@dp.message(F.text == B_LVC)
async def lvc_ask(m: Message, state: FSMContext):
    await state.set_state(S.lvc)
    await m.answer("🔇 Leave Voice Chat\n\nSend the username or Chat ID.\n\nSend /cancel to abort.", reply_markup=BACK_KB)


@dp.message(S.lvc)
async def lvc_do(m: Message, state: FSMContext):
    chat = parse_target(m.text)
    await state.clear()

    async def fn(app, calls):
        await calls.leave_call(chat)

    ok, fail, secs = await for_all(m.from_user.id, fn)
    await m.answer(report(ok, fail, secs, "Left VC"), reply_markup=kb(m.from_user.id))


# ---------- Send Reaction ----------
@dp.message(F.text == B_REACT)
async def react_ask(m: Message, state: FSMContext):
    await state.set_state(S.react_link)
    await m.answer("❤️ Send Reaction\n\nSend the post link, e.g. https://t.me/channel/123\n\nSend /cancel to abort.", reply_markup=BACK_KB)


@dp.message(S.react_link)
async def react_link(m: Message, state: FSMContext):
    post = parse_post(m.text)
    if not post:
        return await m.answer("❌ Invalid post link. Try again.")
    await state.update_data(post=post)
    await state.set_state(S.react_emoji)
    await m.answer("Now send the emoji to react with (e.g. 👍 ❤️ 🔥).")


@dp.message(S.react_emoji)
async def react_do(m: Message, state: FSMContext):
    chat, mid = (await state.get_data())["post"]
    emoji = m.text.strip()
    await state.clear()

    async def fn(app, _):
        await app.get_chat(chat)
        await app.send_reaction(chat, mid, emoji)

    ok, fail, secs = await for_all(m.from_user.id, fn)
    await m.answer(report(ok, fail, secs, "Reacted"), reply_markup=kb(m.from_user.id))


# ---------- Auto React / Auto View ----------
def auto_list(uid):
    r = ", ".join(f"{c} {h(e)}" for (o, c), e in AUTO_REACT.items() if o == uid) or "none"
    v = ", ".join(str(c) for (o, c) in AUTO_VIEW if o == uid) or "none"
    return f"🔁 Auto React: {r}\n👁 Auto View: {v}"


async def resolve_chat_id(uid, target):
    if isinstance(target, int):
        return target
    for _, (app, _c, _o) in user_clients(uid):
        return (await app.get_chat(target)).id
    raise RuntimeError("No clients logged in")


@dp.message(F.text == B_AR)
async def ar_ask(m: Message, state: FSMContext):
    await state.set_state(S.ar_chat)
    await m.answer(f"🔁 Auto React\n\n{auto_list(m.from_user.id)}\n\nSend the channel username/Chat ID to enable.\n"
                   "Add <code>off</code> after it to disable, e.g. <code>@channel off</code>.\n\nSend /cancel to abort.",
                   reply_markup=BACK_KB)


@dp.message(S.ar_chat)
async def ar_chat(m: Message, state: FSMContext):
    uid = m.from_user.id
    off = m.text.strip().lower().endswith(" off")
    try:
        chat = await resolve_chat_id(uid, parse_target(m.text.rsplit(" ", 1)[0] if off else m.text))
    except Exception as e:
        return await m.answer(f"❌ {h(e)}")
    if off:
        AUTO_REACT.pop((uid, chat), None)
        db.execute("DELETE FROM auto2 WHERE owner=? AND kind='react' AND chat_id=?", (uid, str(chat))); db.commit()
        await state.clear()
        return await m.answer("🔁 Auto React disabled.", reply_markup=kb(uid))
    await state.update_data(chat=chat)
    await state.set_state(S.ar_emoji)
    await m.answer("Send the emoji to auto-react with.")


@dp.message(S.ar_emoji)
async def ar_emoji(m: Message, state: FSMContext):
    uid = m.from_user.id
    chat = (await state.get_data())["chat"]
    emoji = m.text.strip()
    AUTO_REACT[(uid, chat)] = emoji
    db.execute("INSERT OR REPLACE INTO auto2 VALUES(?,'react',?,?)", (uid, str(chat), emoji)); db.commit()
    await state.clear()
    await m.answer(f"✅ Auto React enabled for {chat} with {h(emoji)}", reply_markup=kb(uid))


@dp.message(F.text == B_AV)
async def av_ask(m: Message, state: FSMContext):
    await state.set_state(S.av_chat)
    await m.answer(f"👁 Auto View\n\n{auto_list(m.from_user.id)}\n\nSend the channel username/Chat ID to enable.\n"
                   "Add <code>off</code> to disable, e.g. <code>@channel off</code>.\n\nSend /cancel to abort.", reply_markup=BACK_KB)


@dp.message(S.av_chat)
async def av_chat(m: Message, state: FSMContext):
    uid = m.from_user.id
    off = m.text.strip().lower().endswith(" off")
    try:
        chat = await resolve_chat_id(uid, parse_target(m.text.rsplit(" ", 1)[0] if off else m.text))
    except Exception as e:
        return await m.answer(f"❌ {h(e)}")
    if off:
        AUTO_VIEW.discard((uid, chat))
        db.execute("DELETE FROM auto2 WHERE owner=? AND kind='view' AND chat_id=?", (uid, str(chat)))
    else:
        AUTO_VIEW.add((uid, chat))
        db.execute("INSERT OR REPLACE INTO auto2 VALUES(?,'view',?,'')", (uid, str(chat)))
    db.commit()
    await state.clear()
    await m.answer(f"👁 Auto View {'disabled' if off else 'enabled'} for {chat}", reply_markup=kb(uid))
    if off:
        for k in [k for k in SEEN if k[1] == chat and k[2] == "v"]:
            SEEN.pop(k, None)
        return
    ok = fail = 0
    err = ""
    for tg_id, (app, _c, _o) in user_clients(uid):
        try:
            SEEN[(tg_id, chat, "v")] = set(await recent_ids(app, chat))   # baseline: only NEW posts get views
            ok += 1
        except Exception as e:
            fail += 1
            err = repr(e)
    await m.answer(f"👁 Ready for new posts\n✅ OK: {ok} client(s)\n❌ Failed: {fail} client(s)"
                   + (f"\n\nError: {h(err)}\nMake sure these accounts joined the channel first." if err else
                      "\n\nEvery new post from now on will be viewed by all your clients."))


# ---------- Access / Admin panel ----------
@dp.message(F.text == B_ACC)
async def access(m: Message):
    uid = m.from_user.id
    if uid == OWNER_ID:
        n = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        return await m.answer(f"🔑 Access\n\n👑 You are the admin.\n📋 You control all {n} account(s) in this bot.")
    n = db.execute("SELECT COUNT(*) FROM clients WHERE owner=?", (uid,)).fetchone()[0]
    await m.answer(f"🔑 Access\n\n✅ Your access is active.\n📋 Your clients: {n}")


def count(sql, *a):
    return db.execute(sql, a).fetchone()[0]


def v_panel():
    pend = count("SELECT COUNT(*) FROM users WHERE status='pending'")
    appr = count("SELECT COUNT(*) FROM users WHERE status='approved'")
    txt = (f"🛠 Admin Panel\n\n👥 Approved users: {appr}\n"
           f"📥 Pending requests: {pend}\n"
           f"📋 Total accounts: {count('SELECT COUNT(*) FROM clients')} ({len(RUNNING)} online)")
    return txt, ikb([(f"📥 Requests ({pend})", "adm:req", "primary")], [("👥 Users", "adm:users"), ("📋 All Accounts", "adm:acc")])


def v_req():
    rows = db.execute("SELECT user_id, name, username FROM users WHERE status='pending'").fetchall()
    txt = "📥 Requests\n\n" + ("\n".join(f"• {h(n or '—')} | 🆔 {i}" for i, n, _u in rows) if rows else "No pending requests.")
    btns = [[(f"✅ {(n or str(i))[:14]}", f"adm:ap:{i}", "success"), (f"❌ {(n or str(i))[:14]}", f"adm:rj:{i}", "danger")]
            for i, n, _u in rows[:30]]
    return txt, ikb(*btns, [("⬅️ Back", "adm:home")])


def v_users():
    rows = db.execute("SELECT user_id, name, status FROM users WHERE status!='pending' ORDER BY status").fetchall()
    icons = {"approved": "✅", "rejected": "❌", "revoked": "🚫"}
    txt = "👥 Users\n\n" + ("\n".join(f"{icons.get(s, '•')} {h(n or '—')} | 🆔 {i}" for i, n, s in rows) if rows else "No users yet.")
    btns = []
    for i, n, s in rows[:30]:
        nm = (n or str(i))[:14]
        btns.append([(f"🚫 Revoke {nm}", f"adm:rv:{i}", "danger")] if s == "approved"
                    else [(f"✅ Approve {nm}", f"adm:ap:{i}", "success")])
    return txt, ikb(*btns, [("⬅️ Back", "adm:home")])


def v_acc():
    rows = db.execute("SELECT id, name, owner FROM clients ORDER BY owner").fetchall()
    groups = {}
    for cid, n, o in rows:
        groups.setdefault(o, []).append((cid, n))
    parts = []
    for o, items in groups.items():
        parts.append(f"👥 {h(owner_name(o))} ({len(items)})\n" + "\n".join(
            f"   {'🟢' if c in RUNNING else '🔴'} {h(n)}" for c, n in items))
    body = "\n\n".join(parts) or "No accounts yet."
    if len(body) > 3500:
        body = body[:3500] + "\n…"
    return f"📋 All Accounts ({len(rows)})\n\n{body}", ikb([("⬅️ Back", "adm:home")])


async def show(c: CallbackQuery, view):
    text, markup = view
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except Exception:
        await c.message.answer(text, reply_markup=markup)


@dp.message(F.text == B_ADMIN)
async def admin_panel(m: Message):
    if m.from_user.id != OWNER_ID:
        return await m.answer("⛔ Admin only.")
    txt, k = v_panel()
    await m.answer(txt, reply_markup=k)


@dp.callback_query(F.data.startswith("adm:"))
async def admin_cb(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return await c.answer("⛔ Admin only", show_alert=True)
    parts = c.data.split(":")
    act = parts[1]
    if act in ("ap", "rj", "rv"):
        uid = int(parts[2])
        status = {"ap": "approved", "rj": "rejected", "rv": "revoked"}[act]
        db.execute("UPDATE users SET status=? WHERE user_id=?", (status, uid)); db.commit()
        msg = {"ap": "✅ Your access request has been approved!\n\nSend /start to begin.",
               "rj": "❌ Your access request was rejected by the bot owner.",
               "rv": "🚫 Your access has been revoked by the bot owner."}[act]
        try:
            await c.bot.send_message(uid, msg)
        except Exception:
            pass
        head = c.message.text or ""
        if head.startswith("🔔"):
            result = {"ap": "✅ Accepted", "rj": "❌ Rejected", "rv": "🚫 Revoked"}[act]
            await c.message.edit_text(f"{h(head)}\n\n{result}")
        elif head.startswith("📥"):
            await show(c, v_req())
        else:
            await show(c, v_users())
        return await c.answer("Done")
    views = {"home": v_panel, "req": v_req, "users": v_users, "acc": v_acc}
    if act in views:
        await show(c, views[act]())
    await c.answer()


# ---------- boot ----------
async def main():
    load_auto()
    for tg_id, session, owner in db.execute("SELECT id, session, owner FROM clients").fetchall():
        try:
            await start_client(fernet.decrypt(session).decode(), tg_id, owner or OWNER_ID)
            log.info("client %s started", tg_id)
        except Exception as e:
            log.warning("client %s failed to start: %r", tg_id, e)
    asyncio.create_task(watcher())
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if PREMIUM:
        bot.session.middleware(PremiumMW())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
