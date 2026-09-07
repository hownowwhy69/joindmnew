"""
╔══════════════════════════════════════════════════════════════════════╗
║  WELCOME JOINER BOT — Telegram  (MULTI-USER, single file)              ║
║                                                                        ║
║  • Koi bhi user bot use kar sakta hai — har user ka apna data          ║
║  • Join requests: approve kiye BINA welcome PM  (approve owner karta)  ║
║  • Har requester ka user ID save → 📣 per-owner broadcast              ║
║  • Welcome: text + premium emojis ✨ + media + inline buttons          ║
║  • Unlimited users / channels / groups                                 ║
║                                                                        ║
║  Run:  pip install -r requirements.txt   →   python main.py            ║
║  Config: .env file ya environment vars (BOT_TOKEN, ADMIN_IDS)          ║
╚══════════════════════════════════════════════════════════════════════╝
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from html import escape as html_escape
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import (
    ChatMemberUpdatedFilter,
    Command,
    CommandStart,
    JOIN_TRANSITION,
    LEAVE_TRANSITION,
)
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ═══════════════════════════ CONFIG ═══════════════════════════


# ── Bot token from @BotFather ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ── Your numeric Telegram user id(s), comma separated ───────────────────────
# Get it from @userinfobot — only these users may control the bot.
def _parse_admin_ids(raw: str) -> list:
    ids = []
    for part in raw.replace(" ", "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit() and part:
            ids.append(int(part))
    return ids

ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", "") or "")

# ── SQLite file ─────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "data.db")          # purana SQLite (auto-import ke liye)

# ── MongoDB (data store) ─────────────────────────────────────────────────
# Local:  mongodb://localhost:27017
# Atlas:  mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "welcome_joiner_bot")

BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")   # auto-backups yahan save honge
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "10"))  # kitne purane backups rakhne hain

# ═══════════════════════════ DATABASE ═══════════════════════════
log = logging.getLogger("welcome-joiner")


@dataclass
class ChatRow:
    chat_id: int
    chat_type: str          # "channel" | "supergroup" | "group"
    title: str
    owner_id: int = 0       # 0 = unclaimed (koi bhi claim kar sakta hai)
    welcome_text: str = ""
    media_file_id: str = ""
    media_kind: str = ""    # "photo" | "video" | "animation" | "document" | "audio"
    buttons: str = "[]"     # JSON list of button rows
    emoji: str = "[]"       # JSON list of {"id": custom_emoji_id, "char": emoji char}
    approve: int = 0        # 0 = message WITHOUT approving (default); 1 = auto-approve
    custom_bot_token: str = ""   # Channel-Help jaisa custom bot (welcome isi se jayega)
    custom_bot_name: str = ""    # custom bot ka @username (display ke liye)
    created_at: int = 0

    def button_rows(self) -> list:
        """Return buttons as a list of rows: [[{text,url}, ...], ...]."""
        try:
            data = json.loads(self.buttons or "[]")
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        if data and isinstance(data[0], dict):
            return [data]
        return [r for r in data if isinstance(r, list)]

    def emoji_list(self) -> list:
        try:
            data = json.loads(self.emoji or "[]")
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def short_desc(self) -> str:
        icon = "📢" if self.chat_type == "channel" else "👥"
        return f"{icon} {self.title}"


@dataclass
class UserRow:
    owner_id: int
    user_id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    added_at: int = 0
    last_seen: int = 0

    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or "User"


class Database:
    """
    MongoDB-backed storage (pymongo).

    • MONGO_URI / MONGO_DB .env se aate hain (default localhost:27017)
    • Atlas (cloud) use karoge to data kabhi nahi khota — VPS mar jaye tab bhi!
    • Purana SQLite data.db (agar hai) pehli baar connect par KHUD import hota hai
    • backup()/restore() — JSON export/import (data_*.json files)
    """

    def __init__(self, path: str = DB_PATH, uri: str | None = None,
                 db_name: str | None = None, client=None):
        self.path = path
        self.uri = uri or MONGO_URI
        self.db_name = db_name or MONGO_DB
        self._client = client          # tests/injection ke liye
        self._db = None

    # ── collections ─────────────────────────────────────────────────────────
    @property
    def chats(self): return self._db["chats"]

    @property
    def users(self): return self._db["users"]

    @property
    def user_chats(self): return self._db["user_chats"]

    def connect(self, legacy_owner: int = 0):
        """MongoDB se connect + indexes + purane SQLite data ka import."""
        if self._client is None:
            try:
                from pymongo import MongoClient
                self._client = MongoClient(self.uri, serverSelectionTimeoutMS=8000)
                self._client.admin.command("ping")
            except Exception as e:
                emsg = str(e)
                err = emsg.lower()
                hint = ""
                if "escape" in err or "rfc 3986" in err or "invalid" in err:
                    hint = ("\n   ⚡ PROBLEM: MONGO_URI ka format hi galat hai!\n"
                            "      • '<db_username>' jaisa placeholder mat chhodo — uski jagah\n"
                            "        apna ASLI username daalo (Atlas → Database Access me dikhta hai)\n"
                            "      • URI me [brackets] / (mailto:) / <> jaise characters nahi\n"
                            "        hone chahiye — sirf ye format:\n"
                            "        mongodb+srv://USERNAME:PASSWORD@cluster0.xxxx.mongodb.net/?appName=Cluster0")
                elif "authentication failed" in err or "bad auth" in err:
                    hint = ("\n   ⚡ PROBLEM: USERNAME ya PASSWORD galat hai!\n"
                            "      • cloud.mongodb.com → 'Database Access' → apna username dekho\n"
                            "      • Password bhool gaye ho to: Database Access → Edit →\n"
                            "        'Edit Password' → naya banao\n"
                            "      • Phir URI me daalo:  mongodb+srv://ASLI-USERNAME:NAYA-PASSWORD@cluster0...")
                elif "timed out" in err or "select" in err or "dns" in err:
                    hint = ("\n   ⚡ PROBLEM: Cluster tak network nahi pahunch raha!\n"
                            "      • Atlas → 'Network Access' → 'Add IP Address' →\n"
                            "        0.0.0.0/0 (Allow from anywhere) daalo — zaroori hai!\n"
                            "      • Cluster 'M0' hokar PAUSED to nahi? (Atlas dashboard check karo)")
                raise SystemExit(
                    f"❌❌ MongoDB se connection nahi ho paya — isliye bot start nahi hua! ❌❌\n\n"
                    f"   Error: {emsg[:200]}{hint}\n\n"
                    "   Atlas free banana: cloud.mongodb.com → Build a Database → M0 (FREE)\n"
                    "   Local install: sudo apt install -y mongodb-org && sudo systemctl enable --now mongod\n"
                    "   Packages: pip install -r requirements.txt")
        self._db = self._client[self.db_name]

        # unique indexes (multi-user data isolation ke liye)
        self.chats.create_index("chat_id", unique=True)
        self.users.create_index([("owner_id", 1), ("user_id", 1)], unique=True)
        self.user_chats.create_index(
            [("owner_id", 1), ("user_id", 1), ("chat_id", 1)], unique=True)

        # purana SQLite data → MongoDB (sirf pehli baar, jab mongo khali ho)
        self._migrate_sqlite(legacy_owner)

    # ── SQLite → MongoDB migration ──────────────────────────────────────────
    def _migrate_sqlite(self, legacy_owner: int):
        if not os.path.exists(self.path):
            return
        if self.chats.count_documents({}) > 0 or self.users.count_documents({}) > 0:
            return  # mongo me pehle se data hai — sqlite ko chhodo
        try:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            imported = 0
            for table, col in (("chats", self.chats), ("users", self.users),
                               ("user_chats", self.user_chats)):
                try:
                    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                    docs = []
                    for r in rows:
                        d = dict(r)
                        d.setdefault("owner_id", legacy_owner)
                        docs.append(d)
                    if docs:
                        col.insert_many(docs)
                        imported += len(docs)
                except sqlite3.Error:
                    pass
            conn.close()
            if imported:
                renamed = self.path + ".sqlite-imported"
                try:
                    os.rename(self.path, renamed)
                    log.info("✅ SQLite (%s records) MongoDB me import ho gaya. "
                             "Purani file: %s", imported, renamed)
                except OSError:
                    pass
        except Exception as e:
            log.warning("SQLite migration failed: %s", e)

    # ── admins (authorized bot users) ──────────────────────────────────────
    @property
    def admins(self): return self._db["admins"]

    def is_admin(self, user_id: int) -> bool:
        return self.admins.find_one({"user_id": user_id}) is not None

    def add_admin(self, user_id: int, added_by: int):
        self.admins.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id, "added_by": added_by,
                              "added_at": int(time.time())}},
            upsert=True)

    def remove_admin(self, user_id: int):
        self.admins.delete_one({"user_id": user_id})

    def get_admins(self) -> list:
        return list(self.admins.find().sort("added_at", 1))

    # ── chats ───────────────────────────────────────────────────────────────
    def _doc_to_chat(self, d) -> ChatRow:
        return ChatRow(
            chat_id=d.get("chat_id", 0),
            chat_type=d.get("chat_type", "channel"),
            title=d.get("title", ""),
            owner_id=d.get("owner_id", 0),
            welcome_text=d.get("welcome_text", ""),
            media_file_id=d.get("media_file_id", ""),
            media_kind=d.get("media_kind", ""),
            buttons=d.get("buttons", "[]"),
            emoji=d.get("emoji", "[]"),
            approve=d.get("approve", 0),
            custom_bot_token=d.get("custom_bot_token", ""),
            custom_bot_name=d.get("custom_bot_name", ""),
            created_at=d.get("created_at", 0),
        )

    def add_chat(self, chat_id: int, chat_type: str, title: str,
                 owner_id: int = 0) -> ChatRow:
        self.chats.update_one(
            {"chat_id": chat_id},
            {"$setOnInsert": {
                "chat_id": chat_id, "chat_type": chat_type, "title": title,
                "owner_id": owner_id, "welcome_text": "", "media_file_id": "",
                "media_kind": "", "buttons": "[]", "emoji": "[]",
                "approve": 0, "created_at": int(time.time())}},
            upsert=True)
        if owner_id:
            self.chats.update_one({"chat_id": chat_id, "owner_id": 0},
                                  {"$set": {"owner_id": owner_id}})
        return self.get_chat(chat_id)

    def claim_chat(self, chat_id: int, owner_id: int) -> bool:
        res = self.chats.update_one({"chat_id": chat_id, "owner_id": 0},
                                    {"$set": {"owner_id": owner_id}})
        return (res.modified_count or 0) > 0

    def remove_chat(self, chat_id: int):
        self.chats.delete_one({"chat_id": chat_id})

    def get_chat(self, chat_id: int) -> Optional[ChatRow]:
        d = self.chats.find_one({"chat_id": chat_id})
        return self._doc_to_chat(d) if d else None

    def get_chats(self, owner_id: int | None = None) -> list:
        if owner_id is None:
            docs = self.chats.find().sort("created_at", 1)
        else:
            docs = self.chats.find({"owner_id": owner_id}).sort("created_at", 1)
        return [self._doc_to_chat(d) for d in docs]

    def update_chat(self, chat_id: int, **fields):
        allowed = {"welcome_text", "media_file_id", "media_kind", "buttons",
                   "emoji", "approve", "title", "chat_type",
                   "custom_bot_token", "custom_bot_name"}
        keys = [k for k in fields if k in allowed]
        if not keys:
            return
        self.chats.update_one({"chat_id": chat_id},
                              {"$set": {k: fields[k] for k in keys}})

    # ── users (join-requesters, per-owner for broadcast) ────────────────────
    def upsert_user(self, owner_id: int, user_id: int, first_name: str,
                    last_name: str, username: str):
        now = int(time.time())
        self.users.update_one(
            {"owner_id": owner_id, "user_id": user_id},
            {"$set": {"first_name": first_name, "last_name": last_name,
                      "username": username, "last_seen": now},
             "$setOnInsert": {"added_at": now}},
            upsert=True)

    def link_user_chat(self, owner_id: int, user_id: int, chat_id: int):
        self.user_chats.update_one(
            {"owner_id": owner_id, "user_id": user_id, "chat_id": chat_id},
            {"$setOnInsert": {"created_at": int(time.time())}},
            upsert=True)

    def get_users(self, owner_id: int) -> list:
        docs = self.users.find({"owner_id": owner_id}).sort("added_at", -1)
        return [self._row_to_user(d) for d in docs]

    def count_users(self, owner_id: int) -> int:
        return self.users.count_documents({"owner_id": owner_id})

    def _row_to_user(self, d) -> UserRow:
        return UserRow(
            owner_id=d.get("owner_id", 0),
            user_id=d.get("user_id", 0),
            first_name=d.get("first_name", ""),
            last_name=d.get("last_name", ""),
            username=d.get("username", ""),
            added_at=d.get("added_at", 0),
            last_seen=d.get("last_seen", 0),
        )

    # ── backup / restore (JSON export — cloud se bhi copy kar sakte ho) ─────
    def backup(self, dest_path: str):
        from bson.json_util import dumps
        data = {
            "chats": list(self.chats.find()),
            "users": list(self.users.find()),
            "user_chats": list(self.user_chats.find()),
        }
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(dumps(data))

    def restore(self, src_path: str):
        from bson.json_util import loads
        with open(src_path, "r", encoding="utf-8") as f:
            data = loads(f.read())
        for col in ("chats", "users", "user_chats"):
            self._db[col].delete_many({})
            docs = data.get(col) or []
            if docs:
                self._db[col].insert_many(docs)


# ═══════════════════════════ BOT ════════════════════════════════

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import (
    ChatMemberUpdatedFilter,
    Command,
    CommandStart,
    JOIN_TRANSITION,
    LEAVE_TRANSITION,
)
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)


# ─────────────────────────────────────────────────────────────────────────────

if not BOT_TOKEN:
    raise SystemExit(
        "❌❌ BOT_TOKEN empty hai — isliye bot start nahi hua! ❌❌\n\n"
        "TOKEN DAALNE KE 2 TAREEKE (koi ek karo):\n\n"
        "  1️⃣  .env file banao (RECOMMENDED)\n"
        "      → folder me ek nayi file banao jiska NAAM ho:  .env   (bina kisi extension!)\n"
        "      → usme ek line likho:  BOT_TOKEN=1234567890:AAH.....tumhara-token\n"
        "      ⚠️ Windows me dhyan: file ko .env.txt mat naam dena — sirf .env\n"
        "      ⚠️ .env.example me mat daalo — bot usse padhta hi NAHI hai!\n\n"
        "  2️⃣  main.py me direct daalo (koi file nahi chahiye)\n"
        "      → main.py kholo, CONFIG section me line no. ~69 ke paas:\n"
        "         BOT_TOKEN = os.getenv(\"BOT_TOKEN\", \"\")\n"
        "      → isko badal kar likho:\n"
        "         BOT_TOKEN = \"1234567890:AAH.....tumhara-token\"\n\n"
        "TOKEN KAHAN SE: @BotFather → /mybots → apna bot → API Token\n")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ═══════════════════════════ ADMIN GATE (auth) ═══════════════════════════
# Sirf AUTHORIZED admins hi bot use kar sakte hain. Baaki sabko:
#   "🚫 You are not authorized to use this bot."
# Admin banane ka power sirf SUPER-ADMINS (ADMIN_IDS) ke paas hai.

AUTH_MSG = "🚫 You are not authorized to use this bot."


def is_super_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_admin(user_id: int) -> bool:
    return is_super_admin(user_id) or db.is_admin(user_id)


GATE_CACHE: dict = {}          # uid -> (monotonic_time, allowed)


def is_authorized(user_id: int) -> bool:
    """Admin ya chat-owner? (owner ke live chats hain — uske approve buttons
    bhi gate ke through aate hain)."""
    now = time.monotonic()
    cached = GATE_CACHE.get(user_id)
    if cached is not None and now - cached[0] < 120:
        return cached[1]
    allowed = is_admin(user_id) or bool(db.get_chats(user_id))
    GATE_CACHE[user_id] = (now, allowed)
    if len(GATE_CACHE) > 2000:
        GATE_CACHE.clear()
    return allowed


class AdminGate(BaseMiddleware):
    """Message/callback par lagne wala gate — join-request wale events ko nahi."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None and is_authorized(user.id):
            return await handler(event, data)
        # ── unauthorized ──
        try:
            if isinstance(event, Message):
                if (event.text or "").startswith("/start"):
                    await event.answer(AUTH_MSG)
                # baaki normal messages silently ignore (spam nahi)
            elif isinstance(event, CallbackQuery):
                await event.answer(AUTH_MSG, show_alert=True)
        except Exception:
            pass
        return None


router.message.outer_middleware(AdminGate())
router.callback_query.outer_middleware(AdminGate())

db = Database(DB_PATH)

ME_ID: int = 0          # bot's own user id
BOT_USERNAME: str = ""  # bot username (without @)
IST = ZoneInfo("Asia/Kolkata")


def auto_backup(force: bool = False, database=None):
    """
    MongoDB ka saara data JSON export backups/ me daalta hai.
    - Default: har 24 ghante me ek baar (bot start pe bhi check hota hai)
    - force=True (ya /backup command) → abhi backup
    Returns (path, created).
    """
    database = database or db
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        now = time.time()
        newest = None
        for f in os.listdir(BACKUP_DIR):
            if not (f.startswith("data_") and f.endswith(".json")):
                continue
            full = os.path.join(BACKUP_DIR, f)
            try:
                m = os.path.getmtime(full)
            except OSError:
                continue
            if newest is None or m > newest[1]:
                newest = (full, m)

        if not force and newest is not None and (now - newest[1]) < BACKUP_INTERVAL_HOURS * 3600:
            return newest[0], False

        fname = f"data_{datetime.now(IST).strftime('%Y%m%d_%H%M%S_%f')}.json"
        path = os.path.join(BACKUP_DIR, fname)
        db.backup(path)

        # sirf BACKUP_KEEP sabse naye backups rakho, purane delete
        files = []
        for f in os.listdir(BACKUP_DIR):
            full = os.path.join(BACKUP_DIR, f)
            if f.startswith("data_") and f.endswith(".json"):
                try:
                    files.append((os.path.getmtime(full), full))
                except OSError:
                    pass
        for _, old in sorted(files)[:-BACKUP_KEEP]:
            try:
                os.remove(old)
            except OSError:
                pass
        return path, True
    except Exception as e:
        log.warning("auto_backup failed: %s", e)
        return None, False

# Per-owner "current flow" state: user_id -> (state, payload)
STASH: dict = {}

# Message ids of the currently-open chat panel, chat_id -> message_id
PANEL_MSG: dict = {}

# Broadcast job (content the owner queued) + stop flag
BCAST_JOBS: dict = {}        # user_id -> broadcast job (per-user queue)
BROADCAST_STOP: bool = False

# ─────────────────────────────────────────────────────────────────────────────
#  Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def can_access(row, user_id: int) -> bool:
    """Multi-user check: chat ka owner (ya unclaimed chat) hi use kar sakta hai."""
    return row.owner_id == 0 or row.owner_id == user_id


def esc(value) -> str:
    return html_escape(str(value or ""))


# Telegram HTML tag  ->  MessageEntity type (for entities-based sending)
TAG_MAP = {
    "b": "bold", "strong": "bold",
    "i": "italic", "em": "italic",
    "u": "underline", "ins": "underline",
    "s": "strikethrough", "strike": "strikethrough", "del": "strikethrough",
    "code": "code", "pre": "pre", "blockquote": "blockquote",
    "tg-spoiler": "spoiler",
}
ALLOWED_TAGS = set(TAG_MAP)

# Custom-emoji marker embedded in stored welcome text: \x01<index>\x01
EMOJI_MARKER_RE = re.compile(r"(\x01\d+\x01)")
TAG_TOKEN_RE = re.compile(r"(<[^>]{1,40}>)")
TAG_FULL_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)\s*>")


def utf16_len(s: str) -> int:
    """Telegram counts offsets in UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


def display_text(text: str) -> str:
    """Panel preview: show ✨ instead of stored emoji markers."""
    return EMOJI_MARKER_RE.sub("✨", text or "")


def truncate(text: str, n: int = 120) -> str:
    text = (display_text(text) or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def render_welcome(row: ChatRow, first: str, last: str, username: str,
                   user_id: int, chat_title: str | None = None):
    """
    Render the stored welcome into (plain_text, entities).

    Supports in the stored text:
      • placeholders: {name} {first} {last} {username} {chat} {id} {date}
      • HTML-ish tags: <b> <i> <u> <s> <code> <pre> <blockquote> <tg-spoiler>
      • premium custom emoji stored as \x01<idx>\x01 markers
    The result is sent with `entities` (NOT parse_mode), so premium emojis
    and formatting work together in one message.
    """
    full_name = f"{first} {last}".strip() or first
    text = row.welcome_text or ""
    text = text.replace("{name}", full_name)
    text = text.replace("{first}", first)
    text = text.replace("{last}", last)
    text = text.replace("@{username}", f"@{username}" if username else "")
    text = text.replace("{username}", username or "")
    text = text.replace("{id}", str(user_id))
    text = text.replace("{chat}", chat_title or row.title or "")
    text = text.replace("{date}", datetime.now(IST).strftime("%d-%m-%Y %I:%M %p"))

    emoji = row.emoji_list()

    # tokenize: emoji markers + text + tags (raw tag text kept for literal passthrough)
    segs: list = []
    for piece in EMOJI_MARKER_RE.split(text):
        if not piece:
            continue
        m = EMOJI_MARKER_RE.fullmatch(piece)
        if m:
            idx = int(piece[1:-1])
            if 0 <= idx < len(emoji):
                segs.append(("emoji", idx))
            continue
        for tok in TAG_TOKEN_RE.split(piece):
            if not tok:
                continue
            m2 = TAG_FULL_RE.fullmatch(tok)
            if m2 and m2.group(1).lower() in ALLOWED_TAGS:
                tag = m2.group(1).lower()
                segs.append(("close", tag, tok) if tok.startswith("</")
                            else ("open", tag, tok))
            else:
                segs.append(("text", tok, None))

    # pass 1: match open/close pairs (unmatched tags stay literal text)
    open_stack: list[int] = []
    matched: dict[int, int] = {}
    for i, seg in enumerate(segs):
        if seg[0] == "open":
            open_stack.append(i)
        elif seg[0] == "close":
            for j in range(len(open_stack) - 1, -1, -1):
                if segs[open_stack[j]][1] == seg[1]:
                    partner = open_stack.pop(j)
                    matched[i] = partner
                    matched[partner] = i
                    break            # orphans above stay unmatched -> literal

    # pass 2: assemble output text + entities (UTF-16 offsets)
    out: list[str] = []
    entities: list[MessageEntity] = []
    estack: list[tuple[str, int]] = []   # (entity_type, start_offset)
    pos = 0

    for i, seg in enumerate(segs):
        kind = seg[0]
        if kind == "text":
            s = seg[1]
            out.append(s)
            pos += utf16_len(s)
        elif kind == "emoji":
            ch = emoji[seg[1]]["char"]
            out.append(ch)
            length = utf16_len(ch)
            entities.append(MessageEntity(
                type="custom_emoji", offset=pos, length=length,
                custom_emoji_id=emoji[seg[1]]["id"]))
            pos += length
        elif kind == "open":
            if i in matched:
                estack.append((TAG_MAP[seg[1]], pos))
            else:
                out.append(seg[2])
                pos += utf16_len(seg[2])
        elif kind == "close":
            if i in matched:
                etype = TAG_MAP[seg[1]]
                start = None
                for j in range(len(estack) - 1, -1, -1):
                    if estack[j][0] == etype:
                        start = estack[j][1]
                        del estack[j:]
                        break
                if start is not None:
                    entities.append(MessageEntity(type=etype, offset=start,
                                                  length=pos - start))
            else:
                out.append(seg[2])
                pos += utf16_len(seg[2])

    # pass 3: split overlapping entities (premium emoji INSIDE bold/italic etc.
    # causes 'entity overlap' error on Telegram — isliye formatting ko emoji ke
    # aas-paas tukdo me todna padta hai) + merge adjacent same-type pieces
    emoji_ranges = [(e.offset, e.offset + e.length)
                    for e in entities if e.type == "custom_emoji"]
    cleaned: list[MessageEntity] = []
    for e in entities:
        if e.type == "custom_emoji":
            cleaned.append(e)
            continue
        segs = [(e.offset, e.offset + e.length)]
        for es, ee in emoji_ranges:
            new_segs = []
            for s0, s1 in segs:
                if ee <= s0 or es >= s1:
                    new_segs.append((s0, s1))
                    continue
                if es > s0:
                    new_segs.append((s0, es))
                if ee < s1:
                    new_segs.append((ee, s1))
            segs = new_segs
        for s0, s1 in segs:
            if s1 > s0:
                cleaned.append(MessageEntity(type=e.type, offset=s0,
                                             length=s1 - s0))

    merged: list[MessageEntity] = []
    for e in sorted(cleaned, key=lambda x: (x.offset, -x.length)):
        if (merged and merged[-1].type == e.type
                and merged[-1].offset + merged[-1].length == e.offset):
            merged[-1] = MessageEntity(type=merged[-1].type,
                                       offset=merged[-1].offset,
                                       length=merged[-1].length + e.length)
        else:
            merged.append(e)

    merged.sort(key=lambda e: (e.offset, -e.length))
    return "".join(out), merged


def render_welcome_text_only(row: ChatRow, first: str, last: str,
                             username: str, user_id: int,
                             chat_title: str | None = None) -> str:
    """Sirf plain text (entities nahi) — buttons/preview display ke liye."""
    text, _ = render_welcome(row, first, last, username, user_id, chat_title)
    return text


def build_kb(rows: list) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    kb = []
    for row in rows[:15]:
        line = []
        if not isinstance(row, list):
            row = [row]
        for b in row[:8]:
            if not isinstance(b, dict):
                continue
            line.append(InlineKeyboardButton(text=str(b.get("text", ""))[:60],
                                             url=str(b.get("url", ""))))
        if line:
            kb.append(line)
    return InlineKeyboardMarkup(inline_keyboard=kb) if kb else None


def parse_buttons(raw: str):
    """'Label | https://link' — one button per line, blank line = new row."""
    rows = []
    for chunk in (raw or "").split("\n\n"):
        row = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                label, url = line.split("|", 1)
                label, url = label.strip(), url.strip()
            else:
                label, url = line, ""
            if not label:
                continue
            if not url.startswith(("http://", "https://")):
                raise ValueError(f'Button "{label}" has no valid link (https://...)')
            row.append({"text": label, "url": url})
        if row:
            rows.append(row)
    if len(rows) > 15:
        raise ValueError("Too many rows (max 15)")
    if sum(len(r) for r in rows) > 100:
        raise ValueError("Too many buttons (max 100)")
    return rows


def extract_custom_emojis(text: str, entities):
    """
    From a message the owner sent (with premium emojis), return
    (marker_text, emoji_list) where the premium emojis are replaced with
    \x01<idx>\x01 markers and emoji_list = [{"id": .., "char": ..}, ...].
    """
    if not entities:
        return text, []
    emoji_entities = [e for e in entities
                      if e.type == "custom_emoji" and e.custom_emoji_id]
    if not emoji_entities:
        return text, []
    # entities offsets are UTF-16 — find the matching char span in text
    # (text is <= 1024/4096 chars here, so a simple walk suffices)
    spans = []
    for e in sorted(emoji_entities, key=lambda e: e.offset):
        spans.append((e.offset, e.offset + e.length, e.custom_emoji_id))

    def utf16_to_char_index(s: str, utf16_off: int) -> int:
        total = 0
        for i, ch in enumerate(s):
            if total >= utf16_off:
                return i
            total += 2 if ord(ch) > 0xFFFF else 1
        return len(s)

    emoji_list = []
    marker_text = ""
    prev = 0
    for start16, end16, eid in spans:
        co = utf16_to_char_index(text, start16)
        ce = utf16_to_char_index(text, end16)
        char = text[co:ce]
        if not char:
            continue
        emoji_list.append({"id": eid, "char": char})
        marker_text += text[prev:co] + f"\x01{len(emoji_list) - 1}\x01"
        prev = ce
    marker_text += text[prev:]
    return marker_text, emoji_list


async def notify_user(uid: int, text: str, kb: InlineKeyboardMarkup | None = None):
    """Kisi ek user ko PM (owner notifications ke liye)."""
    try:
        await bot.send_message(uid, text, reply_markup=kb)
    except Exception as e:
        log.warning("notify_user -> %s failed: %s", uid, e)


async def notify_owners(text: str, kb: InlineKeyboardMarkup | None = None):
    """Sirf configured ADMIN_IDS ko — unowned chats ke system events ke liye."""
    for uid in ADMIN_IDS:
        await notify_user(uid, text, kb)


def menu_kb(uid: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Add channel / group", callback_data="add")],
        [InlineKeyboardButton(text="📣 Broadcast", callback_data="broadcast"),
         InlineKeyboardButton(text="📊 Users", callback_data="users")],
        [InlineKeyboardButton(text="📋 My chats", callback_data="chats"),
         InlineKeyboardButton(text="❓ Help", callback_data="help")],
    ]
    if uid is not None and is_super_admin(uid):
        rows.append([InlineKeyboardButton(text="👑 Admins",
                                          callback_data="admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────────────────────────────────────────────────────────────────────
#  Welcome composer
# ─────────────────────────────────────────────────────────────────────────────

# ── custom bots (Channel-Help style: per-chat apna bot) ─────────────────────
CUSTOM_BOTS: dict = {}          # token -> Bot instance (cache)
MEDIA_CACHE: dict = {}          # file_id -> bytes (custom bot ke liye download)
WELCOME_SENT: dict = {}         # (user_id, chat_id) -> monotonic time (dedupe)
REQ_SEEN: dict = {}             # (user_id, chat_id) -> time — double notification rokne
CUSTOM_TASKS: dict = {}         # token -> asyncio.Task (custom bot poller)


def get_custom_bot(token: str):
    if token in CUSTOM_BOTS:
        return CUSTOM_BOTS[token]
    from aiogram.client.default import DefaultBotProperties
    b = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    CUSTOM_BOTS[token] = b
    return b


async def _media_bytes(file_id: str):
    """Main bot se file download karo (custom bot ko file_id samajh nahi aati)."""
    if file_id in MEDIA_CACHE:
        return MEDIA_CACHE[file_id]
    import io
    f = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, destination=buf)
    data = buf.getvalue()
    if len(MEDIA_CACHE) > 100:      # simple cache limit
        MEDIA_CACHE.clear()
    MEDIA_CACHE[file_id] = data
    return data


def _media_input(kind: str, file_id: str, data: bytes):
    from aiogram.types import BufferedInputFile
    ext = {"photo": "jpg", "video": "mp4", "animation": "gif",
           "document": "bin", "audio": "mp3"}.get(kind, "bin")
    return BufferedInputFile(file=data, filename=f"welcome.{ext}")


async def _send_media(b: Bot, kind: str, chat_id: int, file_id: str,
                      caption: str | None, entities: list | None, kb):
    """Media bhejo — b=main bot ya custom bot (custom ke liye download+resend)."""
    if b is bot:                       # main bot: file_id direct
        media = file_id
    else:                              # custom bot: bytes dekar bhejna
        media = _media_input(kind, file_id, await _media_bytes(file_id))
    kw = dict(caption=caption or None, caption_entities=entities, reply_markup=kb)
    if kind == "photo":
        await b.send_photo(chat_id, media, **kw)
    elif kind == "video":
        await b.send_video(chat_id, media, **kw)
    elif kind == "animation":
        await b.send_animation(chat_id, media, **kw)
    elif kind == "document":
        await b.send_document(chat_id, media, **kw)
    elif kind == "audio":
        await b.send_audio(chat_id, media, **kw)


async def _retry(fn, attempts: int = 3):
    """
    Retry wrapper — Telegram flood/retry_after (429) aur transient errors
    ki wajah se users ka welcome kabhi-kabhi MISS ho jata tha. Ye fix hai.
    """
    for i in range(attempts):
        try:
            await fn()
            return True
        except TelegramRetryAfter as e:
            wait = float(getattr(e, "retry_after", 3) or 3)
            wait = min(wait, 25) + 0.5
            log.warning("Flood (retry_after=%s) — %s sec wait, attempt %d",
                        wait, int(wait), i + 1)
            await asyncio.sleep(wait)
        except (TelegramForbiddenError,) as e:
            raise e                      # user ne block kiya — retry useless
        except TelegramBadRequest as e:
            raise e                      # content error — fallback handle karega
        except Exception as e:
            log.warning("Send retry %d: %s", i + 1, e)
            await asyncio.sleep(1 + i)
    return False


async def _send_via(b: Bot, row: ChatRow, text: str, entities: list,
                    kb, user_id: int, media_kind: str, file_id: str):
    """Ek bot se poora welcome (media + text + buttons) bhejo."""
    if media_kind and file_id:
        if len(text) <= 1024:
            await _send_media(b, media_kind, user_id, file_id, text, entities, kb)
        else:      # caption too long -> media alone, text alag
            await _send_media(b, media_kind, user_id, file_id, "", None, None)
            await b.send_message(user_id, text, entities=entities,
                                 reply_markup=kb)
    else:
        await b.send_message(user_id, text, entities=entities, reply_markup=kb)




# ── Custom bot long-pollers (Channel-Help style) ────────────────────────────
# Agar custom bot us channel me ADMIN hai, to Telegram usko bhi chat_join_request
# bhejta hai — ye poller usko process karta hai (welcome bhi custom bot se jata hai).

ALLOWED_CB_UPDATES = ["chat_join_request"]


async def _req_seen(uid: int, cid: int, ttl: float = 60.0) -> bool:
    """(user, chat) recently processed? → True (skip). Warna mark karo."""
    key = (uid, cid)
    now = time.monotonic()
    prev = REQ_SEEN.get(key)
    if prev is not None and now - prev < ttl:
        return True
    REQ_SEEN[key] = now
    if len(REQ_SEEN) > 5000:
        REQ_SEEN.clear()
    return False


async def _notify_owner_join(row: ChatRow, cjr: ChatJoinRequest, name: str,
                             username: str, approved: bool):
    """Owner ko join request ki notification (approve/decline buttons)."""
    extra = " — auto-approved ✅" if approved else ""
    text = (f"🙋 <b>{esc(name)}</b>"
            f"{f' (@{esc(username)})' if username else ''} "
            f"requested to join\n<b>{esc(row.title)}</b>{extra}")
    kb = None
    if row.approve == 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve",
                                 callback_data=f"ajr:{row.chat_id}:{cjr.from_user.id}"),
            InlineKeyboardButton(text="❌ Decline",
                                 callback_data=f"djr:{row.chat_id}:{cjr.from_user.id}"),
        ]])
    if row.owner_id:
        await notify_user(row.owner_id, text, kb)
    else:
        await notify_owners(text, kb)


async def _process_join(row: ChatRow, cjr: ChatJoinRequest, b: Bot):
    """Common join-request processing (main bot + custom bot dono ke liye)."""
    user = cjr.from_user
    first = user.first_name or ""
    last = user.last_name or ""
    username = user.username or ""
    name = f"{first} {last}".strip() or "User"

    if await _req_seen(user.id, row.chat_id):
        return                                   # doosre bot ne already handle kiya

    # save user (broadcast ke liye)
    db.upsert_user(row.owner_id, user.id, first, last, username)
    db.link_user_chat(row.owner_id, user.id, row.chat_id)

    # auto-approve (jis bot ne event receive kiya wahi ADMIN hai — use karo)
    approved = False
    if row.approve == 1:
        try:
            await b.approve_chat_join_request(row.chat_id, user.id)
            approved = True
        except TelegramBadRequest as e:
            msg = str(e)
            if "already" not in msg.lower():
                await notify_user(
                    row.owner_id,
                    f"⚠️ Could not approve <b>{esc(name)}</b> in <b>{esc(row.title)}</b>.\n"
                    f"Error: <code>{esc(msg)}</code>\n"
                    f"👉 Make sure I have the <b>Invite users</b> admin right.") \
                    if row.owner_id else await notify_owners(
                        f"⚠️ Could not approve <b>{esc(name)}</b> in "
                        f"<b>{esc(row.title)}</b>: {esc(msg)}")

    ok = await send_welcome(row, first, last, username, user.id)
    if not ok:
        # welcome fail -> REQ_SEEN hatao taaki doosra bot (custom/main)
        # wapas try kar sake
        REQ_SEEN.pop((user.id, row.chat_id), None)
    await _notify_owner_join(row, cjr, name, username, approved)


async def _poll_custom_bot(token: str):
    """Ek custom bot ke updates lo (sirf chat_join_request)."""
    cb = get_custom_bot(token)
    offset = None
    while True:
        try:
            updates = await cb.get_updates(offset=offset, timeout=25,
                                           allowed_updates=ALLOWED_CB_UPDATES)
            if not updates:
                await asyncio.sleep(0.2)   # network hiccup par busy-loop nahi
                continue
            for u in updates:
                offset = u.update_id + 1
                cjr = getattr(u, "chat_join_request", None)
                if cjr is None:
                    continue
                row = db.get_chat(cjr.chat.id)
                if row is None or not row.custom_bot_token:
                    continue
                if row.custom_bot_token != token:
                    continue
                try:
                    await _process_join(row, cjr, cb)
                except Exception as e:
                    log.warning("custom-bot join processing: %s", e)
        except TelegramConflictError:
            # koi aur process isi token ko poll kar raha hai (webhook/other host)
            log.warning("Custom bot token %s... — 409 CONFLICT. Is token par "
                        "koi aur polling/webhook active hai. Is bot ko main bot "
                        "se bhi control kar sakte hain (welcome main bot se jayega).",
                        token[:12])
            await asyncio.sleep(60)
        except Exception as e:
            log.warning("Custom bot poller (%s...): %s", token[:12], e)
            await asyncio.sleep(5)


def sync_custom_pollers():
    """DB me jo custom tokens hain unke liye poller start/stop karo."""
    wanted = {}
    for row in db.get_chats():
        if row.custom_bot_token:
            wanted.setdefault(row.custom_bot_token, True)
    for tok in list(CUSTOM_TASKS):
        if tok not in wanted:
            CUSTOM_TASKS.pop(tok).cancel()
            log.info("Custom bot poller STOPPED (%s...)", tok[:12])
    for tok in wanted:
        if tok not in CUSTOM_TASKS or CUSTOM_TASKS[tok].done():
            CUSTOM_TASKS[tok] = asyncio.create_task(_poll_custom_bot(tok))
            log.info("Custom bot poller STARTED (%s...)", tok[:12])


async def send_welcome(row: ChatRow, first: str, last: str, username: str,
                       user_id: int, chat_title: str | None = None) -> bool:
    """Send the configured welcome to one user (PM). Returns success."""
    title = chat_title or row.title or "our channel"
    text, entities = render_welcome(row, first, last, username, user_id, title)
    if not text.strip():
        text, entities = f"🎉 Welcome to {title}!", []

    kb = build_kb(row.button_rows())
    media_kind, file_id = row.media_kind, row.media_file_id

    # ── dedupe: custom bot + main bot dono ko yehi join request milti hai ──
    dkey = (user_id, row.chat_id)
    now = time.monotonic()
    last = WELCOME_SENT.get(dkey)
    if last is not None and now - last < 60:
        return True                       # doosre bot ne abhi bheja — skip
    WELCOME_SENT[dkey] = now
    if len(WELCOME_SENT) > 5000:
        WELCOME_SENT.clear()

    bots_to_try = [bot]
    custom = None
    if row.custom_bot_token:
        try:
            custom = get_custom_bot(row.custom_bot_token)
            bots_to_try.insert(0, custom)     # custom bot pehle aayega
        except Exception:
            custom = None

    last_err = None
    for b in bots_to_try:
        is_custom = (b is not bot)
        try:
            ok = await _retry(lambda: _send_via(b, row, text, entities, kb,
                                                user_id, media_kind, file_id))
            if ok:
                WELCOME_SENT[dkey] = time.monotonic()
                return True
            last_err = "retries exhausted (flood?)"
        except TelegramBadRequest as e:
            last_err = e
            msg = str(e).lower()
            if "message is too long" in msg:
                await notify_owners(
                    f"⚠️ Welcome for {esc(first)} too long — shorten it "
                    f"(max {(1024 if media_kind else 4096)} chars).")
                return False
            # entity/parse galat (stale emoji id etc.) → bina entities ke resend
            try:
                ok2 = await _retry(lambda: _send_via(
                    b, row, text, None, kb, user_id, media_kind, file_id))
                if ok2:
                    WELCOME_SENT[dkey] = time.monotonic()
                    return True
            except Exception as e2:
                last_err = e2
            # custom bot fail ho to MAIN bot try karo (loop me agla b hai)
            if is_custom:
                continue
            break
        except TelegramForbiddenError:
            if is_custom:
                # custom bot user ke saath conversation start nahi kar sakta
                # (user ne custom bot ko /start nahi kiya) → main bot try karo
                last_err = "custom bot forbidden (user ne start nahi kiya)"
                continue
            return False                       # user ne MAIN bot block kiya hai
        except TelegramRetryAfter:
            last_err = "flood retries exhausted"
            if is_custom:
                continue
        except Exception as e:
            last_err = e
            if is_custom:
                continue                       # custom bot issue → main bot se bhejo
            break

    await notify_owners(
        f"⚠️ Welcome user {esc(first)} ({user_id}) ko NAHI gaya"
        f"{' (custom bot se)' if custom else ''}: {esc(str(last_err)[:120])}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  Broadcast engine
# ─────────────────────────────────────────────────────────────────────────────

def capture_broadcast_content(message: Message) -> dict | None:
    """Extract the owner's message as a broadcast job (text or media)."""
    media = None
    if message.photo:
        media = ("photo", message.photo[-1].file_id)
    elif message.video:
        media = ("video", message.video.file_id)
    elif message.animation:
        media = ("animation", message.animation.file_id)
    elif message.audio:
        media = ("audio", message.audio.file_id)
    elif message.document:
        media = ("document", message.document.file_id)
    if media:
        return {"kind": media[0], "file_id": media[1],
                "text": message.caption or "",
                "entities": message.caption_entities or None}
    if message.text:
        return {"kind": "text", "file_id": None,
                "text": message.text, "entities": message.entities or None}
    return None


async def _send_broadcast_job(job: dict, user_id: int):
    """Deliver one broadcast message to one user (same content as received)."""
    text = job.get("text") or None
    entities = job.get("entities") or None
    kind = job["kind"]
    if kind == "text":
        await bot.send_message(user_id, text, entities=entities)
    elif kind == "photo":
        await bot.send_photo(user_id, job["file_id"], caption=text,
                             caption_entities=entities)
    elif kind == "video":
        await bot.send_video(user_id, job["file_id"], caption=text,
                             caption_entities=entities)
    elif kind == "animation":
        await bot.send_animation(user_id, job["file_id"], caption=text,
                                 caption_entities=entities)
    elif kind == "document":
        await bot.send_document(user_id, job["file_id"], caption=text,
                                caption_entities=entities)
    elif kind == "audio":
        await bot.send_audio(user_id, job["file_id"], caption=text,
                             caption_entities=entities)


async def run_broadcast(job: dict, progress_msg: Message, owner_id: int):
    """Send `job` to every user saved under THIS owner, with progress + stop."""
    global BROADCAST_STOP
    BROADCAST_STOP = False
    users = db.get_users(owner_id)
    total = len(users)
    sent = failed = blocked = 0

    for i, u in enumerate(users, 1):
        if BROADCAST_STOP:
            break
        try:
            await _send_broadcast_job(job, u.user_id)
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 2)) + 1)
            try:
                await _send_broadcast_job(job, u.user_id)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            blocked += 1                       # user blocked the bot
        except Exception:
            failed += 1
        if i % 25 == 0 or i == total:
            try:
                await progress_msg.edit_text(
                    f"📣 Broadcasting… <b>{i}/{total}</b>")
            except TelegramBadRequest:
                pass
        await asyncio.sleep(0.05)              # ~20 msg/sec, safe rate

    status = ("⏹ <b>Broadcast stopped</b>" if BROADCAST_STOP
              else "✅ <b>Broadcast complete!</b>")
    await progress_msg.edit_text(
        f"{status}\n"
        f"📨 Sent: <b>{sent}</b>  |  ⚠️ Failed: <b>{failed}</b>  "
        f"|  🚫 Blocked you: <b>{blocked}</b>\n"
        f"👥 Total users: <b>{total}</b>")


# ─────────────────────────────────────────────────────────────────────────────
#  Chat panel (management screen per channel/group)
# ─────────────────────────────────────────────────────────────────────────────

def panel_text(row: ChatRow) -> str:
    media = {
        "photo": "🖼 Photo", "video": "🎥 Video", "animation": "🎞️ GIF",
        "document": "🗂️ File", "audio": "🎵 Audio",
    }.get(row.media_kind, "—")
    emoji_n = len(row.emoji_list())
    btn_lines = row.button_rows()
    btn_summary = " —" if not btn_lines else ""
    for rows in btn_lines:
        btn_summary += "\n   🔘 " + " | ".join(
            str(b.get("text", "")) for b in rows if isinstance(b, dict))
    return (
        f"<b>{esc(row.chat_type.upper())}</b> — {esc(row.title)}\n\n"
        f"📝 Welcome text:\n{truncate(row.welcome_text) or '—'}\n"
        f"✨ Premium emojis: {emoji_n}\n"
        f"🖼 Welcome media: {media}\n"
        f"🔘 Inline buttons:{btn_summary}\n"
        f"⚙️ Auto-approve: {'✅ ON' if row.approve else '⛔ OFF'}\n"
        f"🤖 Custom bot: {('✅ @' + esc(row.custom_bot_name)) if row.custom_bot_name else '❌ off'}\n"
        f"🆔 <code>{row.chat_id}</code>"
    )


def panel_kb(row: ChatRow) -> InlineKeyboardMarkup:
    cid = row.chat_id
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Welcome text", callback_data=f"wel:{cid}"),
         InlineKeyboardButton(text="🖼 Welcome media", callback_data=f"med:{cid}")],
        [InlineKeyboardButton(text="🔘 Inline buttons", callback_data=f"btn:{cid}"),
         InlineKeyboardButton(text="🧪 Preview", callback_data=f"prev:{cid}")],
        [InlineKeyboardButton(
            text="✅ Auto-approve ON" if row.approve else "⛔ Auto-approve OFF",
            callback_data=f"auto:{cid}"),
         InlineKeyboardButton(text="🗑 Remove", callback_data=f"del:{cid}")],
        [InlineKeyboardButton(text="🤖 Custom bot" + (" ✅" if row.custom_bot_token else ""),
                              callback_data=f"cbot:{cid}")],
        [InlineKeyboardButton(text="🔙 Back to chats", callback_data="chats")],
    ])


async def show_panel(message: Message, row: ChatRow):
    PANEL_MSG[row.chat_id] = message.message_id
    await message.answer(panel_text(row), reply_markup=panel_kb(row),
                         disable_web_page_preview=True)


async def edit_panel(message: Message, row: ChatRow):
    PANEL_MSG[row.chat_id] = message.message_id
    await message.edit_text(panel_text(row), reply_markup=panel_kb(row),
                            disable_web_page_preview=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Command handlers  (owner only)
# ─────────────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    my_chats = db.get_chats(uid)
    my_users = db.count_users(uid)
    text = (
        "<b>👋 Welcome!</b>\n\n"
        "Ye <b>multi-user</b> bot hai — aap apne <b>private channels/groups</b> "
        "ke join requests manage karo, aur sirf <b>aapka</b> data aapko dikhta hai.\n\n"
        f"📊 Aapke chats: <b>{len(my_chats)}</b>  |  👥 Saved users: <b>{my_users}</b>\n\n"
        "Kaise use karein:\n"
        "1️⃣ Mujhe apne channel/group me <b>admin</b> banao\n"
        "2️⃣ <b>➕ Add channel / group</b> dabao → us chat ki koi message forward karo\n"
        "3️⃣ Welcome text (+ premium emojis ✨), photo/video aur buttons set karo\n\n"
        "✅ Join request aane par:\n"
        "   • main <b>BINA approve kiye</b> user ko PM me welcome karta hoon\n"
        "   • uska ID <b>aapke account me save</b> hota hai (broadcast ke liye)\n"
        "   • aapko Approve/Decline buttons milte hain\n\n"
        "📣 <b>Broadcast:</b> sirf <b>aapke</b> saved users ko message — "
        "menu me Broadcast dabao."
    )
    await message.answer(text, reply_markup=menu_kb(message.from_user.id))


@router.message(Command("add"))
async def cmd_add(message: Message):
    STASH[message.chat.id] = ("add", None)
    await message.answer(
        "<b>➕ Add a channel / group</b>\n\n"
        "Now do <b>one</b> of these:\n"
        "• <b>Forward</b> me any message <i>from</i> that channel/group\n"
        "• Send its <b>@username</b> or <b>t.me link</b>\n"
        "• Send its <b>chat id</b> (e.g. <code>-1001234567890</code>)\n\n"
        "📌 Make sure I am <b>admin</b> there first "
        "(with <i>Invite users</i> permission). Tap /cancel to abort.\n\n"
        "⚠️ Agar ye chat kisi aur user ne pehle se add ki hai, to wo usi ki "
        "rahegi — aapko nahi milegi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]])
    )


@router.message(Command("chats"))
async def cmd_chats(message: Message):
    await send_chats_list(message, message.from_user.id)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(help_text(), reply_markup=menu_kb(message.from_user.id),
                         disable_web_page_preview=True)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if STASH.pop(message.chat.id, None):
        await message.answer("🗑 Cancelled.")
    else:
        await message.answer("Nothing to cancel.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    uid = message.from_user.id
    total = db.count_users(uid)
    STASH[message.chat.id] = ("bcast", None)
    await message.answer(
        f"📣 <b>Broadcast</b>\n\n"
        f"👥 Aapke saved users: <b>{total}</b>\n\n"
        "Ab jo bhi <b>message</b> aap mujhe bhejoge (text, photo, video, GIF, "
        "file ya audio) — wahi <b>sabhi users ko</b> jayega.\n\n"
        "⚠️ Ek baar me ek broadcast. /cancel se cancel karo.\n"
        "🚫 Broadcast chalu hone ke baad /stopbroadcast se rok sakte ho.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]]))


@router.message(Command("users"))
async def cmd_users(message: Message):
    await send_users_list(message, message.from_user.id)


# ── 👑 ADMIN MANAGEMENT (sirf SUPER-ADMINS) ──────────────────────────────
async def _resolve_user(message: Message):
    """Reply wala user / id / @username — return (user_id, name) ya None."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, f"{u.first_name} {u.last_name or ''}".strip() or "User"
    parts = (message.text or "").split()
    if len(parts) >= 2:
        arg = parts[1].strip()
        if arg.lstrip("-").isdigit():
            return int(arg), arg
        handle = arg.lstrip("@")
        if handle:
            try:
                u = await bot.get_chat(handle)
                return u.id, getattr(u, "title", None) or getattr(u, "first_name", None) or handle
            except Exception:
                return None, handle
    return None, ""


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    if not is_super_admin(message.from_user.id):
        await message.answer("⛔ Sirf super-admin hi admin bana sakta hai.")
        return
    uid, name = await _resolve_user(message)
    if not uid:
        await message.answer(
            "❌ User nahi mila. Tarike:\n"
            "• /addadmin <user_id>   ya   /addadmin @username\n"
            "• Ya kisi user ke message ka REPLY karke /addadmin")
        return
    db.add_admin(uid, message.from_user.id)
    await message.answer(f"✅ <b>{esc(name)}</b> (<code>{uid}</code>) ab bot ka "
                         f"<b>ADMIN</b> hai — wo bot ko use kar sakta hai.")


@router.message(Command("rmadmin"))
async def cmd_rmadmin(message: Message):
    if not is_super_admin(message.from_user.id):
        await message.answer("⛔ Sirf super-admin hi admin hata sakta hai.")
        return
    uid, name = await _resolve_user(message)
    if not uid:
        await message.answer("❌ User nahi mila. /rmadmin <user_id> ya reply karke.")
        return
    if is_super_admin(uid):
        await message.answer(f"⚠️ <code>{uid}</code> to super-admin hai — nahi hata sakte.")
        return
    if db.is_admin(uid):
        db.remove_admin(uid)
        await message.answer(f"🗑 <b>{esc(name)}</b> (<code>{uid}</code>) admin se hata diya "
                             f"— ab wo bot use nahi kar payega.")
    else:
        await message.answer(f"ℹ️ <code>{uid}</code> admin list me tha hi nahi.")


@router.message(Command("admins"))
async def cmd_admins(message: Message):
    if not is_super_admin(message.from_user.id):
        await message.answer("⛔ Sirf super-admin hi admins dekh sakta hai.")
        return
    await send_admins_list(message)


async def send_admins_list(reply_to: Message, edit: bool = False):
    text = "<b>👑 Bot Admins</b>\n\n"
    text += "• <b>SUPER-ADMIN</b> (hamesha allowed):\n"
    for uid in ADMIN_IDS:
        text += f"   └ <code>{uid}</code>\n"
    admins = db.get_admins()
    text += f"\n• <b>Admins added by you</b> ({len(admins)}):\n"
    if not admins:
        text += "   └ (koi nahi — /addadmin se banao)\n"
    for a in admins:
        text += f"   └ <code>{a.get('user_id')}</code>  (by {esc(a.get('added_by', ''))})\n"
    text += "\n➕ <code>/addadmin</code>  🗑 <code>/rmadmin</code>  (reply karke ya id se)"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Main menu", callback_data="home")]])
    if edit:
        try:
            await reply_to.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest:
            await reply_to.answer(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await reply_to.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("backup"))
async def cmd_backup(message: Message):
    """Sirf super-admins: abhi turant backup banao (data bhulne ka khatra nahi)."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Ye command sirf super-admin ke liye hai.")
        return
    path, created = auto_backup(force=True)
    if not path:
        await message.answer("❌ Backup nahi ban paya (disks check karo).")
        return
    kb = 0
    try:
        kb = os.path.getsize(path) // 1024
    except OSError:
        pass
    await message.answer(
        f"💾 <b>Backup ban gaya!</b>\n\n"
        f"📁 <code>{path}</code>\n"
        f"📦 Size: ~{kb} KB\n"
        f"🔄 Aur backup 24h me khud banega (bot start pe bhi).\n\n"
        f"♻️ Restore: <code>python main.py --restore {path}</code>\n\n"
        f"☁️ Mongo Atlas use kar rahe ho to data cloud me already safe hai.")


@router.message(Command("stopbroadcast"))
async def cmd_stopbroadcast(message: Message):
    global BROADCAST_STOP
    if BROADCAST_STOP:
        await message.answer("Broadcast already stopped.")
        return
    BROADCAST_STOP = True
    await message.answer("⏹ Broadcast roka ja raha hai… (thoda wait karo)")


def users_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton(text="🔙 Main menu", callback_data="home")],
    ])


async def send_users_list(reply_to: Message, uid: int):
    users = db.get_users(uid)
    total = len(users)
    text = f"📊 <b>Aapke saved users:</b> {total}\n\n"
    if not users:
        text += ("Abhi koi user nahi. Jab users aapke channels/groups me join "
                 "request bhejenge, unka data yahan save hoga.")
    for u in users[:10]:
        handle = f" <code>@{u.username}</code>" if u.username else ""
        text += f"• {esc(u.display_name())}{handle} — <code>{u.user_id}</code>\n"
    if total > 10:
        text += f"… aur {total - 10} aur users"
    await reply_to.answer(text, reply_markup=users_kb())


async def send_chats_list(reply_to: Message, uid: int):
    rows = db.get_chats(uid)
    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add channel / group", callback_data="add")]])
        await reply_to.answer("📋 <b>Aapke chats:</b>\n\nAbhi koi channel/group "
                              "add nahi hua.", reply_markup=kb)
        return
    kb_rows = []
    for r in rows:
        kb_rows.append([InlineKeyboardButton(
            text=f"{'📢' if r.chat_type == 'channel' else '👥'} {truncate(r.title, 30)}",
            callback_data=f"chat:{r.chat_id}")])
    kb_rows.append([InlineKeyboardButton(text="➕ Add channel / group",
                                         callback_data="add")])
    await reply_to.answer(
        f"📋 <b>Aapke chats:</b> ({len(rows)})\n\n"
        "Kisi chat ko tap karke uska welcome set karo.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


def help_text() -> str:
    return (
        "<b>❓ How to use</b> (multi-user — aapki chats/users sirf aapko dikhte hain)\n\n"
        "<b>1. Private channel</b>\n"
        "• Channel settings → <i>Channel type</i> → <b>Private</b>\n"
        "• Channel settings → <i>Join requests</i> → turn <b>ON</b>\n"
        "• Administrators → add this bot → grant "
        "<b>Invite users (Add subscribers)</b> ✅\n\n"
        "<b>2. Group</b>\n"
        "• Administrators → add this bot → grant <b>Invite users</b> ✅\n"
        "• Invite links: create one with "
        "<i>“Request admin approval to join”</i> ON\n\n"
        "<b>3. Tell me the chat</b>\n"
        "Use /add and forward me a message from it — done.\nChat usi user ke naam ho jati hai jo add karega.\n\n"
        "<b>Welcome text placeholders:</b>\n"
        "<code>{name} {first} {last} {username} {chat} {id} {date}</code>\n\n"
        "<b>Formatting tags:</b> <b>bold</b> <i>italic</i> <u>underline</u> "
        "<s>strike</s> <code>code</code> <tg-spoiler>spoiler</tg-spoiler>\n\n"
        "✨ <b>Premium emojis:</b> just paste them — they are sent as real "
        "premium custom emojis.\n\n"
        "When someone sends a join request I:\n"
        "💬 <b>BINA approve kiye</b> welcome message bhejta hoon (PM me)\n"
        "💾 unka <b>user ID save</b> karta hoon\n"
        "📨 aapko <b>Approve / Decline</b> buttons ke saath notify karta hoon\n\n"
        "<b>📣 Broadcast</b>\n"
        "• /broadcast — sabhi saved users ko message bhejo\n"
        "  (text, photo, video, GIF, file ya audio — sab chalta hai)\n"
        "• /users — saved users ki list\n"
        "• /stopbroadcast — chalu broadcast rokna\n\n"
        "💡 Agar kisi chat ke liye <b>auto-approve</b> wapas chahiye to us chat "
        "ke panel me <b>Auto-approve ON</b> dabao.\n\n"
        "<b>🤖 Custom bot (per-chat)</b>\n"
        "• Chat panel me <b>Custom bot</b> button → @BotFather ka naya bot token "
        "bhejo\n"
        "• Welcome us custom bot se jayega (Channel Help style)\n"
        "• Best: custom bot ko channel me bhi <b>admin</b> banao + <b>Invite users</b> "
        "right do\n\n"
        "👑 <b>Admin access</b>\n"
        "• Bot sirf <b>authorized</b> users use kar sakte hain\n"
        "• Super-admin naye admins bana sakta hai: <code>/addadmin</code> "
        "(reply karke ya id se), hatane ke liye <code>/rmadmin</code>\n"
        "• Chat owners hamesha allowed hain (unke approve buttons chahiye)"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Generic message flow (adding chats / editing welcome content)
# ─────────────────────────────────────────────────────────────────────────────

@router.message(F.chat.type == ChatType.PRIVATE)
async def on_private_message(message: Message):
    # commands are handled by their own handlers — never treat them as content
    if (message.text or "").startswith("/"):
        return
    state = STASH.pop(message.chat.id, None)
    if state is None:
        return  # ordinary message, ignore

    flow, payload = state

    # ── waiting for chat input ──────────────────────────────────────────────
    if flow == "add":
        await handle_chat_input(message)
        return

    # ── waiting for welcome text ────────────────────────────────────────────
    if flow == "wel":
        chat_id = int(payload)
        text = message.text or message.caption or ""
        if not text.strip():
            await message.answer(
                "📝 Please send me the welcome <b>text</b> (not a sticker/file).")
            STASH[message.chat.id] = state
            return
        marker_text, emoji_list = extract_custom_emojis(text, message.entities)
        db.update_chat(chat_id, welcome_text=marker_text,
                       emoji=json.dumps(emoji_list))
        row = db.get_chat(chat_id)
        extra = f" with {len(emoji_list)} premium emoji✨" if emoji_list else ""
        await message.answer(f"✅ Welcome text saved for <b>{esc(row.title)}</b>{extra}.")
        await edit_panel(message, row)
        return

    # ── waiting for media ───────────────────────────────────────────────────
    if flow == "med":
        chat_id = int(payload)
        kind = None
        if message.photo:
            kind = ("photo", message.photo[-1].file_id)
        elif message.video:
            kind = ("video", message.video.file_id)
        elif message.animation:
            kind = ("animation", message.animation.file_id)
        elif message.audio:
            kind = ("audio", message.audio.file_id)
        elif message.document:
            kind = ("document", message.document.file_id)
        if kind is None:
            await message.answer(
                "🖼 Please send a <b>photo / video / GIF / audio / file</b> "
                "as media for the welcome. (Send /cancel to abort.)")
            STASH[message.chat.id] = state
            return
        db.update_chat(chat_id, media_file_id=kind[1], media_kind=kind[0])
        row = db.get_chat(chat_id)
        await message.answer(f"✅ Welcome media saved ({kind[0]}).")
        await edit_panel(message, row)
        return

    # ── waiting for buttons ─────────────────────────────────────────────────
    if flow == "btn":
        chat_id = int(payload)
        raw = message.text or ""
        try:
            rows = parse_buttons(raw)
        except ValueError as e:
            await message.answer(f"❌ {e}\n\nSend again:")
            STASH[message.chat.id] = state
            return
        db.update_chat(chat_id, buttons=json.dumps(rows))
        row = db.get_chat(chat_id)
        await message.answer(f"✅ Saved {sum(len(r) for r in rows)} button(s).")
        await edit_panel(message, row)
        return

    # ── waiting for custom-bot token ────────────────────────────────────────
    if flow == "cbt":
        chat_id = int(payload)
        raw = (message.text or "").strip()
        # user ke message me kahin token ho to nikaal lo
        m = re.search(r"[0-9]{6,12}:[A-Za-z0-9_\-]{30,}", raw)
        token = m.group(0) if m else raw
        if not re.fullmatch(r"[0-9]{6,12}:[A-Za-z0-9_\-]{30,}", token):
            await message.answer(
                "❌ Ye sahi token nahi lag raha.\n\n"
                "@BotFather → /newbot → token copy karke bhejo.\n"
                "Format: <code>1234567890:AAH.....</code>\n"
                "(sirf /cancel se band kar sakte ho)")
            STASH[message.chat.id] = state
            return
        if token == BOT_TOKEN:
            await message.answer(
                "⚠️ Ye to main bot ka hi token hai! Custom bot ke liye "
                "@BotFather se <b>naya bot</b> banao ({username}).")
            STASH[message.chat.id] = state
            return
        # validate + get name
        try:
            tb = get_custom_bot(token)
            me = await tb.get_me()
        except Exception as e:
            await message.answer(
                f"❌ Token <b>invalid</b> hai: {esc(str(e)[:120])}\n\n"
                "• Copy-paste me koi space/quote nahi hona chahiye\n"
                "• @BotFather se fresh token lo\n"
                "• Dobara bhej do")
            STASH[message.chat.id] = state
            return
        db.update_chat(chat_id, custom_bot_token=token,
                       custom_bot_name=me.username or me.first_name or "")
        row = db.get_chat(chat_id)
        sym = me.username or ""
        sync_custom_pollers()
        await message.answer(
            f"✅ Custom bot <b>@{esc(sym)}</b> set ho gaya for "
            f"<b>{esc(row.title)}</b>!\n\n"
            "💡 Best result ke liye custom bot ko channel me <b>admin</b> banao "
            "(<b>Invite users</b> right do) — phir welcome 100% usi se jayega.")
        await edit_panel(message, row)
        return

    # ── waiting for broadcast content ───────────────────────────────────────
    if flow == "bcast":
        job = capture_broadcast_content(message)
        if job is None:
            await message.answer(
                "❌ Sirf <b>text / photo / video / GIF / file / audio</b> "
                "bhejo. /cancel se cancel karo.")
            STASH[message.chat.id] = state
            return
        BCAST_JOBS[message.from_user.id] = job
        total = db.count_users(message.from_user.id)
        preview = (job["text"][:80] or "🖼 [media]").replace("\n", " ")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 Broadcast to {total} users",
                                  callback_data="bcast:go")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="bcast:cancel")],
        ])
        await message.answer(
            f"📣 Ye message <b>{total}</b> users ko bhejna hai.\n\n"
            f"<b>Preview:</b> {esc(preview)}\n\n"
            "Start karein?",
            reply_markup=kb)
        return


# ─────────────────────────────────────────────────────────────────────────────
#  Add-chat flow
# ─────────────────────────────────────────────────────────────────────────────

async def handle_chat_input(message: Message):
    chat = None

    # a) forwarded message from the chat
    if message.forward_from_chat is not None:
        chat = message.forward_from_chat
    elif message.forward_origin is not None and hasattr(message.forward_origin, "chat"):
        chat = message.forward_origin.chat

    # b) @username / t.me link / chat id in text
    if chat is None and message.text:
        text = message.text.strip()
        if text.startswith("@") or "t.me/" in text or "/+" in text:
            handle = re.sub(r"^.*t\.me/", "", text).split("?")[0].split("/")[0]
            handle = handle.lstrip("@")
            if handle:
                try:
                    chat = await bot.get_chat(handle)
                except TelegramBadRequest:
                    pass
        elif text.lstrip("-").isdigit():
            try:
                chat = await bot.get_chat(int(text))
            except TelegramBadRequest:
                pass

    if chat is None:
        await message.answer(
            "❌ Could not find that chat.\n\n"
            "Try again — or better: <b>forward me any message from the "
            "channel/group</b>. Tap /cancel to abort.")
        STASH[message.chat.id] = ("add", None)
        return

    chat_id = chat.id
    if chat.type not in ("channel", "supergroup", "group"):
        await message.answer("❌ That's not a channel or group.")
        return

    uid = message.from_user.id
    row = db.get_chat(chat_id)
    if row is None:
        # naya chat → is user ke naam register
        row = db.add_chat(chat_id, chat.type, chat.title or "Unknown", owner_id=uid)
    elif row.owner_id == 0:
        # pehle unclaimed tha → ab is user ka
        db.claim_chat(chat_id, uid)
        row = db.get_chat(chat_id)
        await message.answer(f"✅ <b>{esc(row.title)}</b> ab aapke naam ho gaya.")
    elif row.owner_id != uid:
        # kisi aur ka chat — data isolation
        await message.answer(
            f"⛔ <b>{esc(row.title)}</b> pehle se kisi aur user ne add kiya hai.\n"
            "Multi-user bot me ek chat ek hi user ke pass ho sakti hai — "
            "aap ise change nahi kar sakte.")
        STASH.pop(message.chat.id, None)
        return

    member = None
    try:
        member = await bot.get_chat_member(chat_id, ME_ID)
    except TelegramBadRequest:
        pass

    if member is None or getattr(member, "status", "") not in ("administrator", "creator"):
        await message.answer(
            f"⚠️ I saved <b>{esc(row.title)}</b> but I am <b>not an admin</b> there yet!\n\n"
            "1. Open that chat → <b>Administrators → Add admin →</b> select me\n"
            "2. Grant permission: <b>Invite users</b> "
            "(for channels: <i>Add subscribers</i>) and <i>Change info</i>\n"
            "3. Then tap <b>🧪 Preview</b> or just wait for a join request.\n\n"
            "ℹ️ I can't auto-approve requests until I have that permission.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Back", callback_data=f"chat:{chat_id}")]]))
        return

    can_invite = getattr(member, "can_invite_users", True)
    if can_invite is False:
        await message.answer(
            f"⚠️ {esc(row.title)}: I'm admin but <b>Invite users</b> permission is OFF.\n"
            "Please enable it, otherwise approval will fail for every request.")

    await show_panel(message, row)


# ─────────────────────────────────────────────────────────────────────────────
#  Join request — the core feature
# ─────────────────────────────────────────────────────────────────────────────

@router.chat_join_request()
async def on_join_request(update: ChatJoinRequest):
    user = update.from_user

    # Bot ko join request sirf wahan se milti hai jahan wo ADMIN hai.
    row = db.get_chat(update.chat.id)
    if row is None:
        # chat abhi bot me add nahi hui — unclaimed ke roop me auto-add.
        try:
            title = update.chat.title or "Unknown"
        except Exception:
            title = "Unknown"
        row = db.add_chat(update.chat.id, update.chat.type, title, owner_id=0)
        await notify_owners(
            f"🆕 <b>{esc(title)}</b> me join request aayi (main wahan admin hoon) "
            f"par chat abhi kisi user ne claim nahi ki.\n"
            "Jo user ise /add se add karega, iske requests usi ko milenge.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⚙️ Add / Configure",
                                     callback_data=f"chat:{update.chat.id}")]]))

    owner_id = row.owner_id
    first = user.first_name or ""
    last = user.last_name or ""
    username = user.username or ""
    name = f"{first} {last}".strip() or "User"

    # 1) SAVE the user under the CHAT OWNER (broadcast is per-owner)
    db.upsert_user(owner_id, user.id, first, last, username)
    db.link_user_chat(owner_id, user.id, update.chat.id)

    # 2/3) dedupe + welcome + owner-notification (custom bot poller ke saath shared)
    #      (_process_join apne andar _req_seen check karta hai)
    await _process_join(row, update, bot)


# ─────────────────────────────────────────────────────────────────────────────
#  Bot added / removed to a chat
# ─────────────────────────────────────────────────────────────────────────────

@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added(update: ChatMemberUpdated):
    chat = update.chat
    if chat.type not in ("channel", "supergroup", "group"):
        return
    title = chat.title or "Unknown"
    # Jisne bot add kiya wahi chat ka owner (multi-user)
    # ⚠️ ADMIN GATE: unauthorized user chat CLAIM nahi kar sakta
    adder = update.from_user
    owner_id = 0
    if adder is not None and is_authorized(adder.id):
        owner_id = adder.id
    elif adder is not None and not is_authorized(adder.id):
        try:
            await bot.send_message(adder.id, AUTH_MSG)
        except Exception:
            pass
    row = db.get_chat(chat.id)
    if row is None:
        row = db.add_chat(chat.id, chat.type, title, owner_id=owner_id)
    elif row.owner_id == 0 and owner_id:
        db.claim_chat(chat.id, owner_id)
        row = db.get_chat(chat.id)
    if owner_id:
        await notify_user(
            owner_id,
            f"🎉 I was added to <b>{esc(title)}</b> "
            f"({'📢 channel' if chat.type == 'channel' else '👥 group'}).\n"
            f"Ye chat ab <b>aapke naam</b> hai — welcome message configure karo:",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⚙️ Configure",
                                     callback_data=f"chat:{chat.id}")]]))
    else:
        await notify_owners(
            f"🎉 I was added to <b>{esc(title)}</b> but I can't tell who added "
            f"me. Koi bhi user /add se claim kar sakta hai.")


@router.my_chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def bot_removed(update: ChatMemberUpdated):
    chat = update.chat
    row = db.get_chat(chat.id)
    if row is not None:
        db.remove_chat(chat.id)
        if row.owner_id:
            await notify_user(
                row.owner_id,
                f"🗑 I was removed from <b>{esc(chat.title or 'a chat')}</b> — "
                f"us chat ke saare settings delete ho gaye.")


# ─────────────────────────────────────────────────────────────────────────────
#  Callback queries
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query()
async def on_callback(query: CallbackQuery):
    # MULTI-USER: koi bhi use kar sakta hai — har callback apne data me verifiy hota hai
    uid = query.from_user.id
    data = query.data or ""
    msg = query.message

    def owned(chat_id: int):
        """Chat row + ownership check; None + alert agar aapka nahi hai."""
        row = db.get_chat(chat_id)
        if row is None:
            return None
        if row.owner_id and row.owner_id != uid:
            return None
        if row.owner_id == 0:
            db.claim_chat(chat_id, uid)   # unclaimed -> is user ka
            row = db.get_chat(chat_id)
        return row

    # ── basics ──────────────────────────────────────────────────────────────
    if data == "cancel":
        STASH.pop(query.from_user.id, None)
        await query.answer("Cancelled")
        await msg.edit_text("🗑 Cancelled.")
        return

    if data == "home":
        await query.answer()
        await msg.edit_text("<b>👋 Main menu</b>", reply_markup=menu_kb(uid))
        return

    if data == "add":
        await query.answer()
        STASH[query.from_user.id] = ("add", None)
        await msg.edit_text(
            "<b>➕ Add a channel / group</b>\n\n"
            "Now do <b>one</b> of these:\n"
            "• <b>Forward</b> me any message <i>from</i> that channel/group\n"
            "• Send its <b>@username</b> / <b>t.me link</b>\n"
            "• Send its <b>chat id</b>\n\n"
            "📌 I must be <b>admin</b> there (with <i>Invite users</i> permission).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]]))
        return

    if data == "chats":
        await query.answer()
        await send_chats_list(msg, uid)
        return

    if data == "users":
        await query.answer()
        await send_users_list(msg, uid)
        return

    if data == "admins":
        if not is_super_admin(uid):
            await query.answer("⛔ Sirf super-admin.", show_alert=True)
            return
        await query.answer()
        await send_admins_list(msg, edit=True)
        return

    if data == "broadcast":
        await query.answer()
        total = db.count_users(uid)
        STASH[query.from_user.id] = ("bcast", None)
        await msg.edit_text(
            f"📣 <b>Broadcast</b>\n\n"
            f"👥 Saved users: <b>{total}</b>\n\n"
            "Ab jo bhi <b>message</b> bhejoge (text, photo, video, GIF, "
            "file ya audio) — wahi <b>sabhi users ko</b> jayega.\n\n"
            "🚫 Chalu hone ke baad /stopbroadcast se rok sakte ho.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]]))
        return

    if data == "bcast:cancel":
        BCAST_JOBS.pop(uid, None)
        STASH.pop(query.from_user.id, None)
        await query.answer("Broadcast cancelled")
        await msg.edit_text("🗑 Broadcast cancelled.",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="🔙 Main menu",
                                                     callback_data="home")]]))
        return

    if data == "bcast:go":
        job = BCAST_JOBS.get(uid)
        if not job:
            await query.answer("Kuch broadcast queue me nahi hai.", show_alert=True)
            return
        await query.answer("🚀 Broadcast shuru!")
        STASH.pop(query.from_user.id, None)
        await run_broadcast(job, msg, uid)
        return

    if data == "help":
        await query.answer()
        await msg.edit_text(help_text(), disable_web_page_preview=True,
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="🔙 Back",
                                                     callback_data="home")]]))
        return

    # ── approve / decline a pending join request (from owner log) ───────────
    if data.startswith(("ajr:", "djr:")):
        action, cid, uid = data.split(":", 2)
        cid, uid = int(cid), int(uid)
        _row = db.get_chat(cid)
        if _row is not None and _row.owner_id and _row.owner_id != uid:
            await query.answer("⛔ Ye chat aapki nahi hai.", show_alert=True)
            return
        # custom bot set hai to wahan se approve/decline karo (wo admin hai; main
        # bot nahi ho sakta agar sirf custom bot channel me hai)
        which = _row if _row is not None else None
        b = bot
        if which is not None and which.custom_bot_token:
            try:
                b = get_custom_bot(which.custom_bot_token)
            except Exception:
                b = bot
        try:
            if action == "ajr":
                await b.approve_chat_join_request(cid, uid)
                text = "✅ Approved — user can now join."
            else:
                await b.decline_chat_join_request(cid, uid)
                text = "❌ Declined."
        except TelegramBadRequest as e:
            text = f"⚠️ {e}"
        await query.answer(text, show_alert=True)
        try:
            await msg.edit_text((msg.html_text or "") + f"\n\n{text}")
        except TelegramBadRequest:
            pass
        return

    # ── chat panel ──────────────────────────────────────────────────────────
    if data.startswith("chat:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.", show_alert=True)
            return
        await query.answer()
        await edit_panel(msg, row)
        return

    # ── custom bot (per-chat) ───────────────────────────────────────────────
    if data.startswith("cbot:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.", show_alert=True)
            return
        if row.custom_bot_token:
            STASH[query.from_user.id] = ("cbt", str(row.chat_id))
            await query.answer()
            await msg.edit_text(
                f"🤖 <b>Custom bot</b> — {esc(row.title)}\n\n"
                f"✅ Abhi set hai: <b>@{esc(row.custom_bot_name)}</b>\n\n"
                "Is chat ki join requests par welcome <b>custom bot se</b> jayega.\n"
                "• Naya token set karne ke liye bhej do (saath me old replace ho jayega)\n"
                "• Token format: <code>1234567890:AAH....</code>\n"
                "• Custom bot ko channel me <b>admin</b> banao + <b>Invite users</b> right do — "
                "tab wo khud requests receive karega (best).\n"
                "• Nahi bhi banao to bhi welcome custom bot se jayega (jab tak wo user "
                "ko start kar sakta hai) — nahi to main bot bhej dega.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🗑 Remove custom bot",
                                          callback_data=f"cbrm:{row.chat_id}")],
                    [InlineKeyboardButton(text="🔙 Back", callback_data=f"chat:{row.chat_id}")]]))
            return
        STASH[query.from_user.id] = ("cbt", str(row.chat_id))
        await query.answer()
        await msg.edit_text(
            f"🤖 <b>Custom bot</b> — {esc(row.title)}\n\n"
            "Apna custom bot lagana hai to @BotFather se bot banao aur uska "
            "<b>token</b> yahan bhej do.\n\n"
            "<b>Kyon?</b> Is chat ke join-request users ko welcome <b>custom bot se</b> "
            "jayega (Channel Help style) — aapka apna brand.\n\n"
            "• Token format: <code>1234567890:AAH....</code>\n"
            "• Token aapke alawa kisi ko nahi dikhta (sirf is chat ke saath save hota hai)\n"
            "• Cancel: /cancel",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Back", callback_data=f"chat:{row.chat_id}")]]))
        return

    if data.startswith("cbrm:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai.", show_alert=True)
            return
        db.update_chat(row.chat_id, custom_bot_token="", custom_bot_name="")
        sync_custom_pollers()
        await query.answer("Custom bot hataya gaya.")
        await edit_panel(msg, db.get_chat(row.chat_id))
        return

    if data.startswith("auto:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.", show_alert=True)
            return
        db.update_chat(row.chat_id, approve=0 if row.approve else 1)
        row = db.get_chat(row.chat_id)
        await query.answer(f"Auto-approve is now {'ON' if row.approve else 'OFF'}")
        await edit_panel(msg, row)
        return

    if data.startswith("del:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.", show_alert=True)
            return
        await query.answer()
        await msg.edit_text(
            f"🗑 Remove <b>{esc(row.title)}</b> from my list?\n\n"
            "(⚠️ Main wahan ab bhi admin hoon, isliye us chat ki koi nayi join "
            "request aayi to wo wapas auto-add ho jayega. Full remove ke liye "
            "mujhe us chat ke admins se nikaal do.)",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Yes, remove",
                                      callback_data=f"delok:{row.chat_id}")],
                [InlineKeyboardButton(text="🔙 Back",
                                      callback_data=f"chat:{row.chat_id}")],
            ]))
        return

    if data.startswith("delok:"):
        chat_id = int(data.split(":", 1)[1])
        row = owned(chat_id)          # ownership check (sirf owner delete kar sakta hai)
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai.", show_alert=True)
            return
        db.remove_chat(chat_id)
        await query.answer("Removed")
        await msg.edit_text(
            f"🗑 Removed <b>{esc(row.title if row else 'chat')}</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 My chats", callback_data="chats")]]))
        return

    # ── welcome editors ─────────────────────────────────────────────────────
    if data.startswith("wel:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.", show_alert=True)
            return
        await query.answer()
        STASH[query.from_user.id] = ("wel", str(row.chat_id))
        await msg.edit_text(
            f"✏️ <b>Welcome text</b> for {esc(row.title)}\n\n"
            "Send me the new <b>text</b> now (message or media caption).\n\n"
            "<b>Placeholders</b> (auto-filled per user):\n"
            "<code>{name} {first} {last} {username} {chat} {id} {date}</code>\n\n"
            "<b>Formatting:</b> <b>bold</b> <i>italic</i> <u>underline</u> "
            "<s>strike</s> <code>code</code> <tg-spoiler>spoiler</tg-spoiler>\n"
            "e.g. <code>&lt;b&gt;Welcome {name}!&lt;/b&gt;</code> 🎉\n\n"
            "✨ <b>Premium emojis:</b> paste them like normal — they will be "
            "sent as real premium emojis to every user!",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Cancel",
                                     callback_data=f"chat:{row.chat_id}")]]))
        return

    if data.startswith("med:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.", show_alert=True)
            return
        await query.answer()
        STASH[query.from_user.id] = ("med", str(row.chat_id))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Photo", callback_data="mdis:photo"),
             InlineKeyboardButton(text="🎥 Video", callback_data="mdis:video"),
             InlineKeyboardButton(text="🎞️ GIF", callback_data="mdis:animation")],
            [InlineKeyboardButton(text="🗂️ File", callback_data="mdis:document"),
             InlineKeyboardButton(text="🎵 Audio", callback_data="mdis:audio"),
             InlineKeyboardButton(text="❌ Remove media", callback_data="mdis:clear")],
            [InlineKeyboardButton(text="🔙 Back", callback_data=f"chat:{row.chat_id}")],
        ])
        await msg.edit_text(
            f"🖼 <b>Welcome media</b> for {esc(row.title)}\n\n"
            "Choose a type, then simply <b>send me</b> the photo / video / "
            "GIF / audio / file.\n\n"
            "The user receives: <b>media</b> + your <b>text as caption</b> + "
            "inline buttons.",
            reply_markup=kb)
        return

    if data.startswith("mdis:"):
        _, what = data.split(":", 1)
        state = STASH.get(query.from_user.id)
        if not state or state[0] != "med":
            await query.answer("Session expired — open the media editor again.")
            return
        chat_id = int(state[1])
        row = owned(chat_id)
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai.", show_alert=True)
            return
        if what == "clear":
            db.update_chat(chat_id, media_file_id="", media_kind="")
            await query.answer("Media removed")
            await edit_panel(msg, row)
            return
        await query.answer(f"OK — now send me the {what}")
        return

    if data.startswith("btn:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.", show_alert=True)
            return
        await query.answer()
        STASH[query.from_user.id] = ("btn", str(row.chat_id))
        await msg.edit_text(
            f"🔘 <b>Inline buttons</b> for {esc(row.title)}\n\n"
            "Send one button per line:\n"
            "<code>Button text | https://link</code>\n\n"
            "Leave a <b>blank line</b> between rows.\n\n"
            "Example:\n"
            "<code>🚀 Join channel | https://t.me/MyChannel</code>\n"
            "<code>👥 Join group | https://t.me/MyGroup</code>\n\n"
            "Reply <code>/cancel</code> to abort.",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Clear all buttons",
                                     callback_data=f"btnclear:{row.chat_id}"),
                InlineKeyboardButton(text="🔙 Back",
                                     callback_data=f"chat:{row.chat_id}")]]))
        return

    if data.startswith("btnclear:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.", show_alert=True)
            return
        db.update_chat(row.chat_id, buttons="[]")
        await query.answer("Buttons cleared")
        await edit_panel(msg, row)
        return

    if data.startswith("prev:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.", show_alert=True)
            return
        owner = query.from_user
        await query.answer("Sending preview…")
        await bot.send_message(
            query.from_user.id,
            "🧪 <b>Welcome preview</b> — this is exactly what a user will receive:\n"
            "--------------------------------")
        await send_welcome(row, owner.first_name or "User", owner.last_name or "",
                           owner.username or "", owner.id, row.title)
        return

    await query.answer()


# ─────────────────────────────────────────────────────────────────────────────
#  Startup
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    global ME_ID, BOT_USERNAME

    # ── 0) TOKEN CHECK ──
    # Agar pehle isi token pe kisi webhook/host ka bot chala tha, to polling
    # kabhi update nahi leti — bot "respond nahi karta". Webhook hatao:
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("Webhook check OK (conflict nahi)")
    except Exception as e:
        log.warning("delete_webhook: %s", e)

    # ── 1) MONGODB ──
    try:
        db.connect(legacy_owner=ADMIN_IDS[0] if ADMIN_IDS else 0)
    except SystemExit:
        raise

    # ── 2) TOKEN VALID? (getMe) ──
    try:
        me = await bot.get_me()
    except Exception as e:
        raise SystemExit(
            "❌❌ BOT_TOKEN galat lag raha hai! ❌❌\n\n"
            f"   Telegram error: {e}\n\n"
            "   • Token me extra space/quote nahi hona chahiye\n"
            "   • @BotFather → /mybots → apna bot → API Token → dobara copy\n"
            "   • BotFather me bot 'disabled' na ho (us par unblock karo)\n"
            "   • .env me line aisi ho: BOT_TOKEN=1234567890:AAHxxxx (naam = BOT_TOKEN)")
    ME_ID = me.id
    BOT_USERNAME = me.username or ""
    log.info("✅ Bot @%s verify ho gaya (token sahi hai). SUPER-ADMINS: %s",
             BOT_USERNAME, ADMIN_IDS)

    # ── 2.5) CUSTOM BOT POLLERS (per-chat bots) ──
    try:
        sync_custom_pollers()
    except Exception as e:
        log.warning("sync_custom_pollers: %s", e)

    # ── 3) AUTO-BACKUP ──
    try:
        bpath, bcreated = auto_backup()
        if bpath:
            log.info("Backup %s: %s",
                     "CREATED" if bcreated else "up-to-date, skipped", bpath)
    except Exception as e:
        log.warning("Startup backup failed: %s", e)

    # ── 4) BOT COMMANDS ──
    await bot.set_my_commands([
        BotCommand(command="start", description="Main menu"),
        BotCommand(command="add", description="Add a channel / group"),
        BotCommand(command="chats", description="My chats / settings"),
        BotCommand(command="broadcast", description="📣 Broadcast to all saved users"),
        BotCommand(command="users", description="📊 Saved users list"),
        BotCommand(command="stopbroadcast", description="⏹ Stop running broadcast"),
        BotCommand(command="backup", description="💾 Manual backup (super-admin)"),
        BotCommand(command="help", description="Help & instructions"),
        BotCommand(command="cancel", description="Cancel current action"),
    ])

    # clean up chats we are no longer part of
    for row in db.get_chats():
        try:
            member = await bot.get_chat_member(row.chat_id, ME_ID)
            if getattr(member, "status", "") in ("left", "kicked"):
                db.remove_chat(row.chat_id)
                log.info("Removed stale chat #%s", row.chat_id)
        except Exception:
            pass

    # ── ready summary ──
    try:
        n_chats = len(db.get_chats())
        n_users = db.users.count_documents({})
    except Exception:
        n_chats = n_users = "?"
    log.info("──────────────────────────────────────────────")
    log.info("✅ Bot @%s CHALU HO GAYA — sab kuch ready!", BOT_USERNAME)
    log.info("   📚 Chats: %s  |  👥 Saved users: %s", n_chats, n_users)
    log.info("   👉 Ab Telegram me @%s ko /start karo.", BOT_USERNAME)
    log.info("   ❌ Agar /start pe reply NAHI aata to:")
    log.info("      • Token sahi hai? (upar verify ho chuka hai)")
    log.info("      • Bot Father me bot 'disabled' to nahi?")
    log.info("      • Do jagah (dusra PC/VPS) par same bot nahi chala rahe?")
    log.info("      • Isi token pe dusre host (pythonanywhere etc.) se webhook set to nahi?")
    log.info("──────────────────────────────────────────────")
    log.info("Polling started — waiting for join requests…")
    await dp.start_polling(bot, allowed_updates=[
        "message", "callback_query", "chat_join_request", "my_chat_member",
    ])


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "--restore":
        # python main.py --restore backups/data_xxx.json
        db.connect(legacy_owner=ADMIN_IDS[0] if ADMIN_IDS else 0)
        db.restore(args[1])
        print(f"✅ Restore ho gaya: {args[1]}")
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            log.info("Bot stopped.")
        except SystemExit as e:
            # hamare Hinglish error messages ko properly dikhao (swallow mat karo)
            if str(e):
                print(str(e))
            sys.exit(1)
