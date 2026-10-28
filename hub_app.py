"""
Render server = मूक मध्यस्थ (silent middleman) + Telegram admin bot.

• बाहेरच्या कुणालाही काहीही दिसत नाही — सगळे routes रिकामा 404 देतात
  (फक्त /, /health, /healthz → "ok", तुमच्या heartbeat / health check साठी).
• Bot: कुणीही message केलं की PASSWORD मागतो (4 प्रयत्न). बरोबर password = admin.
  Admin ids आता fix नाहीत → admin चा Telegram account गेला तरी नव्या account ने
  password टाकून परत access मिळतो.
• PC → server: दर PC long-poll करतो (/api/sync). Button दाबताच command लगेच PC ला जातो.

Environment variables (Render dashboard → Environment):
  TELEGRAM_BOT_TOKEN  तुमच्या control bot चा token                         [required]
  ADMIN_PASSWORD      bot login password (किमान 10 अक्षरं, लांब ठेवा)       [required]
  COLAB_SECRET        तुमची secret (नवीन PC च्या .env मध्ये जाते)           [required]
  PANEL_PASSWORD      सगळ्या PC च्या web terminal चा password               [required]
  MONGODB_URI         Atlas URI — admin login/lockout restart नंतरही राहतात [strongly recommended]
  KOYEB_URL           default job/poll server URL (नवीन PC साठी)            [optional]
  SESSION_KEY         Telegram sessions encrypt करणारी Fernet key           [Telegram accounts साठी]
  TG_API_ID/TG_API_HASH  default api (login वेळी वेगळा देता येतो)          [optional]
  BW_LIMIT_GB / MAX_ACCOUNTS / MAX_UPLOAD_MB / MAX_MEDIA_MB                  [optional]
  ADMIN_CHAT_ID       (optional) कायमचे admin ids, password शिवाय — रिकामं सोडा
  LOCKOUT_HOURS       4 चुकीच्या प्रयत्नांनंतर block किती तास (default 24, 0 = कायमचं)
  ADMIN_SESSION_DAYS  login किती दिवस टिकतो (default 30, 0 = कधीच संपत नाही)
  LOGIN_GLOBAL_MAX    तासाला सगळ्यांचे मिळून चुकीचे प्रयत्न (default 20) → login 1 तास बंद
  AGENT_MASTER / PUBLIC_URL / MONGODB_DB                                     [optional]
"""
import os, re, hmac, html, json, time, base64, struct, hashlib, secrets, threading, collections
import urllib.request, urllib.parse
from pathlib import Path
from flask import Flask, request, Response, abort, jsonify, has_request_context
try:
    import tgweb                                    # Telegram accounts (optional)
except Exception as _e:                             # pragma: no cover
    tgweb = None
    print('tgweb load failed:', _e)

app = Flask(__name__)
STORAGE = None            # storage_app module, plugged in by app.py (Backup Browser, same bot)
FILES = Path(__file__).parent / 'files'

COLAB_SECRET = os.environ['COLAB_SECRET']
PANEL_PASSWORD = os.environ['PANEL_PASSWORD']
TG_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
ENV_ADMINS = {int(x) for x in os.environ.get('ADMIN_CHAT_ID', '').replace(' ', '').split(',') if x.strip().isdigit()}
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
if len(ADMIN_PASSWORD) < 10:
    raise RuntimeError('ADMIN_PASSWORD env लागतो आणि किमान 10 अक्षरांचा हवा (लांब, वेगळा password ठेवा)')
MAX_TRIES = 4
LOCK_SECS = int(float(os.environ.get('LOCKOUT_HOURS', '24')) * 3600)     # 0 = कायमचं
SESSION_DAYS = int(os.environ.get('ADMIN_SESSION_DAYS', '30'))           # 0 = कधीच संपत नाही
GLOBAL_MAX = int(os.environ.get('LOGIN_GLOBAL_MAX', '20'))
FOREVER = 32503680000                                                    # सन 3000
ADMIN_TOTP_SECRET = re.sub(r'\s+', '', os.environ.get('ADMIN_TOTP_SECRET', '')).upper()
TOTP_ON = bool(ADMIN_TOTP_SECRET)
if TOTP_ON:
    try:
        base64.b32decode(ADMIN_TOTP_SECRET + '=' * (-len(ADMIN_TOTP_SECRET) % 8))
    except Exception:
        raise RuntimeError('ADMIN_TOTP_SECRET चुकीचा आहे (base32 अक्षरं A-Z आणि 2-7 हवीत)')
STEALTH_TRIGGER = os.environ.get('STEALTH_TRIGGER', '').strip()          # असेल तर अनोळखीला उत्तरच नाही
STEPUP_SECS = int(float(os.environ.get('STEPUP_MINUTES', '10')) * 60)
SENSITIVE = {'t', 'u', 'ua', 'n', 'ar', 'aa', 'tg', 'tr', 'tx', 'sl'}                            # यासाठी ताजा 2FA code लागतो
KOYEB_DEFAULT = [os.environ.get('KOYEB_URL', '').rstrip('/')]
AGENT_MASTER = os.environ.get('AGENT_MASTER') or hashlib.sha256(
    f'agent|{COLAB_SECRET}|{TG_TOKEN}'.encode()).hexdigest()
SYNC_WAIT = float(os.environ.get('SYNC_WAIT', '20'))
BOOT = time.time()

PCS = {}                         # pcid -> {name, st, ts, url, ev, cmds, offline}
TOKENS = {}                      # one-time install tokens
PENDING = {}                     # admin user id -> ('seturl', pcid|'ALL')
LOCK = threading.Lock()
BAD = collections.defaultdict(list)


# ───────────────────────── helpers ─────────────────────────
def eq(a, b):
    return hmac.compare_digest(str(a).encode(), str(b).encode())

def esc(s):
    return html.escape(str(s), quote=False)

def client_ip():
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.remote_addr or '?')

def throttle_check():
    ip, now = client_ip(), time.time()
    BAD[ip] = [t for t in BAD[ip] if now - t < 600]
    if len(BAD[ip]) >= 10:
        abort(404)                           # lockout सुद्धा 404 — काहीच कळू देत नाही

def bad_attempt():
    BAD[client_ip()].append(time.time())
    abort(404)

def root_url():
    u = (os.environ.get('PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
    if not u and has_request_context():
        u = request.url_root.rstrip('/')
    if u.startswith('http://') and 'localhost' not in u and '127.0.0.1' not in u:
        u = 'https://' + u[len('http://'):]
    return u

def agent_key(pcid):
    return hmac.new(AGENT_MASTER.encode(), pcid.encode(), hashlib.sha256).hexdigest()[:40]

def text_resp(body):
    return Response(body, mimetype='text/plain', headers={'Cache-Control': 'no-store'})

try:                                # gunicorn स्वतः "Server: gunicorn" लिहितो — त्याचं नाव बदलतो
    import gunicorn.http.wsgi as _gw
    _gw.SERVER = 'web'
except Exception:
    pass

@app.after_request
def _hygiene(resp):
    resp.headers['Server'] = 'web'                         # gunicorn/Flask चं नाव लपवतो
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    return resp

# सगळ्या चुका रिकामा 404 — route अस्तित्वात आहे का नाही हे सुद्धा बाहेरून कळू नये
for _c in (400, 403, 404, 405, 429):
    app.register_error_handler(_c, lambda e: ('', 404))
app.register_error_handler(500, lambda e: ('', 500))


# ───────────────────────── Telegram ─────────────────────────
def tg(method, **p):
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in p.items()}).encode()
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            f'https://api.telegram.org/bot{TG_TOKEN}/{method}', data=data), timeout=45)
        return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        if 'not modified' not in str(e):
            print(f'tg {method} failed: {e}')
        return {}

def notify(text, kb=None):
    def go(chat):
        tg('sendMessage', chat_id=chat, text=text[:4000], parse_mode='HTML',
           disable_web_page_preview=True, **({'reply_markup': kb} if kb else {}))
    for chat in AUTH.admin_ids():
        threading.Thread(target=go, args=(chat,), daemon=True).start()

def online(pc):
    return time.time() - pc['ts'] < 90

def yn(v):
    return '❓' if v is None else ('✅' if v else '❌')

def fmt_dur(s):
    s = int(s or 0)
    h, m = divmod(s // 60, 60)
    return f'{h}h {m}m' if h else f'{m}m'

def main_text():
    if not PCS:
        return '🖥 <b>Worker PCs</b>\n\nअजून एकही PC जोडलेला नाही.\n➕ दाबून नवा PC install करा.'
    lines = ['🖥 <b>Worker PCs</b>', '']
    for pcid, pc in sorted(PCS.items(), key=lambda kv: kv[1]['name']):
        st = pc['st']
        w = '✅' if st.get('running') else ('⏸' if not st.get('want', True) else '❌')
        lines.append(f"{'🟢' if online(pc) else '🔴'} <b>{esc(pc['name'])}</b> — worker {w}")
    return '\n'.join(lines)

def main_kb():
    rows = [[{'text': f"{'🟢' if online(pc) else '🔴'} {pc['name']}", 'callback_data': f'p:{pcid}'}]
            for pcid, pc in sorted(PCS.items(), key=lambda kv: kv[1]['name'])]
    rows.append([{'text': '🌐 सगळ्यांचा Job URL बदला', 'callback_data': 'ua'}])
    rows.append([{'text': '👤 Telegram Accounts', 'callback_data': 'ta'}])
    rows.append([{'text': '📦 Storage (Backup Browser)', 'callback_data': 'sb'}])
    rows.append([{'text': '👥 Admins', 'callback_data': 'adm'}])
    rows.append([{'text': '➕ नवा PC install', 'callback_data': 'n'},
                 {'text': '🔄 Refresh', 'callback_data': 'm'}])
    return {'inline_keyboard': rows}

def pc_text(pcid):
    pc = PCS[pcid]
    st = pc['st']
    state = ('✅ चालू' if st.get('running')
             else ('⏸ थांबवलेला' if not st.get('want', True) else '❌ बंद / सुरू होतोय'))
    age = st.get('log_age')
    return '\n'.join([
        f"{'🟢' if online(pc) else '🔴'} <b>{esc(pc['name'])}</b>  <code>{pcid}</code>",
        f"Worker: {state} · uptime {fmt_dur(st.get('uptime'))} · restarts {st.get('restarts', 0)}",
        f"Internet: {yn(st.get('internet'))} · Job-server: {yn(st.get('koyeb_ok'))}",
        f"Job URL: <code>{esc(st.get('koyeb_url') or '-')}</code>",
        f"Last log: {'-' if age is None else str(age) + 's पूर्वी'} · Encoder: {esc(st.get('encoder') or '-')}",
        f"Last job: {esc(st.get('last_job') or '-')}",
        f"PC शेवटचा दिसला: {int(time.time() - pc['ts'])}s पूर्वी",
    ])

def pc_kb(pcid):
    rows = [
        [{'text': '🔄 Refresh', 'callback_data': f'p:{pcid}'},
         {'text': '🩺 Check worker', 'callback_data': f'c:{pcid}:check'}],
        [{'text': '▶️ Start', 'callback_data': f'c:{pcid}:start'},
         {'text': '⏹ Stop', 'callback_data': f'c:{pcid}:stop'},
         {'text': '🔁 Restart', 'callback_data': f'c:{pcid}:restart'}],
        [{'text': '📜 Logs', 'callback_data': f'c:{pcid}:logs'},
         {'text': '🌐 Job URL बदला', 'callback_data': f'u:{pcid}'}],
        [{'text': '♻️ Manager restart', 'callback_data': f'c:{pcid}:rmgr'}],
    ]
    if PCS[pcid].get('url'):
        rows.append([{'text': '🖥 Terminal (एकदाचा link)', 'callback_data': f't:{pcid}'}])
    rows.append([{'text': '⬅️ Back', 'callback_data': 'm'}])
    return {'inline_keyboard': rows}

def show(chat, mid, text, kb):
    if mid:
        tg('editMessageText', chat_id=chat, message_id=mid, text=text, parse_mode='HTML',
           disable_web_page_preview=True, reply_markup=kb)
    else:
        tg('sendMessage', chat_id=chat, text=text, parse_mode='HTML',
           disable_web_page_preview=True, reply_markup=kb)

def enqueue(pcid, ctype, arg=''):
    with LOCK:
        pc = PCS.get(pcid)
        if not pc:
            return False
        if len(pc['cmds']) < 10:
            pc['cmds'].append({'id': secrets.token_hex(3), 'type': ctype, 'arg': arg})
        pc['ev'].set()
    return True

def new_token():
    t = secrets.token_urlsafe(24)
    with LOCK:
        TOKENS[t] = {'exp': time.time() + 900, 'left': 10}
    return t

URL_RE = re.compile(r'^https?://[^\s<>"\']{4,200}$')

def on_callback(cbid, uid, chat, mid, data):
    parts = data.split(':')
    kind = parts[0]
    pcid = parts[1] if len(parts) > 1 else ''
    ans = lambda text='', alert=False: (tg('answerCallbackQuery', callback_query_id=cbid, text=text,
                                           show_alert='true' if alert else 'false') if cbid else None)
    if TOTP_ON and kind in SENSITIVE and not AUTH.stepup_valid(uid):
        PENDING[uid] = ('otp', data, chat, mid, 0)
        ans('🔑 2FA code लागतो')
        tg('sendMessage', chat_id=chat, text='🔑 सुरक्षेसाठी Authenticator app मधला 6 अंकी code पाठवा.\nरद्द: /cancel')
        return
    if kind == 'ta':
        ans(); t, kb = ta_view(); show(chat, mid, t, kb); return
    if kind == 'tb':
        st = tgweb.bw_status() if TG_ON else {'used': 0, 'limit': 0, 'month': '-'}
        ans(f"📊 {st['used'] / 1048576:.1f} MB / {st['limit'] / 1073741824:.1f} GB ({st['month']})", True); return
    if kind == 'tg':
        if not TG_ON:
            ans('Telegram accounts बंद आहेत', True); return
        tok = tgweb.new_token()
        ans('✅ Link पाठवतोय')
        send_ephemeral(chat, f"👤 <b>Telegram UI</b>\n{esc(root_url())}/app?k={tok}\n\n"
                             f"⏱ एकदाच चालतो, 2 मिनिटं. हा message 90 सेकंदांत आपोआप delete होईल.")
        return
    if kind in ('tp', 'tc', 'tr', 'tx'):
        aid = parts[1] if len(parts) > 1 else ''
        if not TG_ON or not re.fullmatch(r'a_[0-9a-f]{8}', aid):
            ans('चुकीचं', True); return
        if kind == 'tp':
            t, kb = tp_view(aid)
            ans() if t else ans('Account सापडला नाही', True)
            if t:
                show(chat, mid, t, kb)
            return
        if kind == 'tc':
            a = next((x for x in tgweb.M.accounts_view() if x['id'] == aid), None)
            if not a:
                ans('Account सापडला नाही', True); return
            tgweb.M.set_enabled(aid, not a['enabled'])
            ans('⏸ Disconnect केलं' if a['enabled'] else '▶️ Connect होतंय…')
            t, kb = tp_view(aid); show(chat, mid, t, kb); return
        if kind == 'tr':
            ans()
            show(chat, mid, f'🗑 <b>{aid}</b> काढायचा?\n\n• "फक्त server वरून" — Telegram मधलं session चालू राहतं\n'
                            '• "logout सकट" — Telegram मधलं हे session कायमचं रद्द होतं',
                 {'inline_keyboard': [[{'text': '🗑 फक्त server वरून काढा', 'callback_data': f'tx:{aid}:0'}],
                                      [{'text': '🚪 Telegram मधून logout + काढा', 'callback_data': f'tx:{aid}:1'}],
                                      [{'text': '⬅️ रद्द', 'callback_data': f'tp:{aid}'}]]}); return
        if kind == 'tx':
            ok = tgweb.M.remove_account(aid, logout=(len(parts) > 2 and parts[2] == '1'))
            ans('✅ काढला' if ok else 'सापडला नाही', not ok)
            t, kb = ta_view(); show(chat, mid, t, kb); return
    if kind == 'sb':
        ans(); t, kb = sb_view(); show(chat, mid, t, kb); return
    if kind == 'sl':
        if STORAGE is None:
            ans('Storage बंद आहे', True); return
        try:
            url = STORAGE.magic_login_url()
        except Exception as e:
            ans(f'Error: {type(e).__name__}', True); return
        ans('✅ Link पाठवतोय')
        send_ephemeral(chat, f"📦 <b>Backup Browser</b>\n{esc(url)}\n\n"
                             f"⏱ 5 मिनिटं, एकदाच चालतो. हा message 90 सेकंदांत आपोआप delete होईल.")
        return
    if kind == 'adm':
        ans(); t, kb = admins_view(uid); show(chat, mid, t, kb); return
    if kind == 'ar':
        ok = pcid.isdigit() and int(pcid) != uid and AUTH.remove(int(pcid))
        if ok:
            tg('sendMessage', chat_id=int(pcid), text='🔒 तुमचा admin access काढला गेला.')
        ans('✅ काढला' if ok else 'काढता आलं नाही'); t, kb = admins_view(uid); show(chat, mid, t, kb); return
    if kind == 'al':
        if uid in ENV_ADMINS:
            ans('हा कायमचा admin आहे (env) — logout होत नाही', True); return
        AUTH.remove(uid); ans('🚪 Logout झालं')
        tg('sendMessage', chat_id=chat, text='🚪 Logout झालं. परत login साठी password लागेल.'); return
    if kind == 'aa':
        n = AUTH.remove_others(uid); ans(f'🔒 {n} जण logout केले', True)
        t, kb = admins_view(uid); show(chat, mid, t, kb); return
    if kind == 'm':
        ans(); show(chat, mid, main_text(), main_kb()); return
    if kind == 'n':
        t = new_token()
        cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -Command "irm {root_url()}/i/{t} | iex"'
        ans()
        tg('sendMessage', chat_id=chat, parse_mode='HTML', disable_web_page_preview=True, text=(
            '➕ <b>नवा PC install</b>\n\n'
            '1️⃣ त्या PC वर <b>Win+R</b> दाबा\n'
            '2️⃣ खालची command paste करा\n'
            '3️⃣ <b>Enter नाही — Ctrl+Shift+Enter</b> दाबा (UAC ला Yes)\n\n'
            f'<code>{esc(cmd)}</code>\n\n'
            '⏱ ही command फक्त 15 मिनिटं चालते. कुणाला देऊ नका.'))
        return
    if kind == 'ua':
        PENDING[uid] = ('seturl', 'ALL')
        ans()
        tg('sendMessage', chat_id=chat, text='🌐 सगळ्या PC साठी नवा Job-server URL पाठवा (https://...)\nरद्द: /cancel')
        return
    if not re.fullmatch(r'[0-9a-f]{8}', pcid) or pcid not in PCS:
        ans('PC सापडला नाही — थोड्या वेळाने Refresh करा', True); return
    pc = PCS[pcid]
    if kind == 'p':
        ans(); show(chat, mid, pc_text(pcid), pc_kb(pcid))
    elif kind == 't':
        if not online(pc) or not pc.get('url'):
            ans('PC offline आहे किंवा URL नाही', True); return
        tok = secrets.token_urlsafe(24)
        enqueue(pcid, 'ptoken', tok)             # PC ला हा token 2 मिनिटं मान्य
        ans('✅ Link पाठवतोय')
        r = tg('sendMessage', chat_id=chat, parse_mode='HTML', disable_web_page_preview=True, text=(
            f"🖥 <b>{esc(pc['name'])}</b> terminal\n{esc(pc['url'])}/?k={tok}\n\n"
            f"⏱ एकदाच चालतो, 2 मिनिटं. हा message 90 सेकंदांत आपोआप delete होईल."))
        m2 = (r.get('result') or {}).get('message_id') if isinstance(r, dict) else None
        if m2:
            t = threading.Timer(90, tg, args=('deleteMessage',), kwargs={'chat_id': chat, 'message_id': m2})
            t.daemon = True
            t.start()
    elif kind == 'u':
        PENDING[uid] = ('seturl', pcid)
        ans()
        tg('sendMessage', chat_id=chat, parse_mode='HTML', text=(
            f"🌐 <b>{esc(pc['name'])}</b> साठी नवा Job-server URL पाठवा (https://...)\nरद्द: /cancel"))
    elif kind == 'c' and len(parts) > 2 and parts[2] in ('start', 'stop', 'restart', 'logs', 'check', 'rmgr'):
        enqueue(pcid, parts[2])
        ans('⏳ पाठवलं' + ('' if online(pc) else ' (PC offline — online आल्यावर चालेल)'))
    else:
        ans()

def on_message(uid, chat, text, mid=None):
    if text == '/logout':
        if uid in ENV_ADMINS:
            tg('sendMessage', chat_id=chat, text='हा कायमचा admin आहे (env) — logout होत नाही'); return
        AUTH.remove(uid)
        tg('sendMessage', chat_id=chat, text='🚪 Logout झालं. परत login साठी password लागेल.'); return
    if text in ('/cancel',):
        PENDING.pop(uid, None)
        tg('sendMessage', chat_id=chat, text='रद्द केलं.'); return
    pend = PENDING.get(uid)
    if pend and pend[0] == 'otp':
        if mid:
            tg('deleteMessage', chat_id=chat, message_id=mid)
        if AUTH.stepup_verify(uid, text):
            PENDING.pop(uid, None)
            tg('sendMessage', chat_id=chat, text='✅ Verify झालं')
            on_callback('', uid, pend[2], pend[3], pend[1]); return
        tries = pend[4] + 1
        if tries >= 3:
            PENDING.pop(uid, None)
            AUTH.remove(uid)                      # संशयास्पद → session बंद, परत password + code
            tg('sendMessage', chat_id=chat, text='🔒 3 चुकीचे code — तुमचं session बंद केलं. परत login करा.')
            notify(f'🚨 Admin <code>{uid}</code> च्या संवेदनशील कृतीसाठी 3 चुकीचे 2FA code — session बंद केलं.')
        else:
            PENDING[uid] = pend[:4] + (tries,)
            tg('sendMessage', chat_id=chat, text=f'❌ चुकीचा code. {3 - tries} प्रयत्न बाकी (किंवा /cancel).')
        return
    if pend and pend[0] == 'seturl' and text and not text.startswith('/'):
        if not URL_RE.match(text):
            tg('sendMessage', chat_id=chat, text='❌ https://... असा URL द्या (किंवा /cancel)'); return
        url = text.rstrip('/')
        PENDING.pop(uid, None)
        if pend[1] == 'ALL':
            KOYEB_DEFAULT[0] = url
            for pid in list(PCS):
                enqueue(pid, 'seturl', url)
            tg('sendMessage', chat_id=chat, text=f'✅ {len(PCS)} PC ला पाठवलं. नवीन install साठी सुद्धा हाच URL '
                                                 f'(Render restart नंतरसाठी KOYEB_URL env सुद्धा बदला).')
        else:
            ok = enqueue(pend[1], 'seturl', url)
            tg('sendMessage', chat_id=chat, text='✅ पाठवलं — result इथे येईल.' if ok else '❌ PC सापडला नाही')
        return
    show(chat, None, main_text(), main_kb())

# ───────────────────────── Admin login (password + 4 प्रयत्न) ─────────────────────────
class Store:
    """Memory मध्ये राहतो; MongoDB दिला असेल तर प्रत्येक बदल तिथे सुद्धा save होतो."""
    def __init__(self, coll=None):
        self.coll, self.data, self._warned = coll, {}, False
        if coll is not None:
            for d in coll.find({}):
                self.data[d['_id']] = d

    def get(self, k):
        return self.data.get(k)

    def put(self, k, doc):
        doc['_id'] = k
        self.data[k] = doc
        if self.coll is not None:
            try:
                self.coll.replace_one({'_id': k}, doc, upsert=True)
            except Exception as e:
                self._warn(e)

    def delete(self, k):
        self.data.pop(k, None)
        if self.coll is not None:
            try:
                self.coll.delete_one({'_id': k})
            except Exception as e:
                self._warn(e)

    def _warn(self, e):
        if not self._warned:
            self._warned = True
            print(f'⚠️ MongoDB write failed: {e}')

MONGO_DB = [None]

def make_store():
    uri = os.environ.get('MONGODB_URI', '')
    if not uri:
        print('⚠️ MONGODB_URI नाही — admin login आणि lockout फक्त memory मध्ये (Render restart नंतर जातील)')
        return Store(None)
    try:
        from pymongo import MongoClient
        c = MongoClient(uri, serverSelectionTimeoutMS=8000, socketTimeoutMS=10000)
        coll = c[os.environ.get('MONGODB_DB', 'workerhub')]['auth']
        MONGO_DB[0] = c[os.environ.get('MONGODB_DB', 'workerhub')]
        coll.find_one({})                           # connection test
        print('✅ MongoDB जोडलं')
        return Store(coll)
    except Exception as e:
        print(f'❌ MongoDB जोडता आलं नाही ({e}) — memory वर चालतोय')
        return Store(None)

def fmt_left(sec):
    sec = int(sec)
    if sec > 10 * 365 * 86400:
        return 'कायमचं'
    h, m = divmod(sec // 60, 60)
    return f'{h}h {m}m' if h else f'{max(m, 1)}m'

def fmt_ago(ts):
    sec = int(time.time() - ts)
    return f'{sec // 86400}d पूर्वी' if sec >= 86400 else (f'{sec // 3600}h पूर्वी' if sec >= 3600 else f'{max(sec // 60, 0)}m पूर्वी')

def totp_code(secret, counter):
    """RFC 6238 (SHA1, 6 अंक, 30s)."""
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    h = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    o = h[-1] & 15
    return f'{(struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % 1000000:06d}'

def totp_verify(secret, code, last_used):
    """बरोबर असेल तर त्या 30s चा counter देतो; एकदा वापरलेला code पुन्हा चालत नाही (replay)."""
    code = re.sub(r'\s+', '', str(code))
    if not re.fullmatch(r'\d{6}', code):
        return None
    now = int(time.time() // 30)
    for c in (now - 1, now, now + 1):
        if c > last_used and eq(code, totp_code(secret, c)):
            return c
    return None

class Auth:
    def __init__(self, store):
        self.s = store
        self._reply = {}

    # ---- sessions
    def is_admin(self, uid):
        if uid in ENV_ADMINS:
            return True
        a = self.s.get(f'adm:{uid}')
        if not a:
            return False
        if SESSION_DAYS and time.time() - a['since'] > SESSION_DAYS * 86400:
            self.s.delete(f'adm:{uid}')
            return False
        return True

    def admin_ids(self):
        ids = set(ENV_ADMINS)
        for k in list(self.s.data):
            if k.startswith('adm:') and self.is_admin(int(k[4:])):
                ids.add(int(k[4:]))
        return ids

    def touch(self, uid):
        a = self.s.get(f'adm:{uid}')
        if a and time.time() - a.get('last', 0) > 3600:
            a['last'] = time.time()
            self.s.put(f'adm:{uid}', a)

    def listing(self):
        rows = [{'uid': u, 'name': 'env admin', 'since': 0, 'last': 0, 'env': True} for u in ENV_ADMINS]
        for k, a in list(self.s.data.items()):
            if k.startswith('adm:') and self.is_admin(a['uid']):
                rows.append({**a, 'env': False})
        return rows

    def remove(self, uid):
        if uid in ENV_ADMINS or f'adm:{uid}' not in self.s.data:
            return False
        self.s.delete(f'adm:{uid}')
        self.s.delete(f'su:{uid}')
        return True

    def remove_others(self, me):
        n = 0
        for k in [k for k in self.s.data if k.startswith('adm:')]:
            if int(k[4:]) != me:
                self.s.delete(k)
                self.s.delete(f'su:{k[4:]}')
                n += 1
        return n

    # ---- 2FA (step-up)
    def _glob(self):
        g = self.s.get('glob') or {}
        g.setdefault('fails', []); g.setdefault('paused_until', 0); g.setdefault('totp_last', 0)
        return g

    def stepup_valid(self, uid):
        if not TOTP_ON:
            return True
        d = self.s.get(f'su:{uid}')
        return bool(d and time.time() - d['at'] < STEPUP_SECS)

    def stepup_verify(self, uid, code):
        g = self._glob()
        c = totp_verify(ADMIN_TOTP_SECRET, code, g['totp_last'])
        if not c:
            return False
        g['totp_last'] = c
        self.s.put('glob', g)
        self.s.put(f'su:{uid}', {'at': time.time()})
        return True

    # ---- अनोळखी user चा flow
    def _say(self, chat, uid, text, kind='x', gap=1.0):
        now = time.time()
        if now - self._reply.get((uid, kind), 0) < gap:
            return
        self._reply[(uid, kind)] = now
        tg('sendMessage', chat_id=chat, text=text, parse_mode='HTML')

    def _grant(self, user, chat, now):
        uid = user['id']
        name = ' '.join(x for x in (user.get('first_name'), user.get('last_name')) if x) or str(uid)
        self.s.put(f'adm:{uid}', {'uid': uid, 'name': name[:40], 'username': user.get('username', ''),
                                  'since': now, 'last': now})
        self.s.put(f'su:{uid}', {'at': now})
        self.s.delete(f'att:{uid}')
        for o in self.admin_ids() - {uid}:
            tg('sendMessage', chat_id=o, parse_mode='HTML',
               text=f"🔔 नवा admin login: <b>{esc(name)}</b> (<code>{uid}</code>)")
        tg('sendMessage', chat_id=chat, text='✅ Login झाला. तुम्ही आता admin आहात.')
        show(chat, None, main_text(), main_kb())

    def _fail(self, chat, uid, user, att, g, now):
        att['fails'] += 1
        att['awaiting_until'] = now + 600
        g['fails'] = [t for t in g['fails'] if now - t < 3600] + [now]
        who = (f"{esc(' '.join(x for x in (user.get('first_name'), user.get('last_name')) if x) or uid)}"
               f"{' @' + esc(user['username']) if user.get('username') else ''} (<code>{uid}</code>)")
        if att['fails'] >= MAX_TRIES:
            att['locked_until'] = now + (LOCK_SECS or FOREVER)
            self.s.put(f'att:{uid}', att)
            self._say(chat, uid, f"🔒 {MAX_TRIES} चुकीचे प्रयत्न — तुम्ही block झालात ({fmt_left(att['locked_until'] - now)}).", 'lock', 0)
            notify(f'🚨 Login चे {MAX_TRIES} चुकीचे प्रयत्न: {who}\nBlock केलं.')
        else:
            self.s.put(f'att:{uid}', att)
            self._say(chat, uid, f"❌ चुकीचं. {MAX_TRIES - att['fails']} प्रयत्न बाकी.", 'wrong', 0)
        if len(g['fails']) >= GLOBAL_MAX and g['paused_until'] < now:
            g['paused_until'] = now + 3600
            notify(f"🚨 तासाभरात {len(g['fails'])} चुकीचे login प्रयत्न (अनेक users) — login 1 तास बंद केला.")
        self.s.put('glob', g)

    def handle_stranger(self, m):
        user, chat = m.get('from') or {}, m['chat']['id']
        uid, mid, text = user.get('id'), m.get('message_id'), (m.get('text') or '')
        now = time.time()
        g = self._glob()
        fresh = {'fails': 0, 'locked_until': 0, 'awaiting_until': 0, 'stage': 'pw', 'otp_until': 0}
        att = self.s.get(f'att:{uid}') or dict(fresh)
        if att['locked_until'] and att['locked_until'] <= now:
            att = dict(fresh)                                              # block संपला
        awaiting = att['awaiting_until'] > now
        if STEALTH_TRIGGER and not awaiting and text.strip() != STEALTH_TRIGGER:
            return                                                         # bot "मेलेला" वाटतो
        if mid:                                                            # password/code चॅटमध्ये राहू नये
            tg('deleteMessage', chat_id=chat, message_id=mid)
        if g['paused_until'] > now:
            self._say(chat, uid, '⏳ Login सध्या तात्पुरता बंद आहे. थोड्या वेळाने प्रयत्न करा.', 'pause', 30); return
        if att['locked_until'] > now:
            self._say(chat, uid, f"🔒 तुम्ही block आहात ({fmt_left(att['locked_until'] - now)} बाकी).", 'lock', 600); return
        if not awaiting or not text or text.startswith('/'):
            att['awaiting_until'], att['stage'] = now + 600, 'pw'
            self.s.put(f'att:{uid}', att)
            self._say(chat, uid, f"🔐 <b>Password पाठवा</b>\n{MAX_TRIES - att['fails']} प्रयत्न बाकी आहेत.\n"
                                 f"<i>(तुमचा message आपोआप delete होईल)</i>", 'prompt'); return
        if att.get('stage') == 'otp':
            if att.get('otp_until', 0) < now:
                att['stage'] = 'pw'
                self.s.put(f'att:{uid}', att)
                self._say(chat, uid, '⏱ Code ची वेळ संपली. परत password पाठवा.', 'prompt'); return
            c = totp_verify(ADMIN_TOTP_SECRET, text, g['totp_last'])
            if c:
                g['totp_last'] = c
                self.s.put('glob', g)
                self._grant(user, chat, now); return
            self._fail(chat, uid, user, att, g, now); return
        if eq(text.strip(), ADMIN_PASSWORD):
            if TOTP_ON:
                att['stage'], att['otp_until'], att['awaiting_until'] = 'otp', now + 120, now + 600
                self.s.put(f'att:{uid}', att)
                self._say(chat, uid, '✅ Password बरोबर.\nआता Authenticator app मधला <b>6 अंकी code</b> पाठवा (2 मिनिटांत).', 'otp', 0)
                return
            self._grant(user, chat, now); return
        self._fail(chat, uid, user, att, g, now)

AUTH = Auth(make_store())

app.secret_key = hashlib.sha256(
    f"flask|{os.environ.get('SESSION_KEY') or COLAB_SECRET}|{TG_TOKEN}".encode()).hexdigest()
app.config.update(SESSION_COOKIE_NAME='hub_session',           # storage_app uses the default 'session' cookie
                  SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '1') == '1',
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
TG_ON = bool(tgweb and tgweb.init(app, MONGO_DB[0], lambda t: notify(t)))
if tgweb and TG_ON:
    app.config['MAX_CONTENT_LENGTH'] = tgweb.CHUNK + 4 * 1024 * 1024        # upload 8 MB तुकड्यांत येतो
elif tgweb:
    print(f'ℹ️ Telegram accounts बंद: {tgweb.reason}')

def send_ephemeral(chat, text, secs=90):
    """Link/माहिती पाठवतो आणि `secs` नंतर chat मधून आपोआप delete करतो."""
    r = tg('sendMessage', chat_id=chat, parse_mode='HTML', disable_web_page_preview=True, text=text)
    m2 = (r.get('result') or {}).get('message_id') if isinstance(r, dict) else None
    if m2:
        t = threading.Timer(secs, tg, args=('deleteMessage',), kwargs={'chat_id': chat, 'message_id': m2})
        t.daemon = True
        t.start()

TG_EMOJI = {'online': '🟢', 'connecting': '🟡', 'reconnecting': '🟡', 'standby': '⚪', 'disabled': '⏸',
            'error': '🟠', 'revoked': '🔴'}

def ta_view():
    back = [{'text': '⬅️ Back', 'callback_data': 'm'}]
    if not TG_ON:
        why = tgweb.reason if tgweb else 'module नाही'
        return (f'👤 <b>Telegram Accounts</b>\n\n❌ बंद आहे: <b>{esc(why)}</b>\n\n'
                'Render env मध्ये <code>SESSION_KEY</code>, <code>MONGODB_URI</code> '
                '(आणि <code>TG_API_ID</code>/<code>TG_API_HASH</code>) टाका.'), {'inline_keyboard': [back]}
    accs = tgweb.M.accounts_view()
    lines = ['👤 <b>Telegram Accounts</b>', '']
    rows = []
    for a in accs:
        lines.append(f"{TG_EMOJI.get(a['state'], '❔')} <b>{esc(a['label'] or a['name'])}</b> — {a['state']}")
        rows.append([{'text': f"{TG_EMOJI.get(a['state'], '❔')} {a['label'] or a['name']}", 'callback_data': f"tp:{a['id']}"}])
    if not accs:
        lines.append('अजून कोणताही account नाही. UI उघडून ＋ दाबा.')
    st = tgweb.bw_status()
    lines += ['', f"📊 {st['used'] / 1048576:.1f} MB / {st['limit'] / 1073741824:.1f} GB ({st['month']})"]
    rows.append([{'text': '🌐 UI उघडा (एकदाचा link)', 'callback_data': 'tg'}])
    rows.append(back)
    return '\n'.join(lines), {'inline_keyboard': rows}

def tp_view(aid):
    a = next((x for x in tgweb.M.accounts_view() if x['id'] == aid), None)
    if not a:
        return None, None
    t = (f"{TG_EMOJI.get(a['state'], '❔')} <b>{esc(a['label'] or a['name'])}</b>  <code>{aid}</code>\n"
         f"नाव: {esc(a['name'])} · {esc(a['phone'])}\nस्थिती: {a['state']}"
         f"{(' — ' + esc(a['err'])) if a['err'] else ''}")
    kb = {'inline_keyboard': [
        [{'text': '⏸ Disconnect' if a['enabled'] else '▶️ Connect', 'callback_data': f'tc:{aid}'}],
        [{'text': '🗑 काढा', 'callback_data': f'tr:{aid}'}],
        [{'text': '⬅️ Back', 'callback_data': 'ta'}]]}
    return t, kb

def sb_view():
    back = [{'text': '⬅️ Back', 'callback_data': 'm'}]
    if STORAGE is None:
        return '📦 <b>Storage</b>\n\n❌ बंद आहे.', {'inline_keyboard': [back]}
    try:
        txt = STORAGE.status_text()
    except Exception as e:
        txt = f'status मिळाला नाही ({type(e).__name__})'
    rows = [[{'text': '🔓 Website login link (एकदाचा)', 'callback_data': 'sl'}],
            [{'text': '👤 Telegram user account login', 'callback_data': 'ul:begin'}],
            [{'text': '🔄 Refresh', 'callback_data': 'sb'}],
            back]
    return ('📦 <b>Storage — Backup Browser</b>\n\n' + esc(txt) +
            '\n\nCommands: /userlogin /userlogout /usercancel /unblock /status'), {'inline_keyboard': rows}

def admins_view(me):
    lines, rows = ['👥 <b>Admins</b>', ''], []
    for a in sorted(AUTH.listing(), key=lambda a: a['since']):
        you = ' <i>(तुम्ही)</i>' if a['uid'] == me else ''
        if a['env']:
            lines.append(f"📌 <code>{a['uid']}</code> — कायमचा (env){you}")
        else:
            lines.append(f"👤 <b>{esc(a['name'])}</b> <code>{a['uid']}</code>{you}\n    login {fmt_ago(a['since'])} · शेवटचा {fmt_ago(a['last'])}")
            if a['uid'] != me:
                rows.append([{'text': f"❌ {a['name'][:24]} काढा", 'callback_data': f"ar:{a['uid']}"}])
    rows.append([{'text': '🚪 Logout (मी)', 'callback_data': 'al'}])
    rows.append([{'text': '🔒 बाकी सगळे logout', 'callback_data': 'aa'}])
    rows.append([{'text': '⬅️ Back', 'callback_data': 'm'}])
    return '\n'.join(lines), {'inline_keyboard': rows}

def handle_update(u):
    if 'callback_query' in u:
        cb = u['callback_query']
        uid = cb['from']['id']
        if not AUTH.is_admin(uid):
            return                               # अनोळखी → काहीच उत्तर नाही
        AUTH.touch(uid)
        if STORAGE is not None and STORAGE.bot_api_claims(u):
            return                               # storage (Pyrogram side) answers this button
        msg = cb.get('message') or {}
        on_callback(cb['id'], uid, msg.get('chat', {}).get('id'), msg.get('message_id'), cb.get('data', ''))
    elif 'message' in u:
        m = u['message']
        if m.get('chat', {}).get('type') != 'private' or not (m.get('from') or {}).get('id'):
            return
        uid = m['from']['id']
        if AUTH.is_admin(uid):
            AUTH.touch(uid)
            if STORAGE is not None and STORAGE.bot_api_claims(u):
                return                           # storage command / user-login conversation
            on_message(uid, m['chat']['id'], (m.get('text') or '').strip(), m.get('message_id'))
        else:
            AUTH.handle_stranger(m)

def bot_loop():
    tg('deleteWebhook')
    offset = 0
    while True:
        r = tg('getUpdates', offset=offset, timeout=30, allowed_updates=['message', 'callback_query'])
        if not r.get('ok'):
            time.sleep(5)
            continue
        for u in r['result']:
            offset = u['update_id'] + 1
            try:
                handle_update(u)
            except Exception as e:
                print('update error:', e)

def monitor_loop():
    while True:
        time.sleep(15)
        for pcid, pc in list(PCS.items()):
            if not online(pc) and not pc['offline']:
                pc['offline'] = True
                notify(f"🔴 <b>{esc(pc['name'])}</b> offline झाला")


# ───────────────────────── public (सगळं 404 / token-protected) ─────────────────────────
# Health check / heartbeat: फक्त "ok" — यातून काहीही माहिती उघड होत नाही.
@app.get('/')
@app.get('/health')
@app.get('/healthz')
def health():
    return text_resp('ok')

def use_token(t):
    throttle_check()
    ok = False
    with LOCK:
        now = time.time()
        for k in [k for k, v in TOKENS.items() if v['exp'] < now or v['left'] <= 0]:
            del TOKENS[k]
        for k, v in TOKENS.items():
            if eq(k, t):
                v['left'] -= 1
                ok = True
    if not ok:
        bad_attempt()

@app.get('/i/<token>')
def install_script(token):
    use_token(token)
    src = (FILES / 'install.ps1').read_text(encoding='utf-8')
    root = root_url()
    return text_resp(src.replace('__BASE__', f'{root}/f/{token}').replace('__SELF__', f'{root}/i/{token}'))

@app.get('/f/<token>/<name>')
def get_file(token, name):
    use_token(token)
    if name == 'env':
        pcid = secrets.token_hex(4)
        lines = [f'COLAB_SECRET={COLAB_SECRET}', f'PANEL_PASSWORD={PANEL_PASSWORD}', 'PANEL_PORT=8765',
                 f'REPORT_BASE={root_url()}', f'PC_ID={pcid}', f'PC_KEY={agent_key(pcid)}']
        if KOYEB_DEFAULT[0]:
            lines.append(f'KOYEB_URL={KOYEB_DEFAULT[0]}')
        return text_resp('\n'.join(lines) + '\n')
    if name in ('watermark.py', 'worker.py'):
        f = FILES / name
        if not f.exists():
            return text_resp(f'# {name} server वर ठेवलेला नाही\n'), 500
        return text_resp(f.read_text(encoding='utf-8'))
    abort(404)


# ───────────────────────── PC ↔ server (long-poll) ─────────────────────────
@app.post('/api/sync')
def api_sync():
    throttle_check()
    pcid = request.headers.get('X-PC-Id', '')
    if not re.fullmatch(r'[0-9a-f]{8}', pcid) or not eq(request.headers.get('X-PC-Key', ''), agent_key(pcid)):
        bad_attempt()
    d = request.get_json(silent=True) or {}
    url = str(d.get('url', ''))
    url = url if url.startswith('https://') and len(url) < 200 else ''
    name = str(d.get('host', '?'))[:40] or '?'
    with LOCK:
        pc = PCS.get(pcid)
        new = pc is None
        if new:
            pc = PCS[pcid] = {'ev': threading.Event(), 'cmds': [], 'offline': False, 'url': '', 'name': name, 'st': {}}
        old_url, was_off = pc['url'], pc['offline']
        pc.update(st=d, ts=time.time(), name=name, url=url, offline=False)
    kb_pc = {'inline_keyboard': [[{'text': '📋 Menu', 'callback_data': f'p:{pcid}'}]]}
    if new and time.time() - BOOT > 90:
        notify(f'🆕 <b>{esc(name)}</b> जोडला गेला', kb_pc)
    elif was_off:
        notify(f'🟢 <b>{esc(name)}</b> परत online', kb_pc)
    elif url and url != old_url and not new:
        notify(f'🔗 <b>{esc(name)}</b> चा terminal URL बदलला', kb_pc)
    for r in (d.get('results') or [])[:10]:
        body = str(r.get('text', ''))[-3500:]
        if not body.strip():
            continue
        notify(f'🖥 <b>{esc(name)}</b>\n<pre>{esc(body)}</pre>')
    with LOCK:
        if not pc['cmds']:
            pc['ev'].clear()
        has = bool(pc['cmds'])
    if not has:
        pc['ev'].wait(SYNC_WAIT)
    with LOCK:
        cmds, pc['cmds'] = pc['cmds'][:], []
        pc['ts'] = time.time()
    return jsonify(cmds=cmds)


# ───────────────────────── start background threads ─────────────────────────
_started = False
def start_background():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    if TG_ON:
        tgweb.start()

if __name__ == '__main__':                       # standalone (without Backup Browser)
    start_background()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)), threaded=True)
