# Worker Hub + Backup Browser (एकच server, एकच bot)

दोन्ही repo एका process मध्ये चालतात:

| File | काम |
|---|---|
| `app.py` | entrypoint — दोन्ही भाग सुरू करतो, web request योग्य भागाकडे पाठवतो |
| `hub_app.py` | Worker Hub: control bot (password + 2FA), PC worker manager (`/api/sync`, `/i`, `/f`) |
| `tgweb.py`, `tgui.py` | अनेक Telegram accounts चा engine + Telegram-सारखा UI (`/app`) |
| `storage_app.py` | Backup Browser (tgstorage): groups browse/stream, backup API (`/hapi/...`), website login |
| `files/` | PC installer / worker / watermark (जुना code तसाच) |

## एकच bot कसा चालतो
* **Worker Hub** = bot चं main menu + login (Bot API). कुणीही bot ला message केलं की password (+2FA) मागतो.
* **Backup Browser** = त्याच token वर Pyrogram client (group files वाचणं/stream करणं).
* **एकदा Hub मध्ये login** केलं की Backup Browser चे commands पण चालतात (वेगळा owner check नाही).
  अनोळखी माणसाला Backup Browser काहीच उत्तर देत नाही.
* Bot menu → **📦 Storage**: website login link (एकदाचा, 2FA लागतो), Telegram user-account login, status.
  Commands: `/userlogin /userlogout /usercancel /unblock /status`.
* `/start` = Hub चा menu. (`/start <token>` = Backup Browser website चा "bot ने login" approve.)

## URLs
| Path | कुणाचं |
|---|---|
| `/`, `/login`, `/browse`, `/api/*`, `/download`, `/stream`, `/hapi/*` | Backup Browser |
| `/app` (+ `/app/...`) | Telegram accounts UI (bot menu → Telegram Accounts → UI उघडा) |
| `/api/sync`, `/i/<tok>`, `/f/<tok>/...` | Worker PCs |
| `/healthz`, `/health` | `ok` — health check / heartbeat साठी **`/healthz`** वापरा |

> जुना Hub heartbeat `/` वर होता; आता `/` Backup Browser चा आहे (login वर redirect). Pinger `/healthz` वर लावा.

## Environment variables
**आवश्यक** (नसेल तर app सुरू होतच नाही आणि कमतरता print करतो):

| Key | अर्थ |
|---|---|
| `BOT_TOKEN` (किंवा `TELEGRAM_BOT_TOKEN`) | एकच bot |
| `API_ID`, `API_HASH` (किंवा `TG_API_ID`, `TG_API_HASH`) | my.telegram.org |
| `WEB_PASSWORD` | Backup Browser website password (किमान 10 अक्षरं) |
| `ADMIN_PASSWORD` | bot login password (किमान 10 अक्षरं) |
| `COLAB_SECRET`, `PANEL_PASSWORD` | Worker PCs |

**जोरदार सुचवलेले:** `MONGO_URI` (किंवा `MONGODB_URI`) — Koyeb/Render free वर restart नंतर index, admin login, accounts टिकण्यासाठी ·
`SESSION_KEY` (Telegram accounts engine साठी, Fernet key) · `ADMIN_TOTP_SECRET` (2FA) · `SECRET_KEY` (64 random) ·
`WEB_URL` (public https URL — `PUBLIC_URL` आपोआप तोच) · `BOT_NAME` · `BACKUP_GROUP_ID` · `OWNER_CHAT_ID`
(Backup Browser "send to bot" साठी; रिकामं असेल तर पहिला admin).

बाकी optional env जुन्या दोन्ही README प्रमाणेच (`BW_LIMIT_GB`, `MAX_ACCOUNTS`, `DISK_CACHE_GB`, `STEALTH_TRIGGER`,
`LOCKOUT_HOURS`, `POLL_INTERVAL`, `CACHE_MAX_GB`, `BACKUP_TOKEN` …). `BACKUP_API.md` पहा.

Secret बनवण्याच्या commands:
* `SESSION_KEY`: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`
* `ADMIN_TOTP_SECRET`: `python -c "import secrets,base64;print(base64.b32encode(secrets.token_bytes(20)).decode())"`

## Deploy (Koyeb / Render / Docker)
1. हा folder private GitHub repo मध्ये टाका (`Dockerfile` वरच्या पातळीवर).
2. Docker service बनवा, port = `PORT` (default 8080), health check path `/healthz`.
3. वरचे env टाका. Bot ला backup group मध्ये admin करा (Post messages + Manage topics).
4. Bot ला message करा → password → 2FA code → menu.

## लक्षात ठेवा
* एका वेळी एकच instance (Telegram sessions / Mongo lease) — redeploy वेळी जुना बंद होईपर्यंत नवा थांबतो.
* RAM: Backup Browser + Telegram accounts (प्रत्येकी ~30-50 MB). Free 512 MB वर `MAX_ACCOUNTS` कमी ठेवा.
* Free tier ची disk ephemeral आहे — म्हणूनच Mongo आवश्यक.
