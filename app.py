"""
Worker Hub + Backup Browser — ONE server, ONE bot.

  hub_app.py      Worker Hub: control bot (password + 2FA), PC worker manager, Telegram accounts UI (/app)
  tgweb.py/tgui.py  Telegram accounts engine + web UI (part of Worker Hub)
  storage_app.py  Backup Browser (tgstorage): browse / stream the backup groups, backup API (/hapi)

Both halves run in this one process. The same bot token is used by both:
  • Worker Hub talks to the Bot API (getUpdates) and owns the main menu / login.
  • Backup Browser keeps its Pyrogram (MTProto) bot client for reading/streaming group files.
  • Whoever is logged in to the Hub bot (password + 2FA) is automatically trusted by Backup Browser's bot commands.
A tiny path router sends each web request to the right half (no URL clashes, separate cookies).
"""
import os, sys, signal


def _alias(a, b):
    va, vb = os.environ.get(a, "").strip(), os.environ.get(b, "").strip()
    if va and not vb:
        os.environ[b] = va
    elif vb and not va:
        os.environ[a] = vb


# One set of credentials is enough — either spelling works.
for _a, _b in (("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"), ("MONGO_URI", "MONGODB_URI"),
               ("API_ID", "TG_API_ID"), ("API_HASH", "TG_API_HASH"), ("WEB_URL", "PUBLIC_URL")):
    _alias(_a, _b)

_missing = [k for k in ("BOT_TOKEN", "API_ID", "API_HASH", "WEB_PASSWORD", "ADMIN_PASSWORD",
                        "COLAB_SECRET", "PANEL_PASSWORD") if not os.environ.get(k, "").strip()]
if _missing:
    print("[config] Missing required environment variables: " + ", ".join(_missing), flush=True)
    sys.exit(1)

import storage_app          # noqa: E402  (Backup Browser)
import hub_app              # noqa: E402  (Worker Hub)

# One login for the whole bot: Hub's admin list decides who may use Backup Browser's bot commands.
storage_app.ADMIN_CHECK = hub_app.AUTH.is_admin
storage_app.ADMIN_IDS = hub_app.AUTH.admin_ids
hub_app.STORAGE = storage_app

# ── path router ──────────────────────────────────────────────────────────
_HUB_EXACT = {"/health", "/api/sync", "/app"}
_HUB_PREFIX = ("/app/", "/i/", "/f/")


def _is_hub(path):
    return path in _HUB_EXACT or path.startswith(_HUB_PREFIX)


def application(environ, start_response):
    target = hub_app.app if _is_hub(environ.get("PATH_INFO", "")) else storage_app.app
    return target(environ, start_response)


def _on_sigterm(signum, frame):
    try:
        import tgweb
        if tgweb.M:
            try:
                tgweb.M.call(tgweb.M.stop_all(), timeout=5)
            except Exception:
                pass
            tgweb.M._release()
    except Exception as e:
        print(f"[shutdown] hub: {e}", flush=True)
    storage_app._on_sigterm(signum, frame)      # stops Pyrogram, saves index snapshot, frees lock, exits


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_sigterm)
    storage_app.start_background()
    hub_app.start_background()
    from waitress import serve
    serve(application, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
          threads=40,                    # every worker PC holds one long-poll connection
          connection_limit=300, channel_timeout=300, cleanup_interval=30,
          send_bytes=1, ident="web")
