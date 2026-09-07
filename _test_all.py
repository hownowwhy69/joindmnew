# -*- coding: utf-8 -*-
"""
Test suite: premium emoji render, welcome-DM reliability, admin gate,
per-chat custom bot.  Run:  python3 _test_all.py
"""
import asyncio
import json
import os
import sys
import types
from datetime import datetime

os.environ["BOT_TOKEN"] = "123456789:" + "A" * 35
os.environ["ADMIN_IDS"] = "111111111"
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["MONGO_DB"] = "test_wjb"

import main as M

import mongomock
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.types import CallbackQuery as _TB_CBQ
from aiogram.types import Chat as _TB_Chat
from aiogram.types import Message as _TB_Message
from aiogram.types import MessageEntity
from aiogram.types import User as _TB_User

class FakeMessage(_TB_Message):
    def __init__(self, uid, text):
        super().__init__(
            message_id=1, date=datetime.now(),
            chat=_TB_Chat(id=uid, type="private"),
            from_user=_TB_User(id=uid, is_bot=False, first_name="Raju"),
            text=text)
        object.__setattr__(self, "got_answers", [])

    async def answer(self, *a, **k):
        self.got_answers.append((a, k))

class FakeCallbackQuery(_TB_CBQ):
    def __init__(self, uid, data, message=None):
        super().__init__(
            id="cb1", from_user=_TB_User(id=uid, is_bot=False, first_name="Raju"),
            chat_instance="x", data=data, message=message)
        object.__setattr__(self, "got_answers", [])

    async def answer(self, *a, **k):
        self.got_answers.append((a, k))

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ❌ {name}  {extra}")


# ════════════════════════════════════════════════════════════════════
# FakeBot — methods record calls; per-test behavior via queues
# ════════════════════════════════════════════════════════════════════
class FakeBot:
    def __init__(self, name="main"):
        self.name = name
        self.calls = []            # (method, kwargs)
        self.sent = []             # messages sent to users
        self.behaviors = {}        # method -> exception / callable

    def __getattr__(self, item):
        async def _call(*args, **kwargs):
            self.calls.append((item, args, kwargs))
            if item in self.behaviors:
                bhv = self.behaviors[item]
                if isinstance(bhv, Exception):
                    raise bhv
                if callable(bhv):
                    return await bhv(*args, **kwargs)
            return types.SimpleNamespace(ok=True)
        return _call

    def behavior(self, item, exc):
        self.behaviors[item] = exc

    # helpers
    def sent_texts(self):
        return [c for c in self.calls if c[0] == "send_message"]

    def sent_media(self):
        return [c for c in self.calls if c[0] in
                ("send_photo", "send_video", "send_animation",
                 "send_document", "send_audio")]


def make_user(uid=555, first="Raju", last="", username="raju"):
    return types.SimpleNamespace(id=uid, first_name=first, last_name=last,
                                 username=username,
                                 full_name=(first + " " + last).strip())



def make_update(cid=12345, uid=555, chat_type="channel"):
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(id=cid, type=chat_type, title="My Test Channel"),
        from_user=make_user(uid),
        approve=lambda *a, **k: None,
        decline=lambda *a, **k: None,
    )


async def async_noop(*a, **k):
    return None


# openai-style async recorder
def recorder():
    out = []

    async def _record(*a, **k):
        out.append((a, k))
        return types.SimpleNamespace(ok=True)
    return out, _record


async def make_db():
    client = mongomock.MongoClient()
    db = M.Database("data.db", client=client)
    db.connect(legacy_owner=111111111)
    return db


async def run():
    M.db = await make_db()
    M.bot = FakeBot("main")
    M.CUSTOM_BOTS.clear()
    M.MEDIA_CACHE.clear()
    M.WELCOME_SENT.clear()
    M.REQ_SEEN.clear()
    M.GATE_CACHE.clear()
    M.STASH.clear()
    M.PANEL_MSG.clear()

    # ════════════════════════════════════════════════════════════════
    print("\n═══ 1. PREMIUM EMOJI RENDER (overlap fix) ═══")
    row = M.ChatRow(chat_id=12345, chat_type="channel", title="T",
                    owner_id=111111111,
                    welcome_text="Hey <b>{\x010\x01}</b>! ",
                    emoji=json.dumps([{"id": "5368324170671202286",
                                       "char": "🔥"}]))
    text, ents = M.render_welcome(row, "Raju", "", "", 555, "T")
    check("text has emoji char", "🔥" in text)
    check("bold entity present", any(e.type == "bold" for e in ents))
    # NO overlap: sorted by offset; each entity's span must not overlap others
    ordered = sorted(ents, key=lambda e: (e.offset, -e.length))
    ok = True
    for a, b in zip(ordered, ordered[1:]):
        if a.offset + a.length > b.offset:
            ok = False
    check("entities non-overlapping", ok, str([(e.offset, e.length, e.type) for e in ents]))

    # text with emoji inside italic + attached bold
    row2 = M.ChatRow(chat_id=1, chat_type="channel", title="T",
                     welcome_text="<i>{\x010\x01}welcome</i><b>x</b> ok",
                     emoji=json.dumps([{"id": "5368324170671202286",
                                        "char": "🔥"}]))
    t2, e2 = M.render_welcome(row2, "A", "", "", 1)
    ordered2 = sorted(e2, key=lambda e: (e.offset, -e.length))
    ok2 = all(a.offset + a.length <= b.offset for a, b in zip(ordered2, ordered2[1:]))
    check("entities non-overlapping (italic+emoji+mixed)", ok2,
          str([(e.offset, e.length, e.type) for e in e2]))

    # text-only helper returns clean text
    t_only = M.render_welcome_text_only(row2, "A", "", "", 1)
    check("text_only helper", t_only.startswith("🔥") or "🔥" in t_only, t_only[:40])

    # ════════════════════════════════════════════════════════════════
    print("\n═══ 2. WELCOME DM RELIABILITY (retry / fallback / dedupe) ═══")
    row_w = M.ChatRow(chat_id=777, chat_type="channel", title="Welcome",
                      owner_id=111111111, welcome_text="Yo <b>{name}</b>!")
    M.db.chats.update_one({"chat_id": 777},
                          {"$set": {"welcome_text": "Yo <b>{name}</b>!"}},
                          upsert=True)
    row_w = M.db.get_chat(777)

    # 2a. main bot ok
    fb = FakeBot("main")
    M.bot = fb
    ok = await M.send_welcome(row_w, "Raju", "", "", 555)
    check("send ok (main bot direct)", ok)
    check("user got message", len(fb.sent_texts()) == 1)
    check("message has entities (formatting)", fb.sent_texts()[0][2].get("entities") is not None or
          fb.sent_texts()[0][2].get("entities") == [])

    # 2b. retry on flood: first attempt raises TelegramRetryAfter
    fb2 = FakeBot("main")
    d = {"n": 0}

    async def flood(*a, **k):
        d["n"] += 1
        if d["n"] == 1:
            raise TelegramRetryAfter(method=None, message="flood", retry_after=1)
        return types.SimpleNamespace(ok=True)
    fb2.behaviors["send_message"] = flood
    M.bot = fb2
    ok = await M.send_welcome(row_w, "Raju", "", "", 556)
    check("flood retry → success", ok and d["n"] >= 2, f"attempts={d['n']}")

    # 2c. Forbidden (user blocked) → report False, no crash
    fb3 = FakeBot("main")
    fb3.behaviors["send_message"] = TelegramForbiddenError(method=None, message="blocked")
    M.bot = fb3
    ok = await M.send_welcome(row_w, "Raju", "", "", 557)
    check("forbidden → False", ok is False)

    # 2d. dedupe: same (user, chat) sent twice quickly → second skips
    fb4 = FakeBot("main")
    M.bot = fb4
    await M.send_welcome(row_w, "Raju", "", "", 558)
    n_after_1 = len(fb4.sent_texts())
    await M.send_welcome(row_w, "Raju", "", "", 558)
    n_after_2 = len(fb4.sent_texts())
    check("dedupe (2nd call skipped)", n_after_1 == 1 and n_after_2 == 1,
          f"{n_after_1}→{n_after_2}")
    M.WELCOME_SENT.clear()

    # 2e. custom bot tried FIRST, then main bot on 403
    row_c = M.ChatRow(chat_id=888, chat_type="channel", title="Custom",
                      owner_id=111111111, welcome_text="Hi {name}",
                      custom_bot_token="555:AAA_customBotToken_1234567890",
                      custom_bot_name="mycustombot")
    M.db.chats.update_one({"chat_id": 888},
                          {"$set": {"custom_bot_token": row_c.custom_bot_token,
                                    "custom_bot_name": row_c.custom_bot_name}},
                          upsert=True)
    row_c = M.db.get_chat(888)

    import main as MM
    cbot = FakeBot("custom")
    cbot.behaviors["send_message"] = TelegramForbiddenError(method=None,
                                                            message="forbidden")
    MM.get_custom_bot = lambda token: cbot
    fbm = FakeBot("main")
    M.bot = fbm
    # wait: send_welcome uses module-level get_custom_bot — actually it uses
    # the name imported... check: it calls get_custom_bot (module global)
    ok = await M.send_welcome(row_c, "Raju", "", "", 559)
    check("custom 403 → main bot fallback", ok is True)
    check("main bot actually sent", len(fbm.sent_texts()) == 1)
    check("custom bot tried once (then main bot)", len(cbot.sent_texts()) == 1)

    # 2f. custom bot BadRequest → main bot fallback
    cbot2 = FakeBot("custom")
    cbot2.behaviors["send_message"] = TelegramBadRequest(method=None,
                                                         message="Bad Request")
    MM.get_custom_bot = lambda token: cbot2
    fbm2 = FakeBot("main")
    M.bot = fbm2
    ok = await M.send_welcome(row_c, "Raju", "", "", 560)
    check("custom BadRequest → main bot fallback", ok is True and len(fbm2.sent_texts()) >= 1)

    # 2g. custom bot media: download via main bot, resend via custom bot (bytes)
    row_med = M.ChatRow(chat_id=999, chat_type="channel", title="Med",
                        owner_id=111111111,
                        welcome_text="Welcome {name}",
                        media_file_id="FILEID_999", media_kind="photo",
                        custom_bot_token="555:AAA_customBotToken_1234567890",
                        custom_bot_name="mycustombot")
    M.db.chats.update_one({"chat_id": 999},
                          {"$set": {"welcome_text": "Welcome {name}",
                                    "media_file_id": "FILEID_999",
                                    "media_kind": "photo",
                                    "custom_bot_token": row_med.custom_bot_token}},
                          upsert=True)
    row_med = M.db.get_chat(999)
    import io

    async def fake_get_file(file_id):
        return types.SimpleNamespace(file_id=file_id, file_path="media/x.jpg")
    fbm3 = FakeBot("main")
    fbm3.behaviors["get_file"] = fake_get_file
    M.bot = fbm3

    async def fake_download(fp, destination=None, **kw):
        destination.write(b"FAKEJPEG")
        return destination
    fbm3.behaviors["download_file"] = fake_download
    cbot3 = FakeBot("custom")
    MM.get_custom_bot = lambda token: cbot3
    ok = await M.send_welcome(row_med, "Raju", "", "", 561)
    check("custom bot media: photo sent via custom", ok is True)
    sends = [c for c in cbot3.calls if c[0] == "send_photo"]
    check("photo via custom bot with BufferedInputFile",
          len(sends) == 1 and "BufferedInputFile" in type(sends[0][1][1]).__name__,
          str([type(x) for x in sends[0][1]] if sends else "none"))
    check("caption passed", len(sends) == 1 and "Welcome Raju" in (sends[0][2].get("caption") or ""))

    # 2h. main bot media: direct file_id (no download)
    row_med2 = M.chat_row_copy = None
    M.db.chats.update_one({"chat_id": 999},
                          {"$set": {"custom_bot_token": ""}}, upsert=True)
    row_med2 = M.db.get_chat(999)
    fbm4 = FakeBot("main")
    M.bot = fbm4
    dl = {"n": 0}
    async def no_download(*a, **kw):
        dl["n"] += 1
    fbm4.behaviors["download_file"] = no_download
    await M.send_welcome(row_med2, "Raju", "", "", 562)
    check("main bot media: file_id direct (no download)", dl["n"] == 0 and
          any(c[0] == "send_photo" for c in fbm4.calls))

    # ════════════════════════════════════════════════════════════════
    print("\n═══ 3. ADMIN GATE ─────────────────────────────")
    check("super-admin authorized", M.is_authorized(111111111) is True)
    M.db.add_admin(222222222, added_by=111111111)
    check("db-admin authorized", M.is_authorized(222222222) is True)
    check("random user NOT authorized", M.is_authorized(987654321) is False)
    # owner with a chat is authorized
    owner_row = M.db.add_chat(424242, "channel", "Owner Chat", owner_id=333333333)
    check("chat-owner authorized", M.is_authorized(333333333) is True)
    check("owner (0-id chat) not auto", M.is_authorized(654321) is False)

    # gate middleware: unauthorized /start → AUTH_MSG, handler NOT called
    gate = M.AdminGate()
    auth_out = []
    async def handler(event, data):
        auth_out.append(True)
        return None

    async def do_gate(uid, text):
        ev = FakeMessage(uid, text)
        await gate.__call__(handler, ev, {})
        return [a[0][0] for a in ev.got_answers], auth_out

    calls, _ = await do_gate(987654321, "/start")
    check("unauthorized /start → AUTH_MSG", calls == ["🚫 You are not authorized to use this bot."], str(calls))
    check("unauthorized handler NOT called", len(auth_out) == 0)

    calls2, _ = await do_gate(111111111, "/start")
    check("authorized /start → handler runs", len(auth_out) == 1 and calls2 == [])

    # callback gate
    cbev = FakeCallbackQuery(987654321, "chats")
    await gate.__call__(handler, cbev, {})
    cb_calls = cbev.got_answers
    check("unauthorized callback → alert", len(cb_calls) == 1 and
          cb_calls[0][0][0] == "🚫 You are not authorized to use this bot." and
          cb_calls[0][1].get("show_alert") is True)

    # menu shows Admins only for super-admin
    kb_na = M.menu_kb(987654321)
    kb_sa = M.menu_kb(111111111)
    flat_na = [b.callback_data for r in kb_na.inline_keyboard for b in r]
    flat_sa = [b.callback_data for r in kb_sa.inline_keyboard for b in r]
    check("no Admins btn for normal admin", "admins" not in flat_na and "admins" in flat_sa,
          f"{flat_na} | {flat_sa}")

    # admin commands
    out = []
    async def ans(text=None, **k):
        out.append(text)

    msg = types.SimpleNamespace(from_user=make_user(111111111), text="/addadmin 444444444",
                                reply_to_message=None, answer=ans)
    async def fake_get_chat(handle):
        return types.SimpleNamespace(id=444444444, first_name="N4", title=None)
    old_ge = M.bot.get_chat
    M.bot.get_chat = fake_get_chat
    await M.cmd_addadmin(msg)
    check("addadmin works", M.db.is_admin(444444444) is True, str(out))

    await M.cmd_rmadmin(types.SimpleNamespace(from_user=make_user(111111111),
                                              text="/rmadmin 444444444",
                                              reply_to_message=None, answer=ans))
    check("rmadmin works", M.db.is_admin(444444444) is False)

    # non-super cannot addadmin
    out2 = []
    async def ans2(t=None, **k):
        out2.append(t)
    await M.cmd_addadmin(types.SimpleNamespace(from_user=make_user(222222222),
                                               text="/addadmin 555555555",
                                               reply_to_message=None, answer=ans2))
    check("non-super addadmin blocked", out2 and "⛔" in out2[0])

    # ════════════════════════════════════════════════════════════════
    print("\n═══ 4. PER-CHAT CUSTOM BOT (Channel Help style) ═══")
    # panel text/kb shows custom bot
    pt = M.panel_text(owner_row)
    pk = M.panel_kb(owner_row)
    cb_btns = [b for r in pk.inline_keyboard for b in r if b.callback_data.startswith("cbot:")]
    check("panel has custom-bot button", len(cb_btns) == 1)
    check("panel text shows custom bot OFF", "Custom bot: ❌ off" in pt, pt[-120:])

    # cbot callback → asks for token (STASH set)
    stash = {}
    async def qa(t=None, show_alert=None, **k):
        pass
    async def edit(t=None, **k):
        pass
    q = types.SimpleNamespace(from_user=make_user(333333333), data="cbot:424242",
                              message=types.SimpleNamespace(edit_text=edit,
                                                            answer=lambda *a, **k: None,
                                                            message_id=9),
                              answer=qa)
    await M.on_callback(q)
    check("STASH cbt set for owner", M.STASH.get(333333333) == ("cbt", "424242"))
    M.STASH.clear()

    # token message flow: valid token → saved + poller synced
    saved = {}

    async def get_me_ok(*a, **k):
        return types.SimpleNamespace(id=555, username="mypremiumbot",
                                     first_name="Premium")
    cbot4 = FakeBot("custom")
    cbot4.behaviors["get_me"] = get_me_ok
    MM.get_custom_bot = lambda token: cbot4
    ans3 = []
    async def ans3f(text=None, **k):
        ans3.append(text)
    token_msg = types.SimpleNamespace(
        chat=types.SimpleNamespace(id=333333333), from_user=make_user(333333333),
        text="6789012345:AAH_custom_bot_token_abcdef123456",
        caption=None, answer=ans3f)
    M.STASH[333333333] = ("cbt", "424242")
    M.PANEL_MSG[424242] = 7
    # patch edit_panel to avoid msg id issues
    old_ep = M.edit_panel
    async def fake_edit_panel(message, row):
        saved["row"] = row
    M.edit_panel = fake_edit_panel
    await M.on_private_message(token_msg)
    row_after = M.db.get_chat(424242)
    check("token saved", row_after.custom_bot_token.startswith("6789012345:") and
          row_after.custom_bot_name == "mypremiumbot",
          f"{row_after.custom_bot_token[:15]} / {row_after.custom_bot_name}")
    check("user told success", ans3 and "✅" in ans3[0])
    check("edit_panel called", saved.get("row") is not None)
    M.edit_panel = old_ep

    # invalid token → error, STASH retained for retry
    async def get_me_bad(*a, **k):
        raise Exception("Unauthorized")
    cbot5 = FakeBot("custom")
    cbot5.behaviors["get_me"] = get_me_bad
    MM.get_custom_bot = lambda token: cbot5
    ans4 = []
    async def ans4f(text=None, **k):
        ans4.append(text)
    bad_msg = types.SimpleNamespace(
        chat=types.SimpleNamespace(id=333333333), from_user=make_user(333333333),
        text="000000000:_invalid_", caption=None, answer=ans4f)
    M.STASH[333333333] = ("cbt", "424242")
    await M.on_private_message(bad_msg)
    check("invalid token → error msg", ans4 and "❌" in ans4[0], str(ans4))
    check("STASH retained for retry", M.STASH.get(333333333) == ("cbt", "424242"))
    row_bad = M.db.get_chat(424242)
    check("invalid token NOT saved", not row_bad.custom_bot_token.startswith("000000000"))

    # main bot token rejected
    ans5 = []
    async def ans5f(text=None, **k):
        ans5.append(text)
    main_msg = types.SimpleNamespace(
        chat=types.SimpleNamespace(id=333333333), from_user=make_user(333333333),
        text=os.environ["BOT_TOKEN"], caption=None, answer=ans5f)
    M.STASH[333333333] = ("cbt", "424242")
    await M.on_private_message(main_msg)
    check("main token rejected", ans5 and "⚠️" in ans5[0], str(ans5))

    # cbrm removes
    M.STASH.clear()
    q2 = types.SimpleNamespace(from_user=make_user(333333333), data="cbrm:424242",
                               message=types.SimpleNamespace(edit_text=edit,
                                                             message_id=9),
                               answer=qa)
    await M.on_callback(q2)
    row_rm = M.db.get_chat(424242)
    check("cbrm clears token", row_rm.custom_bot_token == "" and row_rm.custom_bot_name == "")

    # OTHER user cannot touch chat (ownership)
    ans_own = []
    async def qa_own(t=None, show_alert=None, **k):
        ans_own.append((t, show_alert))
    q3 = types.SimpleNamespace(from_user=make_user(999999999), data="cbot:424242",
                               message=types.SimpleNamespace(edit_text=edit,
                                                             message_id=9),
                               answer=qa_own)
    await M.on_callback(q3)
    check("non-owner cbot blocked", ans_own and "⛔" in ans_own[0][0], str(ans_own))

    # ════════════════════════════════════════════════════════════════
    print("\n═══ 5. JOIN-REQUEST PROCESSING (custom + main, dedupe) ═══")
    # chat with custom bot; _process_join with custom bot
    M.db.add_chat(666, "channel", "Six Six", owner_id=111111111)
    M.db.chats.update_one({"chat_id": 666},
                          {"$set": {"welcome_text": "Yo <b>{name}</b>!",
                                    "custom_bot_token": "555:AAA_customBotToken_1234567890",
                                    "custom_bot_name": "mycustombot"}},
                          upsert=True)
    row6 = M.db.get_chat(666)
    cjr = make_update(cid=666, uid=700)
    cbot6 = FakeBot("custom")
    MM.get_custom_bot = lambda token: cbot6
    fbm6 = FakeBot("main")
    M.bot = fbm6
    await M._process_join(row6, cjr, cbot6)
    check("custom bot sent welcome", len(cbot6.sent_texts()) >= 1)
    notifs6 = [c for c in fbm6.calls if c[0] == "send_message"]
    check("main bot sirf owner-notification (welcome nahi)",
          len(notifs6) == 1 and "requested to join" in str(notifs6[0][1]),
          str([c[1] for c in notifs6])[:120])
    check("user saved", M.db.user_chats.find_one(
        {"owner_id": 111111111, "user_id": 700, "chat_id": 666}) is not None)
    notif = [c for c in fbm6.calls if c[0] == "send_message"]
    check("owner notified via main bot", len(notif) >= 1)

    # second event same user+chat → skipped
    n_notif = len([c for c in fbm6.calls if c[0] == "send_message"])
    await M._process_join(row6, make_update(cid=666, uid=700), fbm6)
    n_notif2 = len([c for c in fbm6.calls if c[0] == "send_message"])
    check("duplicate event skipped (no 2nd welcome/notif)", n_notif == n_notif2)

    # custom bot NotAdmin path: main bot fallback when custom fails?
    # (covered in 2e)

    # auto-approve via custom bot (approve=1)
    M.db.chats.update_one({"chat_id": 666}, {"$set": {"approve": 1}}, upsert=True)
    row6 = M.db.get_chat(666)
    M.REQ_SEEN.clear()
    cbot7 = FakeBot("custom")
    MM.get_custom_bot = lambda token: cbot7
    fbm7 = FakeBot("main")
    M.bot = fbm7
    await M._process_join(row6, make_update(cid=666, uid=701), cbot7)
    appr = [c for c in cbot7.calls if c[0] == "approve_chat_join_request"]
    check("auto-approve via custom bot", len(appr) == 1 and appr[0][1][0] == 666)
    M.db.chats.update_one({"chat_id": 666}, {"$set": {"approve": 0}}, upsert=True)

    # join request main-bot path (on_join_request with unclaimed chat auto-add)
    # set chat 777 to a NEW chat not in DB to test auto-add
    cid_new = 31337
    upd = make_update(cid=cid_new, uid=702)
    fbm8 = FakeBot("main")
    M.bot = fbm8
    import main as M2
    await M.on_join_request(upd)
    row_new = M.db.get_chat(cid_new)
    check("unclaimed chat auto-added", row_new is not None)
    check("owner 0 (unclaimed)", row_new.owner_id == 0)
    M.db.remove_chat(cid_new)

    # ── "admins" callback (super-admin) ─────────────────────────────
    M.db.add_admin(222222222, added_by=111111111)
    edits = []
    async def edit_txt(t=None, **k):
        edits.append(t)
    qa4 = types.SimpleNamespace(from_user=make_user(111111111),
                                data="admins",
                                message=types.SimpleNamespace(
                                    edit_text=edit_txt, message_id=5,
                                    answer=lambda *a, **k: None),
                                answer=async_noop)
    await M.on_callback(qa4)
    check("admins callback shows list", edits and "Bot Admins" in edits[0]
          and "222222222" in edits[0], str(edits)[:80])

    # ── non-super click "admins" → alert ────────────────────────────
    al = []
    async def qa5(t=None, show_alert=None, **k):
        al.append(t)
    qa5q = types.SimpleNamespace(from_user=make_user(222222222), data="admins",
                                 message=types.SimpleNamespace(
                                     edit_text=edit_txt, message_id=5,
                                     answer=lambda *a, **k: None),
                                 answer=qa5)
    await M.on_callback(qa5q)
    check("non-super admins callback blocked", al and "⛔" in al[-1], str(al))

    # ── cmd_start: authorized owner gets menu ────────────────────────
    start_answers = []
    async def start_ans(text=None, reply_markup=None, **k):
        start_answers.append((text, reply_markup))
    st = types.SimpleNamespace(from_user=make_user(333333333),
                               chat=types.SimpleNamespace(id=333333333),
                               text="/start",
                               answer=start_ans,
                               reply_to_message=None)
    await M.cmd_start(st)
    flat_st = []
    if start_answers and start_answers[0][1]:
        flat_st = [b.callback_data
                   for r in start_answers[0][1].inline_keyboard for b in r]
    check("cmd_start works (owner menu)", "chats" in flat_st)
    check("no admins btn for non-super owner", "admins" not in flat_st)

    # ════════════════════════════════════════════════════════════════
    print("\n═══ 5b. GROUPS + NO-CUSTOM-BOT (pehle jaisa normal flow) ═══")
    # ── GROUP (supergroup) + custom bot set → custom bot se welcome ──
    M.db.add_chat(5555, "supergroup", "Private Group", owner_id=111111111)
    M.db.chats.update_one({"chat_id": 5555}, {"$set": {
        "welcome_text": "Group me welcome <b>{name}</b>!",
        "custom_bot_token": "555:AAA_customBotToken_1234567890",
        "custom_bot_name": "mycustombot"}}, upsert=True)
    row_g = M.db.get_chat(5555)
    g_cjr = make_update(cid=5555, uid=800, chat_type="supergroup")
    g_cbot = FakeBot("custom")
    MM.get_custom_bot = lambda token: g_cbot
    g_main = FakeBot("main")
    M.bot = g_main
    M.REQ_SEEN.clear()
    await M._process_join(row_g, g_cjr, g_cbot)
    check("GROUP + custom bot: welcome custom bot se",
          len(g_cbot.sent_texts()) >= 1, str(g_cbot.sent_texts()))
    check("GROUP + custom bot: user saved",
          M.db.user_chats.find_one(
              {"owner_id": 111111111, "user_id": 800, "chat_id": 5555}) is not None)
    check("GROUP + custom bot: owner notified (main bot)",
          any("requested to join" in str(c[1]) for c in g_main.calls))

    # ── GROUP + NO custom bot → main bot se normal welcome ──
    M.db.chats.update_one({"chat_id": 5555}, {"$set": {"custom_bot_token": ""}},
                          upsert=True)
    row_g2 = M.db.get_chat(5555)
    g2_main = FakeBot("main")
    M.bot = g2_main
    M.REQ_SEEN.clear()
    M.WELCOME_SENT.clear()
    await M._process_join(row_g2, make_update(cid=5555, uid=801,
                                              chat_type="supergroup"), g2_main)
    sent = g2_main.sent_texts()
    check("GROUP + NO custom bot: main bot se welcome (pehle jaisa)",
          any("Group me welcome" in str(c[1]) + str(c[2]) for c in sent),
          str([c[1][1] for c in sent])[:100])
    check("GROUP + NO custom bot: user saved",
          M.db.user_chats.find_one(
              {"owner_id": 111111111, "user_id": 801, "chat_id": 5555}) is not None)

    # ── CHANNEL + NO custom bot → main bot se welcome ──
    M.db.add_chat(5556, "channel", "Private Channel", owner_id=111111111)
    M.db.chats.update_one({"chat_id": 5556}, {"$set": {
        "welcome_text": "Channel me welcome <b>{name}</b>!"}}, upsert=True)
    row_c2 = M.db.get_chat(5556)
    c2_main = FakeBot("main")
    M.bot = c2_main
    M.REQ_SEEN.clear()
    M.WELCOME_SENT.clear()
    await M._process_join(row_c2, make_update(cid=5556, uid=802,
                                              chat_type="channel"), c2_main)
    sent2 = c2_main.sent_texts()
    check("CHANNEL + NO custom bot: main bot se welcome",
          any("Channel me welcome" in str(c[1]) + str(c[2]) for c in sent2),
          str([c[1][1] for c in sent2])[:100])

    # ── custom bot poller: group ke liye bhi same update type aata hai ──
    # (chat_join_request update type channel/group dono ke liye same hai —
    #  _process_join me chat_type ka koi branch hi nahi hai)
    check("_process_join me chat_type branch nahi (dono ke liye same code)",
          "supergroup" not in M._process_join.__code__.co_names and
          "chat_type" not in M._process_join.__code__.co_varnames)

    # ════════════════════════════════════════════════════════════════
    print("\n═══ 6. BROADCAST + callback basics still work ═══")
    # make sure nothing broke: users list + broadcast to single user
    M.db.upsert_user(111111111, 123456, "T", "", "t")
    count = M.db.count_users(111111111)
    check("count_users works", count >= 1, count)

    print("\n──────────────────────────────")
    print(f"PASS: {PASS}   FAIL: {FAIL}")
    if FAILED:
        print("FAILED:", *FAILED, sep="\n  - ")
    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(run())
    sys.exit(0 if ok else 1)
