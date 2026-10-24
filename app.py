"""
Render server = मूक मध्यस्थ (silent middleman) + Telegram admin bot.

• बाहेरच्या कुणालाही काहीही दिसत नाही — सगळे routes रिकामा 404 देतात
  (फक्त /, /health, /healthz → "ok", तुमच्या heartbeat / health check साठी).
• सगळं control तुमच्या Telegram bot वर (फक्त ADMIN_CHAT_ID मधले user चालतात).
• PC → server: दर PC long-poll करतो (/api/sync). Button दाबताच command लगेच PC ला जातो.

Environment variables (Render dashboard → Environment):
  TELEGRAM_BOT_TOKEN  तुमच्या control bot चा token                         [required]
  ADMIN_CHAT_ID       तुमचा Telegram user id (अनेक असतील तर comma ने)      [required]
  COLAB_SECRET        तुमची secret (नवीन PC च्या .env मध्ये जाते)           [required]
  PANEL_PASSWORD      सगळ्या PC च्या web terminal चा password               [required]
  KOYEB_URL           default job/poll server URL (नवीन PC साठी)            [optional]
  AGENT_MASTER        PC credentials ची master key (नसेल तर derive होते)    [optional]
  PUBLIC_URL          https://xxx.onrender.com (नसेल तर Render चा आपोआप)   [optional]
"""
import os, re, hmac, html, json, time, hashlib, secrets, threading, collections
import urllib.request, urllib.parse
from pathlib import Path
from flask import Flask, request, Response, abort, jsonify, has_request_context

app = Flask(__name__)
FILES = Path(__file__).parent / 'files'

COLAB_SECRET = os.environ['COLAB_SECRET']
PANEL_PASSWORD = os.environ['PANEL_PASSWORD']
TG_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
ADMIN_IDS = {int(x) for x in os.environ['ADMIN_CHAT_ID'].replace(' ', '').split(',') if x}
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
    for chat in ADMIN_IDS:
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
        [{'text': '🔑 Password', 'callback_data': f'k:{pcid}'},
         {'text': '♻️ Manager restart', 'callback_data': f'c:{pcid}:rmgr'}],
    ]
    url = PCS[pcid].get('url')
    if url:
        rows.append([{'text': '🖥 Terminal उघडा', 'url': url}])
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
    ans = lambda text='', alert=False: tg('answerCallbackQuery', callback_query_id=cbid,
                                          text=text, show_alert='true' if alert else 'false')
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
    elif kind == 'k':
        ans(f'🔑 Panel password:\n{PANEL_PASSWORD}', True)
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

def on_message(uid, chat, text):
    if text in ('/cancel',):
        PENDING.pop(uid, None)
        tg('sendMessage', chat_id=chat, text='रद्द केलं.'); return
    pend = PENDING.get(uid)
    if pend and text and not text.startswith('/'):
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

def handle_update(u):
    if 'callback_query' in u:
        cb = u['callback_query']
        uid = cb['from']['id']
        if uid not in ADMIN_IDS:
            return                               # अनोळखी → काहीच उत्तर नाही
        msg = cb.get('message') or {}
        on_callback(cb['id'], uid, msg.get('chat', {}).get('id'), msg.get('message_id'), cb.get('data', ''))
    elif 'message' in u:
        m = u['message']
        uid = (m.get('from') or {}).get('id')
        if uid not in ADMIN_IDS or m.get('chat', {}).get('type') != 'private':
            return
        on_message(uid, m['chat']['id'], (m.get('text') or '').strip())

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
    kb_pc = {'inline_keyboard': [[{'text': '📋 Menu', 'callback_data': f'p:{pcid}'}] +
                                 ([{'text': '🖥 Terminal', 'url': url}] if url else [])]}
    if new and time.time() - BOOT > 90:
        notify(f'🆕 <b>{esc(name)}</b> जोडला गेला', kb_pc)
    elif was_off:
        notify(f'🟢 <b>{esc(name)}</b> परत online', kb_pc)
    elif url and url != old_url and not new:
        notify(f'🔗 <b>{esc(name)}</b> चा terminal URL बदलला', kb_pc)
    for r in (d.get('results') or [])[:10]:
        body = str(r.get('text', ''))[-3500:]
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

if os.environ.get('NO_BG') != '1':
    start_background()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)), threaded=True)
