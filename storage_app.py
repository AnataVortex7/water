"""
Telegram Backup Browser — storage_app.py  (merged build: runs inside the same process/bot as Worker Hub, see app.py)
Browse files backed up to Telegram groups (forum topics = folders) from any browser.

v2: multiple groups (add / switch from the UI), periodic "anything new?" check
    (interval changeable from the browser), and a security hardening pass.
"""

import os, sys, re, signal, sqlite3, hashlib, hmac, secrets, time, threading, asyncio, mimetypes, ipaddress
from datetime import timedelta
from functools import wraps
from urllib.parse import quote as urlquote

from flask import (Flask, render_template_string, redirect, url_for,
                   session, request, Response, jsonify, stream_with_context)
from pyrogram import Client
from pyrogram import raw as tg_raw
from pyrogram.errors import FloodWait

# ── Config (from env vars) ───────────────────────────────────────────────
def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

API_ID          = _env_int("API_ID", 0)
API_HASH        = os.environ.get("API_HASH", "")
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
BOT_NAME        = os.environ.get("BOT_NAME", "").lstrip("@")
BACKUP_GROUP_ID = _env_int("BACKUP_GROUP_ID", 0)          # first / legacy group
OWNER_CHAT_ID   = _env_int("OWNER_CHAT_ID", 0)

# Merged build: app.py plugs Worker Hub's login (password + 2FA) in here, so ONE login unlocks the whole bot.
# Standalone (hooks left None): only OWNER_CHAT_ID is trusted, exactly like before.
ADMIN_CHECK = None      # callable(uid) -> bool
ADMIN_IDS   = None      # callable() -> iterable of admin ids

def _is_owner(uid):
    if not uid:
        return False
    if ADMIN_CHECK is not None:
        try:
            return bool(ADMIN_CHECK(uid))
        except Exception:
            return False
    return uid == OWNER_CHAT_ID

def _owner_chat():
    if OWNER_CHAT_ID:
        return OWNER_CHAT_ID
    try:
        ids = sorted(ADMIN_IDS()) if ADMIN_IDS else []
    except Exception:
        ids = []
    return ids[0] if ids else 0
WEB_PASSWORD    = os.environ.get("WEB_PASSWORD", "")
WEB_URL         = os.environ.get("WEB_URL", "http://localhost:8080")
MONGO_URI       = os.environ.get("MONGO_URI", "")      # optional: persists index across restarts
MONGO_DB        = os.environ.get("MONGO_DB", "tgbackup")
# Optional: Pyrogram session string of YOUR user account. A bot can never see files
# uploaded by ANOTHER bot, a user account can. When set, it is used to read the group.
USER_SESSION    = os.environ.get("USER_SESSION", "").strip()

# Extra groups that are always present (comma separated). Groups can also be
# added later from the web UI (they are stored in SQLite / the Mongo snapshot).
ENV_GROUP_IDS = []
for _x in [str(BACKUP_GROUP_ID)] + os.environ.get("BACKUP_GROUP_IDS", "").split(",") + [os.environ.get("BACKUP_GROUP", "")]:   # BACKUP_GROUP is always kept, UI can not remove it
    try:
        _g = int(_x.strip())
    except ValueError:
        continue
    if _g and _g not in ENV_GROUP_IDS:
        ENV_GROUP_IDS.append(_g)
LEGACY_GID = BACKUP_GROUP_ID or (ENV_GROUP_IDS[0] if ENV_GROUP_IDS else 0)

MAX_GROUPS   = 20
MIN_POLL     = 10          # seconds — lowest "check for new files" interval
MAX_POLL     = 3600
POLL_DEFAULT = min(MAX_POLL, max(MIN_POLL, _env_int("POLL_INTERVAL", 60)))
MAX_FAIL     = _env_int("MAX_FAIL", 2)           # wrong password attempts before block
BLOCK_SECS   = _env_int("BLOCK_SECS", 86400)     # 24 hours
# How many reverse proxies sit in front of this app (Koyeb/Render/Cloudflare...).
# The client IP is taken from the RIGHT side of X-Forwarded-For, so a visitor
# can't fake it. 0 = no proxy (use the socket address).
TRUST_PROXY  = _env_int("TRUST_PROXY", 1)

# ── Security: refuse obviously unsafe config ──────────────────────────────
_pw = (WEB_PASSWORD or "").strip().strip('"').strip("'")
if _pw.lower() in ("", "changeme", "yourstrongpassword123!"):
    print("[security] WEB_PASSWORD is empty / default — refusing to start. "
          "Set a strong WEB_PASSWORD env var.", flush=True)
    sys.exit(1)
if len(_pw) < 10:
    print("[security] WARNING: WEB_PASSWORD is shorter than 10 characters.", flush=True)

_sk = os.environ.get("SECRET_KEY", "")
if len(_sk) < 32 or _sk.startswith("CHANGE_THIS"):
    print("[security] WARNING: SECRET_KEY missing/weak — using a random key "
          "(everyone gets logged out on every restart). Set SECRET_KEY to 64 random chars.",
          flush=True)
    _sk = secrets.token_hex(32)
SECRET_KEY = _sk
if "localhost" in WEB_URL or "127.0.0.1" in WEB_URL:
    print("[config] WARNING: WEB_URL is localhost — the login link sent by the bot will not work "
          "from your phone. Set WEB_URL to your public https URL.", flush=True)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH  = os.path.join(DATA_DIR, "browser.db")
TMP_DIR  = os.environ.get("TMP_DL_DIR", "/tmp/tg_dl")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TMP_DIR,  exist_ok=True)

# ── Flask ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Quiet logs: hide health-check / polling noise (GET /, /login, /healthz, /api/status, favicon) ──
import logging
class _QuietAccess(logging.Filter):
    _NOISE = ('"GET / HTTP', '"GET /login HTTP', '"GET /healthz', '"GET /favicon.ico', '"GET /api/status')
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(n in msg for n in self._NOISE)
logging.getLogger("werkzeug").addFilter(_QuietAccess())
logging.getLogger("waitress").setLevel(logging.WARNING)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(days=7)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=WEB_URL.startswith("https"),
    SESSION_COOKIE_HTTPONLY=True,
    MAX_CONTENT_LENGTH=1024 * 1024,      # API bodies are tiny JSON
)
print(f"[env] groups={ENV_GROUP_IDS} | TRUST_PROXY={TRUST_PROXY} | "
      f"SECRET_KEY_from_env={'SECRET_KEY' in os.environ} | WEB_URL={WEB_URL}", flush=True)

@app.after_request
def _security_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")        # same-origin: preview iframe still works
    if resp.mimetype == "text/html":
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                     "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                     "media-src 'self' blob:; frame-src 'self'; object-src 'self'; "
                     "connect-src 'self'; base-uri 'none'; form-action 'self'; "
                     "frame-ancestors 'self'")
        h.setdefault("Cache-Control", "no-store")
    if request.path.startswith("/api/"):
        h["Cache-Control"] = "no-store"
    if WEB_URL.startswith("https"):
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp

# ── Database ─────────────────────────────────────────────────────────────
_db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def db_exec(sql, params=()):
    """Runs a write; returns the number of rows affected."""
    with _db_lock:
        conn = get_db()
        cur = conn.execute(sql, params)
        conn.commit()
        n = cur.rowcount
        conn.close()
        return n

def db_many(sql, params=()):
    with _db_lock:
        conn = get_db()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows

def db_one(sql, params=()):
    with _db_lock:
        conn = get_db()
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return row

def init_db():
    with _db_lock:
        conn = get_db()
        tcols = {r[1] for r in conn.execute("PRAGMA table_info(topics)")}
        legacy = bool(tcols) and "group_id" not in tcols
        if legacy:      # v1 (single group) database -> migrate below
            conn.executescript("""
                DROP INDEX IF EXISTS idx_files_topic;
                DROP INDEX IF EXISTS idx_files_path;
                DROP INDEX IF EXISTS idx_files_name;
                ALTER TABLE topics RENAME TO topics_old;
                ALTER TABLE files  RENAME TO files_old;
            """)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id  INTEGER PRIMARY KEY,
                title     TEXT DEFAULT '',
                source    TEXT DEFAULT 'ui',
                added_at  REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS topics (
                group_id    INTEGER NOT NULL,
                topic_id    INTEGER NOT NULL,
                name        TEXT NOT NULL,
                path        TEXT NOT NULL,
                parent_path TEXT NOT NULL DEFAULT '',
                depth       INTEGER DEFAULT 0,
                msg_count   INTEGER DEFAULT 0,
                files_indexed INTEGER DEFAULT 0,
                updated_at  REAL DEFAULT 0,
                PRIMARY KEY (group_id, topic_id)
            );
            CREATE TABLE IF NOT EXISTS files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    INTEGER NOT NULL,
                msg_id      INTEGER NOT NULL,
                topic_id    INTEGER NOT NULL,
                folder_path TEXT NOT NULL,
                filename    TEXT NOT NULL,
                size        INTEGER DEFAULT 0,
                mime_type   TEXT DEFAULT '',
                UNIQUE(group_id, msg_id, topic_id)
            );
            CREATE INDEX IF NOT EXISTS idx_files_gtopic ON files(group_id, topic_id);
            CREATE INDEX IF NOT EXISTS idx_files_gpath  ON files(group_id, folder_path);
            CREATE INDEX IF NOT EXISTS idx_files_gmsg   ON files(group_id, msg_id);
            CREATE INDEX IF NOT EXISTS idx_files_name   ON files(filename COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS login_tokens (
                token       TEXT PRIMARY KEY,
                created_at  REAL NOT NULL,
                validated   INTEGER DEFAULT 0,
                ip          TEXT DEFAULT '',
                ua          TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS blocked_ips (
                ip            TEXT PRIMARY KEY,
                attempts      INTEGER DEFAULT 0,
                blocked_until REAL DEFAULT 0
            );
        """)
        if legacy:
            conn.execute("""INSERT OR IGNORE INTO topics
                (group_id, topic_id, name, path, parent_path, depth, msg_count, files_indexed, updated_at)
                SELECT ?, topic_id, name, path, parent_path, depth, msg_count, files_indexed, updated_at
                FROM topics_old""", (LEGACY_GID,))
            conn.execute("""INSERT OR IGNORE INTO files
                (group_id, msg_id, topic_id, folder_path, filename, size, mime_type)
                SELECT ?, msg_id, topic_id, folder_path, filename, size, mime_type
                FROM files_old""", (LEGACY_GID,))
            conn.execute("DROP TABLE topics_old")
            conn.execute("DROP TABLE files_old")
            old = conn.execute("SELECT value FROM meta WHERE key='scan_cursor'").fetchone()
            if old:
                conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                             (f"cursor_{LEGACY_GID}", old[0]))
            print(f"[db] migrated single-group database -> group {LEGACY_GID}", flush=True)
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(login_tokens)")}
        for col in ("ip", "ua"):
            if col not in lcols:
                conn.execute(f"ALTER TABLE login_tokens ADD COLUMN {col} TEXT DEFAULT ''")
        for g in ENV_GROUP_IDS:
            conn.execute("INSERT OR IGNORE INTO groups (group_id, title, source, added_at) "
                         "VALUES (?, '', 'env', ?)", (g, time.time()))
            conn.execute("UPDATE groups SET source='env' WHERE group_id=?", (g,))
        conn.commit()
        conn.close()

init_db()

# ── Groups ───────────────────────────────────────────────────────────────
_chan_to_gid = {}       # telegram channel id (positive) -> group id (-100...)
_topics      = {}       # gid -> {topic_id: path}
_group_ver   = {}       # gid -> counter, bumped on every index change (browser polls it)
_last_check  = {}       # gid -> time of last "anything new?" check
_last_new    = {}       # gid -> time the last new item was found

def chan_id(gid):
    return (-1000000000000 - gid) if gid < -1000000000000 else 0

def list_groups():
    return db_many("SELECT group_id, title, source FROM groups ORDER BY added_at, group_id")

def refresh_chan_map():
    _chan_to_gid.clear()
    for r in list_groups():
        c = chan_id(r["group_id"])
        if c:
            _chan_to_gid[c] = r["group_id"]

refresh_chan_map()

def req_group():
    """Group the request is about: ?g=<id> or {"g": id} in the JSON body.
    Falls back to the first group. Returns None if unknown."""
    raw = request.args.get("g")
    if raw is None:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            raw = body.get("g")
    if raw is not None and str(raw).strip() != "":
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            return None
        return gid if db_one("SELECT 1 FROM groups WHERE group_id=?", (gid,)) else None
    r = db_one("SELECT group_id FROM groups ORDER BY added_at, group_id LIMIT 1")
    return r["group_id"] if r else None

def _no_group():
    return jsonify({"ok": False, "msg": "group not found"}), 404

# ── Auth helpers ─────────────────────────────────────────────────────────
def get_ip():
    remote = request.remote_addr or ""
    ip = remote
    if TRUST_PROXY > 0:
        parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if len(parts) >= TRUST_PROXY:
            ip = parts[-TRUST_PROXY]          # rightmost = added by OUR proxy, can't be forged
    for cand in (ip, remote):
        try:
            return str(ipaddress.ip_address(cand))
        except ValueError:
            continue
    return "unknown"

def is_blocked(ip):
    row = db_one("SELECT attempts, blocked_until FROM blocked_ips WHERE ip=?", (ip,))
    if not row:
        return False
    if row["blocked_until"] > time.time():
        return True
    if row["attempts"] >= MAX_FAIL:
        db_exec("UPDATE blocked_ips SET attempts=0, blocked_until=0 WHERE ip=?", (ip,))
    return False

def record_fail(ip):
    db_exec("""
        INSERT INTO blocked_ips (ip, attempts, blocked_until)
        VALUES (?, 1, 0)
        ON CONFLICT(ip) DO UPDATE SET
            attempts = attempts + 1,
            blocked_until = CASE
                WHEN attempts + 1 >= ? THEN ? ELSE blocked_until
            END
    """, (ip, MAX_FAIL, time.time() + BLOCK_SECS))

def unblock_ip(ip):
    db_exec("UPDATE blocked_ips SET attempts=0, blocked_until=0 WHERE ip=?", (ip,))

def check_password(pwd):
    a = hashlib.sha256((pwd or "").strip().encode()).digest()
    b = hashlib.sha256((WEB_PASSWORD or "").strip().strip('"').strip("'").encode()).digest()
    return hmac.compare_digest(a, b)

def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapped

def require_api(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"ok": False, "msg": "login required"}), 401
        if request.method == "POST" and request.headers.get("X-Requested-With") != "fetch":
            return jsonify({"ok": False, "msg": "bad request"}), 400
        return f(*args, **kwargs)
    return wrapped

def _clean(s, n=120):
    """Make untrusted text safe to show inside a Telegram markdown message."""
    return re.sub(r"[`*_\[\]\r\n]", "", str(s or ""))[:n] or "?"

# ── Telegram client ──────────────────────────────────────────────────────
_tg_loop   = None
_tg_client = None
_tg_user   = None      # optional user-account client (reads the group)
_warmed    = False
_tg_ready  = threading.Event()

def _load_session():
    """Saved bot session string (Mongo, or SESSION_STRING env). Reusing it
    avoids auth.ImportBotAuthorization on every restart — that call is what
    Telegram flood-limits when you redeploy several times."""
    env = os.environ.get("SESSION_STRING", "").strip()
    if env:
        return env
    if not MONGO_URI:
        return ""
    try:
        from pymongo import MongoClient
        db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)[MONGO_DB]
        doc = db["session"].find_one({"_id": "bot"})
        return (doc or {}).get("session_string", "")
    except Exception as e:
        print(f"[session] load failed: {e}", flush=True)
        return ""

def _save_session(session_string):
    if not MONGO_URI or not session_string:
        return
    try:
        from pymongo import MongoClient
        db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)[MONGO_DB]
        db["session"].replace_one({"_id": "bot"},
                                  {"_id": "bot", "session_string": session_string},
                                  upsert=True)
        print("[session] saved", flush=True)
    except Exception as e:
        print(f"[session] save failed: {e}", flush=True)

# ── One Telegram session user at a time (rolling deploys) ────────────────
# On Koyeb the NEW instance starts (and is "healthy" for the web) while the OLD
# one is still running for a while. Using the same Telegram session in both gives
# AUTH_KEY_DUPLICATED. So: a lease in MongoDB. The old instance keeps refreshing it
# (every 10s) and releases it on SIGTERM; the new instance waits until the lease is
# free (or stale) BEFORE it connects to Telegram. The web UI keeps working meanwhile.
INSTANCE_ID = secrets.token_hex(6)
LOCK_STALE = 35               # seconds without heartbeat -> old instance is dead
_lock_started = False

def _lock_coll():
    from pymongo import MongoClient
    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)[MONGO_DB]["lock"]

def _acquire_lock(max_wait=420):
    global _lock_started
    if not MONGO_URI:
        return
    try:
        coll = _lock_coll()
        t0 = time.time()
        while True:
            doc = coll.find_one({"_id": "tg"})
            age = time.time() - (doc or {}).get("ts", 0)
            if not doc or doc.get("owner") == INSTANCE_ID or age > LOCK_STALE:
                break
            if time.time() - t0 > max_wait:
                print("[lock] old instance still alive after waiting — continuing anyway", flush=True)
                break
            _index_status["status"] = f"waiting for old instance to stop ({int(time.time() - t0)}s)"
            time.sleep(3)
        coll.replace_one({"_id": "tg"}, {"_id": "tg", "owner": INSTANCE_ID, "ts": time.time()}, upsert=True)
        print(f"[lock] acquired by {INSTANCE_ID} after {int(time.time() - t0)}s", flush=True)
    except Exception as e:
        print(f"[lock] unavailable ({e}) — continuing without it", flush=True)
        return
    if not _lock_started:
        _lock_started = True
        threading.Thread(target=_lock_heartbeat, daemon=True, name="lock_hb").start()

def _lock_heartbeat():
    while True:
        time.sleep(10)
        try:
            _lock_coll().update_one({"_id": "tg", "owner": INSTANCE_ID}, {"$set": {"ts": time.time()}})
        except Exception as e:
            print(f"[lock] heartbeat failed: {e}", flush=True)

def _release_lock():
    if not MONGO_URI:
        return
    try:
        _lock_coll().delete_one({"_id": "tg", "owner": INSTANCE_ID})
    except Exception:
        pass

def _on_sigterm(signum, frame):
    """Deploy/stop: log out of Telegram cleanly and free the lease so the NEW instance can start at once."""
    print("[shutdown] SIGTERM — stopping Telegram clients, releasing lock", flush=True)
    try:
        if _tg_loop:
            for c in (_tg_user, _tg_client):
                if c is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(c.stop(), _tg_loop).result(timeout=4)
                    except Exception:
                        pass
        if _snapshot_dirty and MONGO_URI:
            snapshot_save()
    except Exception as e:
        print(f"[shutdown] {e}", flush=True)
    _release_lock()
    os._exit(0)

def _delete_session():
    """Forget the saved bot session (Mongo) so the next start logs in fresh with BOT_TOKEN."""
    if not MONGO_URI:
        return
    try:
        from pymongo import MongoClient
        MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)[MONGO_DB]["session"].delete_one({"_id": "bot"})
        print("[session] bad bot session deleted from MongoDB", flush=True)
    except Exception as e:
        print(f"[session] delete failed: {e}", flush=True)

def magic_login_url():
    """One-time (5 min) website login URL. Used by the bot reply AND by Worker Hub's menu button."""
    db_exec("DELETE FROM login_tokens WHERE created_at < ?", (time.time() - 900,))
    token = secrets.token_urlsafe(32)
    db_exec("INSERT INTO login_tokens (token, created_at, validated, ip, ua) VALUES (?, ?, 2, 'bot', 'bot')",
            (token, time.time()))
    return f"{WEB_URL.rstrip('/')}/magic_login?t={token}"

async def _send_magic_link(msg):
    """Owner sent /start or /login to the bot -> reply with a one-time login button.
    (validated=2 marks a 'magic' token; opening the link logs that browser in.)"""
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    url = magic_login_url()
    # Inline URL button (no link preview, so Telegram's preview fetcher can't burn the token)
    await msg.reply(
        "🔐 **Backup Browser login**\n\n"
        "खालचं बटन दाब — login होऊन site उघडेल.\n"
        "⏰ 5 मिनिटं valid, फक्त **एकदाच** चालतं. तू मागितलं नसेल तर वापरू नकोस.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔓 Open Backup Browser", url=url)],
            [InlineKeyboardButton("👤 Telegram User Login", callback_data="ul:begin")]]),
        disable_web_page_preview=True)

async def _send_keyboard(msg):
    """Persistent button under the chat box, so no command has to be typed."""
    from pyrogram.types import ReplyKeyboardMarkup, KeyboardButton
    try:
        kb = ReplyKeyboardMarkup([[KeyboardButton("🔓 Login"), KeyboardButton("👤 User Login")]], resize_keyboard=True, is_persistent=True)
    except TypeError:
        kb = ReplyKeyboardMarkup([[KeyboardButton("🔓 Login"), KeyboardButton("👤 User Login")]], resize_keyboard=True)
    await msg.reply("👇 **🔓 Login** = website login link, **👤 User Login** = Telegram account login (files दिसण्यासाठी).", reply_markup=kb)

def run_telegram():
    global _tg_loop, _tg_client
    _tg_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_tg_loop)

    async def _start():
        global _tg_client
        from pyrogram import filters
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        _loop = asyncio.get_running_loop()
        await _loop.run_in_executor(None, _acquire_lock)      # wait for the OLD instance to stop
        sess = await _loop.run_in_executor(None, _load_session)
        print(f"[session] using saved session: {bool(sess)}", flush=True)

        _tg_client = Client(
            name="tg_browser",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            session_string=sess or None,
            in_memory=True,
            sleep_threshold=120,
        )

        # ── /start TOKEN — ask the owner to approve a web login ──────────
        @_tg_client.on_message(filters.command("start") & filters.private)
        async def on_start(client, msg):
            if not msg.from_user or not _is_owner(msg.from_user.id):
                pass                       # stay silent to strangers (hub decides what they see)
                return
            args = msg.text.split(maxsplit=1)
            token = args[1].strip() if len(args) > 1 else ""
            if not token:
                if ADMIN_CHECK is None:             # merged build: Worker Hub shows the main menu instead
                    await _send_magic_link(msg)
                    await _send_keyboard(msg)
                return
            row = db_one("SELECT * FROM login_tokens WHERE token=? AND validated=0", (token,))
            if not row:
                await msg.reply("❌ Invalid or already-used link. Request a new one from the browser.")
                return
            if time.time() - row["created_at"] > 300:
                await msg.reply("⏰ Link expired (5 min). Request a new one.")
                return
            # Anyone can send the owner a link containing THEIR token (phishing),
            # so never approve silently — show who is asking and require a tap.
            await msg.reply(
                "🔐 **Login request**\n\n"
                f"🌐 IP: `{_clean(row['ip'])}`\n"
                f"💻 Browser: `{_clean(row['ua'])}`\n\n"
                "ही request **तूच** केलीस का? नसेल तर ❌ Deny दाब.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"ok:{token}"),
                    InlineKeyboardButton("❌ Deny",    callback_data=f"no:{token}"),
                ]]))

        @_tg_client.on_message(filters.command("login") & filters.private)
        async def on_login(client, msg):
            if not msg.from_user or not _is_owner(msg.from_user.id):
                pass                       # stay silent to strangers (hub decides what they see)
                return
            await _send_magic_link(msg)

        @_tg_client.on_message(filters.command("userlogin") & filters.private)
        async def on_userlogin(client, msg):
            if not msg.from_user or not _is_owner(msg.from_user.id):
                pass                       # stay silent to strangers (hub decides what they see)
                return
            await _login_begin(msg, msg.from_user.id)

        @_tg_client.on_message(filters.command("usercancel") & filters.private)
        async def on_usercancel(client, msg):
            if not msg.from_user or not _is_owner(msg.from_user.id):
                return
            await _login_cleanup(msg.from_user.id)
            await msg.reply("🚫 User login रद्द केला.")

        @_tg_client.on_message(filters.command("userlogout") & filters.private)
        async def on_userlogout(client, msg):
            global _tg_user
            if not msg.from_user or not _is_owner(msg.from_user.id):
                return
            if _tg_user is not None:
                old, _tg_user = _tg_user, None
                try:
                    await old.stop()
                except Exception:
                    pass
            _user_status.update(ready=False, me="", error="")
            await _loop.run_in_executor(None, _delete_user_session)
            extra = "\n(USER_SESSION env variable आहे — तो तिथून काढ.)" if USER_SESSION else ""
            await msg.reply("👋 User session काढला (database मधून delete). "
                            "Telegram app → Settings → Devices मधूनही हा session terminate करू शकतोस." + extra)

        @_tg_client.on_message(filters.private & filters.text, group=1)
        async def on_text(client, msg):
            # group=1 -> runs AFTER the command handlers, so /unblock, /status etc. still work.
            t = msg.text or ""
            if t.startswith("/"):
                return
            if not msg.from_user or not _is_owner(msg.from_user.id):
                return
            if msg.from_user.id in _login_state:        # in the middle of user login
                await _login_step(msg)
                return
            if t.strip() == "👤 User Login":
                await _login_begin(msg, msg.from_user.id)
                return
            if ADMIN_CHECK is None or t.strip() == "🔓 Login":
                await _send_magic_link(msg)             # "🔓 Login" button (standalone: any text)

        async def _storage_cb_f(_, __, q):
            d = q.data.decode(errors="ignore") if isinstance(q.data, bytes) else (q.data or "")
            return d.split(":", 1)[0] in ("ul", "ok", "no")

        @_tg_client.on_callback_query(filters.create(_storage_cb_f))
        async def on_cb(client, cq):
            if not cq.from_user or not _is_owner(cq.from_user.id):
                pass
                return
            data = cq.data.decode() if isinstance(cq.data, bytes) else (cq.data or "")
            action, _, token = data.partition(":")
            if action == "ul":
                await _login_begin(cq.message, cq.from_user.id)
                await cq.answer()
                return
            if action == "ok":
                n = db_exec("UPDATE login_tokens SET validated=1 "
                            "WHERE token=? AND validated=0 AND created_at>?",
                            (token, time.time() - 300))
                text = ("✅ **Login approved!**\n\nआता ज्या browser मध्ये login page उघडलं होतं "
                        "तिथे परत जा, ते आपोआप login होईल.") if n else \
                       "⏰ Link expired / already used. Request a new one."
            elif action == "no":
                db_exec("DELETE FROM login_tokens WHERE token=?", (token,))
                text = "🚫 Login denied."
            else:
                await cq.answer()
                return
            try:
                await cq.message.edit_text(text)
            except Exception:
                pass
            await cq.answer()

        # ── /unblock [IP] ────────────────────────────────────────────────
        @_tg_client.on_message(filters.command("unblock") & filters.private)
        async def on_unblock(client, msg):
            if not msg.from_user or not _is_owner(msg.from_user.id):
                return
            args = msg.text.split(maxsplit=1)
            if len(args) < 2:
                rows = db_many(
                    "SELECT ip, blocked_until FROM blocked_ips WHERE blocked_until > ?",
                    (time.time(),)
                )
                if not rows:
                    await msg.reply("✅ No IPs are currently blocked.")
                else:
                    from datetime import datetime
                    lines = [
                        f"🚫 `{r['ip']}` — until {datetime.fromtimestamp(r['blocked_until']).strftime('%d/%m %H:%M')}"
                        for r in rows
                    ]
                    await msg.reply(
                        "**Blocked IPs:**\n" + "\n".join(lines) +
                        "\n\nUse `/unblock <IP>` to unblock."
                    )
                return
            ip = args[1].strip()
            unblock_ip(ip)
            await msg.reply(f"✅ `{_clean(ip)}` unblocked.")

        # ── /status ──────────────────────────────────────────────────────
        @_tg_client.on_message(filters.command("status") & filters.private)
        async def on_status(client, msg):
            if not msg.from_user or not _is_owner(msg.from_user.id):
                return
            topics_count = db_one("SELECT COUNT(*) as c FROM topics")["c"]
            files_count  = db_one("SELECT COUNT(*) as c FROM files")["c"]
            groups_count = db_one("SELECT COUNT(*) as c FROM groups")["c"]
            await msg.reply(
                f"📊 **Backup Browser**\n\n"
                f"👥 Groups: `{groups_count}`\n"
                f"🗂 Topics indexed: `{topics_count}`\n"
                f"📄 Files indexed:  `{files_count}`\n"
                f"👤 User session: `{'ON ' + _user_status['me'] if _user_status['ready'] else 'OFF (/userlogin)'}`\n"
                f"🔄 Indexer: `{_index_status['status']}`\n"
                f"⏱ Check every: `{get_poll_interval()}s`\n"
                f"🌐 URL: {WEB_URL}"
            )

        from pyrogram.handlers import RawUpdateHandler
        # group=-1 so it runs alongside (not instead of) the command handlers
        _tg_client.add_handler(RawUpdateHandler(_on_raw_update), group=-1)
        from pyrogram.errors import AuthKeyDuplicated, AuthKeyUnregistered
        resets = 0
        dups = 0
        while True:
            try:
                await _tg_client.start()
                break
            except AuthKeyDuplicated as e:
                # Probably the old instance is still shutting down: just wait and retry
                # (keep the session). Only after ~2 minutes treat it as dead.
                dups += 1
                if dups <= 12:
                    _index_status["status"] = f"telegram: session busy, retry {dups}/12"
                    print(f"[tg] session in use elsewhere (old instance still stopping?) — "
                          f"retry {dups}/12 in 10s", flush=True)
                    try:
                        await _tg_client.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(10)
                    continue
                resets += 1
                print("[tg] session still busy after 2 min — discarding it", flush=True)
                try:
                    await _tg_client.disconnect()
                except Exception:
                    pass
                await _loop.run_in_executor(None, _delete_session)
                from pyrogram.storage import MemoryStorage
                _tg_client.storage = MemoryStorage("tg_browser")
                sess = ""
                await asyncio.sleep(3)
            except AuthKeyUnregistered as e:
                # Saved session was revoked: throw it away, log in again with BOT_TOKEN.
                resets += 1
                if resets > 2:
                    raise
                print(f"[tg] saved bot session rejected ({type(e).__name__}) — "
                      f"logging in fresh with BOT_TOKEN. Make sure only ONE instance runs, "
                      f"and that SESSION_STRING env is not set.", flush=True)
                _index_status["status"] = "telegram: re-login with bot token"
                try:
                    await _tg_client.disconnect()
                except Exception:
                    pass
                await _loop.run_in_executor(None, _delete_session)
                from pyrogram.storage import MemoryStorage
                _tg_client.storage = MemoryStorage("tg_browser")
                sess = ""
                await asyncio.sleep(3)
            except FloodWait as e:
                wait = int(getattr(e, "value", 60)) + 5
                _index_status["status"] = f"telegram login flood wait {wait}s"
                print(f"[tg] login FloodWait {wait}s — waiting (don't redeploy)", flush=True)
                try:
                    await _tg_client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(wait)
        if not sess:
            try:
                new_sess = await _tg_client.export_session_string()
                await _loop.run_in_executor(None, _save_session, new_sess)
            except Exception as e:
                print(f"[session] export failed: {e}", flush=True)
        usess = await _loop.run_in_executor(None, _load_user_session)
        if usess:
            for attempt in range(1, 13):
                try:
                    await _start_user(usess)
                    print(f"[tg] user client ready as {_user_status['me']} (reads the group)", flush=True)
                    break
                except AuthKeyDuplicated:
                    print(f"[tg] user session in use elsewhere — retry {attempt}/12 in 10s", flush=True)
                    _user_status.update(ready=False, error="session busy (old instance stopping)")
                    await asyncio.sleep(10)
                except Exception as e:
                    _user_status.update(ready=False, error=f"{type(e).__name__}: {e}")
                    print(f"[tg] user session failed: {e} — send /userlogin to the bot", flush=True)
                    break
        asyncio.create_task(_bridge_sweeper())
        _tg_ready.set()
        print("[tg] Client ready", flush=True)
        await asyncio.Event().wait()

    try:
        _tg_loop.run_until_complete(_start())
    except Exception as e:
        import traceback
        traceback.print_exc()
        _index_status["status"] = f"telegram error: {type(e).__name__}: {e}"
        print(f"[tg] FATAL — Telegram client stopped: {e}", flush=True)

def tg(coro, timeout=120):
    """Run a coroutine on the Telegram event loop from any thread."""
    if not _tg_ready.is_set():
        coro.close()
        raise RuntimeError("Telegram अजून connect झालं नाही (status bar बघ / logs तपास)")
    return asyncio.run_coroutine_threadsafe(coro, _tg_loop).result(timeout=timeout)

def _rd():
    """Client used to READ the group: user account if configured, else the bot."""
    return _tg_user or _tg_client

async def _warm():
    """A fresh user session has an empty peer cache; loading dialogs fills it."""
    global _warmed
    if _tg_user and not _warmed:
        async for _ in _tg_user.get_dialogs():
            pass
        _warmed = True

async def _get_chat(gid):
    """Bot first (the bot must be in the group anyway to open files), user account as fallback."""
    try:
        return await _tg_client.get_chat(gid)
    except Exception:
        if not _tg_user:
            raise
        await _warm()
        return await _tg_user.get_chat(gid)

# ── Opening files: BOT does the heavy work, user account only helps briefly ─
# A bot cannot read files uploaded by ANOTHER bot. When that happens the user
# account makes an instant, server-side copy of the message inside the group
# (no data transfer, a fraction of a second); the copy is a normal user message,
# so the bot can read it and does the real download / streaming itself.
# Copies are deleted again after BRIDGE_TTL seconds of inactivity.
# Order: 1) bot reads original  2) bot reads user-made copy  3) user reads directly.
_bridge = {}                 # (gid, msg_id) -> {"id": copy_id, "ts": last use, "hold": busy-until}
_bridge_lock = None
BRIDGE_TTL = 600

def _bridge_touch(gid, msg_id, hold=0):
    b = _bridge.get((gid, msg_id))
    if b:
        b["ts"] = time.time()
        if hold:
            b["hold"] = time.time() + hold

def _bridge_release(gid, msg_id):
    b = _bridge.get((gid, msg_id))
    if b:
        b["hold"] = 0
        b["ts"] = time.time()

def _ok_msg(m):
    return bool(m) and not getattr(m, "empty", False) and getattr(m, "media", None)

async def _resolve_msg(gid, msg_id):
    """-> (client, message) that can be downloaded/streamed, or (None, None)."""
    global _bridge_lock
    key = (gid, msg_id)
    b = _bridge.get(key)
    if b:                                            # already bridged: bot reads the copy
        try:
            m = await _tg_client.get_messages(gid, b["id"])
            if _ok_msg(m):
                b["ts"] = time.time()
                return _tg_client, m
        except Exception:
            pass
        _bridge.pop(key, None)
    try:                                             # 1) bot reads the original
        m = await _tg_client.get_messages(gid, msg_id)
        if _ok_msg(m):
            return _tg_client, m
    except Exception as e:
        print(f"[open] bot read failed: {e}", flush=True)
    if not _tg_user:
        return None, None
    if _bridge_lock is None:
        _bridge_lock = asyncio.Lock()
    async with _bridge_lock:                         # 2) user makes a copy, bot reads it
        b = _bridge.get(key)
        if b:
            return _tg_client, await _tg_client.get_messages(gid, b["id"])
        try:
            await _warm()
            c = await _tg_user.copy_message(chat_id=gid, from_chat_id=gid,
                                            message_id=msg_id, disable_notification=True)
            m2 = await _tg_client.get_messages(gid, c.id)
            if _ok_msg(m2):
                _bridge[key] = {"id": c.id, "ts": time.time(), "hold": 0}
                return _tg_client, m2
            try:
                await _tg_user.delete_messages(gid, c.id)
            except Exception:
                pass
            print("[open] bot cannot read the user-made copy either — using user account", flush=True)
        except Exception as e:
            print(f"[open] bridge copy failed: {e}", flush=True)
    try:                                             # 3) last resort: user account reads directly
        mu = await _tg_user.get_messages(gid, msg_id)
        if _ok_msg(mu):
            return _tg_user, mu
    except Exception as e:
        print(f"[open] user read failed: {e}", flush=True)
    return None, None

async def _bridge_sweeper():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        dead = [(k, v) for k, v in list(_bridge.items())
                if now > max(v["ts"] + BRIDGE_TTL, v.get("hold", 0))]
        by_gid = {}
        for k, v in dead:
            _bridge.pop(k, None)
            by_gid.setdefault(k[0], []).append(v["id"])
        for gid, ids in by_gid.items():
            try:
                await (_tg_user or _tg_client).delete_messages(gid, ids)
            except Exception as e:
                print(f"[open] copy cleanup failed: {e}", flush=True)

# ── User-account login (done inside the bot chat) ────────────────────────
# Owner sends /start -> taps "👤 Telegram User Login" (or /userlogin) -> phone ->
# OTP (any separators ok: 1-3-2-2-2) -> 2FA password if enabled.
# Only the resulting session string is stored (SQLite + MongoDB), never the
# phone number / OTP / password. The user's messages with OTP / password are
# deleted from the chat right after they are read.
_user_status = {"ready": False, "me": "", "error": ""}
_login_state = {}            # owner id -> {"step", "client", "phone", "hash", "tries", "ts"}
LOGIN_TTL = 600

def _digits(s):
    return re.sub(r"\D", "", s or "")

def _load_user_session():
    if USER_SESSION:
        return USER_SESSION
    if MONGO_URI:
        try:
            from pymongo import MongoClient
            db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)[MONGO_DB]
            doc = db["session"].find_one({"_id": "user"})
            if doc and doc.get("session_string"):
                return doc["session_string"]
        except Exception as e:
            print(f"[user-session] mongo load failed: {e}", flush=True)
    row = db_one("SELECT value FROM meta WHERE key='user_session_string'")
    return row["value"] if row else ""

def _save_user_session(sess):
    meta_set("user_session_string", sess)
    if MONGO_URI:
        try:
            from pymongo import MongoClient
            db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)[MONGO_DB]
            db["session"].replace_one({"_id": "user"}, {"_id": "user", "session_string": sess}, upsert=True)
        except Exception as e:
            print(f"[user-session] mongo save failed: {e}", flush=True)
    else:
        print("[user-session] MONGO_URI not set — session is lost on restart, "
              "you will have to /userlogin again", flush=True)

def _delete_user_session():
    db_exec("DELETE FROM meta WHERE key='user_session_string'")
    if MONGO_URI:
        try:
            from pymongo import MongoClient
            MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)[MONGO_DB]["session"].delete_one({"_id": "user"})
        except Exception as e:
            print(f"[user-session] mongo delete failed: {e}", flush=True)

async def _start_user(sess):
    """(Re)start the user-account client that reads the group."""
    global _tg_user, _warmed
    from pyrogram.handlers import RawUpdateHandler
    if _tg_user is not None:
        old, _tg_user = _tg_user, None
        try:
            await old.stop()
        except Exception:
            pass
    _warmed = False
    c = Client(name="tg_user", api_id=API_ID, api_hash=API_HASH,
               session_string=sess, in_memory=True, sleep_threshold=120)
    c.add_handler(RawUpdateHandler(_on_raw_update), group=-1)
    await c.start()
    me = await c.get_me()
    _tg_user = c
    _user_status.update(ready=True, error="",
                        me=f"{me.first_name or ''} ({me.id})".strip())

async def _reindex_all():
    """After a (new) user login: wipe + rescan every group with the user account."""
    global _scan_epoch
    _scan_epoch += 1                       # tells running (bot) scans to stop
    for _ in range(120):                   # wait (max ~2 min) until they really stopped
        if not _scanning:
            break
        await asyncio.sleep(1)
    for r in list_groups():
        gid = r["group_id"]
        if gid in _scanning:
            continue
        db_exec("DELETE FROM files  WHERE group_id=?", (gid,))
        db_exec("DELETE FROM topics WHERE group_id=?", (gid,))
        meta_set(f"cursor_{gid}", 0)
        _topics[gid] = {}
        _changed(gid)
        await _full_index(gid)

async def _login_cleanup(uid):
    st = _login_state.pop(uid, None)
    if st and st.get("client"):
        try:
            await st["client"].disconnect()
        except Exception:
            pass

async def _login_begin(msg, uid):
    await _login_cleanup(uid)
    _login_state[uid] = {"step": "phone", "ts": time.time()}
    await msg.reply(
        "👤 **Telegram User Login**\n\n"
        "ज्या Telegram account ने group वाचायचा आहे त्याचा **phone number** country code सह पाठव.\n"
        "उदा. `+919876543210`\n\n"
        "रद्द करायचं असेल तर /usercancel")

async def _login_finish(msg, uid):
    st = _login_state.get(uid) or {}
    c = st.get("client")
    sess = await c.export_session_string()
    await _login_cleanup(uid)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _save_user_session, sess)
    await _start_user(sess)
    await msg.reply(
        f"✅ **User login झाला:** {_clean(_user_status['me'])}\n\n"
        "Session database मध्ये save झाला. आता सगळे groups नव्याने scan होत आहेत — "
        "website च्या खाली status bar मध्ये progress दिसेल.\n\n"
        "⚠ तुझे OTP/password चे messages मी chat मधून delete केले आहेत (नाही झाले असतील तर स्वतः delete कर).")
    asyncio.create_task(_reindex_all())

async def _login_step(msg):
    from pyrogram.errors import (PhoneCodeInvalid, PhoneCodeExpired, SessionPasswordNeeded,
                                 PasswordHashInvalid, PhoneNumberInvalid, FloodWait as _FW)
    uid = msg.from_user.id
    st = _login_state.get(uid)
    if not st:
        return
    if time.time() - st["ts"] > LOGIN_TTL:
        await _login_cleanup(uid)
        await msg.reply("⏰ Login वेळ संपला. परत /userlogin पाठव.")
        return
    st["ts"] = time.time()
    text = (msg.text or "").strip()
    try:
        if st["step"] == "phone":
            raw = re.sub(r"[^\d+]", "", text)
            d = _digits(raw)
            note = ""
            if not raw.startswith("+"):
                if len(d) == 10:
                    raw, note = "+91" + d, " (+91 धरला)"
                else:
                    raw = "+" + d
            if len(_digits(raw)) < 8:
                await msg.reply("❌ Number बरोबर नाही. country code सह पाठव, उदा. `+919876543210`")
                return
            c = Client(name=f"login{uid}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
            await c.connect()
            try:
                sent = await c.send_code(raw)
            except Exception:
                await c.disconnect()
                raise
            st.update(client=c, phone=raw, hash=sent.phone_code_hash, step="code", tries=0)
            await msg.reply(
                f"📨 Code पाठवला{note}. Telegram app मध्ये **Telegram** नावाच्या chat मध्ये बघ.\n\n"
                "Code असा पाठव **मध्ये dash टाकून**, उदा. `1-3-2-2-2` "
                "(dash/space/dot चालेल — मी फक्त आकडे घेतो).\n"
                "सरळ आकडे पाठवले तर Telegram code रद्द करू शकतो.\n\n/usercancel = रद्द")
            return

        if st["step"] == "code":
            code = _digits(text)
            try:
                await msg.delete()
            except Exception:
                pass
            if not 5 <= len(code) <= 6:
                await msg.reply("❌ Code 5 आकडी हवा (उदा. `1-3-2-2-2`). परत पाठव.")
                return
            try:
                await st["client"].sign_in(st["phone"], st["hash"], code)
            except SessionPasswordNeeded:
                st["step"] = "pwd"
                st["tries"] = 0
                await msg.reply("🔐 या account ला **2-step verification (2FA)** आहे.\n"
                                "तुझा 2FA **password** पाठव (मी तो message लगेच delete करेन).")
                return
            except PhoneCodeInvalid:
                st["tries"] += 1
                if st["tries"] >= 3:
                    await _login_cleanup(uid)
                    await msg.reply("❌ 3 वेळा चुकीचा code. परत /userlogin पाठव.")
                else:
                    await msg.reply(f"❌ चुकीचा code ({st['tries']}/3). परत पाठव, उदा. `1-3-2-2-2`")
                return
            except PhoneCodeExpired:
                await _login_cleanup(uid)
                await msg.reply("⏰ Code expire झाला. परत /userlogin पाठव.")
                return
            await _login_finish(msg, uid)
            return

        if st["step"] == "pwd":
            try:
                await msg.delete()
            except Exception:
                pass
            try:
                await st["client"].check_password(text)
            except PasswordHashInvalid:
                st["tries"] += 1
                if st["tries"] >= 3:
                    await _login_cleanup(uid)
                    await msg.reply("❌ 3 वेळा चुकीचा password. परत /userlogin पाठव.")
                else:
                    await msg.reply(f"❌ चुकीचा password ({st['tries']}/3). परत पाठव.")
                return
            await _login_finish(msg, uid)
            return
    except PhoneNumberInvalid:
        await _login_cleanup(uid)
        await msg.reply("❌ Phone number invalid आहे. परत /userlogin पाठव.")
    except _FW as e:
        await _login_cleanup(uid)
        await msg.reply(f"⏳ Telegram ने थांबवलं: {int(getattr(e, 'value', 60))} सेकंद थांब आणि परत try कर.")
    except Exception as e:
        await _login_cleanup(uid)
        print(f"[login] {type(e).__name__}: {e}", flush=True)
        await msg.reply(f"❌ Login failed: `{_clean(type(e).__name__)}` — {_clean(e, 150)}\nपरत /userlogin पाठव.")

# ── Indexer ──────────────────────────────────────────────────────────────
# Design (bots can't read history or list topics, and Telegram flood-limits
# GetMessages):
#   1. The index (topics + file name/size/msg_id) lives in SQLite and is
#      mirrored to MongoDB (MONGO_URI) so it survives restarts.
#   2. On startup (and when a group is added): restore snapshot -> scan ONLY
#      ids newer than the saved cursor of each group.
#   3. New files/topics are indexed live from incoming group updates
#      (no API calls at all).
#   4. SAFETY NET: every <poll interval> seconds (default 60, editable from the
#      browser) a tiny GetMessages call per group probes the next 100 ids after
#      the cursor — catches anything the live updates missed. No spam messages.
#   5. The browser polls /api/status and reloads the folder when the group's
#      version counter changes.
_index_status = {"status": "idle", "progress": "", "last_run": 0}
_scan_epoch = 0              # bumped when the reading client changes (user login) -> running scans restart
_scanning = set()            # gids currently doing a full catch-up scan
_scan_progress = {}          # gid -> {"done": last scanned msg id, "total": newest msg id}
_scan_lock = None            # asyncio.Lock (created on the TG loop): only ONE group scans at a time
_queued = set()              # gids waiting for their turn
_snapshot_dirty = False
_dbg = {"topic_create": 0, "empty": 0, "other_service": 0, "no_media": 0,
        "media_not_document": 0, "doc_unknown_topic": 0, "doc_indexed": 0, "doc_dup": 0}
_dbg_samples = []            # a few examples of documents that were skipped

def meta_get(key, default=0):
    row = db_one("SELECT value FROM meta WHERE key=?", (key,))
    try:
        return int(row["value"]) if row else default
    except (TypeError, ValueError):
        return default

def meta_set(key, value):
    db_exec("""
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))

def get_poll_interval():
    return max(MIN_POLL, min(MAX_POLL, meta_get("poll_interval", POLL_DEFAULT)))

def _changed(gid):
    global _snapshot_dirty
    _snapshot_dirty = True
    _group_ver[gid] = _group_ver.get(gid, 0) + 1
    _last_new[gid] = time.time()

def _norm_path(title):
    return title.replace("\\", "/").replace("//", "/").strip("/")

def _save_topic(gid, tid, title):
    path = _norm_path(title)
    parts = path.rsplit("/", 1)
    parent = parts[0] if len(parts) > 1 else ""
    depth = path.count("/")
    db_exec("""
        INSERT INTO topics (group_id, topic_id, name, path, parent_path, depth, files_indexed, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(group_id, topic_id) DO UPDATE SET
            name=excluded.name, path=excluded.path,
            parent_path=excluded.parent_path, depth=excluded.depth,
            updated_at=excluded.updated_at
    """, (gid, tid, title, path, parent, depth, time.time()))
    db_exec("UPDATE files SET folder_path=? WHERE group_id=? AND topic_id=?", (path, gid, tid))
    _topics.setdefault(gid, {})[tid] = path
    _changed(gid)
    return path

def _topic_id_of(m):
    rt = getattr(m, "reply_to", None)
    if rt is None:
        return None
    return getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)

def _handle_raw_message(gid, m):
    """Index one raw Telegram message (topic service msg or document).
    Returns True when a NEW file was added to the index."""
    if isinstance(m, tg_raw.types.MessageEmpty):
        _dbg["empty"] += 1
        return False
    if isinstance(m, tg_raw.types.MessageService):
        act = m.action
        if isinstance(act, tg_raw.types.MessageActionTopicCreate):
            _dbg["topic_create"] += 1
            _save_topic(gid, m.id, act.title)
        elif isinstance(act, tg_raw.types.MessageActionTopicEdit):
            new_title = getattr(act, "title", None)
            tid = _topic_id_of(m)
            if new_title and tid:
                _save_topic(gid, tid, new_title)
        else:
            _dbg["other_service"] += 1
        return False

    media = getattr(m, "media", None)
    if media is None:
        _dbg["no_media"] += 1
        return False
    if not isinstance(media, tg_raw.types.MessageMediaDocument):
        _dbg["media_not_document"] += 1
        return False
    doc = media.document
    if doc is None or not hasattr(doc, "attributes"):
        _dbg["media_not_document"] += 1
        return False
    tid = _topic_id_of(m)
    topics = _topics.get(gid, {})
    if tid not in topics:
        _dbg["doc_unknown_topic"] += 1
        if len(_dbg_samples) < 5:
            _dbg_samples.append({"msg_id": m.id, "topic_id_guess": tid,
                                 "reply_to": repr(getattr(m, "reply_to", None))[:300]})
        return False
    filename = f"file_{m.id}"
    for attr in doc.attributes:
        if isinstance(attr, tg_raw.types.DocumentAttributeFilename):
            filename = attr.file_name
            break
    n = db_exec("""
        INSERT OR IGNORE INTO files
            (group_id, msg_id, topic_id, folder_path, filename, size, mime_type)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (gid, m.id, tid, topics[tid], filename,
          getattr(doc, "size", 0) or 0,
          getattr(doc, "mime_type", "") or ""))
    if not n:
        _dbg["doc_dup"] += 1
        return False
    _dbg["doc_indexed"] += 1
    db_exec("UPDATE topics SET msg_count=(SELECT COUNT(*) FROM files WHERE group_id=? AND topic_id=?) "
            "WHERE group_id=? AND topic_id=?", (gid, tid, gid, tid))
    _changed(gid)
    return True

async def _on_raw_update(client, update, users, chats):
    """Live indexing: new group messages arrive as updates — zero API calls."""
    try:
        if isinstance(update, tg_raw.types.UpdateDeleteChannelMessages):
            gid = _chan_to_gid.get(update.channel_id)
            if gid is not None and update.messages:
                tids = set()
                for mid in update.messages:
                    r = db_one("SELECT topic_id FROM files WHERE group_id=? AND msg_id=?", (gid, mid))
                    if r:
                        tids.add(r["topic_id"])
                        db_exec("DELETE FROM files WHERE group_id=? AND msg_id=?", (gid, mid))
                if tids:
                    _recount(gid, tids)
                    _changed(gid)
            return
        if not isinstance(update, tg_raw.types.UpdateNewChannelMessage):
            return
        m = update.message
        peer = getattr(m, "peer_id", None)
        gid = _chan_to_gid.get(getattr(peer, "channel_id", None))
        if gid is None:
            return
        _handle_raw_message(gid, m)
        if gid not in _scanning and m.id == meta_get(f"cursor_{gid}", 0) + 1:
            meta_set(f"cursor_{gid}", m.id)
    except Exception as e:
        print(f"[live] {e}", flush=True)

# ── Persistence (MongoDB snapshot) ───────────────────────────────────────
def _mongo_coll():
    from pymongo import MongoClient
    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)[MONGO_DB]["index_snapshot"]

CHUNK = 20000

def snapshot_save():
    if not MONGO_URI:
        return
    coll = _mongo_coll()
    topics = [{k: r[k] for k in ("group_id", "topic_id", "name", "path", "parent_path",
                                 "depth", "msg_count", "updated_at")}
              for r in db_many("SELECT * FROM topics")]
    groups = [{"group_id": r["group_id"], "title": r["title"], "source": r["source"]}
              for r in list_groups()]
    cursors = {str(g["group_id"]): meta_get(f"cursor_{g['group_id']}", 0) for g in groups}
    rows = [[r["group_id"], r["msg_id"], r["topic_id"], r["folder_path"], r["filename"],
             r["size"], r["mime_type"]]
            for r in db_many("SELECT group_id, msg_id, topic_id, folder_path, filename, size, mime_type FROM files")]
    n = 0
    for i in range(0, len(rows), CHUNK):
        coll.replace_one({"_id": f"files_{n}"},
                         {"_id": f"files_{n}", "rows": rows[i:i + CHUNK]}, upsert=True)
        n += 1
    coll.delete_many({"_id": {"$in": [f"files_{k}" for k in range(n, n + 50)]}})
    coll.replace_one({"_id": "meta"},
                     {"_id": "meta", "cursors": cursors, "groups": groups, "topics": topics,
                      "nchunks": n, "saved_at": time.time()},
                     upsert=True)
    print(f"[snapshot] saved {len(groups)} groups, {len(topics)} topics, {len(rows)} files", flush=True)

def snapshot_load():
    """Restore groups + index into an empty SQLite after a restart.
    Also understands the old single-group snapshot format."""
    if not MONGO_URI:
        print("[snapshot] MONGO_URI not set — index/groups will NOT survive restarts", flush=True)
        return
    coll = _mongo_coll()
    meta = coll.find_one({"_id": "meta"})
    if not meta:
        print("[snapshot] no snapshot yet", flush=True)
        return
    for g in meta.get("groups", []):
        db_exec("INSERT OR IGNORE INTO groups (group_id, title, source, added_at) VALUES (?, ?, ?, ?)",
                (g["group_id"], g.get("title", ""), g.get("source", "ui"), time.time()))
    refresh_chan_map()
    if db_one("SELECT COUNT(*) AS c FROM topics")["c"] > 0:
        return
    old = LEGACY_GID            # v1 snapshots have no group ids
    with _db_lock:
        conn = get_db()
        conn.executemany("""
            INSERT OR REPLACE INTO topics
              (group_id, topic_id, name, path, parent_path, depth, msg_count, files_indexed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, [(t.get("group_id", old), t["topic_id"], t["name"], t["path"], t["parent_path"],
               t["depth"], t["msg_count"], t["updated_at"]) for t in meta["topics"]])
        for i in range(meta.get("nchunks", 0)):
            doc = coll.find_one({"_id": f"files_{i}"})
            if doc:
                rows = [r if len(r) == 7 else [old] + list(r) for r in doc["rows"]]
                conn.executemany("""
                    INSERT OR IGNORE INTO files
                      (group_id, msg_id, topic_id, folder_path, filename, size, mime_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, rows)
        conn.commit()
        conn.close()
    cursors = meta.get("cursors") or {str(old): meta.get("cursor", 0)}
    for g, c in cursors.items():
        meta_set(f"cursor_{g}", c)
    print(f"[snapshot] restored {len(meta['topics'])} topics, cursors={cursors}", flush=True)

def run_snapshotter():
    """Saves the snapshot at most every 2 minutes, only when something changed."""
    global _snapshot_dirty
    while True:
        time.sleep(120)
        if _snapshot_dirty and MONGO_URI:
            _snapshot_dirty = False
            try:
                snapshot_save()
            except Exception as e:
                _snapshot_dirty = True
                print(f"[snapshot] save failed: {e}", flush=True)

# ── Catch-up scan + periodic check ───────────────────────────────────────
async def _invoke_safe(req, cl=None):
    """invoke() that waits out FloodWait instead of failing.
    `cl` pins the client: access hashes are PER ACCOUNT, so a scan must use the same
    client for resolving the channel and for every GetMessages call."""
    cl = cl or _rd()
    while True:
        try:
            return await cl.invoke(req)
        except FloodWait as e:
            wait = int(getattr(e, "value", 30)) + 2
            _index_status["status"] = f"scanning (flood wait {wait}s)"
            print(f"[index] FloodWait {wait}s", flush=True)
            await asyncio.sleep(wait)

async def _input_channel(gid, cl=None):
    cl = cl or _rd()
    try:
        peer = await cl.resolve_peer(gid)
    except Exception:
        if cl is not _tg_user:
            raise
        await _warm()
        peer = await cl.resolve_peer(gid)
    return tg_raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)

async def _latest_msg_id(gid, cl=None):
    """User account: read the newest id from history. Bot: post a temp message to learn it
    (bots can't read history). Only used for the startup / new-group / re-index scan."""
    cl = cl or _rd()
    if cl is _tg_user and _tg_user is not None:
        await _warm()
        async for hm in cl.get_chat_history(gid, limit=1):
            return hm.id
        return 0
    m = await cl.send_message(gid, "⏳ indexing…")
    mid = m.id
    try:
        await cl.delete_messages(gid, mid)
    except Exception:
        pass
    return mid

async def _scan_group(gid):
    global _snapshot_dirty
    _scanning.add(gid)
    cl, ep = _rd(), _scan_epoch          # pin client for the whole scan
    try:
        ch = await _input_channel(gid, cl)
        start = meta_get(f"cursor_{gid}", 0) + 1
        latest = await _latest_msg_id(gid, cl)
        if start >= latest:
            print(f"[index] {gid}: up to date, nothing to scan", flush=True)
            return
        BATCH = 100            # Telegram max ids per GetMessages call
        found = 0
        _scan_progress[gid] = {"done": start - 1, "total": latest}
        for lo in range(start, latest + 1, BATCH):
            if _scan_epoch != ep:        # reading client changed (user login) -> stop, caller restarts
                print(f"[index] {gid}: scan aborted (client changed)", flush=True)
                return
            hi = min(lo + BATCH, latest + 1)
            _index_status["status"] = f"scanning {lo}/{latest}"
            ids = [tg_raw.types.InputMessageID(id=i) for i in range(lo, hi)]
            r = await _invoke_safe(
                tg_raw.functions.channels.GetMessages(channel=ch, id=ids), cl)
            for m in r.messages:
                if _handle_raw_message(gid, m):
                    found += 1
            meta_set(f"cursor_{gid}", hi - 1)
            _scan_progress[gid] = {"done": hi - 1, "total": latest}
            await asyncio.sleep(0.6)      # faster catch-up; FloodWait is handled by _invoke_safe
        _snapshot_dirty = True
        print(f"[index] {gid}: DEBUG counters={_dbg} samples={_dbg_samples}", flush=True)
        print(f"[index] {gid}: scan done up to msg {latest}: +{found} files, "
              f"{len(_topics.get(gid, {}))} topics", flush=True)
    finally:
        _scanning.discard(gid)
        _scan_progress.pop(gid, None)

async def _full_index(gid):
    """Queue a catch-up scan. Scans of different groups run strictly one after another
    (same Telegram account = shared flood limit), duplicates are ignored."""
    global _scan_lock
    if gid in _scanning or gid in _queued:
        return
    if _scan_lock is None:
        _scan_lock = asyncio.Lock()
    _queued.add(gid)
    try:
        async with _scan_lock:
            _queued.discard(gid)
            if not db_one("SELECT 1 FROM groups WHERE group_id=?", (gid,)):
                return                      # group was removed while waiting
            _index_status["status"] = "scanning..."
            try:
                await _scan_group(gid)
                _index_status["status"] = "done"
                _index_status["last_run"] = time.time()
                _last_check[gid] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _index_status["status"] = f"error: {type(e).__name__}: {e}"
                print(f"[index] {gid} Error: {type(e).__name__}: {e}", flush=True)
    finally:
        _queued.discard(gid)

async def _poll_group(gid):
    """Cheap 'anything new?' check: ask Telegram for the 100 message ids after
    our cursor. Existing ones are indexed; empty ones mean nothing new yet."""
    if gid in _scanning or gid in _queued or (_scan_lock is not None and _scan_lock.locked()):
        return 0                         # a scan is running -> it will catch up anyway
    cursor = meta_get(f"cursor_{gid}", 0)
    if cursor <= 0:
        # never scanned (first scan failed / group empty) -> retry the full scan
        # instead of silently doing nothing forever
        await _full_index(gid)
        return 0
    cl = _rd()
    ch = await _input_channel(gid, cl)
    found = 0
    for _ in range(10):               # at most 1000 ids per check
        ids = [tg_raw.types.InputMessageID(id=i) for i in range(cursor + 1, cursor + 101)]
        r = await _invoke_safe(tg_raw.functions.channels.GetMessages(channel=ch, id=ids), cl)
        real = [m for m in r.messages if not isinstance(m, tg_raw.types.MessageEmpty)]
        if not real:
            break
        for m in real:
            if _handle_raw_message(gid, m):
                found += 1
        cursor = max(m.id for m in real)
        meta_set(f"cursor_{gid}", cursor)
    _last_check[gid] = time.time()
    return found

_poll_wakeup = threading.Event()

def run_poller():
    """Every <interval> seconds (editable from the browser) check every group."""
    _tg_ready.wait()
    time.sleep(20)                    # let the startup catch-up scan go first
    next_at = 0.0
    while True:
        forced = _poll_wakeup.wait(timeout=3)
        _poll_wakeup.clear()
        if not forced and time.time() < next_at:
            continue
        next_at = time.time() + get_poll_interval()
        for r in list_groups():
            try:
                asyncio.run_coroutine_threadsafe(
                    asyncio.wait_for(_poll_group(r["group_id"]), 150), _tg_loop).result(timeout=160)
            except Exception as e:
                print(f"[poll] {r['group_id']}: {type(e).__name__} {e}", flush=True)

async def _fill_titles():
    for r in list_groups():
        if r["title"]:
            continue
        try:
            chat = await _get_chat(r["group_id"])
            db_exec("UPDATE groups SET title=? WHERE group_id=?",
                    (chat.title or "", r["group_id"]))
        except Exception as e:
            print(f"[groups] title for {r['group_id']}: {e}", flush=True)

def run_indexer():
    """Startup only: restore snapshot, catch up on new ids, then rely on live updates + poller."""
    try:
        snapshot_load()
    except Exception as e:
        print(f"[snapshot] load failed: {e}", flush=True)
    for r in db_many("SELECT group_id, topic_id, path FROM topics"):
        _topics.setdefault(r["group_id"], {})[r["topic_id"]] = r["path"]
    _tg_ready.wait()
    try:
        asyncio.run_coroutine_threadsafe(_fill_titles(), _tg_loop).result(timeout=120)
    except Exception as e:
        print(f"[groups] {e}", flush=True)
    for g in list_groups():
        try:
            asyncio.run_coroutine_threadsafe(_full_index(g["group_id"]), _tg_loop)   # queued, runs one by one
        except Exception as e:
            _index_status["status"] = f"error: {e}"
            print(f"[index] Error: {e}", flush=True)

def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── HTML Templates ────────────────────────────────────────────────────────
LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backup Browser — Login</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;
  padding:2rem 2.5rem;width:100%;max-width:380px}
.logo{text-align:center;font-size:2.5rem;margin-bottom:.5rem}
h2{text-align:center;font-size:1.3rem;color:#58a6ff;margin-bottom:1.5rem}
label{font-size:.85rem;color:#8b949e;display:block;margin-bottom:.4rem}
input[type=password]{width:100%;padding:.7rem 1rem;background:#0d1117;
  border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:1rem;
  outline:none;margin-bottom:1rem}
input:focus{border-color:#58a6ff}
.btn{width:100%;padding:.75rem;border:none;border-radius:8px;font-size:1rem;
  cursor:pointer;font-weight:600;margin-bottom:.6rem;letter-spacing:.02em}
.btn-pw{background:#238636;color:#fff}
.btn-pw:hover{background:#2ea043}
.btn-tg{background:#1a5276;color:#58a6ff;border:1px solid #1e6091}
.btn-tg:hover{background:#1e6091;color:#fff}
.divider{text-align:center;color:#6e7681;font-size:.85rem;margin:.6rem 0}
.err{background:#3d1a1a;border:1px solid #7d2d2d;color:#f87171;
  padding:.7rem 1rem;border-radius:8px;margin-bottom:1rem;font-size:.9rem}
.blocked{background:#2d1f00;border:1px solid #7d5a00;color:#fbbf24;
  padding:.7rem 1rem;border-radius:8px;font-size:.9rem;text-align:center}
</style>
</head>
<body>
<div class="card">
  <div class="logo">📦</div>
  <h2>Backup Browser</h2>
  {% if blocked %}
  <div class="blocked">
    ⛔ Too many wrong attempts. Blocked for 24 hours.<br><br>
    {% if client_ip %}
    <small>Your IP: <code>{{ client_ip }}</code><br>
    Send <code>/unblock {{ client_ip }}</code> to the bot.</small>
    {% endif %}
  </div>
  {% if auth_bot_name %}
  <div style="margin-top:1rem">
    <div class="divider">— or login via Telegram to unblock —</div>
    <a href="/bot_login" style="text-decoration:none">
      <button class="btn btn-tg">🤖 Login via Telegram Bot</button>
    </a>
  </div>
  {% endif %}
  {% else %}
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="POST" action="/login">
      <label>Password</label>
      <input type="password" name="password" placeholder="Enter password" autofocus>
      <button class="btn btn-pw" type="submit">🔑 Login</button>
    </form>
    {% if auth_bot_name %}
    <div class="divider">— or —</div>
    <a href="/bot_login" style="text-decoration:none">
      <button class="btn btn-tg">🤖 Login via Telegram Bot</button>
    </a>
    {% endif %}
  {% endif %}
</div>
</body>
</html>"""

BOT_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Login</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;
  padding:2rem 2.5rem;width:100%;max-width:380px;text-align:center}
h2{color:#58a6ff;margin-bottom:.75rem;font-size:1.2rem}
p{color:#8b949e;font-size:.9rem;line-height:1.6}
.btn{display:inline-block;background:#0088cc;color:#fff;padding:.75rem 1.5rem;
  border-radius:8px;text-decoration:none;font-weight:600;margin:1.2rem 0;font-size:1rem}
.btn:hover{background:#0099dd}
.spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.status{color:#6e7681;font-size:.85rem;margin-top:.5rem}
.success{color:#3fb950;font-size:1rem;font-weight:600}
</style>
<script>
const TOKEN = "{{ token }}";
let tries = 0;
function poll() {
  fetch('/check_token?t=' + TOKEN, {credentials: 'same-origin'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        document.getElementById('status').innerHTML = '<span class="success">✅ Authenticated! Redirecting...</span>';
        setTimeout(() => window.location.href = '/', 600);
      } else if (tries++ < 100) {
        setTimeout(poll, 3000);
      } else {
        document.getElementById('status').textContent = '⏰ Timed out. Please go back and try again.';
      }
    }).catch(() => { if (tries++ < 100) setTimeout(poll, 5000); });
}
window.onload = () => setTimeout(poll, 2000);
</script>
</head>
<body>
<div class="card">
  <div style="font-size:2.5rem;margin-bottom:.5rem">🤖</div>
  <h2>Login via Telegram</h2>
  <p>Click the button below and press <b>Start</b> in Telegram.<br>
  Then come back to this tab — it logs in automatically.</p>
  <a class="btn" href="{{ bot_url }}" target="_blank">Open @{{ auth_bot_name }}</a>
  <p id="status"><span class="spin">⏳</span> Waiting for Telegram authentication…</p>
  <p style="margin-top:1rem;font-size:.8rem;color:#484f58">
    This page auto-redirects once you confirm in Telegram.
  </p>
</div>
</body>
</html>"""


@app.route("/")
@require_auth
def index():
    return redirect(url_for("browse", path=""))

@app.route("/healthz")
def healthz():
    return Response("ok", mimetype="text/plain")

@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


EXPLORER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backup Explorer</title>
<style>
:root{--bg:#f3f3f3;--content:#fff;--text:#1b1b1b;--muted:#6b6b6b;--line:#e1e1e1;--hover:#eaf3fb;--sel:#cce8ff;--selb:#99d1ff;--menu:#fff;--shadow:0 8px 24px rgba(0,0,0,.18)}
@media(prefers-color-scheme:dark){:root{--bg:#202020;--content:#191919;--text:#f1f1f1;--muted:#a0a0a0;--line:#333;--hover:#2a2d31;--sel:#0b3a5e;--selb:#1a6aa8;--menu:#2b2b2b;--shadow:0 8px 24px rgba(0,0,0,.6)}}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{font:14px/1.35 "Segoe UI",system-ui,-apple-system,Roboto,sans-serif;background:var(--bg);color:var(--text);overflow:hidden}
#app{display:flex;flex-direction:column;height:100vh;height:100dvh}
.bar{display:flex;align-items:center;gap:4px;padding:6px 10px;background:var(--bg)}
.ib,.cb{background:none;border:0;color:inherit;border-radius:6px;cursor:pointer;font:inherit}
.ib{width:32px;height:32px;font-size:16px;flex:none}
.ib:hover,.cb:hover:not(:disabled){background:rgba(127,127,127,.18)}
.ib.on{background:rgba(127,127,127,.28)}
.cb{display:flex;align-items:center;gap:6px;padding:6px 10px;height:34px;white-space:nowrap}
.cb:disabled{opacity:.38;cursor:default}
.addr{flex:1;min-width:0;display:flex;align-items:center;height:34px;padding:0 6px;background:var(--content);border:1px solid var(--line);border-radius:6px;overflow-x:auto;white-space:nowrap;scrollbar-width:none}
.addr a{padding:3px 6px;border-radius:4px;cursor:pointer}
.addr a:hover{background:var(--hover)}
.addr .sep{color:var(--muted)}
#grp{max-width:180px;height:34px;padding:0 6px;border:1px solid var(--line);border-radius:6px;background:var(--content);color:inherit;font:inherit;flex:none}
#q{width:260px;height:34px;padding:0 10px;border:1px solid var(--line);border-radius:6px;background:var(--content);color:inherit;font:inherit}
.cmd{border-bottom:1px solid var(--line);overflow-x:auto;scrollbar-width:none}
.vsep{width:1px;height:20px;background:var(--line);margin:0 4px;flex:none}
.sp{flex:1}
.main{flex:1;display:flex;min-height:0;background:var(--content)}
#nav{width:240px;overflow:auto;padding:6px;border-right:1px solid var(--line);background:var(--content);flex:none}
.tn{display:flex;align-items:center;gap:2px;padding:4px 4px 4px calc(4px + var(--d,0) * 14px);border-radius:6px;cursor:pointer;white-space:nowrap}
.tn:hover{background:var(--hover)}
.tn.cur{background:var(--sel)}
.ar{width:18px;text-align:center;color:var(--muted);font-size:11px;flex:none}
.tl{overflow:hidden;text-overflow:ellipsis}
.tc[hidden]{display:none}
#content{flex:1;overflow:auto;padding:8px;outline:0}
#content.icons{display:flex;flex-wrap:wrap;align-content:flex-start;gap:4px}
.it{user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;border:1px solid transparent;cursor:default}
.it:hover{background:var(--hover)}
.it.sel{background:var(--sel);border-color:var(--selb)}
.it.cut{opacity:.45}
.icons .it{width:112px;padding:8px 4px 6px;text-align:center;border-radius:6px}
.icons .ic{width:64px;height:64px;margin:0 auto 4px}
.icons .nm{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word;font-size:13px}
.icons .sz,.icons .ty,.icons .hd{display:none}
.nm small{display:block;color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis}
.ic svg{width:100%;height:100%;display:block}
.details .hd,.details .it{display:grid;grid-template-columns:28px 1fr 110px 120px;align-items:center;gap:8px;padding:4px 8px;border-radius:4px}
.details .hd{position:sticky;top:-8px;background:var(--content);border-bottom:1px solid var(--line);color:var(--muted);z-index:2;margin:-8px -8px 4px;padding:8px 16px;border-radius:0}
.hd div{cursor:pointer}
.details .ic{width:22px;height:22px}
.details .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.details .sz,.details .ty{color:var(--muted);font-size:13px}
.empty{color:var(--muted);padding:30px;text-align:center;width:100%}
#preview{width:340px;border-left:1px solid var(--line);padding:10px;overflow:auto;flex:none}
#preview[hidden]{display:none}
#preview video,#preview img,#preview iframe,#preview audio{width:100%;border:0;border-radius:6px}
#preview video,#preview iframe{max-height:60vh;background:#000}
#preview iframe{height:60vh;background:#fff}
.pvi{margin-top:10px;font-size:13px;color:var(--muted);word-break:break-word}
.pvi b{color:var(--text);display:block;margin-bottom:2px}
.status{display:flex;align-items:center;gap:14px;padding:3px 12px;background:var(--content);border-top:1px solid var(--line);color:var(--muted);font-size:12px;height:30px;white-space:nowrap}
.status .ib{width:26px;height:24px;font-size:14px}
#menu{position:fixed;z-index:60;min-width:210px;background:var(--menu);border:1px solid var(--line);border-radius:8px;padding:4px;box-shadow:var(--shadow)}
#menu[hidden]{display:none}
.mi{padding:7px 12px;border-radius:5px;cursor:pointer;white-space:nowrap}
.mi:hover{background:var(--hover)}
.mi.dis{opacity:.4;pointer-events:none}
.mi.b{font-weight:600}
.ms{height:1px;background:var(--line);margin:4px 2px}
dialog{margin:auto;border:1px solid var(--line);border-radius:10px;background:var(--menu);color:var(--text);padding:18px;width:min(92vw,420px);box-shadow:var(--shadow)}
dialog::backdrop{background:rgba(0,0,0,.35)}
dialog h3{font-size:16px;margin-bottom:8px}
dialog p{color:var(--muted);margin-bottom:10px;font-size:13px;word-break:break-word}
dialog input{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--content);color:inherit;font:inherit;margin-bottom:12px}
.btns{display:flex;flex-direction:row-reverse;gap:8px}
.btns button{padding:7px 16px;border-radius:6px;border:1px solid var(--line);background:var(--content);color:inherit;font:inherit;cursor:pointer}
.btns .primary{background:#0067c0;border-color:#0067c0;color:#fff}
.btns .danger{background:#c42b1c;border-color:#c42b1c;color:#fff}
#toast{position:fixed;left:50%;bottom:44px;transform:translateX(-50%);background:#323232;color:#fff;padding:9px 16px;border-radius:8px;font-size:13px;z-index:80;max-width:90vw;box-shadow:var(--shadow)}
#toast[hidden]{display:none}
#toast.err{background:#b3261e}
#viewer{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.94);display:flex;flex-direction:column;color:#fff}
#viewer[hidden]{display:none}
#viewer .vh{display:flex;align-items:center;gap:8px;padding:8px 12px;background:rgba(0,0,0,.5)}
#vt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
#viewer .cb:hover:not(:disabled){background:rgba(255,255,255,.18)}
#vbody{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;padding:8px}
#vbody video,#vbody img{max-width:100%;max-height:100%;border-radius:6px}
#vbody audio{width:min(92vw,560px)}
#vbody iframe{width:100%;height:100%;border:0;background:#fff;border-radius:6px}
#st-i{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
#navbtn{display:none}
@media(max-width:760px){
  #navbtn{display:inline-block}
  #nav{position:fixed;left:0;top:96px;bottom:30px;z-index:40;display:none;box-shadow:var(--shadow);width:270px}
  #nav.open{display:block}
  #q{width:92px;padding:0 6px}
  #grp{max-width:96px}
  .lbl{display:none}
  .cb{padding:6px 9px}
  #preview{display:none!important}
  .icons .it{width:96px}
  .details .hd,.details .it{grid-template-columns:28px 1fr 80px}
  .details .ty,.details .hd div:nth-child(4){display:none}
}
</style>
</head>
<body>
<div id="app">
  <div class="bar">
    <button class="ib" id="navbtn" title="Folders">☰</button>
    <button class="ib" id="bk" title="Back">←</button>
    <button class="ib" id="fw" title="Forward">→</button>
    <button class="ib" id="up" title="Up">↑</button>
    <button class="ib" id="rf" title="Check for new files now">⟳</button>
    <select id="grp" title="Group"></select>
    <button class="ib" id="gadd" title="Add group">＋</button>
    <div class="addr" id="addr"></div>
    <button class="ib" id="lo" title="Logout">⏻</button>
    <input id="q" type="search" placeholder="Search files…" autocomplete="off">
  </div>
  <div class="bar cmd">
    <button class="cb" id="c-cut"><span>✂</span><span class="lbl">Cut</span></button>
    <button class="cb" id="c-copy"><span>⧉</span><span class="lbl">Copy</span></button>
    <button class="cb" id="c-paste"><span>📋</span><span class="lbl">Paste</span></button>
    <button class="cb" id="c-ren"><span>✎</span><span class="lbl">Rename</span></button>
    <button class="cb" id="c-tg"><span>📤</span><span class="lbl">Telegram</span></button>
    <button class="cb" id="c-dl"><span>⬇</span><span class="lbl">Download</span></button>
    <button class="cb" id="c-del"><span>🗑</span><span class="lbl">Delete</span></button>
    <span class="vsep"></span>
    <button class="cb" id="c-sort"><span>⇅</span><span class="lbl">Sort</span></button>
    <button class="cb" id="c-view"><span>▦</span><span class="lbl">View</span></button>
    <button class="cb" id="c-more"><span>⋯</span></button>
    <span class="sp"></span>
    <button class="cb" id="c-prev"><span>◧</span><span class="lbl">Preview</span></button>
  </div>
  <div class="main">
    <aside id="nav"><div id="tree"></div></aside>
    <section id="content" class="icons" tabindex="0"></section>
    <aside id="preview" hidden></aside>
  </div>
  <div class="status">
    <span id="st-n"></span>
    <span id="st-s"></span>
    <span class="sp"></span>
    <span id="st-i"></span>
    <button class="ib" id="v-d" title="Details">☰</button>
    <button class="ib" id="v-i" title="Large icons">▦</button>
  </div>
</div>
<div id="viewer" hidden>
  <div class="vh"><span id="vt"></span><span class="sp"></span>
    <button class="cb" id="vdl">⬇ <span class="lbl">Download</span></button>
    <button class="cb" id="vx">✕ <span class="lbl">Close</span></button></div>
  <div id="vbody"></div>
</div>
<div id="menu" hidden></div>
<div id="toast" hidden></div>
<dialog id="dlg">
  <form method="dialog">
    <h3 id="dt"></h3>
    <p id="dx"></p>
    <input id="di" autocomplete="off">
    <div class="btns">
      <button id="dok" value="ok" class="primary">OK</button>
      <button value="cancel">Cancel</button>
    </div>
  </form>
</dialog>

<script>
(() => {
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const enc = p => p.split('/').map(encodeURIComponent).join('/');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fmt = n => { n = +n || 0; for (const u of ['B','KB','MB','GB']) { if (n < 1024) return (u === 'B' ? n : n.toFixed(1)) + ' ' + u; n /= 1024; } return n.toFixed(1) + ' TB'; };
const COARSE = matchMedia('(pointer: coarse)').matches;
const store = { get(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };

const S = { path: '', search: '', folders: [], files: [], items: [], map: new Map(), topicId: null,
  sel: new Set(), anchor: null, view: store.get('xview', 'icons'), sort: store.get('xsort', 'name'),
  desc: store.get('xdesc', '') === '1', clip: null, preview: false, idx: '',
  group: null, groups: [], ver: null, topics: null, interval: 60, lastCheck: 0, minIv: 10, maxIv: 3600 };
const gq = () => '?g=' + encodeURIComponent(S.group);

/* ---------- icons ---------- */
const FOLDER = '<svg viewBox="0 0 64 64"><path d="M4 14a4 4 0 0 1 4-4h14l6 6h28a4 4 0 0 1 4 4v32a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="#f0b93a"/><path d="M4 24h56v28a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" fill="#fcd462"/></svg>';
const EC = { pdf:'#d93025', py:'#3572a5', js:'#c9a400', ts:'#2b7489', json:'#6d6d6d', html:'#e34c26', css:'#563d7c', md:'#555', txt:'#777',
  csv:'#1d7a45', xlsx:'#1d7a45', xls:'#1d7a45', doc:'#2b579a', docx:'#2b579a', ppt:'#d24726', pptx:'#d24726',
  zip:'#8a6d3b', rar:'#8a6d3b', '7z':'#8a6d3b', apk:'#3ddc84', exe:'#0067c0', sh:'#444', bat:'#444' };
function colorFor(e) {
  if (EC[e]) return EC[e];
  if (/^(mp4|mkv|avi|mov|webm|m4v)$/.test(e)) return '#7b3fe4';
  if (/^(mp3|wav|m4a|ogg|flac|aac)$/.test(e)) return '#e8710a';
  if (/^(png|jpe?g|gif|webp|bmp|svg)$/.test(e)) return '#1a9c5b';
  return '#8a8f98';
}
function fileIcon(e) {
  const t = (e || 'file').toUpperCase().slice(0, 4);
  return '<svg viewBox="0 0 64 64"><path d="M14 4h26l14 14v38a4 4 0 0 1-4 4H14a4 4 0 0 1-4-4V8a4 4 0 0 1 4-4z" fill="#fff" stroke="#c4c4c4" stroke-width="2"/><path d="M40 4v14h14" fill="#e6e6e6" stroke="#c4c4c4" stroke-width="2"/><rect x="5" y="30" width="42" height="18" rx="3" fill="' + colorFor(e) + '"/><text x="26" y="43" font-size="' + (t.length > 3 ? 11 : 13) + '" font-weight="700" fill="#fff" text-anchor="middle" font-family="Arial,sans-serif">' + esc(t) + '</text></svg>';
}
function kind(x) {
  const m = (x.mime || '').toLowerCase(), e = x.ext;
  if (m.startsWith('video/') || /^(mp4|webm|mov|m4v|mkv)$/.test(e)) return 'video';
  if (m.startsWith('audio/') || /^(mp3|m4a|wav|ogg|flac|aac)$/.test(e)) return 'audio';
  if (m.startsWith('image/') || /^(png|jpe?g|gif|webp|bmp)$/.test(e)) return 'image';
  if (m === 'application/pdf' || e === 'pdf') return 'pdf';
  return 'text';
}

/* ---------- api / ui helpers ---------- */
async function api(url, body) {
  const o = { headers: { 'X-Requested-With': 'fetch' } };
  const scoped = S.group != null && !/^\/api\/(groups|settings)/.test(url);
  if (scoped && !body) url += (url.includes('?') ? '&' : '?') + 'g=' + encodeURIComponent(S.group);
  if (body) { if (scoped) body = Object.assign({ g: S.group }, body); o.method = 'POST'; o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(url, o); } catch (e) { return { ok: false, msg: 'network error' }; }
  if (r.status === 401) { location.href = '/login'; return { ok: false, msg: 'login required' }; }
  try { return await r.json(); } catch (e) { return { ok: false, msg: 'bad response (' + r.status + ')' }; }
}
let toastT;
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = err ? 'err' : ''; t.hidden = false;
  clearTimeout(toastT); toastT = setTimeout(() => { t.hidden = true; }, err ? 5000 : 2600);
}
function dialog(o) {
  return new Promise(res => {
    const d = $('#dlg'), inp = $('#di'), ok = $('#dok');
    $('#dt').textContent = o.title; $('#dx').textContent = o.text || '';
    inp.style.display = o.input != null ? '' : 'none'; inp.value = o.input || '';
    ok.textContent = o.ok || 'OK'; ok.className = o.danger ? 'danger' : 'primary';
    d.returnValue = '';
    d.onclose = () => res(d.returnValue === 'ok' ? (o.input != null ? inp.value.trim() : true) : null);
    d.showModal();
    if (o.input != null) { inp.focus(); const dot = o.input.lastIndexOf('.'); inp.setSelectionRange(0, dot > 0 ? dot : o.input.length); }
  });
}

/* ---------- data ---------- */
function tag() {
  S.folders.forEach(f => { f.key = 'd:' + f.path; f.type = 'dir'; });
  S.files.forEach(f => { f.key = 'f:' + f.id; f.type = 'file'; });
}
const cmpName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
function buildItems() {
  const items = [...S.folders, ...S.files];
  items.sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
    let r;
    if (S.sort === 'size') r = (a.size || 0) - (b.size || 0) || cmpName(a, b);
    else if (S.sort === 'type') r = (a.ext || '').localeCompare(b.ext || '') || cmpName(a, b);
    else r = cmpName(a, b);
    return S.desc ? -r : r;
  });
  return items;
}
const selItems = () => [...S.sel].map(k => S.map.get(k)).filter(Boolean);
const selFiles = () => selItems().filter(i => i.type === 'file');

async function go(path, push = true) {
  const r = await api('/api/list?path=' + encodeURIComponent(path));
  if (r.ok === false) { toast('❌ ' + r.msg, true); return; }
  S.search = ''; $('#q').value = '';
  S.path = r.path; S.topicId = r.topic_id; S.folders = r.folders; S.files = r.files; tag();
  S.sel.clear(); S.anchor = null;
  if (push) history.pushState({ p: S.path }, '', '/browse/' + enc(S.path) + gq());
  document.title = (S.path.split('/').pop() || 'Backup') + ' — Backup Explorer';
  render(); drawAddr(); revealTree(S.path); closeNav();
}
async function runSearch(q) {
  const r = await api('/api/search?q=' + encodeURIComponent(q));
  if (r.ok === false) { toast('❌ ' + r.msg, true); return; }
  S.search = q; S.folders = []; S.files = r.files; S.topicId = null; tag(); S.sel.clear(); S.anchor = null;
  render(); drawAddr();
}
const refresh = () => S.search ? runSearch(S.search) : go(S.path, false);
const parentOf = p => p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '';

/* ---------- render ---------- */
function itemHtml(i) {
  const ic = i.type === 'dir' ? FOLDER : fileIcon(i.ext);
  const cut = S.clip && S.clip.mode === 'cut' && i.type === 'file' && S.clip.set.has(i.id) ? ' cut' : '';
  const sub = i.type === 'file' && S.search ? '<small>' + esc(i.folder) + '</small>' : '';
  const size = i.type === 'dir' ? '' : fmt(i.size);
  const typ = i.type === 'dir' ? 'File folder' : (i.ext ? i.ext.toUpperCase() + ' file' : 'File');
  return '<div class="it' + (S.sel.has(i.key) ? ' sel' : '') + cut + '" data-key="' + esc(i.key) + '" title="' + esc(i.name) + '">' +
    '<div class="ic">' + ic + '</div><div class="nm">' + esc(i.name) + sub + '</div><div class="sz">' + size + '</div><div class="ty">' + typ + '</div></div>';
}
function render() {
  S.items = buildItems(); S.map = new Map(S.items.map(i => [i.key, i]));
  for (const k of [...S.sel]) if (!S.map.has(k)) S.sel.delete(k);
  const c = $('#content'); c.className = S.view;
  let h = '';
  if (S.view === 'details') {
    const ar = k => S.sort === k ? (S.desc ? ' ▾' : ' ▴') : '';
    h += '<div class="hd"><div></div><div data-sort="name">Name' + ar('name') + '</div><div data-sort="size">Size' + ar('size') + '</div><div data-sort="type">Type' + ar('type') + '</div></div>';
  }
  for (const i of S.items) h += itemHtml(i);
  if (!S.items.length) h += '<div class="empty">' + (S.search ? 'No files found.' : 'This folder is empty.') + '</div>';
  c.innerHTML = h;
  $('#v-d').classList.toggle('on', S.view === 'details'); $('#v-i').classList.toggle('on', S.view === 'icons');
  $('#c-prev').classList.toggle('on', S.preview);
  updateBar();
}
function paintSel() { $$('#content .it').forEach(el => el.classList.toggle('sel', S.sel.has(el.dataset.key))); }
function updateBar() {
  const f = selFiles(), n = S.sel.size;
  $('#c-cut').disabled = $('#c-copy').disabled = $('#c-del').disabled = $('#c-tg').disabled = $('#c-dl').disabled = !f.length;
  $('#c-ren').disabled = !(n === 1 && f.length === 1);
  $('#c-paste').disabled = !canPaste();
  $('#st-n').textContent = S.items.length + ' item' + (S.items.length === 1 ? '' : 's');
  $('#st-s').textContent = n ? n + ' selected' + (f.length ? '  ·  ' + fmt(f.reduce((a, x) => a + (x.size || 0), 0)) : '') : '';
  drawPreview();
  drawIdx();
}
function drawAddr() {
  const a = $('#addr'); let h = '<a data-p="">🏠 Backup</a>';
  if (S.search) h += '<span class="sep">›</span><span style="padding:3px 6px">Search: ' + esc(S.search) + '</span>';
  else { let acc = ''; for (const p of (S.path ? S.path.split('/') : [])) { acc = acc ? acc + '/' + p : p; h += '<span class="sep">›</span><a data-p="' + esc(acc) + '">' + esc(p) + '</a>'; } }
  a.innerHTML = h; a.scrollLeft = a.scrollWidth;
}
function drawIdx() {
  const st = S.stats || {}, parts = [], s = S.idx;
  const n = x => (+x || 0).toLocaleString();
  const plural = (k, w) => n(k) + ' ' + w + (k === 1 ? '' : 's');
  if (S.tg === false && !/telegram/.test(s || '')) parts.push('⚠ Telegram connect होत आहे…');
  if (S.user === false) parts.push('⚠ User login नाही — files दिसणार नाहीत; bot ला /userlogin पाठव');
  // this folder
  if (S.search) parts.push('🔎 ' + plural(S.files.length, 'result'));
  else if (S.path) {
    const sub = S.folders.reduce((a, f) => a + (f.count || 0), 0);
    parts.push('📁 ' + plural(S.files.length, 'file') + (sub ? ' (+' + n(sub) + ' in subfolders)' : ''));
  }
  // whole group (what is in the database)
  if (st.files != null) parts.push('🗄 ' + plural(st.files, 'file') + ' · ' + plural(st.topics, 'folder') + (st.bytes ? ' · ' + fmt(st.bytes) : ''));
  // scan progress / remaining
  const sc = st.scan;
  if (sc && sc.total) {
    const left = Math.max(0, sc.total - sc.done);
    parts.push('⟳ scanning ' + Math.min(100, Math.floor(sc.done * 100 / sc.total)) + '% · ' + n(left) + ' msgs left');
  } else if (s === 'done') parts.push('✓ up to date');
  else if (s && s !== 'idle') parts.push('⟳ ' + s);
  if (S.lastCheck) {
    const a = Math.max(0, Math.round(Date.now() / 1000 + (S.skew || 0) - S.lastCheck));
    parts.push('🔎 ' + (a < 60 ? a + 's' : Math.round(a / 60) + 'm') + ' ago / ' + (S.interval >= 60 && S.interval % 60 === 0 ? S.interval / 60 + 'm' : S.interval + 's'));
  }
  const el = $('#st-i'); el.textContent = parts.join('  ·  ');
  el.title = parts.join('\n') + (st.scan ? '\n(msgs = Telegram messages still to be checked; files among them are counted as they are found)' : '') + (st.allFiles != null ? '\nAll groups: ' + plural(st.allFiles, 'file') : '');
}

/* ---------- selection / open ---------- */
function select(key, e) {
  const keys = S.items.map(i => i.key);
  if (e.shiftKey && S.anchor && S.map.has(S.anchor)) {
    const a = keys.indexOf(S.anchor), b = keys.indexOf(key), lo = Math.min(a, b), hi = Math.max(a, b);
    S.sel = new Set(keys.slice(lo, hi + 1));
  } else if (e.ctrlKey || e.metaKey) { S.sel.has(key) ? S.sel.delete(key) : S.sel.add(key); S.anchor = key; }
  else { S.sel = new Set([key]); S.anchor = key; }
  paintSel(); updateBar();
}
const clearSel = () => { S.sel.clear(); S.anchor = null; paintSel(); updateBar(); };
function openItem(i) {
  if (!i) return;
  if (i.type === 'dir') return go(i.path);
  if (i.view) openViewer(i);
  else downloadFiles([i]);
}

/* ---------- viewer (same tab, Close button, Back/Esc also closes) ---------- */
const viewer = $('#viewer');
function openViewer(i) {
  const url = '/stream/' + i.id + '/' + encodeURIComponent(i.name) + gq(), k = kind(i);
  const body = k === 'video' ? '<video controls autoplay playsinline src="' + url + '"></video>'
    : k === 'audio' ? '<audio controls autoplay src="' + url + '"></audio>'
    : k === 'image' ? '<img alt="" src="' + url + '">'
    : '<iframe src="' + url + '"></iframe>';
  $('#vt').textContent = i.name;
  $('#vdl').onclick = () => downloadFiles([i]);
  $('#vbody').innerHTML = body;
  viewer.hidden = false;
  history.pushState({ p: S.path, viewer: true }, '');
}
function closeViewer(fromPop) {
  if (viewer.hidden) return;
  viewer.hidden = true;
  $('#vbody').innerHTML = '';                 // stops video/audio
  if (!fromPop && history.state && history.state.viewer) { S.closing = true; history.back(); }
}
$('#vx').onclick = () => closeViewer(false);

/* ---------- actions ---------- */
function downloadFiles(files) {
  files.forEach((f, n) => setTimeout(() => {
    const a = document.createElement('a');
    a.href = '/download/' + f.id + '/' + encodeURIComponent(f.name) + gq(); a.download = f.name;
    document.body.appendChild(a); a.click(); a.remove();
  }, n * 700));
}
async function sendTG(files) {
  let ok = 0;
  for (const f of files) {
    const r = await api('/sendtobot/' + f.id, {});
    if (r.ok) ok++; else toast('❌ ' + (r.msg || 'failed'), true);
    await sleep(350);
  }
  if (ok) toast('✅ ' + ok + ' file(s) Telegram वर पाठवल्या');
}
async function delFiles(files) {
  if (!files.length) return;
  const yes = await dialog({ title: 'Delete ' + files.length + ' file' + (files.length > 1 ? 's' : '') + '?',
    text: 'Telegram group मधून कायमच्या delete होतील. हे परत करता येणार नाही.', ok: 'Delete', danger: true });
  if (!yes) return;
  const ids = files.map(f => f.id); let gone = 0;
  for (let i = 0; i < ids.length; i += 50) {
    const b = ids.slice(i, i + 50), r = await api('/api/delete', { ids: b });
    if (!r.ok) { toast('❌ ' + r.msg, true); break; }
    const dead = new Set(r.deleted || b); gone += dead.size;
    S.files = S.files.filter(f => !dead.has(f.id));
  }
  S.sel.clear(); render();
  if (gone) toast('🗑 ' + gone + ' deleted');
}
async function renameSel() {
  const f = selFiles(); if (S.sel.size !== 1 || f.length !== 1) return;
  const nm = await dialog({ title: 'Rename', text: 'फक्त या browser मधलं नाव बदलतं. Telegram मधलं मूळ नाव तसंच राहतं.', input: f[0].name, ok: 'Rename' });
  if (!nm || nm === f[0].name) return;
  const r = await api('/api/rename', { id: f[0].id, name: nm });
  if (r.ok) { f[0].name = r.name; f[0].ext = r.ext; render(); toast('✎ Renamed'); } else toast('❌ ' + r.msg, true);
}
function setClip(mode) {
  const f = selFiles(); if (!f.length) return;
  S.clip = { mode, ids: f.map(x => x.id), set: new Set(f.map(x => x.id)) };
  render(); toast((mode === 'cut' ? '✂ Cut: ' : '⧉ Copied: ') + f.length + ' — folder उघडून Paste कर');
}
const canPaste = () => !!(S.clip && S.topicId != null && !S.search);
async function paste() {
  if (!S.clip) return;
  if (!canPaste()) { toast('इथे paste करता येत नाही — हा topic folder नाही', true); return; }
  const { mode, ids } = S.clip; let done = 0;
  toast((mode === 'cut' ? 'Moving ' : 'Copying ') + ids.length + ' file(s)…');
  for (let i = 0; i < ids.length; i += 15) {
    const r = await api('/api/paste', { ids: ids.slice(i, i + 15), topic_id: S.topicId, mode: mode === 'cut' ? 'move' : 'copy' });
    if (!r.results) { toast('❌ ' + r.msg, true); break; }
    r.results.forEach(x => { if (x.ok) done++; else toast('❌ ' + x.msg, true); });
  }
  if (mode === 'cut') S.clip = null;
  await refresh(); toast('✅ ' + done + ' file(s) ' + (mode === 'cut' ? 'moved' : 'copied'));
}
function setView(v) { S.view = v; store.set('xview', v); render(); }
function sortBy(k) { if (S.sort === k) S.desc = !S.desc; else { S.sort = k; S.desc = false; } store.set('xsort', S.sort); store.set('xdesc', S.desc ? '1' : ''); render(); }
async function reindex() {
  const yes = await dialog({ title: 'Re-index this group?', text: 'या group चा index साफ होऊन Telegram group पुन्हा पूर्ण scan होईल (वेळ लागतो). Rename केलेली नावं जातील.', ok: 'Re-index', danger: true });
  if (!yes) return;
  const r = await api('/api/reindex', {});
  toast(r.ok ? '⟳ Re-index सुरू' : '❌ ' + r.msg, !r.ok); if (r.ok) { S.idx = 'scanning'; drawIdx(); }
}

/* ---------- menus ---------- */
const menu = $('#menu');
function hideMenu() { menu.hidden = true; }
function drawMenu(x, y, items) {
  menu.innerHTML = items.map((m, n) => m === '-' ? '<div class="ms"></div>' : '<div class="mi' + (m[2] ? ' dis' : '') + (m[3] ? ' b' : '') + '" data-n="' + n + '">' + esc(m[0]) + '</div>').join('');
  menu.hidden = false;
  const w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = Math.max(4, Math.min(x, innerWidth - w - 4)) + 'px';
  menu.style.top = Math.max(4, Math.min(y, innerHeight - h - 4)) + 'px';
  menu.onclick = e => { const el = e.target.closest('.mi'); if (!el) return; const it = items[+el.dataset.n]; hideMenu(); it[1](); };
}
function showMenu(x, y, kind) {
  const f = selFiles(), it = selItems()[0], one = S.sel.size === 1; let m = [];
  if (kind === 'item') {
    if (one && it.type === 'dir') m = [['Open', () => go(it.path), 0, 1]];
    else if (f.length) {
      if (one) m.push([f[0].view ? 'Open' : 'Download', () => openItem(f[0]), 0, 1]);
      if (one && S.search) m.push(['📂 Open file location', () => go(f[0].folder)]);
      m.push(['⬇ Download', () => downloadFiles(f)], ['📤 Send to Telegram', () => sendTG(f)], '-',
        ['✂ Cut', () => setClip('cut')], ['⧉ Copy', () => setClip('copy')], '-');
      if (one) m.push(['✎ Rename', renameSel]);
      m.push(['🗑 Delete', () => delFiles(f)]);
    }
  } else {
    m = [['📋 Paste', paste, !canPaste()], ['⟳ Refresh', refresh], '-', ['▦ Large icons', () => setView('icons')], ['☰ Details', () => setView('details')]];
  }
  if (m.length) drawMenu(x, y, m);
}
function btnMenu(btn, items) { const r = btn.getBoundingClientRect(); drawMenu(r.left, r.bottom + 4, items); }

/* ---------- tree ---------- */
const treeCache = new Map();
async function kids(path) {
  if (treeCache.has(path)) return treeCache.get(path);
  const r = await api('/api/list?dirs=1&path=' + encodeURIComponent(path));
  const k = r.folders || []; treeCache.set(path, k); return k;
}
const nodeHtml = (f, d) => '<div class="tn" data-path="' + esc(f.path) + '" style="--d:' + d + '"><span class="ar" data-ar>▸</span><span class="tl">📁 ' + esc(f.name) + '</span></div><div class="tc" hidden></div>';
async function openNode(tn) {
  const box = tn.nextElementSibling;
  if (!box.dataset.loaded) {
    const k = await kids(tn.dataset.path), d = (+tn.style.getPropertyValue('--d') || 0) + 1;
    box.innerHTML = k.map(x => nodeHtml(x, d)).join(''); box.dataset.loaded = '1';
    if (!k.length) tn.querySelector('.ar').textContent = '';
  }
  if (box.innerHTML) { box.hidden = false; tn.querySelector('.ar').textContent = '▾'; }
}
async function toggleNode(tn) {
  const box = tn.nextElementSibling;
  if (box.dataset.loaded && !box.hidden) { box.hidden = true; tn.querySelector('.ar').textContent = '▸'; } else await openNode(tn);
}
async function revealTree(path) {
  $$('#tree .tn.cur').forEach(n => n.classList.remove('cur'));
  let cur = $('#tree .tn'); let acc = '';
  if (!cur) return;
  for (const p of (path ? path.split('/') : [])) {
    acc = acc ? acc + '/' + p : p;
    await openNode(cur);
    const next = [...cur.nextElementSibling.children].find(n => n.classList.contains('tn') && n.dataset.path === acc);
    if (!next) break; cur = next;
  }
  if (S.path === path) { $$('#tree .tn.cur').forEach(n => n.classList.remove('cur')); cur.classList.add('cur'); cur.scrollIntoView({ block: 'nearest' }); }
}
async function initTree() {
  $('#tree').innerHTML = '<div class="tn" data-path="" style="--d:0"><span class="ar">▾</span><span class="tl">🏠 Backup</span></div><div class="tc"></div>';
  const root = $('#tree .tn'); await openNode(root);
}
const closeNav = () => $('#nav').classList.remove('open');

/* ---------- preview ---------- */
function drawPreview() {
  const p = $('#preview'); p.hidden = !S.preview;
  p.querySelectorAll('video,audio').forEach(v => { v.pause(); v.removeAttribute('src'); v.load(); });
  if (!S.preview) { p.innerHTML = ''; return; }
  const f = selFiles();
  if (S.sel.size !== 1 || f.length !== 1) { p.innerHTML = '<div class="empty">Select a file to preview</div>'; return; }
  const x = f[0], url = '/stream/' + x.id + '/' + encodeURIComponent(x.name) + gq(), k = kind(x);
  let body = '';
  if (!x.view) body = '<div class="empty">Preview उपलब्ध नाही — Download वापर</div>';
  else if (k === 'video') body = '<video controls preload="metadata" src="' + url + '"></video>';
  else if (k === 'audio') body = '<audio controls preload="metadata" src="' + url + '"></audio>';
  else if (k === 'image') body = '<img alt="" src="' + url + '">';
  else body = '<iframe src="' + url + '"></iframe>';
  p.innerHTML = body + '<div class="pvi"><b>' + esc(x.name) + '</b>' + fmt(x.size) + (x.ext ? ' · ' + esc(x.ext.toUpperCase()) : '') + '</div>';
}

/* ---------- events ---------- */
let swallow = false, lp = null;
const content = $('#content');
content.addEventListener('click', e => {
  const sh = e.target.closest('[data-sort]'); if (sh) { sortBy(sh.dataset.sort); return; }
  const el = e.target.closest('.it');
  if (swallow) { swallow = false; return; }
  if (!el) { if (!(e.ctrlKey || e.metaKey)) clearSel(); return; }
  if (COARSE && !e.ctrlKey && !e.shiftKey) { openItem(S.map.get(el.dataset.key)); return; }
  select(el.dataset.key, e);
});
content.addEventListener('dblclick', e => { const el = e.target.closest('.it'); if (el && !COARSE) openItem(S.map.get(el.dataset.key)); });
content.addEventListener('contextmenu', e => {
  e.preventDefault();
  const el = e.target.closest('.it');
  if (el) { const k = el.dataset.key; if (!S.sel.has(k)) { S.sel = new Set([k]); S.anchor = k; paintSel(); updateBar(); } } else clearSel();
  showMenu(e.clientX, e.clientY, el ? 'item' : 'blank');
});
content.addEventListener('touchstart', e => {
  const t = e.touches[0], el = e.target.closest('.it');
  lp = setTimeout(() => {
    lp = null; swallow = true; setTimeout(() => { swallow = false; }, 900);
    if (el) { const k = el.dataset.key; if (!S.sel.has(k)) { S.sel = new Set([k]); S.anchor = k; paintSel(); updateBar(); } }
    showMenu(t.clientX, t.clientY, el ? 'item' : 'blank');
  }, 550);
}, { passive: true });
['touchend', 'touchmove', 'touchcancel'].forEach(ev => content.addEventListener(ev, () => { if (lp) { clearTimeout(lp); lp = null; } }, { passive: true }));

document.addEventListener('click', e => { if (!e.target.closest('#menu')) hideMenu(); });
document.addEventListener('scroll', hideMenu, true);
window.addEventListener('resize', hideMenu);
$('#addr').addEventListener('click', e => { const a = e.target.closest('a[data-p]'); if (a) go(a.dataset.p); });
$('#tree').addEventListener('click', e => {
  const tn = e.target.closest('.tn'); if (!tn) return;
  if (e.target.closest('.ar')) toggleNode(tn); else { go(tn.dataset.path); openNode(tn); }
});
$('#navbtn').onclick = e => { e.stopPropagation(); $('#nav').classList.toggle('open'); };
$('#bk').onclick = () => history.back();
$('#fw').onclick = () => history.forward();
$('#up').onclick = () => { if (S.search) go(S.path, false); else if (S.path) go(parentOf(S.path)); };
$('#rf').onclick = checkNow;
$('#c-cut').onclick = () => setClip('cut');
$('#c-copy').onclick = () => setClip('copy');
$('#c-paste').onclick = paste;
$('#c-ren').onclick = renameSel;
$('#c-tg').onclick = () => sendTG(selFiles());
$('#c-dl').onclick = () => downloadFiles(selFiles());
$('#c-del').onclick = () => delFiles(selFiles());
$('#c-prev').onclick = () => { S.preview = !S.preview; $('#c-prev').classList.toggle('on', S.preview); drawPreview(); };
$('#v-d').onclick = () => setView('details');
$('#v-i').onclick = () => setView('icons');
$('#c-view').onclick = e => { e.stopPropagation(); btnMenu(e.currentTarget, [[(S.view === 'icons' ? '✓ ' : '') + 'Large icons', () => setView('icons')], [(S.view === 'details' ? '✓ ' : '') + 'Details', () => setView('details')]]); };
$('#c-sort').onclick = e => {
  e.stopPropagation();
  const c = (k, l) => [(S.sort === k ? '✓ ' : '') + l, () => { S.sort = k; store.set('xsort', k); render(); }];
  btnMenu(e.currentTarget, [c('name', 'Name'), c('size', 'Size'), c('type', 'Type'), '-',
    [(!S.desc ? '✓ ' : '') + 'Ascending', () => { S.desc = false; store.set('xdesc', ''); render(); }],
    [(S.desc ? '✓ ' : '') + 'Descending', () => { S.desc = true; store.set('xdesc', '1'); render(); }]]);
};
$('#c-more').onclick = e => {
  e.stopPropagation();
  btnMenu(e.currentTarget, [['Select all', () => { S.sel = new Set(S.items.map(i => i.key)); paintSel(); updateBar(); }], ['Check for new files now', checkNow], ['⏱ Check interval…', setIntervalDlg], '-', ['➕ Add group…', addGroup], ['➖ Remove this group', removeGroup], '-', ['Re-index this group', reindex], '-', ['Logout', () => { location.href = '/logout'; }]]);
};
$('#q').addEventListener('keydown', e => {
  if (e.key === 'Enter') { const q = e.target.value.trim(); if (q) runSearch(q); else go(S.path, false); }
  else if (e.key === 'Escape') { e.target.value = ''; if (S.search) go(S.path, false); }
});
window.addEventListener('popstate', e => {
  if (S.closing) { S.closing = false; return; }          // we closed the viewer ourselves
  if (!viewer.hidden) { closeViewer(true); return; }     // browser Back closes the viewer
  go(e.state && e.state.p != null ? e.state.p : pathFromUrl(), false);
});
document.addEventListener('keydown', e => {
  if (!viewer.hidden) { if (e.key === 'Escape') closeViewer(false); return; }
  if (e.target.closest('input,textarea,dialog')) return;
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if (k === 'delete') delFiles(selFiles());
  else if (k === 'f2') renameSel();
  else if (k === 'enter') { const i = selItems(); if (i.length === 1) openItem(i[0]); }
  else if (k === 'escape') { hideMenu(); clearSel(); }
  else if (k === 'backspace' || (e.altKey && k === 'arrowup')) { e.preventDefault(); if (S.path && !S.search) go(parentOf(S.path)); }
  else if (mod && k === 'a') { e.preventDefault(); S.sel = new Set(S.items.map(i => i.key)); paintSel(); updateBar(); }
  else if (mod && k === 'c') { if (selFiles().length) { e.preventDefault(); setClip('copy'); } }
  else if (mod && k === 'x') { if (selFiles().length) { e.preventDefault(); setClip('cut'); } }
  else if (mod && k === 'v') { e.preventDefault(); paste(); }
});

/* ---------- groups ---------- */
function drawGroups() {
  const sel = $('#grp');
  sel.innerHTML = S.groups.map(g => '<option value="' + esc(g.id) + '"' + (String(g.id) === String(S.group) ? ' selected' : '') + '>' + esc(g.title) + '</option>').join('');
  sel.style.display = S.groups.length > 1 ? '' : 'none';
}
async function loadGroups() {
  const r = await api('/api/groups');
  S.groups = r.groups || [];
  const want = new URLSearchParams(location.search).get('g') || store.get('xgroup', '');
  const hit = S.groups.find(g => String(g.id) === String(want)) || S.groups[0];
  S.group = hit ? hit.id : null;
  if (S.group != null) store.set('xgroup', String(S.group));
  drawGroups();
}
async function switchGroup(id) {
  S.group = id; store.set('xgroup', String(id));
  S.clip = null; S.ver = null; S.topics = null; S.sel.clear(); S.search = ''; $('#q').value = '';
  treeCache.clear(); drawGroups();
  await initTree(); await go('', true);
  pollStatus();
}
async function addGroup() {
  const v = await dialog({ title: 'Add group', text: 'Telegram group चा ID (उदा. -1001234567890). Bot त्या group मध्ये admin असावा आणि group मध्ये Topics ON असावेत.', input: '', ok: 'Add' });
  if (!v) return;
  const r = await api('/api/groups', { group_id: v });
  if (!r.ok) { toast('❌ ' + r.msg, true); return; }
  toast('✅ ' + r.group.title + ' add झाला — index होत आहे');
  const l = await api('/api/groups'); S.groups = l.groups || [];
  await switchGroup(r.group.id);
}
async function removeGroup() {
  const g = S.groups.find(x => String(x.id) === String(S.group));
  if (!g) return;
  if (g.source === 'env') { toast('हा group env variable मधून आहे — तिथूनच काढ', true); return; }
  const yes = await dialog({ title: 'Remove group?', text: '"' + g.title + '" इथून काढला जाईल (फक्त index; Telegram वरच्या files delete होत नाहीत).', ok: 'Remove', danger: true });
  if (!yes) return;
  const r = await api('/api/groups/remove', { group_id: g.id });
  if (!r.ok) { toast('❌ ' + r.msg, true); return; }
  const l = await api('/api/groups'); S.groups = l.groups || [];
  await switchGroup(S.groups.length ? S.groups[0].id : null);
}
$('#grp').onchange = e => switchGroup(e.target.value);
$('#gadd').onclick = addGroup;
$('#lo').onclick = () => { location.href = '/logout'; };

/* ---------- index status / auto-check ---------- */
async function setIntervalDlg() {
  const v = await dialog({ title: 'Check interval', text: 'नवीन files साठी किती seconds नी check करायचं? (' + S.minIv + ' – ' + S.maxIv + '). Server आणि browser दोन्ही हाच interval वापरतात.', input: String(S.interval), ok: 'Save' });
  if (!v) return;
  const n = parseInt(v, 10);
  if (!(n >= S.minIv && n <= S.maxIv)) { toast('❌ ' + S.minIv + ' ते ' + S.maxIv + ' मधला number दे', true); return; }
  const r = await api('/api/settings', { poll_interval: n });
  if (!r.ok) { toast('❌ ' + r.msg, true); return; }
  S.interval = r.interval; toast('⏱ आता दर ' + r.interval + ' seconds ला check होईल'); drawIdx(); pollStatus();
}
async function softRefresh(announce) {
  const before = new Set(S.files.map(f => f.id));
  const url = S.search ? '/api/search?q=' + encodeURIComponent(S.search) : '/api/list?path=' + encodeURIComponent(S.path);
  const r = await api(url);
  if (r.ok === false) return;
  if (S.search) { S.folders = []; S.files = r.files; }
  else { S.topicId = r.topic_id; S.folders = r.folders; S.files = r.files; }
  tag(); render();
  if (announce) {
    const added = S.files.filter(f => !before.has(f.id)).length;
    toast(added ? '🆕 ' + added + ' new file' + (added > 1 ? 's' : '') : '🔄 Updated');
  }
}
async function checkNow() {
  const b = $('#rf'); b.disabled = true;
  const r = await api('/api/checknow', {});
  b.disabled = false;
  if (!r.ok) { toast('❌ ' + (r.msg || 'check failed'), true); return; }
  if (!r.found) toast(r.scanning ? '⟳ Scan चालू आहे…' : '✓ नवीन काही नाही');
  await pollStatus();
}
let pollT;
async function pollStatus() {
  clearTimeout(pollT);
  const r = await api('/api/status');
  let next = 5000;
  if (r && r.index) {
    const was = S.idx; S.idx = r.index.status; S.tg = r.tg_ready; S.user = r.user_ready; S.stats = { files: r.files, topics: r.topics, bytes: r.bytes, scan: r.scan, allFiles: r.all_files };
    S.interval = r.interval || S.interval; S.minIv = r.min_interval || S.minIv; S.maxIv = r.max_interval || S.maxIv;
    S.lastCheck = r.group ? r.group.last_check : 0; if (r.now) S.skew = r.now - Date.now() / 1000;
    const ver = r.group ? r.group.ver : null;
    const busy = !!document.querySelector('dialog[open]');
    if (r.topics !== S.topics) {
      const first = S.topics == null; S.topics = r.topics;
      if (!first) { treeCache.clear(); initTree().then(() => revealTree(S.path)); }
    }
    if (ver != null) {
      if (S.ver == null) S.ver = ver;
      else if (ver !== S.ver && !busy) { S.ver = ver; await softRefresh(true); }
    }
    if (was && was !== S.idx && S.idx === 'done' && !S.search) await softRefresh(false);
    drawIdx();
    next = /^(scanning|idle|telegram)/.test(S.idx) ? 4000 : Math.max(S.minIv, S.interval) * 1000;
  }
  pollT = setTimeout(pollStatus, next);
}

/* ---------- init ---------- */
function pathFromUrl() {
  const m = location.pathname.match(/^\/browse\/(.*)$/);
  try { return m ? decodeURIComponent(m[1]).replace(/^\/+|\/+$/g, '') : ''; } catch (e) { return ''; }
}
(async function init() {
  const p = pathFromUrl();
  history.replaceState({ p }, '');
  await loadGroups();
  await initTree();
  await go(p, false);
  pollStatus();
})();
})();
</script>
</body>
</html>
"""

@app.route("/browse/", defaults={"path": ""})
@app.route("/browse/<path:path>")
@require_auth
def browse(path):
    return Response(EXPLORER_HTML, mimetype="text/html")

@app.route("/search")
@require_auth
def search():
    return redirect("/browse/")

def _file_json(r):
    name = r["filename"]
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return {
        "id": r["msg_id"], "name": name, "ext": ext,
        "size": r["size"] or 0, "mime": r["mime_type"] or "",
        "folder": r["folder_path"],
        "view": _view_mime(name, r["mime_type"] or "") is not None,
    }

# ── Groups ──────────────────────────────────────────────────────────────
@app.route("/api/groups")
@require_api
def api_groups():
    rows = db_many("""
        SELECT g.group_id, g.title, g.source,
               (SELECT COUNT(*) FROM files f WHERE f.group_id = g.group_id) AS files
        FROM groups g ORDER BY g.added_at, g.group_id""")
    return jsonify({"ok": True, "groups": [
        {"id": r["group_id"], "title": r["title"] or f"Group {r['group_id']}",
         "source": r["source"], "files": r["files"]} for r in rows]})

@app.route("/api/groups", methods=["POST"])
@require_api
def api_group_add():
    d = request.get_json(silent=True) or {}
    try:
        gid = int(str(d.get("group_id", "")).strip())
    except ValueError:
        return jsonify({"ok": False, "msg": "Group ID number हवा (उदा. -1001234567890)"}), 400
    if gid >= -1000000000000:
        return jsonify({"ok": False, "msg": "Supergroup ID -100… ने सुरू होतो"}), 400
    if db_one("SELECT 1 FROM groups WHERE group_id=?", (gid,)):
        return jsonify({"ok": False, "msg": "हा group आधीच add आहे"}), 409
    if db_one("SELECT COUNT(*) AS c FROM groups")["c"] >= MAX_GROUPS:
        return jsonify({"ok": False, "msg": f"जास्तीत जास्त {MAX_GROUPS} groups"}), 400
    if not _tg_ready.is_set():
        return jsonify({"ok": False, "msg": "Telegram अजून तयार नाही, थोड्या वेळाने try कर"}), 503

    async def _chk():
        return await _get_chat(gid)
    try:
        chat = tg(_chk(), timeout=30)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Bot ला हा group दिसत नाही — bot group मध्ये "
                                            f"admin add केला आहे का? ({type(e).__name__})"}), 400
    if getattr(chat, "is_forum", None) is False:
        return jsonify({"ok": False, "msg": "या group मध्ये Topics (Forum) ON नाहीत — "
                                            "folders topics वरून बनतात"}), 400
    title = (getattr(chat, "title", "") or "")[:100]
    db_exec("INSERT OR IGNORE INTO groups (group_id, title, source, added_at) VALUES (?, ?, 'ui', ?)",
            (gid, title, time.time()))
    refresh_chan_map()
    _topics.setdefault(gid, {})
    _changed(gid)
    asyncio.run_coroutine_threadsafe(_full_index(gid), _tg_loop)
    return jsonify({"ok": True, "group": {"id": gid, "title": title or f"Group {gid}"}})

@app.route("/api/groups/remove", methods=["POST"])
@require_api
def api_group_remove():
    d = request.get_json(silent=True) or {}
    try:
        gid = int(d.get("group_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "bad group"}), 400
    row = db_one("SELECT source FROM groups WHERE group_id=?", (gid,))
    if not row:
        return _no_group()
    if row["source"] == "env":
        return jsonify({"ok": False, "msg": "हा group env variable (BACKUP_GROUP_ID/S) मधून आहे — "
                                            "तिथूनच काढ"}), 400
    # Only the local index is removed; nothing is deleted from Telegram.
    db_exec("DELETE FROM files  WHERE group_id=?", (gid,))
    db_exec("DELETE FROM topics WHERE group_id=?", (gid,))
    db_exec("DELETE FROM groups WHERE group_id=?", (gid,))
    db_exec("DELETE FROM meta   WHERE key=?", (f"cursor_{gid}",))
    _topics.pop(gid, None)
    refresh_chan_map()
    global _snapshot_dirty
    _snapshot_dirty = True
    return jsonify({"ok": True})

# ── Settings / live check ───────────────────────────────────────────────
@app.route("/api/settings", methods=["POST"])
@require_api
def api_settings():
    d = request.get_json(silent=True) or {}
    try:
        iv = int(d.get("poll_interval"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "number हवा"}), 400
    if not (MIN_POLL <= iv <= MAX_POLL):
        return jsonify({"ok": False, "msg": f"{MIN_POLL} ते {MAX_POLL} seconds"}), 400
    meta_set("poll_interval", iv)
    _poll_wakeup.set()
    return jsonify({"ok": True, "interval": iv})

@app.route("/api/checknow", methods=["POST"])
@require_api
def api_checknow():
    gid = req_group()
    if gid is None:
        return _no_group()
    if gid in _scanning or gid in _queued:
        return jsonify({"ok": True, "found": 0, "scanning": True})
    try:
        found = tg(asyncio.wait_for(_poll_group(gid), 60), timeout=70)
    except (asyncio.TimeoutError, TimeoutError):
        return jsonify({"ok": True, "found": 0, "busy": True,
                        "msg": "Telegram flood-wait चालू आहे, थोड्या वेळाने आपोआप check होईल"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"check failed: {type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "found": found})

# ── File API ────────────────────────────────────────────────────────────
@app.route("/api/list")
@require_api
def api_list():
    gid = req_group()
    if gid is None:
        return _no_group()
    path = (request.args.get("path", "") or "").strip("/")
    dirs_only = request.args.get("dirs") == "1"
    prefix = f"{path}/" if path else ""
    children = {}
    for t in db_many("SELECT path, msg_count FROM topics WHERE group_id=?", (gid,)):
        p = t["path"]
        if not p.startswith(prefix) or p == path:
            continue
        child = p[len(prefix):].split("/")[0]
        if child:
            children[child] = children.get(child, 0) + (t["msg_count"] or 0)
    folders = [{"name": n, "path": prefix + n, "count": c}
               for n, c in sorted(children.items(), key=lambda x: x[0].lower())]
    if dirs_only:
        return jsonify({"ok": True, "folders": folders})
    topic = db_one("SELECT topic_id FROM topics WHERE group_id=? AND path=?", (gid, path)) if path else None
    files = []
    if path:
        files = [_file_json(r) for r in db_many(
            "SELECT * FROM files WHERE group_id=? AND folder_path=? ORDER BY filename COLLATE NOCASE",
            (gid, path))]
    return jsonify({"ok": True, "path": path,
                    "topic_id": topic["topic_id"] if topic else None,
                    "folders": folders, "files": files})

@app.route("/api/search")
@require_api
def api_search():
    gid = req_group()
    if gid is None:
        return _no_group()
    q = (request.args.get("q", "") or "").strip()
    if not q:
        return jsonify({"ok": True, "files": []})
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db_many(
        "SELECT * FROM files WHERE group_id=? AND filename LIKE ? ESCAPE '\\' "
        "ORDER BY filename COLLATE NOCASE LIMIT 300", (gid, like))
    return jsonify({"ok": True, "files": [_file_json(r) for r in rows]})

def _ids(data, limit=100):
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids or len(ids) > limit:
        return None
    try:
        return [int(i) for i in ids]
    except Exception:
        return None

def _recount(gid, topic_ids):
    for tid in set(topic_ids):
        db_exec("UPDATE topics SET msg_count=(SELECT COUNT(*) FROM files WHERE group_id=? AND topic_id=?) "
                "WHERE group_id=? AND topic_id=?", (gid, tid, gid, tid))

@app.route("/api/delete", methods=["POST"])
@require_api
def api_delete():
    gid = req_group()
    if gid is None:
        return _no_group()
    ids = _ids(request.get_json(silent=True) or {})
    if not ids:
        return jsonify({"ok": False, "msg": "no files selected"}), 400
    rows = [r for r in (db_one("SELECT msg_id, topic_id FROM files WHERE group_id=? AND msg_id=?", (gid, i))
                        for i in ids) if r]
    if not rows:
        return jsonify({"ok": False, "msg": "files not found in index"}), 404

    async def _del():
        ids = [r["msg_id"] for r in rows]
        try:
            return await _tg_client.delete_messages(gid, ids)
        except Exception:
            if not _tg_user:
                raise
            return await _tg_user.delete_messages(gid, ids)
    try:
        tg(_del(), timeout=60)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Telegram delete failed: {e} "
                        "(bot ला group मध्ये 'Delete messages' admin right हवा)"}), 500
    for r in rows:
        db_exec("DELETE FROM files WHERE group_id=? AND msg_id=?", (gid, r["msg_id"]))
    _recount(gid, [r["topic_id"] for r in rows])
    _changed(gid)
    return jsonify({"ok": True, "deleted": [r["msg_id"] for r in rows]})

@app.route("/api/rename", methods=["POST"])
@require_api
def api_rename():
    gid = req_group()
    if gid is None:
        return _no_group()
    d = request.get_json(silent=True) or {}
    try:
        mid = int(d.get("id"))
    except Exception:
        return jsonify({"ok": False, "msg": "bad id"}), 400
    name = str(d.get("name", "")).strip()
    if not name or len(name) > 255 or any(c in name for c in '/\\\0'):
        return jsonify({"ok": False, "msg": "invalid name"}), 400
    if not db_one("SELECT 1 FROM files WHERE group_id=? AND msg_id=?", (gid, mid)):
        return jsonify({"ok": False, "msg": "file not found"}), 404
    db_exec("UPDATE files SET filename=? WHERE group_id=? AND msg_id=?", (name, gid, mid))
    _changed(gid)
    return jsonify({"ok": True, "name": name,
                    "ext": os.path.splitext(name)[1].lstrip(".").lower()})

@app.route("/api/paste", methods=["POST"])
@require_api
def api_paste():
    """Copy / move files into another topic folder of the SAME group (server-side
    copy on Telegram, no download/upload through this server)."""
    gid = req_group()
    if gid is None:
        return _no_group()
    d = request.get_json(silent=True) or {}
    ids = _ids(d, 20)
    mode = d.get("mode")
    try:
        tid = int(d.get("topic_id"))
    except Exception:
        return jsonify({"ok": False, "msg": "bad target folder"}), 400
    if not ids or mode not in ("copy", "move"):
        return jsonify({"ok": False, "msg": "bad request"}), 400
    target = db_one("SELECT topic_id, path FROM topics WHERE group_id=? AND topic_id=?", (gid, tid))
    if not target:
        return jsonify({"ok": False, "msg": "target folder not found"}), 404

    async def _copy(mid):
        m = await _rd().copy_message(chat_id=gid, from_chat_id=gid,
                                          message_id=mid, message_thread_id=tid)
        return m.id

    async def _rm(mid):
        await _rd().delete_messages(gid, mid)

    results, touched = [], [tid]
    for mid in ids:
        row = db_one("SELECT * FROM files WHERE group_id=? AND msg_id=?", (gid, mid))
        if not row:
            results.append({"id": mid, "ok": False, "msg": "file not in index"}); continue
        if mode == "move" and row["topic_id"] == tid:
            results.append({"id": mid, "ok": True, "new_id": mid}); continue
        try:
            new_id = tg(_copy(mid), timeout=60)
        except Exception as e:
            results.append({"id": mid, "ok": False, "msg": f"{row['filename']}: {e}"}); continue
        db_exec("""INSERT OR IGNORE INTO files (group_id, msg_id, topic_id, folder_path, filename, size, mime_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (gid, new_id, tid, target["path"], row["filename"], row["size"], row["mime_type"]))
        if mode == "move":
            try:
                tg(_rm(mid), timeout=30)
                db_exec("DELETE FROM files WHERE group_id=? AND msg_id=?", (gid, mid))
                touched.append(row["topic_id"])
            except Exception as e:
                results.append({"id": mid, "ok": False,
                                "msg": f"copied, but original not deleted: {e}"}); continue
        results.append({"id": mid, "ok": True, "new_id": new_id})
        time.sleep(0.4)
    _recount(gid, touched)
    _changed(gid)
    return jsonify({"ok": True, "results": results})

@app.route("/api/reindex", methods=["POST"])
@require_api
def api_reindex():
    gid = req_group()
    if gid is None:
        return _no_group()
    if gid in _scanning or gid in _queued:
        return jsonify({"ok": False, "msg": "scan आधीच चालू/queue मध्ये आहे"}), 409
    db_exec("DELETE FROM files  WHERE group_id=?", (gid,))
    db_exec("DELETE FROM topics WHERE group_id=?", (gid,))
    meta_set(f"cursor_{gid}", 0)
    _topics[gid] = {}
    _changed(gid)
    _index_status["status"] = "idle"
    asyncio.run_coroutine_threadsafe(_full_index(gid), _tg_loop)
    return jsonify({"ok": True})

# ── Download / stream ───────────────────────────────────────────────────
def _content_disposition(kind, name):
    """Header-safe Content-Disposition that also works for Marathi/Unicode names."""
    ascii_name = re.sub(r'[^\x20-\x7e]', "_", name).replace('"', "_").replace("\\", "_")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{urlquote(name, safe='')}"

# ── Disk cache (fast delivery; Telegram stays the permanent store) ──────────
# The cache is DISPOSABLE: if the disk is wiped (Render free restarts/redeploys),
# nothing is lost — a file is simply fetched from Telegram again on next use.
CACHE_DIR       = os.environ.get("CACHE_DIR", "/var/tmp/tg_cache")
CACHE_MAX_BYTES = int(float(os.environ.get("CACHE_MAX_GB", "8")) * 1024 ** 3)
CACHE_MAX_FILE  = int(float(os.environ.get("CACHE_MAX_FILE_MB", "1024")) * 1024 ** 2)
CACHE_ENABLED   = os.environ.get("CACHE_ENABLED", "1") != "0"
CACHE_MIN_FREE  = 300 * 1024 ** 2          # always keep this much of the disk free
try:
    os.makedirs(CACHE_DIR, exist_ok=True)
except OSError as _e:
    print(f"[cache] cannot create {CACHE_DIR}: {_e} — cache disabled", flush=True)
    CACHE_ENABLED = False

_cache_mutex = threading.Lock()
_cache_keylocks = {}                        # key -> Lock (one Telegram fetch per file at a time)
_cache_stats = {"hits": 0, "misses": 0}

def _cache_path(gid, msg_id):
    return os.path.join(CACHE_DIR, f"{abs(gid)}_{msg_id}")

def cache_get(gid, msg_id, size):
    """Path of a complete cached copy, or None."""
    if not CACHE_ENABLED:
        return None
    p = _cache_path(gid, msg_id)
    try:
        st = os.stat(p)
    except OSError:
        return None
    if size and st.st_size != size:
        return None
    try:
        os.utime(p, None)                   # mark as recently used (LRU)
    except OSError:
        pass
    return p

def _cache_make_room(need):
    """Delete least-recently-used files until `need` bytes fit. False if impossible."""
    import shutil
    with _cache_mutex:
        files = []
        for n in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, n)
            try:
                st = os.stat(p)
                files.append([st.st_mtime, st.st_size, p, n.endswith(".part")])
            except OSError:
                pass
        now = time.time()
        for f in files:                     # stale half-downloads
            if f[3] and now - f[0] > 7200:
                try: os.unlink(f[2])
                except OSError: pass
                f[1] = 0
        total = sum(f[1] for f in files)
        files.sort(key=lambda f: f[0])
        def free_ok():
            return shutil.disk_usage(CACHE_DIR).free >= need + CACHE_MIN_FREE
        while files and (total + need > CACHE_MAX_BYTES or not free_ok()):
            f = files.pop(0)
            if f[3] and now - f[0] <= 7200:
                continue                    # a download in progress — do not touch
            try:
                os.unlink(f[2]); total -= f[1]
            except OSError:
                pass
        return total + need <= CACHE_MAX_BYTES and free_ok()

def cache_fill(gid, msg_id, size, timeout=3600):
    """Download the file from Telegram into the cache. -> path or None.
    Only one fetch per file at a time; others wait and then hit the cache."""
    if not CACHE_ENABLED or not size or size > CACHE_MAX_FILE:
        return None
    key = (gid, msg_id)
    with _cache_mutex:
        lk = _cache_keylocks.setdefault(key, threading.Lock())
    with lk:
        hit = cache_get(gid, msg_id, size)
        if hit:
            return hit
        if not _cache_make_room(size):
            print(f"[cache] no room for {size} bytes — serving without cache", flush=True)
            return None
        final = _cache_path(gid, msg_id)
        part = final + ".part"
        async def _dl():
            cl, msg = await _resolve_msg(gid, msg_id)
            if not msg:
                return False
            _bridge_touch(gid, msg_id, hold=3600)
            try:
                return bool(await cl.download_media(msg, file_name=part))
            finally:
                _bridge_release(gid, msg_id)
        try:
            ok = tg(_dl(), timeout=timeout)
            if ok and os.path.exists(part) and os.path.getsize(part) == size:
                os.replace(part, final)
                _cache_stats["misses"] += 1
                return final
        except Exception as e:
            print(f"[cache] fill failed for {msg_id}: {e}", flush=True)
        try: os.unlink(part)
        except OSError: pass
        return None
    # (lock dict entries are tiny; they are left in place on purpose)

def cache_fill_bg(gid, msg_id, size):
    """Start a background fill unless already cached / running."""
    if not CACHE_ENABLED or not size or size > CACHE_MAX_FILE or cache_get(gid, msg_id, size):
        return
    with _cache_mutex:
        lk = _cache_keylocks.setdefault((gid, msg_id), threading.Lock())
    if lk.locked():
        return
    threading.Thread(target=cache_fill, args=(gid, msg_id, size), daemon=True, name="cache_fill").start()

@app.route("/api/cache")
@require_api
def api_cache():
    used = n = 0
    if CACHE_ENABLED:
        for f in os.listdir(CACHE_DIR):
            try:
                used += os.path.getsize(os.path.join(CACHE_DIR, f)); n += 1
            except OSError:
                pass
    return jsonify({"enabled": CACHE_ENABLED, "files": n, "bytes": used,
                    "max_bytes": CACHE_MAX_BYTES, **_cache_stats})

@app.route("/download/<int:msg_id>/<path:filename>")
@require_auth
def download(msg_id, filename):
    """Download a file: from the disk cache if present, else fetch from Telegram (and cache it)."""
    gid = req_group()
    row = db_one("SELECT filename, size FROM files WHERE group_id=? AND msg_id=?", (gid, msg_id)) if gid else None
    if not row:
        return jsonify({"error": "file not found in index"}), 404
    from flask import send_file
    hit = cache_get(gid, msg_id, row["size"])
    if hit:
        _cache_stats["hits"] += 1
    else:
        hit = cache_fill(gid, msg_id, row["size"])
    if hit:
        return send_file(hit, mimetype="application/octet-stream", as_attachment=True,
                         download_name=row["filename"], conditional=True, max_age=0)
    tmp_path = os.path.join(TMP_DIR, f"{msg_id}_{secrets.token_hex(4)}")

    async def _dl():
        cl, msg = await _resolve_msg(gid, msg_id)
        if not msg:
            return False
        _bridge_touch(gid, msg_id, hold=3600)       # keep the copy alive while downloading
        try:
            return bool(await cl.download_media(msg, file_name=tmp_path))
        finally:
            _bridge_release(gid, msg_id)

    def _cleanup():
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    try:
        ok = tg(_dl(), timeout=180)
    except Exception as e:
        _cleanup()
        return jsonify({"error": str(e)}), 500
    if not ok or not os.path.exists(tmp_path):
        _cleanup()
        return jsonify({"error": "file no longer exists in Telegram"}), 404

    def stream_and_clean():
        try:
            with open(tmp_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            _cleanup()

    headers = {
        "Content-Disposition": _content_disposition("attachment", row["filename"]),
        "Content-Type": "application/octet-stream",
        "Content-Length": str(os.path.getsize(tmp_path)),
    }
    return Response(stream_with_context(stream_and_clean()), headers=headers)

# ── Inline viewing (stream from Telegram, no full download) ──────────────
STREAM_CHUNK = 1024 * 1024      # pyrogram stream_media chunk size (1 MiB)

_TEXT_EXT = {".txt", ".md", ".json", ".py", ".js", ".ts", ".jsx", ".tsx", ".css",
             ".csv", ".log", ".yml", ".yaml", ".xml", ".sh", ".conf", ".ini",
             ".toml", ".env", ".go", ".java", ".c", ".cpp", ".h", ".sql",
             ".html", ".htm", ".rs", ".php", ".rb", ".gitignore"}

def _view_mime(filename, mime):
    """Content-Type to serve inline, or None if the file should be download-only."""
    name = (filename or "").lower()
    ext = os.path.splitext(name)[1]
    mime = (mime or "").lower()
    if ext == ".svg" or mime == "image/svg+xml":
        return None                     # SVG can carry scripts
    if ext in _TEXT_EXT or name in ("dockerfile", "makefile") or mime.startswith("text/"):
        return "text/plain; charset=utf-8"   # html etc. shown as plain text, never executed
    if mime.startswith(("video/", "audio/", "image/")) or mime == "application/pdf":
        return mime
    guessed = (mimetypes.guess_type(name)[0] or "").lower()
    if guessed.startswith(("video/", "audio/", "image/")) and guessed != "image/svg+xml":
        return guessed
    if ext == ".pdf":
        return "application/pdf"
    return None

def _parse_range(header, size):
    """Returns (start, end), None (no Range header) or 'bad'."""
    if not header or not header.startswith("bytes="):
        return None
    try:
        a, b = header[6:].split(",")[0].strip().split("-", 1)
        if a == "":
            n = int(b)
            start, end = max(0, size - n), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            return "bad"
        return start, end
    except Exception:
        return "bad"

# NOTE: every pyrogram call must be created INSIDE a coroutine that runs on the
# Telegram loop (tg(...)). Calling _tg_client.xxx() directly from a Flask thread
# makes pyrogram use another loop -> "attached to a different loop".
async def _stream_open(gid, msg_id, first_chunk, nchunks):
    cl, msg = await _resolve_msg(gid, msg_id)
    if not msg:
        raise RuntimeError("file cannot be opened right now")
    return cl.stream_media(msg, limit=nchunks, offset=first_chunk)

async def _stream_next(agen):
    try:
        return await agen.__anext__()
    except StopAsyncIteration:
        return None

async def _stream_close(agen):
    try:
        await agen.aclose()
    except Exception:
        pass

@app.route("/stream/<int:msg_id>/<path:filename>")
@require_auth
def stream(msg_id, filename):
    """Open a file in the browser (video/audio/image/pdf/text) with Range support."""
    gid = req_group()
    row = db_one("SELECT size, mime_type, filename FROM files WHERE group_id=? AND msg_id=?",
                 (gid, msg_id)) if gid else None
    if not row or not row["size"]:
        return jsonify({"error": "file not found in index"}), 404
    ctype = _view_mime(row["filename"], row["mime_type"] or "")
    if not ctype:
        return jsonify({"error": "preview not supported for this file — use Download"}), 415

    size = row["size"]
    hit = cache_get(gid, msg_id, size)
    if hit:                                  # served from disk: fast, no Telegram request
        from flask import send_file
        _cache_stats["hits"] += 1
        return send_file(hit, mimetype=ctype.split(";")[0], conditional=True, max_age=3600,
                         download_name=row["filename"], as_attachment=False)
    cache_fill_bg(gid, msg_id, size)         # first view: stream now, cache in background
    rng = _parse_range(request.headers.get("Range"), size)
    if rng == "bad":
        return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = rng if rng else (0, size - 1)
    length = end - start + 1
    first_chunk = start // STREAM_CHUNK
    skip = start % STREAM_CHUNK
    nchunks = (skip + length + STREAM_CHUNK - 1) // STREAM_CHUNK

    def generate():
        agen = None
        try:
            agen = tg(_stream_open(gid, msg_id, first_chunk, nchunks), timeout=30)
            sent = 0
            first = True
            while sent < length:
                chunk = tg(_stream_next(agen), timeout=60)
                _bridge_touch(gid, msg_id)
                if chunk is None:
                    break
                if first:
                    chunk = chunk[skip:]
                    first = False
                if len(chunk) > length - sent:
                    chunk = chunk[:length - sent]
                sent += len(chunk)
                yield chunk
        finally:
            if agen is not None:
                try:
                    tg(_stream_close(agen), timeout=10)
                except Exception:
                    pass

    headers = {
        "Content-Type": ctype,
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition("inline", row["filename"]),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600",
    }
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(stream_with_context(generate()),
                    status=206 if rng else 200, headers=headers)

@app.route("/sendtobot/<int:msg_id>", methods=["POST"])
@require_api
def sendtobot(msg_id):
    """POST (not GET) so a malicious page can't trigger it via a link/img tag."""
    gid = req_group()
    if gid is None or not db_one("SELECT 1 FROM files WHERE group_id=? AND msg_id=?", (gid, msg_id)):
        return jsonify({"ok": False, "msg": "file not found in index"}), 404

    async def _fwd():
        cl, msg = await _resolve_msg(gid, msg_id)
        if not msg:
            raise RuntimeError("file cannot be read")
        return await cl.forward_messages(
            chat_id=_owner_chat(),
            from_chat_id=gid,
            message_ids=msg.id,
        )
    try:
        tg(_fwd(), timeout=30)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

# ── Login routes ───────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("authenticated"):
        return redirect("/")
    ip = get_ip()
    if is_blocked(ip):
        return render_template_string(LOGIN_HTML,
            blocked=True, error=None, auth_bot_name=BOT_NAME, client_ip=ip)
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if check_password(pwd):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            return redirect("/")
        record_fail(ip)
        row = db_one("SELECT attempts FROM blocked_ips WHERE ip=?", (ip,))
        attempts = row["attempts"] if row else 1
        remaining = max(0, MAX_FAIL - attempts)
        if remaining == 0:
            return render_template_string(LOGIN_HTML,
                blocked=True, error=None, auth_bot_name=BOT_NAME, client_ip=ip)
        return render_template_string(LOGIN_HTML, blocked=False,
            error=f"❌ Wrong password. {remaining} attempt{'s' if remaining != 1 else ''} remaining.",
            auth_bot_name=BOT_NAME, client_ip=ip)
    return render_template_string(LOGIN_HTML,
        blocked=False, error=None, auth_bot_name=BOT_NAME, client_ip=ip)

@app.route("/bot_login")
def bot_login():
    if not BOT_NAME:
        return redirect("/login")
    db_exec("DELETE FROM login_tokens WHERE created_at < ?", (time.time() - 900,))
    if db_one("SELECT COUNT(*) AS c FROM login_tokens WHERE validated=0")["c"] >= 50:
        return Response("Too many pending login requests, try again in a few minutes.", status=429)
    token = secrets.token_urlsafe(32)
    db_exec("INSERT INTO login_tokens (token, created_at, ip, ua) VALUES (?, ?, ?, ?)",
            (token, time.time(), get_ip(), (request.headers.get("User-Agent", "") or "")[:200]))
    bot_url = f"https://t.me/{BOT_NAME}?start={token}"
    return render_template_string(BOT_LOGIN_HTML,
        token=token, bot_url=bot_url, auth_bot_name=BOT_NAME)

@app.route("/magic_login")
def magic_login():
    """One-time link sent by the bot to the owner: valid 5 min, works once."""
    token = request.args.get("t", "")
    n = db_exec("DELETE FROM login_tokens WHERE token=? AND validated=2 AND created_at>?",
                (token, time.time() - 300))
    if not n:
        return Response("❌ Link expired or already used. Send /start to the bot again.",
                        status=403, mimetype="text/plain")
    session.clear()
    session.permanent = True
    session["authenticated"] = True
    unblock_ip(get_ip())
    return redirect("/")

@app.route("/check_token")
def check_token():
    """Polled by the browser that opened /bot_login. Once the owner approved
    the token in Telegram, THIS browser gets the session cookie."""
    token = request.args.get("t", "")
    row = db_one("SELECT validated, created_at FROM login_tokens WHERE token=?", (token,))
    if row and row["validated"] == 1 and time.time() - row["created_at"] < 300:
        session.clear()
        session.permanent = True
        session["authenticated"] = True
        unblock_ip(get_ip())
        db_exec("DELETE FROM login_tokens WHERE token=?", (token,))
        return jsonify({"ok": True})
    return jsonify({"ok": False})

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/api/status")
@require_api
def api_status():
    gid = req_group()
    g = {"id": gid, "ver": 0, "last_check": 0, "last_new": 0}
    if gid is not None:
        g.update(ver=_group_ver.get(gid, 0), last_check=_last_check.get(gid, 0),
                 last_new=_last_new.get(gid, 0))
        topics = db_one("SELECT COUNT(*) as c FROM topics WHERE group_id=?", (gid,))["c"]
        files = db_one("SELECT COUNT(*) as c FROM files WHERE group_id=?", (gid,))["c"]
        size = db_one("SELECT COALESCE(SUM(size), 0) as s FROM files WHERE group_id=?", (gid,))["s"]
    else:
        topics = files = size = 0
    return jsonify({
        "index": _index_status,
        "topics": topics,
        "files": files,
        "bytes": size,
        "all_files": db_one("SELECT COUNT(*) as c FROM files")["c"],
        "scan": _scan_progress.get(gid),
        "queued": gid in _queued,
        "queue_len": len(_queued),
        "tg_ready": _tg_ready.is_set(),
        "user_ready": _user_status["ready"], "user_error": _user_status["error"],
        "interval": get_poll_interval(),
        "min_interval": MIN_POLL, "max_interval": MAX_POLL,
        "group": g,
        "now": time.time(),
    })

@app.route("/api/debug")
@require_api
def api_debug():
    return jsonify({"counters": _dbg, "samples": _dbg_samples,
                    "known_topics": {str(g): len(t) for g, t in _topics.items()}})

# ── Backup API (for Hermes on Render, token auth, NOT the web login) ──────
# POST /hapi/backup/<name>   body = .tar.gz  -> uploaded to Telegram (topic folder BACKUP_TOPIC)
# GET  /hapi/backup/<name>   -> newest backup of <name>  (404 = none yet, 503 = index not ready)
# GET  /hapi/backups         -> JSON list
# Telegram holds the data, so a Koyeb/Render restart can never lose a backup.
BACKUP_TOKEN  = os.environ.get("BACKUP_TOKEN", "").strip()
BACKUP_TOPIC  = os.environ.get("BACKUP_TOPIC", "hermes-backup")
BACKUP_KEEP   = max(1, _env_int("BACKUP_KEEP", 5))
BACKUP_MAX_MB = _env_int("BACKUP_MAX_MB", 200)
BACKUP_GID    = _env_int("BACKUP_GROUP", 0) or LEGACY_GID
_BK_NAME      = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_backup_lock  = threading.Lock()

def _bk_auth():
    """None if the bearer token is valid, else an error response."""
    if len(BACKUP_TOKEN) < 24:
        return jsonify({"ok": False, "msg": "backup API disabled (set BACKUP_TOKEN, 24+ chars)"}), 503
    h = request.headers.get("Authorization", "")
    tok = h[7:].strip() if h.startswith("Bearer ") else ""
    a = hashlib.sha256(tok.encode()).digest()
    b = hashlib.sha256(BACKUP_TOKEN.encode()).digest()
    if not hmac.compare_digest(a, b):
        time.sleep(1.5)                       # slow down guessing
        return jsonify({"ok": False, "msg": "unauthorized"}), 401
    return None

def _bk_rows(gid, name):
    """Backups of <name>, newest first."""
    rx = re.compile(rf"^{re.escape(name)}-\d{{8}}-\d{{6}}\.tar\.gz$")
    rows = db_many("SELECT msg_id, filename, size, topic_id FROM files WHERE group_id=? "
                   "AND topic_id=(SELECT topic_id FROM topics WHERE group_id=? AND name=? LIMIT 1) "
                   "ORDER BY msg_id DESC", (gid, gid, BACKUP_TOPIC))
    return [r for r in rows if rx.match(r["filename"])]

def _bk_topic(gid):
    """topic_id of the backup folder; creates the forum topic if it is missing."""
    r = db_one("SELECT topic_id FROM topics WHERE group_id=? AND name=?", (gid, BACKUP_TOPIC))
    if r:
        return r["topic_id"]
    async def _mk():
        import inspect                       # the raw field is `channel` in older layers, `peer` in newer ones
        _p = inspect.signature(tg_raw.functions.messages.CreateForumTopic.__init__).parameters
        _kw = {("peer" if "peer" in _p else "channel"): await _tg_client.resolve_peer(gid),
               "title": BACKUP_TOPIC, "random_id": _tg_client.rnd_id()}
        res = await _tg_client.invoke(tg_raw.functions.messages.CreateForumTopic(**_kw))
        for u in res.updates:
            m = getattr(u, "message", None)
            if m is not None and getattr(m, "id", None):
                return m.id
        raise RuntimeError("topic created but its id was not returned")
    tid = tg(_mk(), timeout=30)
    _save_topic(gid, tid, BACKUP_TOPIC)
    return tid

def _bk_ready():
    if not _tg_ready.is_set():
        return jsonify({"ok": False, "msg": "telegram not connected yet"}), 503
    if not BACKUP_GID or not db_one("SELECT 1 FROM groups WHERE group_id=?", (BACKUP_GID,)):
        return jsonify({"ok": False, "msg": "backup group not configured"}), 503
    return None

@app.route("/hapi/backup/<name>", methods=["POST", "PUT"])
def hapi_backup_put(name):
    bad = _bk_auth() or _bk_ready()
    if bad:
        return bad
    if not _BK_NAME.match(name):
        return jsonify({"ok": False, "msg": "bad name (A-Z a-z 0-9 _ -, max 40)"}), 400
    if not _index_status.get("last_run"):
        return jsonify({"ok": False, "msg": "index not ready yet — retry later"}), 503
    try:
        length = int(request.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length <= 0:
        return jsonify({"ok": False, "msg": "Content-Length required"}), 411
    if length > BACKUP_MAX_MB * 1024 * 1024:
        return jsonify({"ok": False, "msg": f"too large (max {BACKUP_MAX_MB} MB)"}), 413
    gid = BACKUP_GID
    fname = f"{name}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.tar.gz"
    tmp = os.path.join(TMP_DIR, f"up_{secrets.token_hex(6)}")
    inp, left = request.environ["wsgi.input"], length     # MAX_CONTENT_LENGTH (1 MB) only guards normal requests
    try:
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = inp.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk); left -= len(chunk)
        with open(tmp, "rb") as f:
            magic = f.read(2)
        if left or magic != b"\x1f\x8b":
            return jsonify({"ok": False, "msg": "upload incomplete or not a .tar.gz"}), 400
        with _backup_lock:
            tid = _bk_topic(gid)
            async def _up():
                m = await _tg_client.send_document(gid, tmp, file_name=fname, force_document=True,
                                                   caption=f"backup {fname}", message_thread_id=tid)
                return m.id
            mid = tg(_up(), timeout=900)
            trow = db_one("SELECT path FROM topics WHERE group_id=? AND topic_id=?", (gid, tid))
            db_exec("""INSERT OR IGNORE INTO files (group_id, msg_id, topic_id, folder_path, filename, size, mime_type)
                       VALUES (?, ?, ?, ?, ?, ?, 'application/gzip')""",
                    (gid, mid, tid, trow["path"] if trow else BACKUP_TOPIC, fname, length))
            old = [r["msg_id"] for r in _bk_rows(gid, name)[BACKUP_KEEP:]]
            if old:                                           # keep only the newest BACKUP_KEEP
                try:
                    tg(_tg_client.delete_messages(gid, old), timeout=60)
                    for i in old:
                        db_exec("DELETE FROM files WHERE group_id=? AND msg_id=?", (gid, i))
                except Exception as e:
                    print(f"[backup] prune failed: {e}", flush=True)
            _recount(gid, [tid]); _changed(gid)
        print(f"[backup] stored {fname} ({length} bytes) msg={mid}", flush=True)
        return jsonify({"ok": True, "file": fname, "size": length, "msg_id": mid})
    except Exception as e:
        print(f"[backup] upload failed: {e}", flush=True)
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {e}"}), 500
    finally:
        try: os.unlink(tmp)
        except OSError: pass

@app.route("/hapi/backup/<name>", methods=["GET"])
def hapi_backup_get(name):
    bad = _bk_auth() or _bk_ready()
    if bad:
        return bad
    if not _BK_NAME.match(name):
        return jsonify({"ok": False, "msg": "bad name"}), 400
    if not _index_status.get("last_run"):          # never answer "none" while the index is still loading
        return jsonify({"ok": False, "msg": "index not ready yet — retry later"}), 503
    rows = _bk_rows(BACKUP_GID, name)
    if not rows:
        return jsonify({"ok": False, "msg": "no backup yet"}), 404
    r = rows[0]
    from flask import send_file
    path = cache_fill(BACKUP_GID, r["msg_id"], r["size"])
    if path:
        return send_file(path, mimetype="application/gzip", as_attachment=True,
                         download_name=r["filename"], max_age=0)
    tmp = os.path.join(TMP_DIR, f"bk_{secrets.token_hex(6)}")
    async def _dl():
        cl, msg = await _resolve_msg(BACKUP_GID, r["msg_id"])
        return bool(msg) and bool(await cl.download_media(msg, file_name=tmp))
    try:
        ok = tg(_dl(), timeout=600)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500
    if not ok or not os.path.exists(tmp):
        return jsonify({"ok": False, "msg": "backup message not readable"}), 502
    def _gen():
        try:
            with open(tmp, "rb") as f:
                while True:
                    c = f.read(65536)
                    if not c:
                        break
                    yield c
        finally:
            try: os.unlink(tmp)
            except OSError: pass
    return Response(stream_with_context(_gen()), headers={
        "Content-Type": "application/gzip", "Content-Length": str(os.path.getsize(tmp)),
        "Content-Disposition": _content_disposition("attachment", r["filename"])})

@app.route("/hapi/backups")
def hapi_backups():
    bad = _bk_auth() or _bk_ready()
    if bad:
        return bad
    rows = db_many("SELECT msg_id, filename, size FROM files WHERE group_id=? AND topic_id="
                   "(SELECT topic_id FROM topics WHERE group_id=? AND name=? LIMIT 1) ORDER BY msg_id DESC",
                   (BACKUP_GID, BACKUP_GID, BACKUP_TOPIC))
    return jsonify({"ok": True, "index_ready": bool(_index_status.get("last_run")),
                    "backups": [dict(r) for r in rows]})

# ── Hooks used by Worker Hub (same bot, two update consumers) ─────────────
_STORAGE_CMDS = {"/login", "/userlogin", "/usercancel", "/userlogout", "/unblock", "/status"}

def bot_api_claims(u):
    """True if this raw Bot-API update (dict from getUpdates) is handled by the Pyrogram side of the bot.
    Worker Hub must ignore those, otherwise both would answer. Only called for logged-in admins."""
    try:
        cb = u.get("callback_query")
        if cb:
            return (cb.get("data") or "").split(":", 1)[0] in ("ul", "ok", "no")
        m = u.get("message")
        if not m:
            return False
        text = (m.get("text") or "").strip()
        uid = (m.get("from") or {}).get("id")
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            if cmd == "/start":
                return bool(arg.strip())            # /start <token> = website login approval
            return cmd in _STORAGE_CMDS
        if uid in _login_state:                     # phone / OTP / 2FA password of the user-login conversation
            return True
        return text in ("🔓 Login", "👤 User Login")
    except Exception:
        return False

def status_text():
    topics_count = db_one("SELECT COUNT(*) as c FROM topics")["c"]
    files_count  = db_one("SELECT COUNT(*) as c FROM files")["c"]
    groups_count = db_one("SELECT COUNT(*) as c FROM groups")["c"]
    return (f"👥 Groups: {groups_count}\n🗂 Topics indexed: {topics_count}\n📄 Files indexed: {files_count}\n"
            f"👤 User session: {'ON ' + _user_status['me'] if _user_status['ready'] else 'OFF'}\n"
            f"🔄 Indexer: {_index_status['status']}\n⏱ Check every: {get_poll_interval()}s\n🌐 {WEB_URL}")

# ── Jinja filter ─────────────────────────────────────────────────────────
app.jinja_env.filters["urlencode"] = lambda s: urlquote(str(s), safe="")

# ── Main ──────────────────────────────────────────────────────────────────
_bg_started = False
def start_background():
    """Telegram client + indexer + poller + snapshotter. Idempotent (merged app.py calls it)."""
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    for fn, name in ((run_telegram, "tg_client"), (run_indexer, "indexer"),
                     (run_poller, "poller"), (run_snapshotter, "snapshotter")):
        threading.Thread(target=fn, daemon=True, name=name).start()

if __name__ == "__main__":          # standalone mode (without Worker Hub)
    signal.signal(signal.SIGTERM, _on_sigterm)
    start_background()
    from waitress import serve          # production WSGI server (no dev-server warning, no per-request log spam)
    serve(app, host="0.0.0.0", port=_env_int("PORT", 8080), threads=24, connection_limit=200,
          channel_timeout=300, cleanup_interval=30, ident="tgstorage")
