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

## Files
- app.py              : server + Telegram bot (password login)
- files/worker.py     : तुमचा जुना worker code
- files/watermark.py  : PC manager
- files/install.ps1   : PC installer
