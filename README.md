# Worker Hub (Render)

## Deploy
1. हा सगळा folder (आतल्या files सकट) **private** GitHub repo मध्ये upload करा.
   `Dockerfile` repo च्या सर्वात वरच्या पातळीवर दिसला पाहिजे.
2. Render → New → Web Service → तो repo → Language: **Docker** → Instance: **Free**.
3. Health Check Path: `/healthz`  (`/` आणि `/health` सुद्धा चालतात — तिन्हीवर फक्त `ok`)
4. Environment variables:

| Key | Value |
|---|---|
| TELEGRAM_BOT_TOKEN | @BotFather कडून token |
| **ADMIN_PASSWORD** | bot login password — **किमान 10 अक्षरं**, लांब आणि वेगळा |
| COLAB_SECRET | तुमची secret |
| PANEL_PASSWORD | PC च्या web terminal चा password |
| **MONGODB_URI** | Atlas URI (`mongodb+srv://user:pass@cluster.../`) — login/lockout restart नंतरही टिकतात |
| KOYEB_URL | तुमचा job/poll server URL |
| **ADMIN_TOTP_SECRET** | 2FA (Authenticator) secret — खाली बनवायची command. **जोरदार सुचवलेलं** |
| STEALTH_TRIGGER | (optional) उदा. `/xk93q` — हा शब्द माहीत नसेल तर bot अनोळखीला उत्तरच देत नाही |
| STEPUP_MINUTES | (optional) संवेदनशील कृतीसाठी code किती मिनिटं टिकतो, default 10 |
| ADMIN_CHAT_ID | **रिकामं सोडा / delete करा** (हे फक्त "कायमचे admin, password शिवाय" साठी आहे) |
| LOCKOUT_HOURS | (optional) 4 चुकीच्या प्रयत्नांनंतर block, default 24; 0 = कायमचं |
| ADMIN_SESSION_DAYS | (optional) login किती दिवस, default 30; 0 = कधीच संपत नाही |

5. Heartbeat: `https://<तुमचा-app>.onrender.com/` (किंवा `/health`)

## 2FA (Authenticator app) सेटअप
1. Secret बनवा (कुठेही Python असेल तिथे):
   `python -c "import secrets,base64;print(base64.b32encode(secrets.token_bytes(20)).decode())"`
2. Google Authenticator / Aegis / Authy → **Add → Enter a setup key** → Name: Worker Hub,
   Key: वरचा secret, Type: **Time based** → Add.
3. तोच secret Render मध्ये `ADMIN_TOTP_SECRET` म्हणून टाका. (दुसरीकडे कुठे save करू नका.)

## Bot login
- Bot ला कुणीही message केलं → "🔐 Password पाठवा" (4 प्रयत्न). बरोबर password → 6 अंकी 2FA code.
  Password + code मिळून 4 प्रयत्न; 4 चुकीचे → block (default 24 तास). दोन्ही बरोबर → admin.
- संवेदनशील कृतींसाठी (Terminal link, Job URL बदल, नवा PC install, admin काढणं) ताजा 2FA code.
  3 चुकीचे code → तो session आपोआप रद्द.
- Terminal: bot ने दिलेला **एकदाचा** link (2 मिनिटं, 90s ने chat मधून delete). Internet वरून
  login पानच दिसत नाही.
- Admin चा Telegram account गेला तरी: नव्या account ने bot ला message → password → परत admin.
- 👥 Admins मेनू: कोण login आहे ते बघा, कुणाचा access काढा, बाकी सगळे logout.

## MongoDB Atlas (free M0)
1. cloud.mongodb.com → नवा Project → **Build a Database → M0 Free**.
2. Database Access → Add user (username + password, "Read and write to any database").
3. Network Access → Add IP → **Allow access from anywhere (0.0.0.0/0)** (Render free चा IP बदलतो).
4. Connect → Drivers → URI copy करा, `<password>` च्या जागी तुमचा password टाका.


## Telegram Accounts (अनेक account, server वर कायम चालू, Telegram सारखा UI)
Bot → **👤 Telegram Accounts** → **🌐 UI उघडा (एकदाचा link)** → ☰ → **Add account** → फोन, कोड, (असेल तर) 2FA password.
Login server वर राहतो (MongoDB मध्ये encrypted) → PC बंद / browser data गेला तरी account चालू.

**आवश्यक env:** `MONGODB_URI`, `SESSION_KEY`
**API (my.telegram.org → API development tools):** `TG_API_ID`, `TG_API_HASH` = default.
प्रत्येक login ला "Default API" किंवा "Custom api_id/api_hash" निवडता येतो (तो त्या account साठी encrypted जतन).

`SESSION_KEY` (एकदाच बनवा, सुरक्षित ठिकाणी नोंदवा — हरवली तर accounts परत login करावे लागतील):
`python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`

**UI मधलं:** chat list + folders (All/Unread/Personal/Groups/Channels/Bots) + Archived, avatars, unread/mute/pin, typing,
online/last seen, read ticks, bubbles (reply, forward, edit, delete, pin, copy, select, reactions, formatting/links/spoilers),
photos/video(Range seek)/voice/audio/files/stickers(static)/GIF, polls (vote), bot inline बटन, location/contact/link preview,
album grid, in-chat + global search, chat info (bio, username, shared media/files/links/voice/music, members), new chat
(@username/phone/t.me link, contacts, join), mute/pin/archive/leave, silent + scheduled send, drafts, emoji panel,
drag-drop/paste upload, light/dark theme, desktop notifications, active sessions (terminate), profile edit, multi-account.
**नाही:** calls, secret chats, stories, voice recording, sticker sending/animated stickers, polls तयार करणं, forum topics.

**मोठ्या फाइल:** Telegram च्या मर्यादेपर्यंत — 2000 MB (Premium account: 4000 MB). Download streaming (RAM मध्ये नाही) + Range;
upload 8 MB तुकड्यांत → server disk (/tmp) → Telegram (progress + resume). Render free ची disk जागा मर्यादित असते — पहिल्यांदा
100-300 MB ने तपासा. 2 GB फाइल = ~2 GB bandwidth खर्च (BW_LIMIT_GB पहा).

| Optional env | default | अर्थ |
|---|---|---|
| BW_LIMIT_GB | 4 | महिन्याला server बाहेर जाणारा data; संपला की मोठ्या फाइल बंद, text चालू |
| MAX_ACCOUNTS | 6 | जास्तीत जास्त accounts (प्रत्येकी ~30-50 MB RAM अंदाज) |
| MAX_UPLOAD_MB | 4000 | वरची कमाल (प्रत्यक्षात Telegram ची मर्यादा: 2000, Premium 4000) |
| MAX_MEDIA_MB | 4000 | उघडता येणाऱ्या फाइलची कमाल |

सुरक्षा: account जोडणं/काढणं/UI link = 2FA code लागतो (ADMIN_TOTP_SECRET असेल तर). एका वेळी एकच instance
accounts चालवतो (Mongo lease) — दोन ठिकाणाहून एकच session चालला तर Telegram तो कायमचा रद्द करतो.
Render redeploy वेळी ~1 मिनिट accounts थांबतात, मग आपोआप परत.

## Files
- app.py              : server + control bot (password+2FA login)
- tgweb.py            : Telegram accounts engine (backend)
- tgui.py             : Telegram सारखा web UI (/app)
- files/worker.py     : तुमचा जुना worker code
- files/watermark.py  : PC manager
- files/install.ps1   : PC installer
