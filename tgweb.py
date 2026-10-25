"""
tgweb.py — Render वर अनेक Telegram account चालवणारा web client.

• Account login server वर (Telethon). Session string Fernet ने encrypt होऊन MongoDB मध्ये.
  PC बंद झाला / browser data गेला तरी account चालू राहतो.
• UI: /app — फक्त bot ने दिलेल्या एकदाच चालणाऱ्या link ने उघडतो (बाकी सगळ्यांना रिकामा 404).
• Bandwidth: media कधीच आपोआप येत नाही (tap केल्यावरच). महिन्याचा counter + मर्यादा.
• एका वेळी एकच instance accounts चालवतो (Mongo lease) — नाहीतर Telegram session कायमचा रद्द करतो.

Env: SESSION_KEY (Fernet key), MONGODB_URI, TG_API_ID / TG_API_HASH (default; प्रत्येक login ला वेगळं देता येतं),
     BW_LIMIT_GB (4), MAX_ACCOUNTS (6), MAX_UPLOAD_MB (15), MAX_MEDIA_MB (25)
"""
import os, io, re, time, json, uuid, atexit, asyncio, threading, secrets, collections
from flask import Blueprint, request, session, jsonify, Response, abort

try:
    from cryptography.fernet import Fernet
except Exception:                                   # pragma: no cover
    Fernet = None
try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
except Exception:                                   # pragma: no cover
    TelegramClient = events = StringSession = None

SESSION_KEY = os.environ.get('SESSION_KEY', '').strip()
DEF_API_ID = os.environ.get('TG_API_ID', '').strip()
DEF_API_HASH = os.environ.get('TG_API_HASH', '').strip()
BW_LIMIT = int(float(os.environ.get('BW_LIMIT_GB', '4')) * 1024 ** 3)
MAX_ACCOUNTS = int(os.environ.get('MAX_ACCOUNTS', '6'))
MAX_UPLOAD = int(float(os.environ.get('MAX_UPLOAD_MB', '15')) * 1024 * 1024)
MAX_MEDIA = int(float(os.environ.get('MAX_MEDIA_MB', '25')) * 1024 * 1024)
UI_SESSION_SECS = 7200
LEASE_SECS = 45
INSTANCE = uuid.uuid4().hex[:8]

M = None                 # Manager (init() नंतर)
reason = ''              # feature बंद असेल तर कारण
TOKENS, TOKENS_LOCK = {}, threading.Lock()
FAILS = collections.defaultdict(list)
BW = {'month': '', 'bytes': 0, 'dirty': False}
BW_LOCK = threading.Lock()
_F = [None]


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


# ───────────────────────── crypto ─────────────────────────
def _fernet():
    if _F[0] is None:
        _F[0] = Fernet(SESSION_KEY.encode())
    return _F[0]

def enc(s):
    return _fernet().encrypt(str(s).encode()).decode()

def dec(t):
    return _fernet().decrypt(t.encode()).decode()


# ───────────────────────── bandwidth ─────────────────────────
def _month():
    return time.strftime('%Y-%m')

def bw_add(n):
    with BW_LOCK:
        if BW['month'] != _month():
            BW.update(month=_month(), bytes=0)
        BW['bytes'] += int(n)
        BW['dirty'] = True

def bw_status():
    with BW_LOCK:
        if BW['month'] != _month():
            BW.update(month=_month(), bytes=0)
        return {'used': BW['bytes'], 'limit': BW_LIMIT, 'month': BW['month']}


# ───────────────────────── serialization (Telethon objects → JSON) ─────────────────────────
def _name(e):
    if e is None:
        return ''
    t = getattr(e, 'title', None)
    if t:
        return str(t)
    return ' '.join(str(x) for x in (getattr(e, 'first_name', None), getattr(e, 'last_name', None)) if x) \
        or str(getattr(e, 'username', '') or '')

def media_info(m):
    if not getattr(m, 'media', None):
        return None
    f = getattr(m, 'file', None)
    kind = 'other'
    for attr, k in (('photo', 'photo'), ('sticker', 'sticker'), ('voice', 'voice'), ('video_note', 'videonote'),
                    ('gif', 'gif'), ('video', 'video'), ('audio', 'audio'), ('document', 'file')):
        if getattr(m, attr, None):
            kind = k
            break
    return {'kind': kind, 'size': int(getattr(f, 'size', 0) or 0),
            'name': str(getattr(f, 'name', '') or ''), 'mime': str(getattr(f, 'mime_type', '') or '')}

def ser_msg(m):
    rt = getattr(m, 'reply_to', None)
    text = getattr(m, 'message', '') or ''
    if not text and getattr(m, 'action', None):
        text = '⚙️ ' + type(m.action).__name__
    d = getattr(m, 'date', None)
    return {'id': m.id, 'out': bool(getattr(m, 'out', False)), 'ts': int(d.timestamp()) if d else 0,
            'text': text, 'from': _name(getattr(m, 'sender', None)),
            'reply': getattr(rt, 'reply_to_msg_id', None) if rt else None,
            'edited': bool(getattr(m, 'edit_date', None)), 'fwd': bool(getattr(m, 'fwd_from', None)),
            'media': media_info(m)}

def ser_dialog(d):
    m = d.message
    last = (getattr(m, 'message', '') or '') if m else ''
    if m and not last and getattr(m, 'media', None):
        last = '📎 ' + (media_info(m) or {}).get('kind', 'media')
    dt = getattr(m, 'date', None) if m else None
    kind = 'user' if d.is_user else ('group' if d.is_group else 'channel')
    return {'id': d.id, 'name': d.name or '', 'unread': d.unread_count or 0, 'last': last[:90],
            'ts': int(dt.timestamp()) if dt else 0, 'type': kind, 'pinned': bool(getattr(d, 'pinned', False)),
            'out': bool(getattr(m, 'out', False)) if m else False}


def default_factory(sess, api_id, api_hash):
    return TelegramClient(StringSession(sess or ''), int(api_id), api_hash, device_model='Worker Hub',
                          app_version='1.0', system_version='Linux', lang_code='en',
                          connection_retries=None, retry_delay=3, auto_reconnect=True, flood_sleep_threshold=60)

client_factory = default_factory          # tests मध्ये बदलता येतो


REVOKED = {'AuthKeyUnregisteredError', 'SessionRevokedError', 'UserDeactivatedError',
           'UserDeactivatedBanError', 'AuthKeyDuplicatedError', 'SessionExpiredError', 'PermissionError'}


# ───────────────────────── Manager ─────────────────────────
class Manager:
    def __init__(self, db, notify):
        self.db, self.notify = db, notify
        self.acc, self.meta = db['tgacc'], db['meta']
        self.loop = asyncio.new_event_loop()
        self.clients, self.state, self.next_try = {}, {}, {}
        self.logins, self.cache = {}, {}
        self.events = collections.deque(maxlen=500)
        self.seq = 0
        self.cond = threading.Condition()
        self.lease = False
        self.last_ping = {}
        self.media_sem = None
        self.instance = uuid.uuid4().hex[:8]

    # --- thread / loop
    def start(self):
        try:
            d = self.meta.find_one({'_id': 'bw:' + _month()}) or {}
            with BW_LOCK:
                BW.update(month=_month(), bytes=int(d.get('bytes', 0)), dirty=False)
        except Exception as e:
            print('bw load failed:', e)
        atexit.register(self._release)
        threading.Thread(target=self._run, daemon=True, name='tg-loop').start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.media_sem = asyncio.Semaphore(2)
        self.loop.create_task(self.supervisor())
        self.loop.run_forever()

    def call(self, coro, timeout=60):
        if not self.loop.is_running():
            coro.close()
            raise ApiError('Telegram engine अजून सुरू झालं नाही — थोड्या वेळाने प्रयत्न करा', 503)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # --- lease (एकाच वेळी एकच instance)
    def _lease(self):
        now = time.time()
        try:
            from pymongo import ReturnDocument
            r = self.meta.find_one_and_update(
                {'_id': 'lease', '$or': [{'owner': self.instance}, {'until': {'$lt': now}}]},
                {'$set': {'owner': self.instance, 'until': now + LEASE_SECS}},
                upsert=True, return_document=ReturnDocument.AFTER)
            return bool(r and r.get('owner') == self.instance)
        except Exception:
            return False                           # DuplicateKey = दुसऱ्या instance कडे lease

    def _release(self):
        try:
            self.meta.update_one({'_id': 'lease', 'owner': self.instance}, {'$set': {'until': 0}})
        except Exception:
            pass

    def _docs(self):
        return sorted(self.acc.find({}), key=lambda d: d.get('created', 0))

    def _flush_bw(self):
        with BW_LOCK:
            if not BW['dirty']:
                return
            month, n = BW['month'], BW['bytes']
            BW['dirty'] = False
        try:
            self.meta.replace_one({'_id': 'bw:' + month}, {'_id': 'bw:' + month, 'bytes': n}, upsert=True)
        except Exception as e:
            print('bw flush failed:', e)

    # --- supervisor
    async def supervisor(self):
        loop = asyncio.get_running_loop()
        last_bw = 0
        while True:
            try:
                got = await loop.run_in_executor(None, self._lease)
                if not got and self.lease:
                    await self.stop_all()
                    self.notify('⚠️ Telegram accounts थांबवले — दुसरा instance चालू आहे (lease गेली).')
                self.lease = got
                if got:
                    await self.reconcile(await loop.run_in_executor(None, self._docs))
                self.expire_logins()
                if time.time() - last_bw > 30:
                    await loop.run_in_executor(None, self._flush_bw)
                    last_bw = time.time()
            except Exception as e:
                print('supervisor error:', e)
            await asyncio.sleep(15)

    async def reconcile(self, docs):
        want = {d['_id']: d for d in docs if d.get('enabled', True)}
        for aid in [a for a in self.clients if a not in want]:
            await self.stop_client(aid)
        for aid, doc in want.items():
            if aid in self.clients:
                cl = self.clients[aid]
                if time.time() - self.last_ping.get(aid, 0) > 600:
                    self.last_ping[aid] = time.time()
                    try:
                        await asyncio.wait_for(cl.get_me(), 30)
                        self.state[aid] = {'state': 'online', 'err': ''}
                    except Exception as e:
                        await self._fail(aid, doc, e)
                elif not cl.is_connected():
                    self.state[aid] = {'state': 'reconnecting', 'err': ''}
                continue
            if self.state.get(aid, {}).get('state') == 'revoked' or time.time() < self.next_try.get(aid, 0):
                continue
            await self.start_client(doc)
        for d in docs:
            if not d.get('enabled', True):
                self.state[d['_id']] = {'state': 'disabled', 'err': ''}

    async def _fail(self, aid, doc, e):
        revoked = type(e).__name__ in REVOKED
        was = self.state.get(aid, {}).get('state')
        self.state[aid] = {'state': 'revoked' if revoked else 'error', 'err': f'{type(e).__name__}: {e}'[:140]}
        cl = self.clients.pop(aid, None)
        if cl:
            try:
                await cl.disconnect()
            except Exception:
                pass
        self.next_try[aid] = time.time() + 60
        if revoked and was != 'revoked':
            self.notify(f"🚨 Telegram account <b>{doc.get('label', aid)}</b> चं session रद्द झालं ({type(e).__name__}). "
                        f"UI मधून परत login करावं लागेल.")

    async def start_client(self, doc):
        aid, cl = doc['_id'], None
        self.state[aid] = {'state': 'connecting', 'err': ''}
        try:
            cl = client_factory(dec(doc['session_enc']), dec(doc['api_id_enc']), dec(doc['api_hash_enc']))
            await asyncio.wait_for(cl.connect(), 45)
            if not await cl.is_user_authorized():
                raise PermissionError('session रद्द / लॉगआउट झालं')
            await asyncio.wait_for(cl.get_dialogs(limit=100), 90)        # entity cache भरतो
            self._hook(aid, cl)
            self.clients[aid] = cl
            self.state[aid] = {'state': 'online', 'err': ''}
            self.last_ping[aid] = time.time()
        except Exception as e:
            if cl is not None:
                self.clients[aid] = cl
            await self._fail(aid, doc, e)

    async def stop_client(self, aid):
        cl = self.clients.pop(aid, None)
        if cl:
            try:
                await cl.disconnect()
            except Exception:
                pass
        self.state[aid] = {'state': 'disabled', 'err': ''}

    async def stop_all(self):
        for aid in list(self.clients):
            await self.stop_client(aid)

    # --- live updates
    def _hook(self, aid, cl):
        async def on_new(ev):
            self.push(aid, 'new', getattr(ev, 'chat_id', None), ser_msg(ev.message))
        async def on_edit(ev):
            self.push(aid, 'edit', getattr(ev, 'chat_id', None), ser_msg(ev.message))
        async def on_del(ev):
            self.push(aid, 'del', getattr(ev, 'chat_id', None), None, list(getattr(ev, 'deleted_ids', []) or []))
        cl.add_event_handler(on_new, events.NewMessage())
        cl.add_event_handler(on_edit, events.MessageEdited())
        cl.add_event_handler(on_del, events.MessageDeleted())

    def push(self, aid, kind, chat_id, msg, ids=None):
        self.cache.pop(aid, None)
        with self.cond:
            self.seq += 1
            self.events.append({'seq': self.seq, 'acc': aid, 'type': kind, 'chat': chat_id, 'msg': msg, 'ids': ids})
            self.cond.notify_all()

    # --- helpers
    def _cl(self, aid):
        cl = self.clients.get(aid)
        if not cl:
            raise ApiError('हा account सध्या online नाही', 409)
        return cl

    # --- API coroutines (Flask thread वरून call() ने)
    async def a_dialogs(self, aid):
        c = self.cache.get(aid)
        if c and time.time() - c[0] < 8:
            return c[1]
        out = [ser_dialog(d) for d in await self._cl(aid).get_dialogs(limit=60)]
        self.cache[aid] = (time.time(), out)
        return out

    async def a_messages(self, aid, chat, before, limit):
        ms = await self._cl(aid).get_messages(chat, limit=limit, offset_id=before or 0)
        return [ser_msg(m) for m in reversed(list(ms))]

    async def a_send(self, aid, chat, text, reply_to):
        m = await self._cl(aid).send_message(chat, text, reply_to=reply_to or None)
        bw_add(len(text.encode()) + 300)
        return ser_msg(m)

    async def a_edit(self, aid, chat, mid, text):
        m = await self._cl(aid).edit_message(chat, mid, text)
        bw_add(len(text.encode()) + 300)
        return ser_msg(m)

    async def a_delete(self, aid, chat, mid):
        await self._cl(aid).delete_messages(chat, [mid])

    async def a_read(self, aid, chat, max_id):
        await self._cl(aid).send_read_acknowledge(chat, max_id=max_id)

    async def a_upload(self, aid, chat, data, name, caption):
        bio = io.BytesIO(data)
        bio.name = name or 'file'
        m = await self._cl(aid).send_file(chat, bio, caption=caption or None)
        bw_add(len(data) + 500)
        return ser_msg(m)

    async def a_media(self, aid, chat, mid, thumb, confirm):
        cl = self._cl(aid)
        m = await cl.get_messages(chat, ids=mid)
        info = media_info(m) if m else None
        if not info:
            raise ApiError('media सापडला नाही', 404)
        size = info['size']
        if size > MAX_MEDIA and not thumb:
            raise ApiError(f'फाइल {MAX_MEDIA // 1048576} MB पेक्षा मोठी आहे', 413)
        st = bw_status()
        if st['used'] + size > st['limit']:
            raise ApiError('या महिन्याची bandwidth मर्यादा संपली — फक्त text चालतो', 429)
        async with self.media_sem:
            if thumb:
                try:
                    data = await cl.download_media(m, file=bytes, thumb=-1)
                except Exception:
                    data = None
            else:
                data = await cl.download_media(m, file=bytes)
        if not data:
            raise ApiError('download अयशस्वी', 502)
        bw_add(len(data))
        return data, info

    # --- login
    def expire_logins(self):
        for lid in [l for l, v in self.logins.items() if v['exp'] < time.time()]:
            v = self.logins.pop(lid)
            asyncio.ensure_future(self._quiet_disc(v['cl']), loop=self.loop)

    async def _quiet_disc(self, cl):
        try:
            await cl.disconnect()
        except Exception:
            pass

    async def a_login_start(self, label, phone, api_id, api_hash):
        if len(self.acc_ids()) >= MAX_ACCOUNTS:
            raise ApiError(f'जास्तीत जास्त {MAX_ACCOUNTS} accounts (MAX_ACCOUNTS env)')
        if len(self.logins) >= 3:
            raise ApiError('आधीचे login पूर्ण करा / 10 मिनिटं थांबा')
        phone = re.sub(r'[^\d+]', '', phone)
        if not re.fullmatch(r'\+\d{8,15}', phone):
            raise ApiError('फोन नंबर +देशकोड सकट द्या (उदा. +919876543210)')
        cl = client_factory('', api_id, api_hash)
        try:
            await asyncio.wait_for(cl.connect(), 30)
            sent = await cl.send_code_request(phone)
        except Exception as e:
            await self._quiet_disc(cl)
            raise ApiError(f'{e}'[:160])
        lid = secrets.token_urlsafe(9)
        self.logins[lid] = {'cl': cl, 'phone': phone, 'hash': sent.phone_code_hash, 'label': label[:30],
                            'api': (str(api_id), api_hash), 'exp': time.time() + 600, 'tries': 0}
        return {'login_id': lid, 'next': 'code'}

    async def a_login_code(self, lid, code):
        v = self._login(lid)
        try:
            await v['cl'].sign_in(phone=v['phone'], code=re.sub(r'\D', '', code), phone_code_hash=v['hash'])
        except Exception as e:
            if type(e).__name__ == 'SessionPasswordNeededError':
                return {'next': 'password'}
            self._bump(lid, v)
            raise ApiError(f'{e}'[:160])
        return await self._finish(lid, v)

    async def a_login_password(self, lid, pw):
        v = self._login(lid)
        try:
            await v['cl'].sign_in(password=pw)
        except Exception as e:
            self._bump(lid, v)
            raise ApiError(f'{e}'[:160])
        return await self._finish(lid, v)

    def _login(self, lid):
        v = self.logins.get(lid)
        if not v or v['exp'] < time.time():
            raise ApiError('login ची वेळ संपली — परत सुरू करा', 410)
        return v

    def _bump(self, lid, v):
        v['tries'] += 1
        if v['tries'] >= 5:
            self.logins.pop(lid, None)
            asyncio.ensure_future(self._quiet_disc(v['cl']), loop=self.loop)

    async def _finish(self, lid, v):
        cl = v['cl']
        me = await cl.get_me()
        loop = asyncio.get_running_loop()
        if any(d.get('uid') == me.id for d in await loop.run_in_executor(None, self._docs)):
            self.logins.pop(lid, None)
            await self._quiet_disc(cl)
            raise ApiError('हा Telegram account आधीच जोडलेला आहे')
        aid = 'a_' + secrets.token_hex(4)
        name = _name(me)
        doc = {'_id': aid, 'label': v['label'] or name, 'name': name, 'phone': v['phone'], 'uid': me.id,
               'api_id_enc': enc(v['api'][0]), 'api_hash_enc': enc(v['api'][1]),
               'session_enc': enc(cl.session.save()), 'created': time.time(), 'enabled': True}
        await loop.run_in_executor(None, self.acc.insert_one, doc)
        self.logins.pop(lid, None)
        try:
            await asyncio.wait_for(cl.get_dialogs(limit=100), 90)
        except Exception:
            pass
        self._hook(aid, cl)
        self.clients[aid] = cl
        self.state[aid] = {'state': 'online', 'err': ''}
        return {'next': 'done', 'account': aid, 'name': name}

    # --- bot / UI साठी sync entry points
    def acc_ids(self):
        return [d['_id'] for d in self._docs()]

    def accounts_view(self):
        out = []
        for d in self._docs():
            st = self.state.get(d['_id'], {'state': 'connecting' if d.get('enabled', True) else 'disabled', 'err': ''})
            if not self.lease and d.get('enabled', True):
                st = {'state': 'standby', 'err': 'दुसरा instance चालवतोय'}
            out.append({'id': d['_id'], 'label': d.get('label', ''), 'name': d.get('name', ''), 'phone': d.get('phone', ''),
                        'enabled': d.get('enabled', True), 'state': st['state'], 'err': st.get('err', '')})
        return out

    def set_enabled(self, aid, on):
        r = self.acc.update_one({'_id': aid}, {'$set': {'enabled': bool(on)}})
        if not r.matched_count:
            return False
        if not on:
            self.call(self.stop_client(aid))
        else:
            self.next_try.pop(aid, None)
            self.state.pop(aid, None)
        return True

    def remove_account(self, aid, logout):
        doc = self.acc.find_one({'_id': aid})
        if not doc:
            return False
        async def go():
            cl = self.clients.pop(aid, None)
            if cl:
                try:
                    await (cl.log_out() if logout else cl.disconnect())
                except Exception:
                    pass
        self.call(go())
        self.acc.delete_one({'_id': aid})
        self.state.pop(aid, None)
        return True


# ───────────────────────── Flask blueprint (/app) ─────────────────────────
bp = Blueprint('tgapp', __name__, url_prefix='/app')

def new_token():
    t = secrets.token_urlsafe(24)
    with TOKENS_LOCK:
        TOKENS[t] = time.time() + 120
    return t

def _ip():
    return request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or request.remote_addr or '?'

def session_ok():
    return bool(session.get('tg_ok')) and time.time() < session.get('tg_exp', 0)

@bp.before_request
def gate():
    if M is None:
        abort(404)
    p = request.path.rstrip('/')
    if request.method == 'GET' and p == '/app' and request.args.get('k'):
        return Response(ENTER_PAGE, mimetype='text/html', headers=_sec_headers())
    if request.method == 'POST' and p == '/app/enter':
        return None
    if not session_ok():
        abort(404)                                   # बाहेरच्याला काहीच नाही — रिकामा 404
    if request.method == 'POST':
        if request.headers.get('X-Requested-With') != 'app':
            abort(404)
        o = request.headers.get('Origin')
        if o and o.split('://', 1)[-1] != request.host:
            abort(404)
    return None

def _sec_headers():
    return {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; connect-src 'self'; img-src 'self' data: blob:; "
                                       "media-src 'self' blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}

@bp.post('/enter')
def enter():
    d = request.get_json(silent=True) or {}
    tok, k, now = str(d.get('k', '')), _ip(), time.time()
    FAILS[k] = [t for t in FAILS[k] if now - t < 300]
    if len(FAILS[k]) >= 5:
        abort(404)
    hit = None
    with TOKENS_LOCK:
        for t in [t for t, e in TOKENS.items() if e < now]:
            del TOKENS[t]
        for t in list(TOKENS):
            if secrets.compare_digest(t.encode(), tok.encode()):
                hit = t
        if hit:
            del TOKENS[hit]                          # एकदाच
    if not hit:
        FAILS[k].append(now)
        time.sleep(1)
        abort(404)
    session.clear()
    session['tg_ok'], session['tg_exp'] = True, now + UI_SESSION_SECS
    return jsonify(ok=1)

def api(fn):
    import functools
    @functools.wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except ApiError as e:
            return jsonify(error=str(e)), e.code
        except Exception as e:
            return jsonify(error=f'{type(e).__name__}: {e}'[:200]), 500
    return w

def _int(name, default=None):
    try:
        return int(request.values.get(name, default))
    except (TypeError, ValueError):
        raise ApiError(f'{name} चुकीचा')

def _acc():
    aid = request.values.get('acc') or (request.get_json(silent=True) or {}).get('acc')
    if not aid or not re.fullmatch(r'a_[0-9a-f]{8}', aid):
        raise ApiError('account निवडा')
    return aid

def _json():
    return request.get_json(silent=True) or {}

@bp.get('', strict_slashes=False)
def ui():
    return Response(UI_PAGE, mimetype='text/html', headers=_sec_headers())

@bp.get('/api/state')
@api
def api_state():
    return jsonify(accounts=M.accounts_view(), bw=bw_status(), default_api=bool(DEF_API_ID and DEF_API_HASH),
                   max_upload=MAX_UPLOAD)

@bp.get('/api/dialogs')
@api
def api_dialogs():
    return jsonify(dialogs=M.call(M.a_dialogs(_acc())))

@bp.get('/api/messages')
@api
def api_messages():
    lim = max(1, min(_int('limit', 30), 60))
    return jsonify(messages=M.call(M.a_messages(_acc(), _int('chat'), _int('before', 0), lim)))

@bp.post('/api/send')
@api
def api_send():
    d = _json()
    text = str(d.get('text', ''))
    if not text.strip() or len(text) > 4096:
        raise ApiError('text रिकामा किंवा 4096 पेक्षा मोठा')
    return jsonify(message=M.call(M.a_send(_acc(), int(d['chat']), text, d.get('reply_to'))))

@bp.post('/api/edit')
@api
def api_edit():
    d = _json()
    text = str(d.get('text', ''))
    if not text.strip() or len(text) > 4096:
        raise ApiError('text रिकामा किंवा मोठा')
    return jsonify(message=M.call(M.a_edit(_acc(), int(d['chat']), int(d['id']), text)))

@bp.post('/api/delete')
@api
def api_delete():
    d = _json()
    M.call(M.a_delete(_acc(), int(d['chat']), int(d['id'])))
    return jsonify(ok=1)

@bp.post('/api/read')
@api
def api_read():
    d = _json()
    M.call(M.a_read(_acc(), int(d['chat']), int(d.get('max_id', 0))))
    return jsonify(ok=1)

@bp.post('/api/upload')
@api
def api_upload():
    if (request.content_length or 0) > MAX_UPLOAD + 65536:
        raise ApiError(f'फाइल {MAX_UPLOAD // 1048576} MB पेक्षा मोठी आहे', 413)
    f = request.files.get('file')
    if not f:
        raise ApiError('फाइल नाही')
    data = f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise ApiError(f'फाइल {MAX_UPLOAD // 1048576} MB पेक्षा मोठी आहे', 413)
    st = bw_status()
    if st['used'] + len(data) > st['limit']:
        raise ApiError('bandwidth मर्यादा संपली', 429)
    name = re.sub(r'[^\w.\- ]', '_', f.filename or 'file')[:80]
    return jsonify(message=M.call(M.a_upload(_acc(), _int('chat'), data, name, request.form.get('caption', '')[:1000]), 180))

INLINE_OK = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'video/mp4', 'audio/mpeg', 'audio/ogg'}

@bp.get('/api/media')
@api
def api_media():
    thumb = request.args.get('thumb') == '1'
    data, info = M.call(M.a_media(_acc(), _int('chat'), _int('id'), thumb, request.args.get('ok') == '1'), 180)
    mime = info['mime'] if info['mime'] in INLINE_OK else 'application/octet-stream'
    if thumb or info['kind'] == 'photo':
        mime = 'image/jpeg' if mime == 'application/octet-stream' else mime
    disp = 'inline' if mime in INLINE_OK else 'attachment'
    fname = re.sub(r'[^\w.\- ]', '_', info['name'] or f"file_{request.args.get('id')}")
    return Response(data, mimetype=mime, headers={
        'Content-Disposition': f"{disp}; filename*=UTF-8''{fname}", 'X-Content-Type-Options': 'nosniff',
        'Cache-Control': 'private, max-age=86400', 'Content-Security-Policy': "default-src 'none'; sandbox"})

@bp.get('/api/events')
@api
def api_events():
    since = _int('since', 0)
    end = time.time() + max(0.0, min(float(request.args.get('wait', 20)), 25.0))
    with M.cond:
        while True:
            ev = [e for e in M.events if e['seq'] > since]
            if ev or time.time() >= end:
                break
            M.cond.wait(timeout=max(0.05, end - time.time()))
        seq = M.seq
    return jsonify(seq=seq, events=ev)

def _login_guard(kind, limit):
    k, now = f'{kind}:' + _ip(), time.time()
    FAILS[k] = [t for t in FAILS[k] if now - t < 600]
    if len(FAILS[k]) >= limit:
        raise ApiError('खूप प्रयत्न — 10 मिनिटं थांबा', 429)
    FAILS[k].append(now)

@bp.post('/api/login/start')
@api
def api_login_start():
    _login_guard('ls', 10)
    d = _json()
    if d.get('api') == 'custom':
        api_id, api_hash = str(d.get('api_id', '')).strip(), str(d.get('api_hash', '')).strip()
        if not api_id.isdigit() or not re.fullmatch(r'[0-9a-f]{32}', api_hash):
            raise ApiError('api_id (संख्या) आणि api_hash (32 अक्षरं) बरोबर द्या')
    else:
        api_id, api_hash = DEF_API_ID, DEF_API_HASH
        if not (api_id and api_hash):
            raise ApiError('default API सेट नाही — "वेगळा API" निवडा')
    return jsonify(M.call(M.a_login_start(str(d.get('label', '')).strip(), str(d.get('phone', '')), api_id, api_hash)))

@bp.post('/api/login/code')
@api
def api_login_code():
    _login_guard('lc', 15)
    d = _json()
    return jsonify(M.call(M.a_login_code(str(d.get('login_id', '')), str(d.get('code', '')))))

@bp.post('/api/login/password')
@api
def api_login_password():
    _login_guard('lc', 15)
    d = _json()
    return jsonify(M.call(M.a_login_password(str(d.get('login_id', '')), str(d.get('password', '')))))

def count_bw(resp):
    try:
        if request.path.startswith('/app') and resp.content_length:
            bw_add(resp.content_length)
    except Exception:
        pass
    return resp


# ───────────────────────── init ─────────────────────────
def init(app, db, notify):
    """True = चालू. नाहीतर `reason` मध्ये कारण."""
    global M, reason
    if Fernet is None or TelegramClient is None:
        reason = 'telethon / cryptography packages नाहीत'
    elif db is None:
        reason = 'MONGODB_URI लागतो'
    elif not SESSION_KEY:
        reason = 'SESSION_KEY लागतो'
    else:
        try:
            _fernet()
        except Exception:
            reason = 'SESSION_KEY चुकीचा (Fernet key हवी)'
        else:
            M = Manager(db, notify)
            app.register_blueprint(bp)
            app.after_request(count_bw)
            reason = ''
    return M is not None

def start():
    if M:
        M.start()


ENTER_PAGE = r'''<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>…</title><body style="font:16px system-ui;background:#0f1115;color:#e6e8ee;padding:30px">⏳
<script>
const k=new URLSearchParams(location.search).get('k');history.replaceState(null,'','/app/');
fetch('/app/enter',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'app'},body:JSON.stringify({k})})
 .then(r=>r.ok?location.replace('/app/'):document.body.textContent='✖ Link संपला किंवा आधीच वापरला').catch(()=>document.body.textContent='✖ error');
</script>'''

UI_PAGE = r'''<!doctype html><html lang="mr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Accounts</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ee;--mut:#8b93a7;--ac:#4f8cff;--bd:#2a2f3a;--out:#25427a;--in:#222733}
*{box-sizing:border-box}html,body{height:100%;margin:0}body{background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif}
#app{display:flex;height:100%}#side{width:340px;border-right:1px solid var(--bd);display:flex;flex-direction:column;background:var(--card)}
#main{flex:1;display:flex;flex-direction:column;min-width:0}.top{display:flex;gap:8px;align-items:center;padding:8px;border-bottom:1px solid var(--bd)}
select,input,textarea,button{background:#0b0d11;color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:8px;font:inherit}
button{cursor:pointer}button.p{background:var(--ac);border-color:var(--ac);color:#fff}button.g{background:none;border:none;padding:4px 6px;color:var(--mut)}
#accsel{flex:1;min-width:0}#bw{padding:4px 10px;color:var(--mut);font-size:12px}#filter{margin:6px 8px}
#dlgs{overflow:auto;flex:1}.d{display:flex;gap:10px;padding:10px;border-bottom:1px solid var(--bd);cursor:pointer}.d:hover,.d.on{background:#1d2230}
.av{width:42px;height:42px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;flex:none}
.dm{flex:1;min-width:0}.dn{display:flex;justify-content:space-between;gap:6px}.dn b,.dl{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.dl{color:var(--mut)}
.u{background:var(--ac);color:#fff;border-radius:10px;padding:0 7px;font-size:12px}
#msgs{flex:1;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:6px}.m{max-width:min(78%,560px);padding:7px 10px;border-radius:12px;background:var(--in);align-self:flex-start;word-break:break-word}
.m.out{background:var(--out);align-self:flex-end}.mt{white-space:pre-wrap}.mf{font-size:12px;color:#7fb0ff;margin-bottom:2px}.mr{font-size:12px;color:var(--mut);border-left:2px solid var(--ac);padding-left:6px;margin-bottom:3px}
.mi{display:flex;gap:4px;justify-content:flex-end;align-items:center;font-size:11px;color:var(--mut);margin-top:3px}.m img{max-width:100%;border-radius:8px;display:block;margin:4px 0}
#bar{padding:6px 10px;color:var(--mut);font-size:12px;border-top:1px solid var(--bd);display:none}.cmp{display:flex;gap:6px;padding:8px;border-top:1px solid var(--bd)}
#txt{flex:1;resize:none;height:42px}#modal{position:fixed;inset:0;background:#000a;display:none;align-items:center;justify-content:center;padding:14px}
#box{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;width:min(420px,100%);display:flex;flex-direction:column;gap:8px}#err{color:#ff6b6b;min-height:1em}
.empty{color:var(--mut);margin:auto}#back{display:none}
@media(max-width:700px){#side{width:100%}#main{display:none}.chat #side{display:none}.chat #main{display:flex}#back{display:inline-block}}
</style></head><body>
<div id="app"><aside id="side"><div class="top"><select id="accsel"></select><button id="add" title="Account जोडा">＋</button></div>
<div id="bw"></div><input id="filter" placeholder="शोधा…"><div id="dlgs"></div></aside>
<main id="main"><div class="top"><button id="back" class="g">←</button><b id="ct">चॅट निवडा</b></div>
<div id="msgs"><div class="empty">डावीकडून चॅट निवडा</div></div><div id="bar"></div>
<div class="cmp"><label class="g" style="cursor:pointer;align-self:center">📎<input id="file" type="file" hidden></label>
<textarea id="txt" placeholder="message…"></textarea><button id="send" class="p">➤</button></div></main></div>
<div id="modal"><div id="box"><b>Telegram account जोडा</b><div id="step"></div><div id="err"></div></div></div>
<script>
const $=s=>document.querySelector(s);let ST={accounts:[],default_api:false},acc=null,chat=null,seq=0,DL=[],MS=[],reply=null,editing=null,oldest=0;
function el(t,p,...k){const e=document.createElement(t);for(const[a,v]of Object.entries(p||{})){if(a==='on')for(const[ev,f]of Object.entries(v))e.addEventListener(ev,f);else if(a==='text')e.textContent=v;else e.setAttribute(a,v)}k.flat().forEach(c=>c&&e.append(c));return e}
async function api(path,body,form){const o=body===undefined&&!form?{}:{method:'POST',headers:form?{'X-Requested-With':'app'}:{'Content-Type':'application/json','X-Requested-With':'app'},body:form||JSON.stringify(body)};
 const r=await fetch(path,o);let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw new Error(d.error||('error '+r.status));return d}
const Q=o=>new URLSearchParams(o).toString(),col=n=>`hsl(${[...n].reduce((a,c)=>a+c.charCodeAt(0),0)%360} 45% 38%)`;
const tm=t=>t?new Date(t*1e3).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'';
const sz=n=>n>1048576?(n/1048576).toFixed(1)+' MB':Math.max(1,Math.round(n/1024))+' KB';
async function loadState(){ST=await api('/app/api/state');const s=$('#accsel');s.replaceChildren(...ST.accounts.map(a=>el('option',{value:a.id,text:`${a.label||a.name} — ${a.state}`})));
 if(!acc&&ST.accounts.length)acc=ST.accounts[0].id;if(acc)s.value=acc;$('#bw').textContent=`📊 ${(ST.bw.used/1048576).toFixed(1)} MB / ${(ST.bw.limit/1073741824).toFixed(1)} GB (${ST.bw.month})`}
async function loadDialogs(){if(!acc)return;try{DL=(await api('/app/api/dialogs?'+Q({acc}))).dialogs;drawDialogs()}catch(e){$('#dlgs').replaceChildren(el('div',{class:'empty',text:e.message}))}}
function drawDialogs(){const f=$('#filter').value.toLowerCase();$('#dlgs').replaceChildren(...DL.filter(d=>d.name.toLowerCase().includes(f)).map(d=>
 el('div',{class:'d'+(d.id===chat?' on':''),on:{click:()=>openChat(d)}},el('div',{class:'av',style:'background:'+col(d.name),text:(d.name||'?').trim().slice(0,1).toUpperCase()}),
 el('div',{class:'dm'},el('div',{class:'dn'},el('b',{text:(d.pinned?'📌 ':'')+d.name}),el('span',{class:'dl',text:tm(d.ts)})),
 el('div',{class:'dn'},el('span',{class:'dl',text:(d.out?'✓ ':'')+d.last}),d.unread?el('span',{class:'u',text:d.unread}):null)))))}
async function openChat(d){chat=d.id;$('#ct').textContent=d.name;document.body.classList.add('chat');setReply(null);MS=[];oldest=0;drawDialogs();
 try{MS=(await api('/app/api/messages?'+Q({acc,chat,limit:30}))).messages}catch(e){MS=[]}drawMsgs(true);markRead()}
async function older(){if(!MS.length)return;const r=(await api('/app/api/messages?'+Q({acc,chat,before:MS[0].id,limit:30}))).messages;MS=r.concat(MS);drawMsgs(false)}
function markRead(){if(MS.length)api('/app/api/read',{acc,chat,max_id:MS[MS.length-1].id}).catch(()=>{})}
function mediaEl(m){const x=m.media;if(!x)return null;const u='/app/api/media?'+Q({acc,chat,id:m.id});
 if(x.kind==='photo'){const b=el('button',{class:'g',text:`📷 फोटो ${sz(x.size)} — लोड करा`});b.onclick=()=>{b.replaceWith(el('img',{src:u+'&ok=1'}))};return b}
 return el('a',{href:u+'&ok=1',target:'_blank',rel:'noopener',style:'color:#7fb0ff',text:`📎 ${x.name||x.kind} (${sz(x.size)}) — उघडा`})}
function drawMsgs(bottom){const box=$('#msgs'),keep=box.scrollHeight-box.scrollTop;const kids=[];if(MS.length)kids.push(el('button',{class:'g',text:'↑ जुने messages',on:{click:older}}));
 for(const m of MS){const r=m.reply?el('div',{class:'mr',text:'↩ '+((MS.find(x=>x.id===m.reply)||{}).text||'#'+m.reply).slice(0,60)}):null;
  kids.push(el('div',{class:'m'+(m.out?' out':'')},m.from&&!m.out?el('div',{class:'mf',text:m.from}):null,r,m.text?el('div',{class:'mt',text:m.text}):null,mediaEl(m),
   el('div',{class:'mi'},el('span',{text:(m.edited?'edited · ':'')+tm(m.ts)}),el('button',{class:'g',text:'↩',on:{click:()=>setReply(m)}}),
   m.out?el('button',{class:'g',text:'✎',on:{click:()=>startEdit(m)}}):null,el('button',{class:'g',text:'🗑',on:{click:()=>del(m)}}))))}
 box.replaceChildren(...(kids.length?kids:[el('div',{class:'empty',text:'message नाहीत'})]));box.scrollTop=bottom?box.scrollHeight:box.scrollHeight-keep}
function setReply(m){reply=m;editing=null;const b=$('#bar');b.style.display=m?'block':'none';b.textContent=m?'↩ '+(m.text||'media').slice(0,80)+'  (रद्द: Esc)':''}
function startEdit(m){editing=m;reply=null;$('#txt').value=m.text;const b=$('#bar');b.style.display='block';b.textContent='✎ edit करत आहात (रद्द: Esc)';$('#txt').focus()}
async function del(m){if(!confirm('हा message delete करायचा?'))return;try{await api('/app/api/delete',{acc,chat,id:m.id});MS=MS.filter(x=>x.id!==m.id);drawMsgs(false)}catch(e){alert(e.message)}}
async function send(){const t=$('#txt').value;if(!t.trim()||!chat)return;$('#txt').value='';try{if(editing){const r=await api('/app/api/edit',{acc,chat,id:editing.id,text:t});MS=MS.map(x=>x.id===r.message.id?r.message:x);setReply(null);editing=null}
 else{const r=await api('/app/api/send',{acc,chat,text:t,reply_to:reply&&reply.id});if(!MS.some(x=>x.id===r.message.id))MS.push(r.message);setReply(null)}drawMsgs(true)}catch(e){alert(e.message);$('#txt').value=t}}
$('#send').onclick=send;$('#txt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&innerWidth>700){e.preventDefault();send()}if(e.key==='Escape'){setReply(null);editing=null;$('#txt').value=''}});
$('#file').onchange=async e=>{const f=e.target.files[0];e.target.value='';if(!f||!chat)return;if(f.size>ST.max_upload){alert('फाइल मोठी आहे (मर्यादा '+Math.floor(ST.max_upload/1048576)+' MB)');return}
 if(!confirm(`"${f.name}" (${sz(f.size)}) पाठवायची? bandwidth खर्च होतो.`))return;const fd=new FormData();fd.append('file',f);fd.append('acc',acc);fd.append('chat',chat);
 try{const r=await api('/app/api/upload',undefined,fd);MS.push(r.message);drawMsgs(true)}catch(err){alert(err.message)}};
$('#accsel').onchange=e=>{acc=e.target.value;chat=null;MS=[];$('#ct').textContent='चॅट निवडा';$('#msgs').replaceChildren(el('div',{class:'empty',text:'चॅट निवडा'}));loadDialogs()};
$('#filter').oninput=drawDialogs;$('#back').onclick=()=>document.body.classList.remove('chat');
let dt=null;function soon(){clearTimeout(dt);dt=setTimeout(()=>{loadDialogs();loadState().catch(()=>{})},600)}
async function poll(){for(;;){try{const r=await api('/app/api/events?'+Q({since:seq,wait:20}));seq=r.seq;
 for(const e of r.events){if(e.acc!==acc)continue;soon();if(e.chat===chat){if(e.type==='new'&&e.msg&&!MS.some(x=>x.id===e.msg.id)){MS.push(e.msg);markRead()}
  else if(e.type==='edit'&&e.msg)MS=MS.map(x=>x.id===e.msg.id?e.msg:x);else if(e.type==='del')MS=MS.filter(x=>!(e.ids||[]).includes(x.id));drawMsgs(e.type==='new')}}}catch(e){await new Promise(r=>setTimeout(r,4000))}}}
const modal=$('#modal'),step=$('#step'),err=$('#err');let LID=null;
function say(t){err.textContent=t||''}
function stepStart(){say();step.replaceChildren(el('input',{id:'l_label',placeholder:'नाव (उदा. Office)'}),el('input',{id:'l_phone',placeholder:'+919876543210',inputmode:'tel'}),
 el('label',{},el('input',{type:'radio',name:'api',value:'default',id:'l_def'}),' Default API (server env)'),el('label',{},el('input',{type:'radio',name:'api',value:'custom'}),' वेगळा api_id / api_hash'),
 el('input',{id:'l_aid',placeholder:'api_id',style:'display:none'}),el('input',{id:'l_ahash',placeholder:'api_hash',style:'display:none'}),
 el('div',{style:'display:flex;gap:8px'},el('button',{class:'p',text:'कोड पाठवा',on:{click:start}}),el('button',{text:'रद्द',on:{click:()=>modal.style.display='none'}})));
 const d=$('#l_def');d.disabled=!ST.default_api;(ST.default_api?d:document.querySelector('input[value=custom]')).checked=true;
 document.querySelectorAll('input[name=api]').forEach(r=>r.onchange=()=>{const c=document.querySelector('input[name=api]:checked').value==='custom';$('#l_aid').style.display=$('#l_ahash').style.display=c?'block':'none'});
 if(!ST.default_api){$('#l_aid').style.display=$('#l_ahash').style.display='block'}}
async function start(){say('…');try{const r=await api('/app/api/login/start',{label:$('#l_label').value,phone:$('#l_phone').value,api:document.querySelector('input[name=api]:checked').value,api_id:$('#l_aid').value,api_hash:$('#l_ahash').value});LID=r.login_id;say();
 step.replaceChildren(el('div',{text:'Telegram app मध्ये आलेला कोड टाका:',style:'color:var(--mut)'}),el('input',{id:'l_code',placeholder:'12345',inputmode:'numeric',autocomplete:'off'}),el('button',{class:'p',text:'Login',on:{click:code}}))}catch(e){say(e.message)}}
async function code(){say('…');try{const r=await api('/app/api/login/code',{login_id:LID,code:$('#l_code').value});if(r.next==='password'){say();
 step.replaceChildren(el('div',{text:'2-step verification password:',style:'color:var(--mut)'}),el('input',{id:'l_pw',type:'password',autocomplete:'off'}),el('button',{class:'p',text:'Login',on:{click:pw}}))}else done(r)}catch(e){say(e.message)}}
async function pw(){say('…');try{done(await api('/app/api/login/password',{login_id:LID,password:$('#l_pw').value}))}catch(e){say(e.message)}}
async function done(r){modal.style.display='none';acc=r.account;await loadState();loadDialogs()}
$('#add').onclick=()=>{stepStart();modal.style.display='flex'};
(async()=>{try{await loadState();await loadDialogs()}catch(e){document.body.textContent='✖ '+e.message}poll();setInterval(()=>loadState().catch(()=>{}),60000)})();
</script></body></html>'''
