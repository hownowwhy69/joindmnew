# 🤖 Welcome Joiner Bot (Telegram) — MULTI-USER

**Koi bhi user bot ko start karke apne private channels/groups ke liye use kar
sakta hai — har user ka data bilkul separate!**

Ye bot **private Telegram channels aur groups** ke join requests par har user ko
**PM** me **welcome message** bhejta hai — text + photo/video/GIF + inline
buttons ke saath. Bot request ko **approve NAHI karta** (approve owner manually
karta hai), aur har requester ka **user ID save** hota hai — jisse owner **📣
broadcast** se apne sabhi users ko ek saath message bhej sakta hai.

> 🧑‍🤝‍🧑 **Multi-user:** Bot ko **koi bhi** start kar sakta hai. Har user sirf
> **apni chats, apne saved users aur apne broadcasts** dekhta hai — kisi aur ka
> data kisi ko nahi dikhta. Chat **usi user ke naam** register hoti hai jo use
> add karta hai (ya jo bot ko admin banata hai).

> ✨ **Premium emojis support** — Welcome text me premium/custom emojis (Channel
> Help jaisa) paste karo, bot unhe **real premium emojis** ke roop me har user ko
> bhejega. Plus **bold/italic/underline/strike/code/spoiler** formatting bhi kaam
> karti hai ek hi message me.

---

## 📋 Features

| Feature | Detail |
|---|---|
| 🧑‍🤝‍🧑 **Multi-user** | Koi bhi use kar sakta hai — har user apni chats/users dekhta hai, total privacy |
| 💬 PM welcome WITHOUT approving | Bot join request ko **approve kiye bina** user ko PM me welcome karta hai (approve owner khud karta hai) |
| 💾 User IDs saved (per-user) | Har user jo owner ki chat me join request bhejta hai — owner ke account me save |
| 📣 Broadcast (per-user) | Owner apne saved users ko **ek saath message** (text/photo/video/GIF/file/audio) |
| 📝 Welcome text | Placeholders: `{name} {first} {last} {username} {chat} {id} {date}` |
| ✨ Premium custom emojis | Channel-Help-jaisi premium/animated emojis welcome me |
| 🅱️ Formatting | `<b>` `<i>` `<u>` `<s>` `<code>` `<tg-spoiler>` etc. |
| 🖼 Welcome media | Photo / Video / GIF / File / Audio — text caption ke saath |
| 🔘 Inline buttons | Multiple rows, multiple buttons (`Text \| https://link`) |
| 📚 Multiple chats | Ek bot me unlimited channels + groups |
| 📨 Owner log | Har request par aapko PM: naam, username, channel — ✅ Approve / ❌ Decline buttons ke saath |
| 🧪 Preview | Preview button se khud dekho user ko kya milega |
| 🚫 /stopbroadcast | Chalu broadcast beeche me rok sakte ho |

---

## 🚀 Setup (5 minute)

### Step 1 — Python + files

```bash
# requirements install karo (aiogram + dotenv + pymongo)
pip install -r requirements.txt
```

### Step 2 — Bot banana (@BotFather)

1. Telegram me **@BotFather** kholo → `/newbot` → naam do
2. Milne wala **token** copy karo (jaise `1234567890:AAH...`)
3. **@userinfobot** ko message karo — apna **numeric user id** mil jayega
   (jaise `987654321`)

### Step 3 — MongoDB (database) setup

Bot ka saara data ab **MongoDB** me save hota hai. 2 tarike:

**A) MongoDB Atlas — FREE cloud (RECOMMENDED ✅)**
- [cloud.mongodb.com](https://cloud.mongodb.com) → free account banao
- Cluster banao (**M0 Free** — 512MB, is bot ke liye kaafi)
- **Database Access** → user/password banao
- **Network Access** → `0.0.0.0/0` allow karo (kahin se bhi connect)
- **Connect → Drivers** → URI copy karo jaise:
  `mongodb+srv://botuser:pass@cluster0.abc.mongodb.net/?retryWrites=true&w=majority`
- Ye URI `.env` me `MONGO_URI=` me daalo

> 🌟 **Atlas ka sabse bada fayda:** data **cloud me** hai — aapka VPS uda bhi jaye
> ya data.db jaisi koi file delete ho, **data kabhi nahi khota!** Naye VPS pe
> sirf bot chalao, sab users/chats wapas aa jayenge.

**B) Local VPS par MongoDB**
```bash
# Ubuntu 22.04+
sudo apt install -y mongodb-org    # (ya official mongodb repo se)
sudo systemctl enable --now mongod
# ya Docker:
# docker run -d --name mongo -p 27017:27017 mongo
```
`.env` me: `MONGO_URI=mongodb://localhost:27017`

> 📦 **Purana SQLite data?** Agar `data.db` file folder me hai, to bot pehli
> baar connect hote hi usko **khud MongoDB me import** kar dega aur file ko
> `data.db.sqlite-imported` me rename kar dega. Kuch nahi karna!

### Step 4 — `.env` file banao

`welcome-joiner-bot` folder me `.env.example` ko copy karke `.env` banao:

```bash
cp .env.example .env
```

`.env` me likho:

```env
BOT_TOKEN=1234567890:AAH.....          # BotFather wala token
ADMIN_IDS=987654321                    # OPTIONAL: super-admin (unowned chats ke liye), blank chhod sakte ho
DB_PATH=data.db
```

### Step 5 — Bot start karo

```bash
python bot.py
```

Log me dikhega: `Bot @AapkaBot started. Polling started — waiting for join requests…`

Ab telegram me apne bot ko `/start` karo.

---

## 🎛 Bot kaise use karein (commands)

| Command | Kaam |
|---|---|
| `/start` | Main menu (buttons ke saath) |
| `/add` | Naya channel/group add karna |
| `/chats` | Saare chats ki list + settings |
| `/broadcast` | 📣 **Sabhi saved users ko message bhejo** |
| `/users` | 📊 Saved users ki list |
| `/stopbroadcast` | ⏹ Chalu broadcast rok do |
| `/help` | Poori guide |
| `/cancel` | Koi bhi process cancel |

### Channel/group add karna

1. Bot ko us channel/group me **admin** banao (neeche permissions dekho)
2. `/add` karo ya menu me **➕ Add channel / group** dabao
3. **Us chat ki koi bhi message forward karo** bot ko (ya @username / t.me link /
   chat id bhejo)

Bas! Bot chat ko save karke **settings panel** khol dega.

### Panel options

- **✏️ Welcome text** — text bhejo (placeholders + formatting + **premium emojis**)
- **🖼 Welcome media** — photo/video/GIF/file/audio select karke bhejo
- **🔘 Inline buttons** — `Button text | https://link` (har line ek button,
  blank line = nayi row)
- **🧪 Preview** — khud dekho welcome kaisa lagega
- **✅/⛔ Auto-approve** — 💡 *Default OFF hai* = bot **bina approve kiye**
  message karta hai aur aap ✅/❌ buttons se approve karte ho. Agar kisi chat
  ke liye turant auto-approve chahiye to ise ON karo.
- **🗑 Remove** — chat delete

---

## 📣 Broadcast — sabhi users ko message (naya!)

Jab bhi koi user **aapke private channels/groups me** (jahan bot admin hai)
**join request** bhejta hai, bot uska **user ID save** karta hai
(`users` table me). Ab aap sabhi saved users ko ek saath message bhej sakte ho:

### 📌 Poora flow (exactly aapki requirement)

```
 user ──join request──▶ 📢 Aapka Private Channel / Group
                            (jahan BOT admin hai)
                                  │
                                  ▼
                    🤖 Bot usse PM me WELCOME karta hai
                    (approve NAHI karta — aap approve karo)
                                  │
                                  ▼
                    💾 user_id + naam + username + time SAVE
                                  │
                                  ▼
                    (jab chaaho) 📣 /broadcast ──▶ SABHI users ko
```

> ⚠️ **Bot ka apna koi join request nahi hota** — save sirf **un users ka** hota
> hai jinhone **aapke add kiye channels/groups** me join request bheji.

1. **`/broadcast`** dabao (ya menu me **📣 Broadcast**)
2. Job **message** bhejo — text, photo, video, GIF, file ya audio (sab chalta hai)
3. Bot count dikhayega → **🚀 Broadcast** button dabao
4. Har user ko wahi message + **progress** dikhega (`Broadcasting… 25/100`)

**Result summary:** kitne ko sent, kitne failed, kitne ne bot block kiya.

- **/users** — saved users ki list (naam, @username, user_id)
- **/stopbroadcast** — chalu broadcast beeche me rok do
- Blocked users automatically count hote hain (retry fees nahi)

> ⚠️ Broadcast **sirf un users ko** jaata hai jinhone aapke channels/groups me
> join request bheji thi — yahi unke user ID save hone ka kaam hai.

### Welcome text example

```
🎉 <b>Welcome {name}!</b> <tg-spoiler>Top secret link 👇</tg-spoiler>

Aapne abhi {chat} join kiya hai — {date}
Apna username: @{username}

🌟 Wapas aana mat bhoolna!
```

**Premium emojis:** Bas apne Telegram ke sticker panel se premium/animated emoji
select karke message me paste karo — bot unhe save kar lega aur **har user ko
whi premium emoji** ke saath welcome bhejega. (Sticker me nahi, message text me
emoji paste karna hai — jaise Channel Help bot karta hai.)

---

## 📢 Channel setup (important!)

Bot ko join requests MILNI chahiye, isliye:

### Private channel ke liye

1. Channel → **Settings** → **Channel Type** → **Private** banao
2. Channel → **Settings** → **Join Requests** → **ON** karo
3. Channel → **Administrators** → bot ko add karo aur ye permissions do:
   - ✅ **Invite Users (Add Subscribers)** — *sabse zaroori!*
   - ✅ Change Info
   - ✅ Post Messages (agar channel me bot se post karna ho)

### Group ke liye

1. Group → **Administrators** → bot add karo → ✅ **Invite Users**
2. Group ka **invite link** banate waqt **"Request admin approval to join"**
   tick karo (ya Group Settings me Join Requests ON karo)

> ⚠️ **Public channels** me users directly join karte hain — wahan koi "join
> request" nahi hoti, isliye bot unhe PM nahi kar sakta (Telegram ki rule hai).
> PM welcome ke liye channel **private** hona chahiye + join requests ON.

---

## ⚙️ Long-term run (VPS / 24x7)

Local PC band hone par bot band ho jayega. 24x7 ke liye koi VPS
(DigitalOcean / Hetzner etc.) ya free tier le lo:

```bash
# VPS pe
pip install -r requirements.txt
# systemd service
sudo tee /etc/systemd/system/welcome-bot.service >/dev/null <<'EOF'
[Unit]
Description=Welcome Joiner Bot
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/welcome-joiner-bot
ExecStart=/usr/bin/python3 bot.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now welcome-bot
```

---

## 💾 Data kab kab khothe? (VPS wala sawaal)

**Normal VPS restart / reboot / crash / bot restart** → ❌ Kuch nahi hota!
`data.db` ek **file hai disk par** (RAM nahi) — file apni jagah rehti hai, aur
bot start hote hi usme se sab wapas padh leta hai (chats, users, settings).

**Bot khud DB bana leta hai** → Agar `data.db` file hi gayab ho to bot **crash
nahi hota** — `CREATE TABLE IF NOT EXISTS` se nayi khali DB khud bana deta hai.
Lekin purana data tab wapas nahi aata. Isliye **backup** lagao.

**Data sach me khatam hota hai in cases me:**
- `data.db` file delete / disk wipe / VPS reset / reinstall / naya VPS (file copy nahi ki)

### 🛡 Auto-backup (ab built-in hai!)
- Bot **har baar start pe** check karta hai: 24h me ek baar saara MongoDB data
  JSON export (`backups/data_*.json`) bana leta hai — cloud me bhi rakh sakte ho
- Sirf **10 sabse naye** backups rakhta hai, purane khud delete
- **`/backup`** (super-admin) → abhi turant backup + path + restore instructions
- Config: `BACKUP_INTERVAL_HOURS` (default 24), `BACKUP_KEEP` (default 10), `BACKUP_DIR`

### ♻️ Manually restore (agar data ud gaya)
```bash
# bot roko, phir:
python main.py --restore backups/data_20260906_235515_951080.json
# bot wapas chalao — sab wapas!
```

### 📤 Extra suraksha (recommended — off-VPS backup)
Hafte me ek baar backup ko VPS se bahar le jao (crash se bachav):
```bash
# cron: roz raat 3 baje VPS ke andar copy
0 3 * * * cp /home/ubuntu/welcome-joiner-bot/backups/*.db /root/safe/ 2>/dev/null
# ya rclone se Google Drive me sync karo
rclone copy /home/ubuntu/welcome-joiner-bot/backups gdrive:bot-backups
```

---

## 🩺 Troubleshooting

**User ko message jarur gaya, lekin user channel me nahi aaya** → Ye normal hai!
Bot **approve nahi karta** — aap **✅ Approve** button dabao (har request par
aapko bot ke chat me Approve/Decline buttons milte hain). Agar chaaho to kisi
chat ke panel me **Auto-approve ON** kar do.

**"Bot is not admin" ya "Could not approve"** (sirf jab auto-approve ON ho) →
Bot ko chat ke administrators me add karke **Invite Users / Add Subscribers**
permission do (bot ko dobara add karo agar already admin hai to permissions
refresh karne ke liye).

**Join request update nahi aata** → Channel private hai? **Join Requests ON** hai?
Invite link **"Request admin approval"** wala hai? Group me **join requests** enable
honi chahiye — agar users bina request ke directly join karte hain to bot ko kuch
nahi milta.

**Welcome PM nahi jata** → Telegram ke strict rules: bot sirf **join request**
wale users ko PM kar sakta hai (bina /start). Public channel / direct join = PM
possible nahi. Isliye private channel + join requests he use karo.

**Premium emoji text me dikhte nahi** → Emoji **message text me** paste karo
(bold ke andar bhi chalega). Agar sirf emoji ya sticker bhejoge to welcome me
nahi jayega. Telegram ke naye clients me custom emojis sabko dikhte hain.

**User का naam `<` ya `&` ke saath** → Bot handle kar leta hai (auto-escape).

**Ek saath bahut saare join requests** → Bot sequence me handle karta hai; error
ho to aapko PM me warning milti hai.

---

## 📁 Files

```
welcome-joiner-bot/
├── main.py             # ✅ SAB KUCH EK HI FILE ME (bot + DB + config)
├── requirements.txt    # dependencies (sirf 2 packages)
├── .env.example        # credentials template (optional)
└── README.md           # ye guide
```

**Notes**
- `data.db` apne aap ban jata hai (sab settings wahan save hoti hain)
- Bot **ek hi** — channels/groups **unlimited**
- Made with aiogram 3 + Python 3.10+

Enjoy! 🎉
