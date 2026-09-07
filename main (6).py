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
from collections import deque
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Optional
from zoneinfo import ZoneInfo

# ── USERBOT (MTProto / Telethon) — optional: premium account se welcome ──
try:
    from telethon import TelegramClient, events, functions
    from telethon import types as tltypes
    from telethon.errors import (
        FloodWaitError, PeerFloodError, SessionPasswordNeededError,
        PhoneCodeInvalidError, PhoneCodeExpiredError, RPCError,
        UserIsBlockedError,
    )
    from telethon.sessions import StringSession
    UB_AVAILABLE = True
except Exception:
    UB_AVAILABLE = False

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
    for part in raw.replace(" ", "").replace("\"", "").replace("'", "").split(","):
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
    ub_enabled: int = 0        # userbot (MTProto) se welcome ON/OFF
    ub_admin: int = 0          # bind time par admin verified tha?
    ub_bot_both: int = 0       # 1 = userbot AUR main bot dono welcome bheje
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
            ub_enabled=d.get("ub_enabled", 0),
            ub_admin=d.get("ub_admin", 0),
            ub_bot_both=d.get("ub_bot_both", 0),
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

    def get_setting(self, key: str, default=None):
        """Bot ke private settings (jaise userbot session string)."""
        d = self._db["settings"].find_one({"key": key})
        return d["value"] if d else default

    def set_setting(self, key: str, value):
        self._db["settings"].update_one(
            {"key": key}, {"$set": {"value": value}}, upsert=True)

    def claim_welcome(self, chat_id: int, user_id: int, ttl: float = 90.0
                      ) -> bool:
        """Dedup: main bot + userbot dono ko ek hi join request milti hai.
        Jo pehle claim karega wahi welcome bhejega (90 sec window)."""
        key = f"w:{chat_id}:{user_id}"
        now = time.time()
        d = self._db["dedup"].find_one({"key": key})
        if d and now - d.get("ts", 0) < ttl:
            return False
        self._db["dedup"].update_one(
            {"key": key}, {"$set": {"ts": now}}, upsert=True)
        return True

    def update_chat(self, chat_id: int, **fields):
        allowed = {"welcome_text", "media_file_id", "media_kind", "buttons",
                   "emoji", "approve", "title", "chat_type",
                   "custom_bot_token", "custom_bot_name",
                   "ub_enabled", "ub_admin", "ub_bot_both"}
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
    final_text = "".join(out)
    # ── auto-premium: popular emojis (🔥👍😭🚀 etc.) plain diye the to bhi
    #    verified premium IDs se ANIMATED bhej do (agar bot premium-capable hai)
    merged, _added = premium_upgrade(final_text, merged)
    return final_text, merged


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


# emoji characters detect karne ke liye (copy-paste warning me use hota hai)
EMOJI_CHAR_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)


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

# ── POPULAR PREMIUM EMOJIS (live-verified animated IDs) ─────────────────────
# Owner ke paas Premium na ho to bhi in emojis ko PREMIUM banata hai.
# NOTE: bot sirf tab hi premium render kar payega jab bot owner ke paas
# Telegram Premium ho YA bot ke paas Fragment username ho — warna Telegram
# khud inse normal emoji dikhata hai (Telegram ka rule, code nahi tod sakta).
# ⚠️ Ye IDs LIVE verified hain (is bot ke token se getCustomEmojiStickers
#    call karke — is_animated=True wale hi rakhe). Web pe bakhte SO answers ke
#    IDs FAKE/static nikle the — isliye sirf verified rakhna zaroori hai.
PREMIUM_LIB: dict[str, str] = {
    "\U0001F44D": "5368324170671202286",       # 👍  (HandEmoji, animated)
    "\U0001F4BB": "5877565553761062314",       # 💻  (EmojiStatus, animated)
    "\U0001F92A": "5408846744727334338",       # 🤪  (DogsEmoji, animated)
}
PREMIUM_LIB_VERIFIED: dict[str, str] = dict(PREMIUM_LIB)


async def verify_premium_lib() -> None:
    """Startup par library IDs check karo — jo galat/static hain unhe hatao."""
    global PREMIUM_LIB_VERIFIED
    if not PREMIUM_LIB or not hasattr(bot, "get_custom_emoji_stickers"):
        return
    try:
        stickers = await bot.get_custom_emoji_stickers(list(PREMIUM_LIB.values()))
        good: dict[str, str] = {}
        for s in stickers:
            eid = getattr(s, "custom_emoji_id", "")
            emoji = getattr(s, "emoji", "")
            if getattr(s, "is_animated", False) and emoji in PREMIUM_LIB                     and PREMIUM_LIB[emoji] == eid:
                good[emoji] = eid
        if good:
            PREMIUM_LIB_VERIFIED = good
        log.info("Premium library: %d/%d IDs verified animated",
                 len(good), len(PREMIUM_LIB))
    except Exception as e:
        log.warning("verify_premium_lib: %s", e)


def premium_upgrade(text: str, entities: list) -> list:
    """Plain popular emojis jo custom_emoji entity ke bina hain, unhe
    PREMIUM_LIB se upgrade karo (sirf agar already premium nahi hai)."""
    out = list(entities)
    covered = {(e.offset, e.offset + e.length)
               for e in out if e.type == "custom_emoji"}
    added = 0
    lib = PREMIUM_LIB_VERIFIED or PREMIUM_LIB
    for ch, eid in lib.items():
        idx = text.find(ch)
        while idx != -1:
            off = 0
            for c in text[:idx]:
                off += 2 if ord(c) > 0xFFFF else 1
            span = (off, off + utf16_len(ch))
            if span not in covered:
                out.append(MessageEntity(type="custom_emoji", offset=off,
                                         length=utf16_len(ch),
                                         custom_emoji_id=eid))
                covered.add(span)
                added += 1
            idx = text.find(ch, idx + 1)
    return out, added


# ── custom bots (Channel-Help style: per-chat apna bot) ─────────────────────
CUSTOM_BOTS: dict = {}          # token -> Bot instance (cache)
MEDIA_CACHE: dict = {}          # file_id -> bytes (custom bot ke liye download)
WELCOME_SENT: dict = {}         # (user_id, chat_id) -> monotonic time (dedupe)
WELCOME_STATUS: dict = {}       # (user_id, chat_id) -> "sent"|"nodm"|"failed"
REQ_SEEN: dict = {}             # (user_id, chat_id) -> time — double notification rokne
JOIN_LOG: deque = deque(maxlen=120)   # latest join events (diagnostics /joins)
UPD_STATS: dict = {"cjr": 0, "msg": 0, "cb": 0, "started": time.time()}

# ─────────────────────────────────────────────────────────────────────────────
#  USERBOT (MTProto / Telethon) — premium account se HAR user ko welcome
#  Telegram ka rule: BOT sirf 5-min window me DM kar sakta hai (warna 403).
#  LEKIN ek USER account (aapka premium ID) kisi se bhi pehli baar baat kar
#  sakta hai. To userbot join request aate hi HAR user ko welcome + premium
#  animated emojis bhejta hai — bina approval ke. Spam-safe queue ke saath.
# ─────────────────────────────────────────────────────────────────────────────
UB_API_ID = int(os.getenv("UB_API_ID", "0") or 0)
UB_API_HASH = os.getenv("UB_API_HASH", "").strip()
UB_MIN_INTERVAL = float(os.getenv("UB_MIN_INTERVAL", "8") or 8)   # sec between msgs
UB_QUEUE_MAX = int(os.getenv("UB_QUEUE_MAX", "300") or 300)
UB_DAILY_CAP = int(os.getenv("UB_DAILY_CAP", "300") or 300)      # 0 = unlimited

UB = {
    "client": None,            # TelegramClient (ya FakeClient in tests)
    "me": None,                # userbot ka User (MTProto)
    "phone": "", "username": "",
    "alive": False,
    "queue": None,             # asyncio.Queue
    "worker": None,            # task
    "sent_total": 0, "sent_day": 0, "day_start": 0.0,
    "last_send": 0.0, "flood_until": 0.0,
    "login": None,             # {"step": "phone"|"code"|"pass", "phone": str}
    "last_err": "",
    "cap_notified": False,
    "pending_cache": {},       # cid -> {uid: User} (approve buttons ke liye)
    "entities": {},            # chat_id -> telethon entity (poller ke liye)
    "updates_seen": 0,         # raw updates kitne aaye (diagnostic)
    "join_updates": 0,         # join-request wale updates kitne aaye
    "poller": None,            # poller task
    "backfill_running": False,
}
UB_LOG: deque = deque(maxlen=60)


def ub_configured() -> bool:
    return bool(UB_AVAILABLE and UB_API_ID and UB_API_HASH)


def ub_ready() -> bool:
    """Userbot live hai aur apna session restore kar chuka hai?"""
    return bool(UB_AVAILABLE and UB["client"] is not None and UB["alive"])


def ub_peer_to_api_id(peer) -> int | None:
    """MTProto peer → Bot-API jaisi chat id (channels: -100xxxxxxxxxx)."""
    if isinstance(peer, tltypes.PeerChannel):
        return -(1000000000000 + peer.channel_id)
    if isinstance(peer, tltypes.PeerChat):
        return -peer.chat_id
    if isinstance(peer, tltypes.PeerUser):
        return peer.user_id
    return None


def _ub_convert_entities(ents) -> list:
    """aiogram entities → Telethon entities (custom_emoji = premium emoji!).
    Offsets dono jagah UTF-16 me hain — direct copy."""
    out = []
    for e in ents or []:
        t = getattr(e, "type", "")
        try:
            if t == "custom_emoji":
                out.append(tltypes.MessageEntityCustomEmoji(
                    e.offset, e.length, int(e.custom_emoji_id)))
            elif t == "bold":
                out.append(tltypes.MessageEntityBold(e.offset, e.length))
            elif t == "italic":
                out.append(tltypes.MessageEntityItalic(e.offset, e.length))
            elif t == "underline":
                out.append(tltypes.MessageEntityUnderline(e.offset, e.length))
            elif t == "strikethrough":
                out.append(tltypes.MessageEntityStrike(e.offset, e.length))
            elif t == "code":
                out.append(tltypes.MessageEntityCode(e.offset, e.length))
            elif t == "pre":
                out.append(tltypes.MessageEntityPre(
                    e.offset, e.length, getattr(e, "language", "") or ""))
            elif t == "spoiler":
                out.append(tltypes.MessageEntitySpoiler(e.offset, e.length))
            elif t == "text_link":
                out.append(tltypes.MessageEntityTextUrl(
                    e.offset, e.length, getattr(e, "url", "")))
            elif t == "url":
                out.append(tltypes.MessageEntityUrl(e.offset, e.length))
        except Exception:
            continue
    return out


def _ub_kb(row: ChatRow):
    """Sirf URL wale buttons userbot ke saath ja sakte hain (callback buttons
    main bot ke hote hain)."""
    rows = []
    for brow in row.button_rows():
        btns = []
        for b in brow:
            if isinstance(b, dict) and b.get("url"):
                btns.append(tltypes.InlineKeyboardButton(
                    text=str(b.get("text", "")), url=b["url"]))
        if btns:
            rows.append(btns)
    if not rows:
        return None
    return tltypes.InlineKeyboardMarkup(rows=rows)


def _ub_log(msg: str):
    UB_LOG.appendleft(msg)
    if len(UB_LOG) > 60:
        UB_LOG.pop()
    log.info("userbot: %s", msg)


async def ub_send(uid: int, text: str, ents: list | None = None,
                  kb=None, tries: int = 0) -> bool:
    """Welcome message ko spam-safe QUEUE me daalo (worker real send karta hai)."""
    if not ub_ready():
        return False
    q = UB.get("queue")
    if q is None:
        return False
    if q.qsize() >= UB_QUEUE_MAX:
        _ub_log(f"queue FULL ({q.qsize()}) — {uid} drop")
        return False
    await q.put({"uid": uid, "text": text, "ents": ents or [],
                 "kb": kb, "tries": tries})
    return True


async def ub_worker():
    """Queue worker — HAR message ke beech UB_MIN_INTERVAL gap (spam-safe),
    FloodWaitError par wait, PeerFloodError par 10 min backoff."""
    q = UB.get("queue")
    while True:
        item = await q.get()
        if not ub_ready():
            _ub_log(f"skip (userbot dead): {item['uid']}")
            continue
        # daily cap
        now = time.time()
        if now - UB["day_start"] > 86400:
            UB["day_start"] = now
            UB["sent_day"] = 0
            UB["cap_notified"] = False
        if UB_DAILY_CAP and UB["sent_day"] >= UB_DAILY_CAP:
            if not UB["cap_notified"]:
                UB["cap_notified"] = True
                await notify_owners(
                    "⚠️ <b>Userbot daily cap</b> (" + str(UB_DAILY_CAP) +
                    " msgs) poora ho gaya — aaj aur welcome nahi jayega.")
            _ub_log(f"daily cap hit: {item['uid']} drop")
            continue
        # spacing + flood wait
        while True:
            t = time.monotonic()
            fw = UB["flood_until"]
            if fw and t < fw:
                await asyncio.sleep(min(fw - t, 55))
                continue
            gap = UB["last_send"] + UB_MIN_INTERVAL - t
            if gap > 0:
                await asyncio.sleep(min(gap, 55))
                continue
            break
        cl = UB.get("client")
        try:
            await cl.send_message(item["uid"], item["text"],
                                  formatting_entities=item["ents"] or None,
                                  parse_mode=None, link_preview=False,
                                  buttons=item["kb"])
            UB["sent_total"] += 1
            UB["sent_day"] += 1
            UB["last_send"] = time.monotonic()
            try:
                dt = datetime.fromtimestamp(time.time())
                UB["last_send_fmt"] = dt.strftime("%H:%M:%S")
            except Exception:
                pass
        except FloodWaitError as e:
            secs = float(getattr(e, "seconds", 60) or 60)
            UB["flood_until"] = time.monotonic() + secs
            _ub_log(f"FloodWait {int(secs)}s — {item['uid']} requeue")
            if item["tries"] < 3:
                item["tries"] += 1
                await q.put(item)
            else:
                await notify_owners(
                    f"⚠️ Userbot flood-wait me hai — welcome "
                    f"{item['uid']} ko miss hua. Queue ruk jayegi.")
        except PeerFloodError:
            UB["flood_until"] = time.monotonic() + 600
            _ub_log("PeerFlood (spam guard) — 10 min backoff")
            await notify_owners(
                "⚠️ Telegram ne userbot ko <b>spam guard</b> laga diya "
                "(PeerFlood) — 10 min rukega. Jaldi-jaadi messages band karo "
                "(UB_MIN_INTERVAL badhao) warna account ban ho sakta hai.")
            if item["tries"] < 3:
                item["tries"] += 1
                await q.put(item)
        except UserIsBlockedError:
            _ub_log(f"user {item['uid']} ne userbot ko BLOCK kiya hai")
            await notify_owners(
                f"⚠️ <b>{item['uid']}</b> ne userbot ko block kiya hai — welcome "
                f"nahi gaya. (Admin usko Telegram me se manual DM kar sakta hai "
                f"ya approve karke add kar de.)")
        except Exception as e:
            emsg = str(e)
            _ub_log(f"send fail {item['uid']}: {emsg[:100]}")
            if "privacy" in emsg.lower() or "can't send" in emsg.lower():
                await notify_owners(
                    f"⚠️ <b>{item['uid']}</b> ki <b>privacy setting</b> strict hai "
                    f"('koi msg nahi') — userbot se welcome nahi gaya.\n"
                    f"Admin ise <b>join request list</b> se manual DM kar sakta "
                    f"hai ya request approve karke add kar de.")
            else:
                await notify_owners(
                    f"⚠️ Userbot se {item['uid']} ko message nahi gaya: "
                    f"{esc(emsg[:100])}")


def _ub_make_client(session_str: str = ""):
    """Factory (tests isse replace karte hain)."""
    return TelegramClient(StringSession(session_str or ""),
                          UB_API_ID, UB_API_HASH)


async def _ub_finalize_login(cl, me, phone: str):
    """Login complete — session save + worker/event handler on."""
    try:
        session_str = cl.session.save()
    except Exception:
        session_str = ""
    db.set_setting("ub_session", session_str)
    db.set_setting("ub_phone", phone)
    UB["client"] = cl
    UB["me"] = me
    UB["phone"] = phone
    UB["username"] = getattr(me, "username", "") or ""
    UB["alive"] = True
    UB["queue"] = asyncio.Queue()
    UB["worker"] = asyncio.create_task(ub_worker())
    UB["updates_seen"] = 0
    UB["join_updates"] = 0
    UB["poller"] = asyncio.create_task(_ub_poller())
    # USER admin ko UpdatePendingJoinRequests milta hai (important!)
    # BOT-admin wala update bhi register — dono handle karte hain.
    try:
        cl.add_event_handler(_ub_on_pending,
                             events.Raw(tltypes.UpdatePendingJoinRequests))
        _ub_log("handler ON: UpdatePendingJoinRequests (user-admin updates)")
    except Exception as e:
        _ub_log(f"pending-handler fail: {e}")
    try:
        cl.add_event_handler(_ub_on_join,
                             events.Raw(tltypes.UpdateBotChatInviteRequester))
    except Exception:
        pass
    # entities map fill (poller ke liye) — userbot jis bhi chat ka member/
    # admin hai usse dialogs se utha lo
    try:
        n = 0
        async for d in cl.iter_dialogs(limit=5000):
            e = getattr(d, "entity", None)
            if isinstance(e, tltypes.Channel):
                UB["entities"][-(1000000000000 + e.id)] = e
                n += 1
            elif isinstance(e, tltypes.Chat):
                UB["entities"][-e.id] = e
                n += 1
        _ub_log(f"entities map: {n} chats loaded")
    except Exception as ex:
        _ub_log(f"dialogs fail: {str(ex)[:60]}")
    name = (getattr(me, "first_name", "") or "") + (
        " @" + (getattr(me, "username", "") or "") if getattr(me, "username", "") else "")
    UB["last_send_fmt"] = ""
    _ub_log(f"LOGIN OK — {name} (premium: {getattr(me, 'premium', None)})")


async def ub_start():
    """Startup: saved session restore karo (agar UB_API_ID/HASH diye hain)."""
    if not ub_configured():
        log.info("Userbot OFF — .env me UB_API_ID / UB_API_HASH nahi hai")
        return
    try:
        saved = db.get_setting("ub_session")
    except Exception:
        saved = None
    if not saved:
        log.info("Userbot: session nahi mili — /ub se login karo")
        return
    try:
        cl = _ub_make_client(saved)
        await cl.connect()
        if not await cl.is_user_authorized():
            await cl.disconnect()
            log.warning("Userbot: saved session invalid — /ub se dobara login")
            return
        me = await cl.get_me()
        await _ub_finalize_login(cl, me, db.get_setting("ub_phone", "") or "")
        log.info("✅ Userbot ready: @%s", UB["username"])
    except Exception as e:
        log.warning("Userbot start failed: %s", e)


async def ub_backfill(owner_uid: int) -> str:
    """Saare LINKED chats ke PENDING join-request users ko queue me daalo.
    (Purani pending requests bhi — jo update chhut gayi thi unke liye.)"""
    if not ub_ready():
        return "⚠️ Userbot login nahi hai"
    if UB["backfill_running"]:
        return "⏳ Backfill pehle se chal raha hai — thoda ruko"
    UB["backfill_running"] = True
    cl = UB["client"]
    total = 0
    chats = 0
    try:
        for row in db.get_chats():
            if not (row.ub_enabled and row.ub_admin):
                continue
            ent = UB["entities"].get(row.chat_id)
            if ent is None:
                continue
            try:
                offset_date = None
                offset_user = None
                got = 0
                while True:
                    r = await cl(functions.messages.GetChatInviteImportersRequest(
                        peer=ent, offset_date=offset_date,
                        offset_user=offset_user,
                        requested=True, limit=100))
                    imps = list(getattr(r, "importers", None) or [])
                    if not imps:
                        break
                    for imp in imps:
                        await _ub_deliver(row.chat_id, getattr(imp, "user_id", 0))
                        got += 1
                        total += 1
                    if len(imps) < 100:
                        break
                    last = imps[-1]
                    ld = getattr(last, "date", None)
                    if isinstance(ld, datetime):
                        offset_date = ld
                    elif ld:
                        try:
                            offset_date = datetime.fromtimestamp(int(ld))
                        except Exception:
                            offset_date = datetime.now(timezone.utc)
                    else:
                        offset_date = datetime.now(timezone.utc)
                    lu = int(getattr(last, "user_id", 0) or 0)
                    offset_user = (tltypes.InputUser(user_id=lu, access_hash=0)
                                   if lu else tltypes.InputUserEmpty())
                if got:
                    chats += 1
                    _ub_log(f"backfill {row.title}: {got} users queue me")
            except Exception as e:
                _ub_log(f"backfill {row.chat_id} err: {str(e)[:60]}")
    except Exception as e:
        _ub_log(f"backfill fatal: {str(e)[:80]}")
    finally:
        UB["backfill_running"] = False
    eta = (total * UB_MIN_INTERVAL) / 60
    return (f"📥 Backfill DONE — <b>{total}</b> users queue me "
            f"({chats} chats). Est. time: <b>{eta:.0f} min</b> "
            f"(gap {UB_MIN_INTERVAL:.0f}s).\n"
            f"Subscribed users ko welcome jayega — approve abhi bhi tumko "
            f"karna hoga.")


async def _ub_get_input_user(uid: int):
    """uid → InputUser (access_hash ke saath, agar mil jaye)."""
    for cache in UB["pending_cache"].values():
        u = cache.get(uid)
        if u is not None:
            return tltypes.InputUser(user_id=uid,
                                     access_hash=getattr(u, "access_hash", 0) or 0)
    try:
        users = await UB["client"](functions.users.GetUsersRequest(
            id=[tltypes.InputUser(user_id=uid, access_hash=0)]))
        if users:
            uu = users[0]
            UB["pending_cache"].setdefault(0, {})[uid] = uu
            return tltypes.InputUser(user_id=uid,
                                     access_hash=getattr(uu, "access_hash", 0) or 0)
    except Exception:
        pass
    return tltypes.InputUser(user_id=uid, access_hash=0)


async def _ub_fetch_pending(cid: int, max_pages: int = 2) -> list[dict]:
    """Ek chat ke saare PENDING join-request users (name + username ke saath).
    User objects cache me bhi store (approve ke liye access_hash chahiye)."""
    if not ub_ready():
        return []
    ent = UB["entities"].get(cid)
    if ent is None:
        _ub_log(f"fetch pending {cid}: entity nahi (dobara link karo)")
        return []
    cl = UB["client"]
    out, seen = [], set()
    cache = UB["pending_cache"].setdefault(cid, {})
    offset_date = datetime.now(timezone.utc)
    offset_user = tltypes.InputUserEmpty()
    for _ in range(max_pages):
        try:
            r = await cl(functions.messages.GetChatInviteImportersRequest(
                peer=ent, offset_date=offset_date, offset_user=offset_user,
                requested=True, limit=100))
        except Exception as e:
            _ub_log(f"fetch pending {cid} err: {str(e)[:60]}")
            break
        for u in getattr(r, "users", None) or []:
            cache[getattr(u, "id", 0)] = u
        imps = list(getattr(r, "importers", None) or [])
        for imp in imps:
            uid = getattr(imp, "user_id", 0)
            if not uid or uid in seen:
                continue
            seen.add(uid)
            usr = cache.get(uid)
            out.append({
                "uid": uid,
                "first": getattr(usr, "first_name", "") or "",
                "last": getattr(usr, "last_name", "") or "",
                "uname": getattr(usr, "username", "") or "",
            })
        if len(imps) < 100:
            break
        last = imps[-1]
        ld = getattr(last, "date", None)
        if isinstance(ld, datetime):
            offset_date = ld
        elif ld:
            try:
                offset_date = datetime.fromtimestamp(int(ld))
            except Exception:
                offset_date = datetime.now(timezone.utc)
        else:
            offset_date = datetime.now(timezone.utc)
        lu = int(getattr(last, "user_id", 0) or 0)
        offset_user = (tltypes.InputUser(user_id=lu, access_hash=0)
                       if lu else tltypes.InputUserEmpty())
    return out


async def _ub_approve_one(cid: int, uid: int, approve: bool) -> tuple[bool, str]:
    """Ek user ko approve (True) ya decline (False) — userbot admin hai."""
    if not ub_ready():
        return False, "⚠️ Userbot login nahi hai"
    ent = UB["entities"].get(cid)
    if ent is None:
        return False, "❌ Chat ki entity nahi mili — /ub se dobara link karo"
    try:
        hu = await _ub_get_input_user(uid)
        await UB["client"](functions.messages.HideChatJoinRequestRequest(
            peer=ent, user_id=hu, approved=approve))
        UB["pending_cache"].pop(cid, {})
        row = db.get_chat(cid)
        title = row.title if row else str(cid)
        act = "✅ APPROVE" if approve else "❌ DECLINE"
        _ub_log(f"{act}: {uid} -> {title}")
        return True, f"{act} ho gaya {uid}"
    except Exception as e:
        _ub_log(f"approve-one {uid} err: {str(e)[:70]}")
        return False, f"❌ Fail: {esc(str(e)[:100])}"


def _ub_pending_text(cid: int, users: list[dict]) -> str:
    row = db.get_chat(cid)
    title = row.title if row else str(cid)
    if not users:
        return (f"👥 <b>{esc(title)}</b>: ✅ koi pending request nahi hai!\n"
                "(Naye join requests userbot ko turant milenge.)")
    n = len(users)
    lines = [f"👥 <b>{esc(title)}</b> — <b>{n}</b> pending request(s)\n",
             "👇 Har user ke <b>✅ Approve</b> / <b>❌ Decline</b> button par "
             "click karo (sirf wohi user approve/decline hoga):\n"]
    for i, u in enumerate(users[:15], 1):
        name = f"{u['first']} {u['last']}".strip() or f"User {u['uid']}"
        uname = f" (@{u['uname']})" if u['uname'] else ""
        lines.append(f"<b>{i}.</b> {esc(name)}{esc(uname)}")
    if n > 15:
        lines.append(f"\n… aur <b>{n - 15}</b> aur. '↻ Refresh' se naye dekho (15/baar).")
    return "\n".join(lines)


def _ub_pending_kb(cid: int, users: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for u in users[:15]:
        label = f"{u['first']} {u['last']}".strip() or f"User {u['uid']}"
        if u["uname"]:
            label = f"@{u['uname']}"
        else:
            label = (label[:20] + "…") if len(label) > 21 else label
        rows.append([
            InlineKeyboardButton(text=f"✅ {label}",
                                 callback_data=f"ubap:{cid}:{u['uid']}"),
            InlineKeyboardButton(text="❌",
                                 callback_data=f"ubdc:{cid}:{u['uid']}"),
        ])
    rows.append([InlineKeyboardButton(text="✅ Approve ALL (is chat)",
                                      callback_data=f"ubapp:{cid}")])
    rows.append([InlineKeyboardButton(text="↻ Refresh",
                                      callback_data=f"ubpend:{cid}"),
                 InlineKeyboardButton(text="🔙 Back",
                                      callback_data=f"chat:{cid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ub_approve_all(cid: int) -> tuple[int, str]:
    """Userbot account (admin hai) se ek chat ke SAARE pending join requests
    ek hi call me approve. Returns (count, message). count=-1 = fail."""
    if not ub_ready():
        return -1, "⚠️ Userbot login nahi hai"
    ent = UB["entities"].get(cid)
    if ent is None:
        return -1, "❌ Chat ki entity nahi mili — /ub se dobara link karo"
    cl = UB["client"]
    # pehle pending count (status ke liye)
    n = 0
    try:
        r = await cl(functions.messages.GetChatInviteImportersRequest(
            peer=ent, offset_date=datetime.now(timezone.utc),
            offset_user=tltypes.InputUserEmpty(),
            requested=True, limit=100))
        n = len(list(getattr(r, "importers", None) or []))
    except Exception as e:
        _ub_log(f"approve-all count {cid} err: {str(e)[:60]}")
    # ek call me sab approve (Telegram ka bulk API)
    try:
        await cl(functions.messages.HideAllChatJoinRequestsRequest(
            peer=ent, approved=True))
    except Exception as e:
        _ub_log(f"approve-all {cid} fail: {str(e)[:80]}")
        return -1, f"❌ <b>{esc(getattr(ent, 'title', '') or str(cid))}</b> approve "                   f"nahi hua: {esc(str(e)[:100])}"
    row = db.get_chat(cid)
    title = row.title if row else (getattr(ent, "title", "") or str(cid))
    _ub_log(f"✅ approve-all {title}: {n} user(s) approve ho gaye")
    return n, f"✅ <b>{esc(title)}</b>: <b>{n}</b> pending request(s) APPROVE ho gaye"


async def _ub_approve_all_task(msg, uid: int, cid: int | None = None):
    """Background: sab linked chats (ya ek chat) ke pending approve karo."""
    try:
        lines, tot = [], 0
        if cid is not None:
            n, m = await _ub_approve_all(cid)
            lines.append(m)
            if n >= 0:
                tot += n
        else:
            for row in db.get_chats():
                if not (row.ub_enabled and row.ub_admin):
                    continue
                n, m = await _ub_approve_all(row.chat_id)
                lines.append(m)
                if n >= 0:
                    tot += n
        if not lines:
            lines.append("ℹ️ Koi linked chat nahi hai — pehle /ub se chat link karo")
        out = "\n\n".join(lines) + f"\n\n📊 Total approved: <b>{tot}</b>"
        try:
            await msg.answer(out, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    except Exception as e:
        _ub_log(f"approve-all task fail: {str(e)[:80]}")
        try:
            await msg.answer(f"❌ Approve-all fail: <code>{esc(str(e)[:100])}</code>",
                             parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def _ub_test_dm(uid: int) -> str:
    """Userbot se khud ko test message — send path live check."""
    if not ub_ready():
        return "⚠️ Userbot login nahi hai"
    try:
        trow = None
        for r in db.get_chats():
            if r.ub_enabled:
                trow = r
                break
        if trow is not None:
            text, ents = render_welcome(trow, "Test", "User", "", uid, trow.title)
            if not text.strip():
                text = f"🧪 Test DM — Welcome to {trow.title}!"
        else:
            text = ("🧪 <b>Test DM — userbot chal raha hai!</b>\n\n"
                    "Ye message userbot account se aaya hai. Ab welcome text "
                    "set karke ek naya join request bhejo — waisa hi message "
                    "har user ko jayega.")
            ents = []
        await UB["client"].send_message(
            uid, text, formatting_entities=_ub_convert_entities(ents) or None,
            parse_mode=None, link_preview=False)
        _ub_log(f"🧪 Test DM -> {uid} OK")
        return "OK"
    except Exception as e:
        _ub_log(f"🧪 Test DM fail: {str(e)[:80]}")
        raise


async def _ub_backfill_task(msg, uid: int):
    """Backfill ko background me chalao — bot block nahi hoga."""
    try:
        res = await ub_backfill(uid)
        try:
            await msg.answer(res, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    except Exception as e:
        _ub_log(f"backfill task fail: {str(e)[:80]}")
        try:
            await msg.answer(f"❌ Backfill fail: <code>{esc(str(e)[:100])}</code>",
                             parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def ub_stop():
    try:
        if UB["worker"]:
            UB["worker"].cancel()
    except Exception:
        pass
    try:
        if UB["poller"]:
            UB["poller"].cancel()
    except Exception:
        pass
    UB["worker"] = None
    UB["poller"] = None
    UB["entities"] = {}
    UB["alive"] = False
    try:
        if UB.get("client"):
            await UB["client"].disconnect()
    except Exception:
        pass
    UB["client"] = None
    UB["me"] = None
    try:
        db.set_setting("ub_session", "")
    except Exception:
        pass
    _ub_log("LOGOUT — session deleted")


async def _ub_deliver(cid: int, uid: int) -> None:
    """Ek user ko welcome bhejo (rate-limited queue ke through)."""
    if not ub_ready():
        _ub_log(f"SKIP {uid} -> {cid}: userbot ready nahi")
        return
    if not uid or not cid:
        return
    row = db.get_chat(cid)
    if row is None:
        _ub_log(f"SKIP {uid} -> {cid}: chat linked nahi hai")
        return
    if not row.ub_enabled or not row.ub_admin:
        _ub_log(f"SKIP {uid} -> {cid}: ub_enabled={row.ub_enabled} "
                f"ub_admin={row.ub_admin}")
        return
    if not db.claim_welcome(cid, uid, ttl=3 * 86400):
        _ub_log(f"SKIP {uid} -> {cid}: already bheja (3 din me dobara nahi)")
        return
    # user info (best-effort)
    first, last, uname = f"User {uid}", "", ""
    try:
        users = await UB["client"](functions.users.GetUsersRequest(
            id=[tltypes.InputUser(user_id=uid, access_hash=0)]))
        if users:
            uu = users[0]
            first = uu.first_name or ""
            last = uu.last_name or ""
            uname = uu.username or ""
    except Exception:
        pass
    name = f"{first} {last}".strip() or f"User {uid}"
    db.upsert_user(row.owner_id, uid, first, last, uname)
    db.link_user_chat(row.owner_id, uid, cid)

    text, ents = render_welcome(row, first, last, uname, uid, row.title)
    if not text.strip():
        text, ents = f"🎉 Welcome to {row.title}!", []
    ok = await ub_send(uid, text, _ub_convert_entities(ents), _ub_kb(row))
    status = ("✅ welcome queue me (userbot se)"
              if ok else "⚠️ welcome queue me nahi ja paya (full)")
    _ub_log(f"📨 {name} -> {row.title} [{status}]")
    JOIN_LOG.append({"ts": time.time(), "user": name, "uid": uid,
                     "chat": row.title or str(cid), "chat_id": cid,
                     "status": "sent" if ok else "failed",
                     "auto": False, "bot": "userbot"})
    await notify_user(row.owner_id,
                      f"🙋 <b>{esc(name)}</b> requested to join\n"
                      f"<b>{esc(row.title)}</b>\n{status}\n"
                      "(approve Telegram app me karo; chaaho to user ko "
                      "<b>join request list</b> se manual DM bhi kar sakte ho)")


async def _ub_on_join(ev):
    """[BOT accounts ke liye] UpdateBotChatInviteRequester."""
    try:
        u = ev.original_update
    except Exception:
        return
    UB["updates_seen"] += 1
    if isinstance(u, tltypes.UpdateBotChatInviteRequester):
        UB["join_updates"] += 1
        cid = ub_peer_to_api_id(getattr(u, "peer", None))
        uid = getattr(u, "user_id", 0)
        _ub_log(f"⚠️ BOT-style invite update: user {uid} -> {cid}")
        await _ub_deliver(cid, uid)


async def _ub_poll_chat(cid: int) -> int:
    """Ek linked chat ke saare PENDING join-request users fetch karke
    queue me daalo. Returns kitne users mile."""
    if not ub_ready():
        return 0
    ent = UB["entities"].get(cid)
    if ent is None:
        _ub_log(f"poll {cid}: entity nahi mili (link dobara karo)")
        return 0
    try:
        cl = UB["client"]
        # telethon 1.44: offset_date + offset_user REQUIRED hain (None = crash).
        # First page = "abhi tak ke saare pending, sabse naye pehle".
        r = await cl(functions.messages.GetChatInviteImportersRequest(
            peer=ent, offset_date=datetime.now(timezone.utc),
            offset_user=tltypes.InputUserEmpty(),
            requested=True, limit=100))
        imps = list(getattr(r, "importers", None) or [])
        for imp in imps:
            await _ub_deliver(cid, getattr(imp, "user_id", 0))
        if imps:
            _ub_log(f"🔎 poll {cid}: {len(imps)} pending user(s)")
        return len(imps)
    except Exception as e:
        _ub_log(f"poll {cid} err: {str(e)[:60]}")
        return 0


async def _ub_on_pending(ev):
    """[USER accounts ke liye — ASLI] UpdatePendingJoinRequests:
    user admin ko ye update milta hai jab join requests aati hain.
    Telethon 1.44: fields = peer, requests_pending (count),
    recent_requesters (naaye requesters). Latest se seedha poll karte hain."""
    try:
        u = ev.original_update
    except Exception:
        return
    UB["updates_seen"] += 1
    if isinstance(u, tltypes.UpdatePendingJoinRequests):
        UB["join_updates"] += 1
        cid = ub_peer_to_api_id(getattr(u, "peer", None))
        n = int(getattr(u, "requests_pending", 0) or 0)
        _ub_log(f"📥 pending-requests update: {n} pending -> chat {cid}")
        await _ub_poll_chat(cid)


async def _ub_poller():
    """Har 60s: linked chats ke PENDING join requests fetch karo aur
    jo bhejna baaki hai unhe bhejo (updates miss hone par bhi 100%)."""
    while True:
        try:
            await asyncio.sleep(60)
            if not ub_ready():
                continue
            for row in db.get_chats():
                if not (row.ub_enabled and row.ub_admin):
                    continue
                await _ub_poll_chat(row.chat_id)
        except Exception:
            pass
    await notify_user(row.owner_id,
                      f"🙋 <b>{esc(name)}</b> requested to join\n"
                      f"<b>{esc(row.title)}</b>\n{status}\n"
                      "(approve Telegram app me karo; chaaho to user ko "
                      "<b>join request list</b> se manual DM bhi kar sakte ho)")


async def _ub_is_admin(cl, chat) -> tuple[bool, str]:
    """Bulletproof admin check — 3 alag tarike, koi ek bhi chal gaya to admin.
    Returns (is_admin, identity_string)."""
    try:
        me = await cl.get_me()
    except Exception:
        me = None
    ident = ""
    if me is not None:
        uname = getattr(me, "username", None) or ""
        ident = (f"{esc(getattr(me, 'first_name', '') or '')}"
                 f"{' @' + esc(uname) if uname else ''}"
                 f" <code>{getattr(me, 'id', '?')}</code>")
    # Method 1: GetFullChannelRequest → full_chat.admin_rights (sabse authentic)
    if isinstance(chat, tltypes.Channel):
        try:
            full = await cl(functions.channels.GetFullChannelRequest(chat))
            ar = getattr(getattr(full, "full_chat", None), "admin_rights", None)
            if ar is not None:
                return True, ident
        except Exception:
            pass
    # Method 2: get_permissions(chat, "me")
    try:
        perms = await cl.get_permissions(chat, "me")
        if getattr(perms, "is_admin", False):
            return True, ident
    except Exception:
        pass
    # Method 3: channels.GetParticipantRequest direct
    try:
        res = await cl(functions.channels.GetParticipantRequest(
            channel=chat, participant="me"))
        if isinstance(getattr(res, "participant", None), (
                tltypes.ChannelParticipantAdmin,
                tltypes.ChannelParticipantCreator)):
            return True, ident
    except Exception:
        pass
    if me is not None:
        try:
            full = await cl(functions.channels.GetFullChannelRequest(chat))
            ch_id = getattr(getattr(full, "full_chat", None), "id", "?")
            ident += f" | chat me member hi NAHI hai (admin ho to member bhi hota hai)"
        except Exception:
            pass
    return False, ident


async def _ub_bind(text: str, owner_uid: int) -> tuple[str, ChatRow | None]:
    """Channel/group link (@username, t.me/link, +hash invite, ya id) ko
    userbot se validate karke chat row me bind karo."""
    if not ub_ready():
        return "⚠️ Userbot login nahi hai — pehle /ub → Login", None
    cl = UB["client"]
    text = (text or "").strip()
    chat = None
    # 1) Invite links: t.me/+hash (naya) ya t.me/joinchat/hash (purana)
    m = re.search(r"t\.me/(?:\+|joinchat/)([A-Za-z0-9_\-]+)", text)
    if m:
        try:
            r = await cl(functions.messages.CheckChatInviteRequest(hash=m.group(1)))
            if isinstance(r, tltypes.ChatInviteAlready):
                chat = r.chat
            else:
                return "❌ Invite link private hai ya expire — public link bhejo", None
        except Exception as e:
            return f"❌ Invite link check nahi hua: {esc(str(e)[:80])}", None
    # 2) Numeric ID — minus ke saath YA bina minus (dono try karo)
    elif text.lstrip("-").isdigit():
        cid = int(text)
        cands = [cid]
        if cid > 0:
            # ch = raw channel id; full chat id = -(1e12 + ch)
            ch = (cid - 1000000000000) if cid > 1000000000000 else cid
            full = -(1000000000000 + ch)
            cands = [cid, full, -cid if cid > 1000000000000 else full]
        got = False
        for cand in cands:
            if cand >= 0:
                continue
            mt = -(cand + 1000000000000)
            try:
                rs = await cl(functions.channels.GetChannelsRequest(
                    id=[tltypes.InputChannel(channel_id=mt, access_hash=0)]))
                if rs.chats:
                    chat = rs.chats[0]
                    got = True
                    break
            except Exception:
                continue
        if not got and cid > 0:
            # last try: userbot ke apne chats (dialogs) me dhoondo
            try:
                async for d in cl.iter_dialogs():
                    e = getattr(d, "entity", None)
                    if isinstance(e, tltypes.Channel):
                        fid = -(1000000000000 + e.id)
                        if e.id == cid or fid == cid or fid == -cid:
                            chat = e
                            got = True
                            break
                    if isinstance(e, tltypes.Chat) and (e.id == cid or -e.id == cid):
                        chat = e
                        got = True
                        break
            except Exception:
                pass
        if not got:
            return ("❌ Ye ID resolve nahi hua. Channel ki asli ID me minus hota "
                    "hai (<code>-100...</code>). Best tarika: <b>invite link</b> "
                    "ya <b>@username</b> bhejo.", None)
    # 3) @username ya t.me/name link
    else:
        t2 = text
        if t2.startswith("http"):
            t2 = t2.rstrip("/").split("/")[-1]
        t2 = t2.split("?")[0].split("#")[0].lstrip("@")
        try:
            chat = await cl.get_entity("@" + t2)
        except Exception:
            try:
                chat = await cl.get_entity(t2)
            except Exception as e:
                return f"❌ Ye resolve nahi hua: {esc(str(e)[:80])}", None
    if isinstance(chat, tltypes.User):
        return "❌ Ye user ID hai — channel/group ka link ya @username bhejo", None
    if not isinstance(chat, (tltypes.Channel, tltypes.Chat)):
        return "❌ Ye chat nahi hai (kisi aur type ka entity)", None
    # Admin check: user="me" dena ZAROORI hai — bina user ke Telegram sirf
    # chat ke default_banned_rights deta hai, jisme is_admin NAHI hota
    # (isliye pehle hamesha 'ADMIN nahi hai' aata tha).
    ok, who = await _ub_is_admin(cl, chat)
    if not ok:
        return (f"❌ <b>{esc(getattr(chat, 'title', '?') or '?')}</b> me "
                f"userbot <b>{who or '?'}</b> ADMIN nahi hai.\n\n"
                "Check karo: <b>/ub</b> → Status me jo account dikh raha hai "
                "(wahi ID) — us account ko <b>channel open karke → "
                "Administrators → Add Admin</b> se admin banao. "
                "Admin bante hi welcome jayega.", None)
    if isinstance(chat, tltypes.Channel):
        cid = -(1000000000000 + chat.id)
        ctype = "channel"
    else:
        cid = -chat.id
        ctype = "group"
    title = getattr(chat, "title", "") or f"Chat {cid}"
    row = db.get_chat(cid)
    if row is None:
        row = db.add_chat(cid, ctype, title, owner_id=owner_uid)
    db.update_chat(cid, ub_enabled=1, ub_admin=1, title=title,
                   chat_type=ctype)
    row = db.get_chat(cid)
    UB["entities"][cid] = chat          # poller ke liye entity yaad rakho
    _ub_log(f"LINKED: {title} ({cid}) — ub_enabled=1 ub_admin=1")
    return f"✅ Linked: <b>{esc(title)}</b> ({cid}) — userbot welcome ON", row


def ub_status_text() -> str:
    if not UB_AVAILABLE:
        return ("❌ <b>Telethon install nahi hai!</b>\n"
                "<code>pip install -r requirements.txt</code> chalao")
    if not ub_configured():
        return ("⚠️ <b>Userbot config nahi hai</b>\n\n"
                ".env me ye 2 cheezein daalo (https://my.telegram.org se):\n"
                "<code>UB_API_ID=1245678</code>\n"
                "<code>UB_API_HASH=abc123...</code>\n\n"
                "Phir /ub → Login karo.")
    if not ub_ready():
        return ("⏳ <b>Userbot logged out hai</b>\n\n"
                "• https://my.telegram.org se apna number login karke API ID/Hash "
                ".env me daalo\n"
                "• Phir 🔑 Login dabao")
    me = UB["me"] or type("X", (), {"first_name": "?"})()
    # linked chats ki state
    n_on = n_admin = n_linked = 0
    try:
        rows = db.get_chats()
        for r in rows:
            if r.ub_enabled or r.ub_admin:
                n_linked += 1
                if r.ub_enabled:
                    n_on += 1
                if r.ub_admin:
                    n_admin += 1
    except Exception:
        pass
    qs = UB["queue"].qsize() if UB["queue"] else 0
    w = "RUNNING" if (UB["worker"] and not UB["worker"].done()) else "STOPPED ❌"
    p = "RUNNING" if (UB["poller"] and not UB["poller"].done()) else "STOPPED ❌"
    ls = UB.get("last_send_fmt", "")
    logtail = "\n".join(list(UB_LOG)[:12]) or "(abhi koi log nahi)"
    return ("🤖 <b>Userbot: LOGIN OK</b>\n"
            f"👤 {esc(getattr(me, 'first_name', '') or '')} "
            f"@{esc(UB['username'])}\n"
            f"🆔 ID: <code>{esc(str(getattr(me, 'id', '?')))}</code>\n"
            f"📱 Phone: {esc(UB['phone'] or '-')}\n"
            f"⚙️ Worker: {w} | Poller: {p}\n"
            f"📥 Updates received: <b>{UB['updates_seen']}</b> "
            f"(join-request wale: {UB['join_updates']})\n"
            f"📤 Sent today: <b>{UB['sent_day']}</b> "
            f"(total {UB['sent_total']}){(' | last: ' + ls) if ls else ''}\n"
            f"⏳ Queue: {qs} | gap: {UB_MIN_INTERVAL:.0f}s | cap: {UB_DAILY_CAP}/day\n"
            f"🔗 Linked chats: {n_linked} "
            f"(ON: {n_on} | ADMIN-confirmed: {n_admin})\n"
            f"🗂️ Spam guard: "
            f"{'ACTIVE (flood wait)' if time.monotonic() < UB['flood_until'] else 'safe ✅'}\n"
            f"\n📜 <b>Last logs:</b>\n<code>{esc(logtail)}</code>")
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
    # ⚠️ IMPORTANT: entities de rahe hain to parse_mode=None zaroor karo —
    # nahi to bot ka default parse_mode=HTML bhi request me chala jata hai aur
    # Telegram entities ko properly apply nahi karta (premium emoji normal ho
    # jata hai — user ki reported problem).
    if entities:
        kw = dict(caption=caption or None, caption_entities=entities,
                  reply_markup=kb, parse_mode=None)
    else:
        kw = dict(caption=caption or None, caption_entities=None,
                  reply_markup=kb)
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
                                 reply_markup=kb,
                                 parse_mode=None if entities else ParseMode.HTML)
    else:
        await b.send_message(user_id, text, entities=entities, reply_markup=kb,
                             parse_mode=None if entities else ParseMode.HTML)




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
                             username: str, approved: bool,
                             status: str = "sent"):
    """Owner ko join request ki notification (approve/decline buttons)."""
    extra = " — auto-approved ✅" if approved else ""
    if status == "ub":
        dl = "\U0001f916 Welcome USERBOT se jayega (MTProto — har user ko pahunchta hai)"
    elif status == "sent":
        dl = "✅ Welcome DM bheja gaya"
    elif status == "nodm":
        dl = ("⚠️ User ko welcome DM NAHI ja sakta (Telegram rule: bot "
              "sirf 5-min window me / start ke baad DM kar sakta hai — "
              "bot 24/7 chale to automatic hota hai)")
    else:
        dl = "❌ Welcome fail — owner ko check karna hai"
    text = (f"🙋 <b>{esc(name)}</b>"
            f"{f' (@{esc(username)})' if username else ''} "
            f"requested to join\n<b>{esc(row.title)}</b>{extra}\n{dl}")
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

    dkey = (user.id, row.chat_id)
    # ── USERBOT MODE: userbot (MTProto) welcome bhejega ──
    if row.ub_enabled and row.ub_admin and ub_ready():
        # default: userbot hi bhejega — bot skip. Par agar "🤝 Bot + Userbot
        # dono" ON hai to BOT bhi try karega (uski 5-min window me agar ho
        # to dono messages jayenge; agar bot 403 de to userbot 100% pahuncha
        # dega — ye fail-safe hai).
        bot_note = ""
        if row.ub_bot_both:
            okb = await send_welcome(row, first, last, username, user.id,
                                     user_chat_id=getattr(cjr, "user_chat_id", None))
            bot_note = "+ bot✅" if okb else "+ bot✖(403/off)"
        JOIN_LOG.append({
            "ts": time.time(), "user": name, "uid": user.id,
            "chat": row.title or str(row.chat_id), "chat_id": row.chat_id,
            "status": "ub" + ("+bot" if row.ub_bot_both else ""),
            "auto": approved,
            "bot": "userbot" + ("+bot" if row.ub_bot_both else "")})
        await _notify_owner_join(row, cjr, name, username, approved, "ub" + bot_note)
        return
    ok = await send_welcome(row, first, last, username, user.id,
                            user_chat_id=getattr(cjr, "user_chat_id", None))
    status = WELCOME_STATUS.get(dkey, "sent" if ok else "failed")
    if not ok:
        # welcome fail -> REQ_SEEN hatao taaki doosra bot (custom/main)
        # wapas try kar sake (sirf "failed" par — "nodm" me koi faayda nahi,
        # doosre bot ko bhi wahi 403 milega)
        if status != "nodm":
            REQ_SEEN.pop(dkey, None)
    JOIN_LOG.append({
        "ts": time.time(), "user": name, "uid": user.id,
        "chat": row.title or str(row.chat_id), "chat_id": row.chat_id,
        "status": status, "auto": approved,
        "bot": "custom" if b is not bot else "main",
    })
    await _notify_owner_join(row, cjr, name, username, approved, status)


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
                UPD_STATS["cjr"] += 1
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
                       user_id: int, chat_title: str | None = None,
                       user_chat_id: int | None = None) -> bool:
    """Send the configured welcome to one user (PM). Returns success.

    user_chat_id: ChatJoinRequest.user_chat_id — Telegram 10.1 ke hisaab se
    bot is ID se join request ke 5 MINUTE ke andar user ko DM bhej sakta hai
    (bina start kiye!). Pehle isi se try karo, phir user_id (jo older
    behavior hai)."""
    # Telegram ka RULE (live-verified 2026-09-07): bot usi user ko PM kar
    # sakta hai jisne bot ko /start diya HO, YA jo abhi join request bhej
    # raha hai (5-min window, user_chat_id se). Baaki sab ko 403 → status
    # "nodm" → owner ko saaf message jata hai.
    targets = [user_chat_id] if (user_chat_id and user_chat_id != user_id) else []
    targets.append(user_id)
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

    async def _attempt(b, is_custom: bool):
        """Ek bot se saare targets (user_chat_id → user_id) try karo.
        "ok" | "forbidden" | "too_long" | ("bad", err)"""
        last_exc = None
        for tid in targets:
            try:
                ok = await _retry(lambda: _send_via(b, row, text, entities, kb,
                                                    tid, media_kind, file_id))
                if ok:
                    WELCOME_SENT[dkey] = time.monotonic()
                    return "ok", None
                last_exc = "retries exhausted (flood?)"
            except TelegramBadRequest as e:
                last_exc = e
                msg = str(e).lower()
                if "message is too long" in msg:
                    await notify_owners(
                        f"⚠️ Welcome for {esc(first)} too long — shorten it "
                        f"(max {(1024 if media_kind else 4096)} chars).")
                    return "too_long", e
                # entity/parse galat (stale emoji id etc.) → bina entities ke resend
                try:
                    ok2 = await _retry(lambda: _send_via(
                        b, row, text, None, kb, tid, media_kind, file_id))
                    if ok2:
                        WELCOME_SENT[dkey] = time.monotonic()
                        return "ok", None
                except Exception as e2:
                    last_exc = e2
            except TelegramForbiddenError:
                # aage ka target try karo (user_chat_id → user_id)
                last_exc = TelegramForbiddenError(method=None,
                                                message="forbidden")
                continue
            except TelegramRetryAfter:
                last_exc = "flood retries exhausted"
            except Exception as e:
                last_exc = e
        if isinstance(last_exc, TelegramForbiddenError):
            return "forbidden", last_exc
        return "bad", last_exc

    WELCOME_STATUS[dkey] = "failed"
    last_err = None
    for b in bots_to_try:
        is_custom = (b is not bot)
        res, err = await _attempt(b, is_custom)
        if res == "ok":
            WELCOME_STATUS[dkey] = "sent"
            return True
        if res == "too_long":
            WELCOME_STATUS[dkey] = "failed"
            return False
        if res == "forbidden":
            if is_custom:
                # custom bot user se baat nahi kar sakta → main bot try karo
                last_err = "custom bot forbidden (user ne start nahi kiya)"
                continue
            # MAIN bot bhi 403 → Telegram rule: user ne bot start nahi kiya
            # YA 5-min join-request window nikal gayi (ya user ne block kiya)
            WELCOME_STATUS[dkey] = "nodm"
            await notify_owners(
                f"⚠️ <b>{esc(first)}</b> ({user_id}) ko welcome DM NAHI ja sakta.\n"
                "Telegram ka rule: bot usi user ko PM kar sakta hai jisne bot "
                "ko <b>/start</b> diya ho, YA jo abhi-abhi join request bhej "
                "raha ho (<b>5 minute</b> ke window me — bot chalta hua chahiye).\n"
                "✅ Agar bot <b>24/7 running</b> hai to ye automatic ho jata hai.\n"
                "User manually bhi bot ko /start karke welcome dekh sakta hai.")
            return False
        last_err = err
        if is_custom:
            continue                       # custom bot issue → main bot se bhejo
        break                              # main bot: aur koi fallback nahi

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
        f"🤖 Userbot welcome: {'✅ ON' if row.ub_enabled else '⛔ OFF'}\n"
        f"{'🤝 Bot bhi bheje: ✅ ON (userbot + bot dono)' if (row.ub_enabled and row.ub_bot_both) else ''}\n"
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
        [InlineKeyboardButton(text="🤖 Userbot welcome" + (" ✅" if row.ub_enabled else " ⛔"),
                              callback_data=f"ubt:{cid}")],
        [InlineKeyboardButton(text="🤝 Bot + Userbot dono" + (" ✅" if row.ub_bot_both else " ⛔"),
                              callback_data=f"ubboth:{cid}")],
        [InlineKeyboardButton(text="👥 Pending requests", callback_data=f"ubpend:{cid}"),
         InlineKeyboardButton(text="✅ Approve ALL", callback_data=f"ubapp:{cid}")],
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


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    """Aapka Telegram ID batao — admin config check karne ke liye."""
    uid = message.from_user.id
    in_list = "✅ Haan — ADMIN_IDS me hai" if uid in ADMIN_IDS else "❌ NAHI hai"
    await message.answer(
        f"🆔 <b>Your Telegram ID:</b> <code>{uid}</code>\n"
        f"{in_list}\n\n"
        f"<b>Bot ke paas abhi:</b> {esc(', '.join(map(str, ADMIN_IDS)) or '(koi nahi — .env me ADMIN_IDS khaali hai!)')}\n\n"
        f"Fix: server par <code>.env</code> kholo → <code>ADMIN_IDS={uid}</code> "
        f"likho (comma se multiple, bina quotes) → bot restart karo.",
        parse_mode=ParseMode.HTML)


@router.message(Command("testemoji"))
async def cmd_testemoji(message: Message):
    """Sirf super-admin: premium emoji test — bot premium bhej sakta hai ya nahi."""
    if not is_super_admin(message.from_user.id):
        await message.answer(
            f"⛔ Sirf super-admin. <b>Tumhara ID:</b> <code>{message.from_user.id}</code> "
            f"— <code>ADMIN_IDS</code> me nahi hai. /myid se check karo.",
            parse_mode=ParseMode.HTML)
        return
    text = "Premium emoji test 🎯\n"
    ents = []
    lib = PREMIUM_LIB_VERIFIED or PREMIUM_LIB
    pos = 0
    for ch, eid in list(lib.items())[:3]:
        text += ch + "  "
        ents.append(MessageEntity(type="custom_emoji", offset=pos,
                                  length=utf16_len(ch), custom_emoji_id=eid))
        pos += 2 + utf16_len(ch)        # emoji + 2 spaces
    text += "\n\nAgar upar wale emojis ANIMATED chal rahe hain → bot premium "
    text += "emoji bhej sakta hai ✅ (welcome me premium emojis kaam karenge).\n"
    text += "Agar NORMAL (static) dikh rahe hain → bot ke owner ke paas "
    text += "Telegram Premium nahi hai ya bot ke paas Fragment username nahi isliye "
    text += "Telegram khud unhe normal bana raha hai.\n"
    text += "\nYAAD RAKHO: welcome text me premium emojis COPY-PASTE karne par "
    text += "Telegram unki hidden ID hata deta hai — sirf PREMIUM PICKER se type "
    text += "karo ya premium wala message FORWARD karo.\n"
    text += "\nSolution: bot banane wale account me Telegram Premium lo "
    text += "(ya fragment.com se username kharido)."
    try:
        await bot.send_message(message.from_user.id, text, entities=ents,
                               parse_mode=None)
        await message.answer("✅ Test message bhej diya — upar dekho.")
    except TelegramBadRequest as e:
        await message.answer(
            f"⚠️ Test bhejne me error: {esc(str(e)[:120])}\n\n"
            "Ye Telegram ke rule ki wajah se hai — bot custom emoji (premium) "
            "bhejne ke liye allowed nahi hai. Ya to bot owner ke paas Telegram "
            "Premium lo, ya bot ke liye fragment.com username.")
    except Exception as e:
        await message.answer(f"⚠️ Test failed: {esc(str(e)[:120])}")


# ─────────────────────────────────────────────────────────────────────────
#  Diagnostics — /joins aur /diag (super-admin)
# ─────────────────────────────────────────────────────────────────────────

@router.message(Command("joins"))
async def cmd_joins(message: Message):
    """Sirf super-admin: recent join requests ka exact record (kis user ko
    welcome GAYA, kisko NAHI — fast diagnosis ke liye)."""
    if not is_super_admin(message.from_user.id):
        await message.answer(
            f"⛔ Sirf super-admin. <b>Tumhara ID:</b> <code>{message.from_user.id}</code> "
            f"— <code>ADMIN_IDS</code> me nahi hai. /myid se check karo.",
            parse_mode=ParseMode.HTML)
        return
    if not JOIN_LOG:
        await message.answer("Abhi koi join request record nahi hai — "
                             "jab log join request bhejenge, yahan dikhega.")
        return
    icons = {"sent": "✅", "nodm": "⚠️", "failed": "❌"}
    lines = []
    for e in reversed(list(JOIN_LOG)[-25:]):
        t = datetime.fromtimestamp(e["ts"]).strftime("%H:%M")
        ic = icons.get(e["status"], "❓")
        who = esc(f"{e['user']} (id {e['uid']})" if e["uid"] else e["user"])
        lines.append(f"{t} {ic} {who} → {esc(e['chat'])} "
                     f"[{e['bot']} bot]"
                     f"{' — auto-approved ✅' if e['auto'] else ''}")
    await message.answer(
        "📋 <b>Recent join requests (last 25)</b>\n"
        "✅ = welcome DM gaya  |  ⚠️ = DM NAHI ja sakta "
        "(user ne bot start nahi kiya / 5-min window)  |  ❌ = fail\n\n"
        + "\n".join(lines))


@router.message(Command("diag"))
async def cmd_diag(message: Message):
    """Sirf super-admin: bot ki health — updates aa rahe hain? bot har chat
    me ADMIN hai? (Welcome tabhi jayega jab bot admin ho aur chal raha ho.)"""
    if not is_super_admin(message.from_user.id):
        await message.answer(
            f"⛔ Sirf super-admin. <b>Tumhara ID:</b> <code>{message.from_user.id}</code> "
            f"— <code>ADMIN_IDS</code> me nahi hai. /myid se check karo.",
            parse_mode=ParseMode.HTML)
        return
    up = time.time() - UPD_STATS["started"]
    hours, rem = divmod(int(up), 3600)
    mins = rem // 60
    lines = [
        "🤖 <b>Bot diagnostics</b>",
        f"⏱ Running: {hours}h {mins}m",
        f"📥 Updates: join={UPD_STATS['cjr']}  msg={UPD_STATS['msg']}  "
        f"callback={UPD_STATS['cb']}",
        f"🔌 Custom bot pollers: "
        f"{sum(1 for t in CUSTOM_TASKS.values() if not t.done())}",
        f"🗂 Chats in DB: {len(db.get_chats())}",
    ]
    bot_id = getattr(bot, "id", None)
    for row in db.get_chats()[:25]:
        try:
            me = await bot.get_chat_member(row.chat_id, bot_id or 0)
            st = getattr(me, "status", "?")
            ok = "✅" if st in ("administrator", "creator") else "❌"
        except Exception:
            ok = "❓"
        title = row.title or str(row.chat_id)
        lines.append(f"{ok} {esc(title)} ({row.chat_id})")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("ub"))
async def cmd_ub(message: Message):
    """Super-admin: userbot (premium MTProto account) — login/link/status."""
    if not is_super_admin(message.from_user.id):
        await message.answer(
            f"⛔ Sirf super-admin ise use kar sakta hai.\n"
            f"<b>Tumhara ID:</b> <code>{message.from_user.id}</code> — "
            f"ye <code>ADMIN_IDS</code> me nahi hai.\n"
            f"Check karne ke liye <b>/myid</b> bhejo.",
            parse_mode=ParseMode.HTML)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Login", callback_data="ub:login"),
         InlineKeyboardButton(text="🚪 Logout", callback_data="ub:logout")],
        [InlineKeyboardButton(text="➕ Link channel/group", callback_data="ub:link"),
         InlineKeyboardButton(text="📋 Linked chats", callback_data="ub:list")],
        [InlineKeyboardButton(text="🔄 Status refresh", callback_data="ub:status"),
         InlineKeyboardButton(text="📖 Help", callback_data="ub:help")],
        [InlineKeyboardButton(text="🧪 Test DM (khud ko)", callback_data="ub:testdm"),
         InlineKeyboardButton(text="📨 Backfill pending", callback_data="ub:backfill")],
        [InlineKeyboardButton(text="✅ Sab pending approve (sab chats)", callback_data="ub:approveall")],
    ])
    await message.answer(ub_status_text(), reply_markup=kb,
                         disable_web_page_preview=True)


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
        "✨ <b>Premium emojis:</b> TYPE from emoji panel or FORWARD a premium "
        "message — COPY-PASTE se Telegram unki ID hata deta hai (warning "
        "milti hai). Popular ones (👍💻🤪) plain likhne par "
        "bhi auto-premium ho jaate hain.\n"
        "• <b>Rule:</b> bot tabhi premium render karega jab bot ke owner ke "
        "paas Telegram Premium ho (ya bot ke paas Fragment username ho) — "
        "warna Telegram khud normal emoji dikhata hai.\n"
        "• Super-admin: <code>/testemoji</code> se check karo ki bot premium "
        "bhej sakta hai ya nahi.\n"
        "• Super-admin: <code>/joins</code> (kaunse user ko DM GAYA/Nahi) aur "
        "<code>/diag</code> (bot health + admin check).\n"
        "• <code>/ub</code> (super-admin): apne <b>premium account</b> ko "
        "userbot ki tarah login karo — phir HAR user ko welcome jayega "
        "(Telegram ki 403 limit nahi, spam-safe queue ke saath).\n"
        "⚠️ <b>Bot ko 24/7 chalte rakho</b> — Telegram bot sirf join "
        "request ke 5 min ke andar DM kar sakta hai.\n\n"
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
    UPD_STATS["msg"] += 1
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

        # ── COPY-PASTE DETECTION ──
        # Agar text me emoji hain par custom_emoji ID nahi mili → user ne
        # copy-paste kiya hai (Telegram copy-paste me ID delete kar deta hai).
        has_emoji = bool(EMOJI_CHAR_RE.search(text or ""))
        if has_emoji and not emoji_list:
            db.update_chat(chat_id, welcome_text=marker_text, emoji="[]")
            row = db.get_chat(chat_id)
            await message.answer(
                "⚠️ <b>Premium emojis nahi capture hue!</b>\n\n"
                "Aapne emojis ko <b>copy-paste</b> kiya lagta hai — Telegram "
                "copy-paste se premium emoji ki hidden ID <b>hata deta hai</b>, "
                "isliye wo <b>normal emoji</b> ban jaate hain.\n\n"
                "<b>Sahi tarike (koi ek):</b>\n"
                "1️⃣ Premium emojis ko emoji panel se <b>type</b> karo "
                "(premium section me se) ya\n"
                "2️⃣ Jis message me premium emojis hain (jaise us channel ki "
                "post) use mujhe <b>FORWARD</b> karo — forward me ID bachi "
                "rehti hai!\n\n"
                "💡 Popular emojis 👍 💻 🤪 plain likhne par bhi main unhe "
                "automatically premium bana deta hoon.\n"
                "Dobara '✏️ Welcome text' dabao aur sahi tarike se bhejo.")
            await edit_panel(message, row)
            return
        db.update_chat(chat_id, welcome_text=marker_text,
                       emoji=json.dumps(emoji_list))
        row = db.get_chat(chat_id)
        extra = f" with {len(emoji_list)} premium emoji✨" if emoji_list else ""
        await message.answer(f"✅ Welcome text saved for <b>{esc(row.title)}</b>{extra}.")
        await edit_panel(message, row)
        return

    # ── waiting for media ───────────────────────────────────────────────────
    # ── USERBOT LOGIN (phone → code → password) ───────────────────────
    if flow == "ubphone":
        phone = re.sub(r"[^\d+]", "", message.text or "")
        if len(phone) < 8:
            await message.answer("📱 Sahi phone number bhejo (country code ke "
                                 "saath, jaise +919876543210) — /cancel se band.")
            STASH[message.chat.id] = state
            return
        try:
            cl = _ub_make_client()
            await cl.connect()
            await cl.send_code_request(phone)
        except Exception as e:
            await message.answer(f"❌ Phone verify nahi hua: {esc(str(e)[:120])}\n"
                                 "/cancel karke dobara try karo")
            return
        UB["client"] = cl
        UB["phone"] = phone
        STASH[message.chat.id] = ("ubcode", None)
        await message.answer("📲 Telegram ne <b>OTP code</b> bheja hai — "
                             "wo 5-digit code yahan bhejo. (/cancel se band)")
        return
    if flow == "ubcode":
        code = (message.text or "").strip()
        cl = UB.get("client")
        if cl is None:
            await message.answer("⚠️ Login session expire — /ub se dobara karo")
            return
        try:
            me = await cl.sign_in(UB["phone"], code)
        except SessionPasswordNeededError:
            STASH[message.chat.id] = ("ubpass", None)
            await message.answer("🔐 Aapke account me <b>2-step password</b> "
                                 "hai — wo password bhejo.")
            return
        except Exception as e:
            await message.answer(f"❌ Code galat/expired: {esc(str(e)[:120])}\n"
                                 "Dobara code bhejo ya /cancel")
            STASH[message.chat.id] = state
            return
        await _ub_finalize_login(cl, me, UB["phone"])
        await message.answer("✅ <b>Userbot login ho gaya!</b>\n"
                             "Ab ➕ Link channel/group se apni chats jodo.")
        return
    if flow == "ubpass":
        cl = UB.get("client")
        if cl is None:
            await message.answer("⚠️ Login session expire — /ub se dobara karo")
            return
        try:
            me = await cl.sign_in(password=message.text or "")
        except Exception as e:
            await message.answer(f"❌ Password galat: {esc(str(e)[:120])}\n"
                                 "Dobara bhejo ya /cancel")
            return
        await _ub_finalize_login(cl, me, UB["phone"])
        await message.answer("✅ <b>Userbot login ho gaya!</b>\n"
                             "Ab ➕ Link channel/group se apni chats jodo.")
        return
    if flow == "ublink":
        resp, row = await _ub_bind(message.text or "", message.from_user.id)
        await message.answer(resp)
        return

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
    UPD_STATS["cjr"] += 1
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
    UPD_STATS["cb"] += 1
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
    if data.startswith("ub:"):
        action = data.split(":", 1)[1]
        if action == "login":
            if not ub_configured():
                await query.answer("⚠️ Pehle .env me UB_API_ID / UB_API_HASH "
                                   "daalo (my.telegram.org se)", show_alert=True)
                return
            if ub_ready():
                await query.answer("✅ Userbot already logged in hai",
                                   show_alert=True)
                return
            await query.answer()
            STASH[uid] = ("ubphone", None)
            try:
                await msg.edit_text(
                    "🔑 <b>Userbot login</b>\n\n"
                    "Apna <b>phone number</b> bhejo (country code ke saath, "
                    "jaise <code>+919876543210</code>).\n\n"
                    "Ye account bot nahi — uske Telegram me aane wala OTP "
                    "code hamein chahiye hoga. (/cancel se band)")
            except Exception:
                pass
            return
        if action == "logout":
            await query.answer()
            await ub_stop()
            try:
                await msg.edit_text(ub_status_text(), reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔑 Login",
                                                          callback_data="ub:login")]]))
            except Exception:
                pass
            return
        if action == "link":
            if not ub_ready():
                await query.answer("⚠️ Pehle userbot login karo", show_alert=True)
                return
            await query.answer()
            STASH[uid] = ("ublink", None)
            try:
                await msg.edit_text(
                    "➕ <b>Link channel/group</b>\n\n"
                    "Channel/group ka <b>invite link</b>, <b>@username</b> ya "
                    "<b>ID</b> bhejo (jisme userbot ADMIN hai).\n\n"
                    "Join request aane par userbot wahan se welcome bhejega.")
            except Exception:
                pass
            return
        if action == "list":
            await query.answer()
            rows = [r for r in db.get_chats() if r.ub_enabled]
            if not rows:
                out = "Abhi koi linked chat nahi hai — ➕ Link channel/group se add karo"
            else:
                out_lines = ["📋 <b>Linked chats (userbot)</b>"]
                for r in rows:
                    out_lines.append(f"• {esc(r.title)} — {'✅ admin verified' if r.ub_admin else '⚠️ admin NA verify'}")
                out = "\n".join(out_lines)
            try:
                await msg.edit_text(out)
            except Exception:
                pass
            return
        if action == "status":
            await query.answer()
            try:
                await msg.edit_text(ub_status_text(), disable_web_page_preview=True)
            except Exception:
                pass
            return
        if action == "help":
            await query.answer()
            try:
                await msg.edit_text(
                    "🤖 <b>Userbot kaise kaam karta hai</b>\n\n"
                    "• <b>Login</b>: apna premium Telegram account (phone → "
                    "OTP code → 2FA password agar hai)\n"
                    "• <b>Link</b>: channel/group ka link ya @username — jisme "
                    "userbot ADMIN ho\n"
                    "• Join request aate hi userbot <b>BINA approve kiye</b> "
                    "user ko welcome bhejta hai (premium animated emojis ke saath)\n"
                    "• <b>Spam-safe</b>: har message ke beech gap + flood-wait "
                    "handling — Telegram ki limit nahi lagegi\n"
                    "• <b>Security</b>: session string sirf aapke MongoDB me "
                    "save hoti hai — service restart par wapas chalu ho jati hai\n\n"
                    "Channel welcome text normal tarike se set karo (✏️ Welcome text) "
                    "— userbot wahi bhejega!")
            except Exception:
                pass
            return
        if action == "testdm":
            if not ub_ready():
                await query.answer("⚠️ Pehle userbot login karo", show_alert=True)
                return
            await query.answer("🧪 Test bhej raha hoon...")
            try:
                await _ub_test_dm(uid)
                await msg.answer("✅ <b>Test DM bhej diya!</b> Apne Telegram me "
                                 "dekho — userbot account se message aayega.",
                                 parse_mode=ParseMode.HTML)
            except Exception as e:
                await msg.answer(f"❌ Test DM fail: <code>{esc(str(e)[:150])}</code>",
                                 parse_mode=ParseMode.HTML)
            return
        if action == "backfill":
            if not ub_ready():
                await query.answer("⚠️ Pehle userbot login karo", show_alert=True)
                return
            await query.answer("📥 Backfill shuru! Result log me aayega.")
            already = UB.get("backfill_running")
            if already:
                await msg.answer("⏳ Backfill pehle se chal raha hai.")
                return
            asyncio.create_task(_ub_backfill_task(msg, uid))
            return
        if action == "approveall":
            if not ub_ready():
                await query.answer("⚠️ Pehle userbot login karo", show_alert=True)
                return
            await query.answer("✅ Sab approve shuru...")
            asyncio.create_task(_ub_approve_all_task(msg, uid, None))
            return
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
    if data.startswith("ubboth:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.",
                               show_alert=True)
            return
        if not row.ub_enabled:
            await query.answer("Pehle '🤖 Userbot welcome' ON karo",
                               show_alert=True)
            return
        new_val = 0 if row.ub_bot_both else 1
        db.update_chat(row.chat_id, ub_bot_both=new_val)
        row = db.get_chat(row.chat_id)
        try:
            await msg.edit_text(panel_text(row), reply_markup=panel_kb(row))
        except Exception:
            pass
        if new_val:
            await query.answer("✅ Userbot + Bot DONO welcome bhejenge",
                               show_alert=True)
        else:
            await query.answer("⛔ Ab sirf userbot bhejega")
        return

    if data.startswith("ubapp:"):
        cid = int(data.split(":", 1)[1])
        row = owned(cid)
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.",
                               show_alert=True)
            return
        if not ub_ready():
            await query.answer("⚠️ Pehle /ub → Login karo (userbot chahiye)",
                               show_alert=True)
            return
        await query.answer("✅ Approve shuru...")
        asyncio.create_task(_ub_approve_all_task(msg, query.from_user.id, cid))
        return

    if data.startswith("ubpend:"):
        cid = int(data.split(":", 1)[1])
        row = owned(cid)
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.",
                               show_alert=True)
            return
        if not ub_ready():
            await query.answer("⚠️ Pehle /ub → Login karo (userbot chahiye)",
                               show_alert=True)
            return
        await query.answer("👥 Fetching...")
        users = await _ub_fetch_pending(cid, max_pages=2)
        try:
            await msg.edit_text(_ub_pending_text(cid, users),
                                reply_markup=_ub_pending_kb(cid, users),
                                parse_mode=ParseMode.HTML)
        except Exception:
            await msg.answer(_ub_pending_text(cid, users),
                             reply_markup=_ub_pending_kb(cid, users),
                             parse_mode=ParseMode.HTML)
        return

    if data.startswith(("ubap:", "ubdc:")):
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        _, cid_s, uid_s = parts
        cid, uid = int(cid_s), int(uid_s)
        row = owned(cid)
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.",
                               show_alert=True)
            return
        if not ub_ready():
            await query.answer("⚠️ Pehle /ub → Login karo (userbot chahiye)",
                               show_alert=True)
            return
        approve = data.startswith("ubap:")
        ok, res = await _ub_approve_one(cid, uid, approve)
        await query.answer(("✅ " if ok else "❌ ") + res[:120], show_alert=not ok)
        # list refresh (jo loop chal raha hai use fir se render karo)
        users = await _ub_fetch_pending(cid, max_pages=1)
        try:
            await msg.edit_text(_ub_pending_text(cid, users),
                                reply_markup=_ub_pending_kb(cid, users),
                                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    if data.startswith("ubt:"):
        row = owned(int(data.split(":", 1)[1]))
        if row is None:
            await query.answer("⛔ Ye chat aapki nahi hai ya delete ho gayi.",
                               show_alert=True)
            return
        if not ub_configured():
            await query.answer("⚠️ Pehle .env me UB_API_ID / UB_API_HASH daalo "
                               "aur /ub → Login karo.", show_alert=True)
            return
        new_val = 0 if row.ub_enabled else 1
        db.update_chat(row.chat_id, ub_enabled=new_val)
        row = db.get_chat(row.chat_id)
        try:
            await msg.edit_text(panel_text(row), reply_markup=panel_kb(row))
        except Exception:
            pass
        if new_val and not row.ub_admin:
            await query.answer("✅ ON — par userbot is chat me ADMIN ho ye "
                               "verify karo (ya /ub se link karo), warna "
                               "updates nahi aayenge", show_alert=True)
        elif new_val:
            await query.answer("✅ Userbot welcome ON")
        else:
            await query.answer("⛔ Userbot welcome OFF")
        return

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

    # ── 2.4) PREMIUM EMOJI LIBRARY VERIFY ──
    try:
        await verify_premium_lib()
    except Exception as e:
        log.warning("verify_premium_lib: %s", e)

    # ── 2.5) CUSTOM BOT POLLERS (per-chat bots) ──
    try:
        sync_custom_pollers()
    except Exception as e:
        log.warning("sync_custom_pollers: %s", e)

    # ── 2.6) USERBOT (MTProto) — premium account se welcome ──
    try:
        await ub_start()
    except Exception as e:
        log.warning("Userbot start failed: %s", e)

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
        BotCommand(command="testemoji", description="✨ Premium emoji test (super-admin)"),
        BotCommand(command="joins", description="📋 Recent joins record (super-admin)"),
        BotCommand(command="diag", description="🦺 Bot diagnostics (super-admin)"),
        BotCommand(command="ub", description="🤖 Userbot (premium account)"),
        BotCommand(command="myid", description="🆔 Mera Telegram ID (admin check)"),
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
    log.info("   🛡 Super-admins (ADMIN_IDS): %s", ADMIN_IDS)
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
