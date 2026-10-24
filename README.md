# Worker Hub (Render)

Render वर deploy करण्यासाठी तयार. Files बदलायची गरज नाही.

## Deploy
1. हा सगळा folder (आतल्या files सकट) नव्या **private** GitHub repo मध्ये upload करा.
   `Dockerfile` repo च्या सर्वात वरच्या पातळीवर दिसला पाहिजे.
2. Render → New → Web Service → तो repo → Language: **Docker** → Instance: **Free**.
3. Advanced → Health Check Path: `/healthz` (`/` आणि `/health` सुद्धा चालतात — तिन्हीवर फक्त `ok` येतं)
4. Environment variables:

| Key | Value |
|---|---|
| TELEGRAM_BOT_TOKEN | @BotFather कडून मिळालेला token |
| ADMIN_CHAT_ID | तुमचा Telegram user id (@userinfobot) |
| COLAB_SECRET | तुमची secret |
| PANEL_PASSWORD | PC च्या terminal साठी मजबूत password |
| KOYEB_URL | तुमचा job/poll server URL |

5. Heartbeat ला `https://<तुमचा-app>.onrender.com/` (किंवा `/health`) द्या.
6. Telegram मध्ये bot ला /start → "➕ नवा PC install" → मिळालेली command PC वर
   Win+R मध्ये paste → Ctrl+Shift+Enter.

## Files
- app.py              : server + Telegram bot
- files/worker.py     : तुमचा जुना worker code (secret शिवाय — env मधून घेतो)
- files/watermark.py  : PC वरचा manager (auto-start, ffmpeg, bot control)
- files/install.ps1   : PC installer (server आपोआप URL भरतो)
