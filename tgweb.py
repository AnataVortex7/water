"""
tgweb.py — Render वर अनेक Telegram account चालवणारा web client (backend).

• Account login server वर (Telethon). Session string Fernet ने encrypt होऊन MongoDB मध्ये.
• UI: /app (tgui.py) — फक्त bot ने दिलेल्या एकदाच चालणाऱ्या link ने; बाकी सगळ्यांना रिकामा 404.
• मोठ्या फाइल (Telegram मर्यादेपर्यंत: 2 GB, Premium 4 GB) RAM मध्ये न घेता तुकड्या-तुकड्यांत
  (download = streaming + Range; upload = 8 MB chunks → disk → Telegram).
• Bandwidth: महिन्याचा counter + मर्यादा. एका वेळी एकच instance accounts चालवतो (Mongo lease).

Env: SESSION_KEY, MONGODB_URI, TG_API_ID / TG_API_HASH (default), BW_LIMIT_GB (4), MAX_ACCOUNTS (6),
     MAX_UPLOAD_MB (4000), MAX_MEDIA_MB (4000)
"""
import os, io, re, time, json, uuid, atexit, shutil, asyncio, tempfile, threading, secrets, collections, urllib.parse
from datetime import datetime, timezone
from flask import Blueprint, request, session, jsonify, Response, abort

try:
    from cryptography.fernet import Fernet
except Exception:                                   # pragma: no cover
    Fernet = None
try:
    from telethon import TelegramClient, events, functions, types, utils
    from telethon.sessions import StringSession
except Exception:                                   # pragma: no cover
    TelegramClient = events = functions = types = utils = StringSession = None
try:
    from tgui import UI_PAGE, ENTER_PAGE
except Exception:                                   # pragma: no cover
    UI_PAGE = ENTER_PAGE = '<!doctype html>UI missing'

SESSION_KEY = os.environ.get('SESSION_KEY', '').strip()
DEF_API_ID = os.environ.get('TG_API_ID', '').strip()
DEF_API_HASH = os.environ.get('TG_API_HASH', '').strip()
MB = 1024 * 1024
BW_LIMIT = int(float(os.environ.get('BW_LIMIT_GB', '4')) * 1024 ** 3)
MAX_ACCOUNTS = int(os.environ.get('MAX_ACCOUNTS', '6'))
TG_LIMIT, TG_LIMIT_PREMIUM = 2000 * MB, 4000 * MB          # Telegram ची खरी मर्यादा
MAX_UPLOAD = int(float(os.environ.get('MAX_UPLOAD_MB', '4000')) * MB)
MAX_MEDIA = int(float(os.environ.get('MAX_MEDIA_MB', '4000')) * MB)
UI_SESSION_SECS = 7200
LEASE_SECS = 45
CHUNK = 8 * MB                                              # browser → server upload तुकडा
REQ = 512 * 1024                                            # server ← Telegram download तुकडा
UPDIR = os.path.join(tempfile.gettempdir(), 'tgup')
CACHE_DIR = os.path.join(tempfile.gettempdir(), 'tgcache')                    # Telegram → server disk (अर्धवट = .part)
DISK_CACHE = int(float(os.environ.get('DISK_CACHE_GB', '10')) * 1024 ** 3)    # cache ची कमाल जागा (जुन्या फाइल आपोआप हटतात)

M = None
reason = ''
TOKENS, TOKENS_LOCK = {}, threading.Lock()
FAILS = collections.defaultdict(list)
BW = {'month': '', 'bytes': 0, 'dirty': False}
BW_LOCK = threading.Lock()
UP, UP_LOCK = {}, threading.Lock()
DL, DL_LOCK = {}, threading.Lock()          # चालू/झालेले server-download jobs
STREAMS = threading.Semaphore(3)
_F = [None]


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


# ───────────────────────── crypto / bandwidth ─────────────────────────
def _fernet():
    if _F[0] is None:
        _F[0] = Fernet(SESSION_KEY.encode())
    return _F[0]

def enc(s):
    return _fernet().encrypt(str(s).encode()).decode()

def dec(t):
    return _fernet().decrypt(t.encode()).decode()

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


# ───────────────────────── serialization ─────────────────────────
def _t(x):
    return x if isinstance(x, str) else (getattr(x, 'text', '') or '')

def _name(e):
    if e is None:
        return ''
    t = getattr(e, 'title', None)
    if t:
        return str(t)
    return ' '.join(str(x) for x in (getattr(e, 'first_name', None), getattr(e, 'last_name', None)) if x) \
        or str(getattr(e, 'username', '') or '')

def _ts(d):
    if d is None:
        return 0
    return int(d.timestamp()) if hasattr(d, 'timestamp') else int(d)

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
    return {'kind': kind, 'size': int(getattr(f, 'size', 0) or 0), 'name': str(getattr(f, 'name', '') or ''),
            'mime': str(getattr(f, 'mime_type', '') or ''), 'dur': int(getattr(f, 'duration', 0) or 0),
            'w': int(getattr(f, 'width', 0) or 0), 'h': int(getattr(f, 'height', 0) or 0),
            'emoji': str(getattr(f, 'emoji', '') or '')}

def ser_entities(m):
    out = []
    for e in (getattr(m, 'entities', None) or []):
        t = type(e).__name__.replace('MessageEntity', '').lower()
        d = {'t': t, 'o': int(e.offset), 'l': int(e.length)}
        if t == 'texturl':
            d['u'] = getattr(e, 'url', '')
        out.append(d)
    return out

def ser_reactions(m):
    r = getattr(m, 'reactions', None)
    if not r or not getattr(r, 'results', None):
        return None
    out = []
    for x in r.results:
        em = getattr(x.reaction, 'emoticon', None)
        if em:
            out.append({'e': em, 'n': int(x.count), 'me': getattr(x, 'chosen_order', None) is not None})
    return out or None

def ser_poll(m):
    mp = getattr(m, 'poll', None)
    if not mp:
        return None
    p, res = mp.poll, getattr(mp, 'results', None)
    rmap = {}
    for r in (getattr(res, 'results', None) or []):
        rmap[bytes(r.option)] = r
    answers = []
    for i, a in enumerate(p.answers):
        r = rmap.get(bytes(a.option))
        answers.append({'i': i, 't': _t(a.text), 'n': int(r.voters) if r else 0,
                        'me': bool(r and r.chosen), 'ok': bool(r and getattr(r, 'correct', False))})
    return {'q': _t(p.question), 'a': answers, 'closed': bool(p.closed), 'multi': bool(p.multiple_choice),
            'quiz': bool(p.quiz), 'total': int(getattr(res, 'total_voters', 0) or 0)}

def ser_web(m):
    w = getattr(m, 'web_preview', None)
    if not w or not getattr(w, 'url', None):
        return None
    return {'url': w.url, 'title': _t(getattr(w, 'title', '')), 'desc': _t(getattr(w, 'description', ''))[:300],
            'site': _t(getattr(w, 'site_name', '')), 'photo': bool(getattr(w, 'photo', None))}

def ser_buttons(m):
    rm = getattr(m, 'reply_markup', None)
    if not rm or type(rm).__name__ != 'ReplyInlineMarkup':
        return None
    rows = []
    for row in (getattr(m, 'buttons', None) or []):
        rows.append([{'t': b.text, 'u': getattr(b, 'url', None) or None, 'cb': bool(getattr(b, 'data', None))} for b in row])
    return rows or None

ACTIONS = {'ChatAddUser': 'joined the group', 'ChatJoinedByLink': 'joined via invite link', 'ChatDeleteUser': 'left the group',
           'ChatEditTitle': 'changed the group name', 'ChatEditPhoto': 'changed the group photo', 'PinMessage': 'pinned a message',
           'ChatCreate': 'created the group', 'ChannelCreate': 'created the channel', 'ContactSignUp': 'joined Telegram',
           'HistoryClear': 'history cleared', 'PhoneCall': 'call', 'ChatJoinedByRequest': 'joined by request'}

def action_text(a):
    n = type(a).__name__.replace('MessageAction', '')
    return ACTIONS.get(n) or re.sub(r'(?<!^)(?=[A-Z])', ' ', n).lower()

def ser_msg(m):
    rt = getattr(m, 'reply_to', None)
    text = getattr(m, 'message', '') or ''
    service = bool(getattr(m, 'action', None))
    if service and not text:
        text = action_text(m.action)
    fw = getattr(m, 'forward', None)
    geo = getattr(m, 'geo', None)
    ct = getattr(m, 'contact', None)
    dice = getattr(m, 'dice', None)
    venue = getattr(m, 'venue', None)
    return {'id': m.id, 'out': bool(getattr(m, 'out', False)), 'ts': _ts(getattr(m, 'date', None)), 'text': text,
            'service': service, 'from': _name(getattr(m, 'sender', None)), 'from_id': getattr(m, 'sender_id', None),
            'reply': getattr(rt, 'reply_to_msg_id', None) if rt else None,
            'edited': bool(getattr(m, 'edit_date', None)),
            'fwd': (getattr(fw, 'from_name', None) or _name(getattr(fw, 'sender', None)) or _name(getattr(fw, 'chat', None)) or 'Unknown') if fw else '',
            'via': _name(getattr(m, 'via_bot', None)), 'media': media_info(m), 'ents': ser_entities(m),
            'react': ser_reactions(m), 'poll': ser_poll(m), 'web': ser_web(m), 'btn': ser_buttons(m),
            'geo': {'lat': geo.lat, 'lon': geo.long, 'title': getattr(venue, 'title', '')} if geo else None,
            'contact': {'name': f"{getattr(ct, 'first_name', '')} {getattr(ct, 'last_name', '')}".strip(), 'phone': getattr(ct, 'phone_number', '')} if ct else None,
            'dice': {'e': dice.emoticon, 'v': dice.value} if dice else None,
            'grp': getattr(m, 'grouped_id', None), 'views': getattr(m, 'views', None), 'pinned': bool(getattr(m, 'pinned', False))}

def _muted(d):
    ns = getattr(getattr(d, 'dialog', None), 'notify_settings', None)
    mu = getattr(ns, 'mute_until', None)
    return bool(mu and _ts(mu) > time.time())

def ser_dialog(d):
    m = d.message
    last = (getattr(m, 'message', '') or '') if m else ''
    if m and not last and getattr(m, 'action', None):
        last = action_text(m.action)
    if m and not last and getattr(m, 'media', None):
        mi = media_info(m) or {}
        last = '📎 ' + (mi.get('emoji') or mi.get('kind', 'media'))
    ent, raw = getattr(d, 'entity', None), getattr(d, 'dialog', None)
    st = type(getattr(ent, 'status', None)).__name__
    kind = 'user' if d.is_user else ('group' if d.is_group else 'channel')
    return {'id': d.id, 'name': 'Saved Messages' if getattr(ent, 'is_self', False) else (d.name or ''),
            'self': bool(getattr(ent, 'is_self', False)), 'unread': d.unread_count or 0,
            'mentions': int(getattr(raw, 'unread_mentions_count', 0) or 0), 'last': last[:90], 'ts': _ts(getattr(m, 'date', None)) if m else 0,
            'type': kind, 'pinned': bool(getattr(d, 'pinned', False)), 'out': bool(getattr(m, 'out', False)) if m else False,
            'muted': _muted(d), 'bot': bool(getattr(ent, 'bot', False)), 'verified': bool(getattr(ent, 'verified', False)),
            'online': st == 'UserStatusOnline', 'read_out': int(getattr(raw, 'read_outbox_max_id', 0) or 0),
            'read_in': int(getattr(raw, 'read_inbox_max_id', 0) or 0), 'archived': bool(getattr(d, 'archived', False))}

def status_text(ent):
    st = type(getattr(ent, 'status', None)).__name__
    if st == 'UserStatusOnline':
        return 'online'
    if st == 'UserStatusOffline':
        w = getattr(ent.status, 'was_online', None)
        return 'last seen ' + (datetime.fromtimestamp(_ts(w)).strftime('%d %b %H:%M') if w else 'recently')
    return {'UserStatusRecently': 'last seen recently', 'UserStatusLastWeek': 'last seen within a week',
            'UserStatusLastMonth': 'last seen within a month'}.get(st, '')

def _thumb_index(m):
    media = getattr(m, 'photo', None) or getattr(m, 'document', None) or getattr(getattr(m, 'web_preview', None), 'photo', None)
    sizes = getattr(media, 'sizes', None) or getattr(media, 'thumbs', None) or []
    return min(2, len(sizes) - 1) if len(sizes) else None


def default_factory(sess, api_id, api_hash):
    return TelegramClient(StringSession(sess or ''), int(api_id), api_hash, device_model='Worker Hub',
                          app_version='1.0', system_version='Linux', lang_code='en',
                          connection_retries=None, retry_delay=3, auto_reconnect=True, flood_sleep_threshold=60)

client_factory = default_factory

REVOKED = {'AuthKeyUnregisteredError', 'SessionRevokedError', 'UserDeactivatedError',
           'UserDeactivatedBanError', 'AuthKeyDuplicatedError', 'SessionExpiredError', 'PermissionError'}

FILTERS = {'media': 'InputMessagesFilterPhotoVideo', 'files': 'InputMessagesFilterDocument', 'links': 'InputMessagesFilterUrl',
           'voice': 'InputMessagesFilterVoice', 'music': 'InputMessagesFilterMusic', 'gifs': 'InputMessagesFilterGif'}


def _lru_put(od, key, val, cap_items=300, cap_bytes=16 * MB):
    od[key] = val
    od.move_to_end(key)
    total = sum(len(v or b'') for v in od.values())
    while od and (len(od) > cap_items or total > cap_bytes):
        _, v = od.popitem(last=False)
        total -= len(v or b'')


def _ckey(aid, chat, mid):
    return f'{aid}_{chat}_{mid}'.replace('-', 'm')

def _cpath(key):
    return os.path.join(CACHE_DIR, key)

def cache_files():
    """(complete फाइल्स, .part चा एकूण आकार)."""
    done, part = [], 0
    try:
        for f in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, f)
            if not os.path.isfile(p):
                continue
            if f.endswith('.part'):
                part += os.path.getsize(p)
            else:
                done.append((os.path.getmtime(p), os.path.getsize(p), p))
    except FileNotFoundError:
        pass
    return done, part

def cache_usage():
    done, part = cache_files()
    return sum(s for _, s, _ in done) + part, len(done)

def cache_evict(need=0):
    """जागा कमी पडली तर सर्वात जुन्या (कमी वापरलेल्या) फाइल्स आधी हटवतो."""
    done, part = cache_files()
    total = sum(s for _, s, _ in done) + part
    for _, s, p in sorted(done):
        if total + need <= DISK_CACHE:
            break
        try:
            os.remove(p)
            total -= s
        except OSError:
            pass

def cache_cleanup():
    cache_evict(0)
    try:
        now = time.time()
        for f in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, f)
            if f.endswith('.part') and now - os.path.getmtime(p) > 24 * 3600:
                os.remove(p)
    except FileNotFoundError:
        pass


# ───────────────────────── Manager ─────────────────────────
class Manager:
    def __init__(self, db, notify):
        self.db, self.notify = db, notify
        self.acc, self.meta = db['tgacc'], db['meta']
        self.loop = asyncio.new_event_loop()
        self.clients, self.state, self.next_try = {}, {}, {}
        self.logins, self.cache = {}, {}
        self.events = collections.deque(maxlen=800)
        self.seq = 0
        self.cond = threading.Condition()
        self.lease = False
        self.last_ping = {}
        self.premium = {}
        self.instance = uuid.uuid4().hex[:8]
        self.avatars, self.thumbs = collections.OrderedDict(), collections.OrderedDict()
        self.up_tasks = {}
        self.dl_tasks = {}

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
        self.avatar_sem = asyncio.Semaphore(3)
        self.loop.create_task(self.supervisor())
        self.loop.run_forever()

    def call(self, coro, timeout=60):
        if not self.loop.is_running():
            coro.close()
            raise ApiError('Telegram engine अजून सुरू झालं नाही — थोड्या वेळाने प्रयत्न करा', 503)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # --- lease
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
            return False

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

    def _cleanup_uploads(self):
        try:
            now = time.time()
            for d in os.listdir(UPDIR):
                p = os.path.join(UPDIR, d)
                if now - os.path.getmtime(p) > 6 * 3600:
                    shutil.rmtree(p, ignore_errors=True)
                    with UP_LOCK:
                        UP.pop(d, None)
        except FileNotFoundError:
            pass
        except Exception as e:
            print('upload cleanup:', e)
        try:
            cache_cleanup()
        except Exception as e:
            print('cache cleanup:', e)

    # --- supervisor
    async def supervisor(self):
        loop = asyncio.get_running_loop()
        last_bw = last_clean = 0
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
                if time.time() - last_clean > 1800:
                    await loop.run_in_executor(None, self._cleanup_uploads)
                    last_clean = time.time()
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
            me = await cl.get_me()
            self.premium[aid] = bool(getattr(me, 'premium', False))
            await asyncio.wait_for(cl.get_dialogs(limit=100), 90)
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
        async def on_read(ev):
            if getattr(ev, 'outbox', False):
                self.push(aid, 'read', getattr(ev, 'chat_id', None), None, None, max_id=getattr(ev, 'max_id', 0))
        async def on_user(ev):
            act = 'typing' if getattr(ev, 'typing', False) else 'recording' if getattr(ev, 'recording', False) \
                else 'uploading' if getattr(ev, 'uploading', False) else ''
            who = ''
            try:
                who = _name(await ev.get_user())
            except Exception:
                pass
            self.push(aid, 'typing', getattr(ev, 'chat_id', None) or getattr(ev, 'user_id', None), None, None,
                      action=act, who=who, online=bool(getattr(ev, 'online', False)))
        cl.add_event_handler(on_new, events.NewMessage())
        cl.add_event_handler(on_edit, events.MessageEdited())
        cl.add_event_handler(on_del, events.MessageDeleted())
        cl.add_event_handler(on_read, events.MessageRead())
        cl.add_event_handler(on_user, events.UserUpdate())

    def push(self, aid, kind, chat_id, msg, ids=None, **extra):
        if kind in ('new', 'edit', 'del', 'read'):
            self.cache.pop(aid, None)
        with self.cond:
            self.seq += 1
            self.events.append({'seq': self.seq, 'acc': aid, 'type': kind, 'chat': chat_id, 'msg': msg, 'ids': ids, **extra})
            self.cond.notify_all()

    def _cl(self, aid):
        cl = self.clients.get(aid)
        if not cl:
            raise ApiError('हा account सध्या online नाही', 409)
        return cl

    def limit_for(self, aid):
        return min(MAX_UPLOAD, TG_LIMIT_PREMIUM if self.premium.get(aid) else TG_LIMIT)

    # ═══════════ API coroutines ═══════════
    async def a_dialogs(self, aid, archived=False):
        key = (aid, archived)
        c = self.cache.get(key)
        if c and time.time() - c[0] < 8:
            return c[1]
        ds = await self._cl(aid).get_dialogs(limit=120, folder=1 if archived else 0)
        out = [ser_dialog(d) for d in ds]
        self.cache[key] = (time.time(), out)
        return out

    async def a_messages(self, aid, chat, before, limit):
        cl = self._cl(aid)
        ms = await cl.get_messages(chat, limit=limit, offset_id=before or 0)
        res = {'messages': [ser_msg(m) for m in reversed(list(ms))]}
        if not before:
            res['pinned'] = None
            try:
                pm = await cl.get_messages(chat, limit=1, filter=types.InputMessagesFilterPinned())
                if pm:
                    res['pinned'] = {'id': pm[0].id, 'text': (pm[0].message or '📎')[:100]}
            except Exception:
                pass
            try:
                peer = await cl.get_input_entity(chat)
                pd = await cl(functions.messages.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer=peer)]))
                res['read_out'] = int(pd.dialogs[0].read_outbox_max_id)
                res['read_in'] = int(pd.dialogs[0].read_inbox_max_id)
            except Exception:
                res['read_out'] = res['read_in'] = 0
        return res

    async def a_around(self, aid, chat, mid):
        ms = await self._cl(aid).get_messages(chat, limit=30, offset_id=mid, add_offset=-15)
        return [ser_msg(m) for m in reversed(list(ms))]

    async def a_search(self, aid, q, chat):
        cl = self._cl(aid)
        ms = await cl.get_messages(chat if chat else None, search=q, limit=30)
        out = []
        for m in ms:
            d = ser_msg(m)
            d['chat'] = getattr(m, 'chat_id', None)
            d['chat_name'] = _name(getattr(m, 'chat', None))
            out.append(d)
        return out

    async def a_send(self, aid, chat, text, reply_to, md, silent, schedule, preview):
        kw = {}
        if schedule:
            kw['schedule'] = datetime.fromtimestamp(int(schedule), tz=timezone.utc)
        m = await self._cl(aid).send_message(chat, text, reply_to=reply_to or None, parse_mode='md' if md else None,
                                             silent=bool(silent), link_preview=bool(preview), **kw)
        bw_add(len(text.encode()) + 300)
        return ser_msg(m)

    async def a_edit(self, aid, chat, mid, text, md):
        m = await self._cl(aid).edit_message(chat, mid, text, parse_mode='md' if md else None)
        bw_add(len(text.encode()) + 300)
        return ser_msg(m)

    async def a_delete(self, aid, chat, ids, revoke):
        await self._cl(aid).delete_messages(chat, ids, revoke=bool(revoke))

    async def a_forward(self, aid, from_chat, ids, to_chat, hide):
        ms = await self._cl(aid).forward_messages(to_chat, ids, from_peer=from_chat, drop_author=bool(hide))
        ms = ms if isinstance(ms, list) else [ms]
        return [ser_msg(m) for m in ms if m]

    async def a_pin(self, aid, chat, mid, unpin):
        cl = self._cl(aid)
        if unpin:
            await cl.unpin_message(chat, mid)
        else:
            await cl.pin_message(chat, mid, notify=False)

    async def a_react(self, aid, chat, mid, emoji):
        cl = self._cl(aid)
        peer = await cl.get_input_entity(chat)
        await cl(functions.messages.SendReactionRequest(
            peer=peer, msg_id=mid, reaction=[types.ReactionEmoji(emoticon=emoji)] if emoji else []))
        m = await cl.get_messages(chat, ids=mid)
        return ser_msg(m) if m else None

    async def a_vote(self, aid, chat, mid, idxs):
        cl = self._cl(aid)
        m = await cl.get_messages(chat, ids=mid)
        if not m or not getattr(m, 'poll', None):
            raise ApiError('poll सापडला नाही', 404)
        answers = m.poll.poll.answers
        opts = [answers[i].option for i in idxs if 0 <= i < len(answers)]
        peer = await cl.get_input_entity(chat)
        await cl(functions.messages.SendVoteRequest(peer=peer, msg_id=mid, options=opts))
        m = await cl.get_messages(chat, ids=mid)
        return ser_msg(m)

    async def a_click(self, aid, chat, mid, i, j):
        cl = self._cl(aid)
        m = await cl.get_messages(chat, ids=mid)
        if not m:
            raise ApiError('message सापडला नाही', 404)
        res = await m.click(i, j)
        return {'text': getattr(res, 'message', None), 'alert': bool(getattr(res, 'alert', False)), 'url': getattr(res, 'url', None)}

    async def a_read(self, aid, chat, max_id):
        await self._cl(aid).send_read_acknowledge(chat, max_id=max_id, clear_mentions=True)

    async def a_typing(self, aid, chat):
        cl = self._cl(aid)
        peer = await cl.get_input_entity(chat)
        await cl(functions.messages.SetTypingRequest(peer=peer, action=types.SendMessageTypingAction()))

    async def a_mute(self, aid, chat, mute):
        cl = self._cl(aid)
        peer = await cl.get_input_entity(chat)
        await cl(functions.account.UpdateNotifySettingsRequest(
            peer=types.InputNotifyPeer(peer=peer), settings=types.InputPeerNotifySettings(mute_until=2147483647 if mute else 0)))
        self.cache.pop((aid, False), None)

    async def a_chatpin(self, aid, chat, pin):
        cl = self._cl(aid)
        peer = await cl.get_input_entity(chat)
        await cl(functions.messages.ToggleDialogPinRequest(peer=types.InputDialogPeer(peer=peer), pinned=bool(pin)))
        self.cache.clear()

    async def a_archive(self, aid, chat, archive):
        await self._cl(aid).edit_folder(chat, 1 if archive else 0)
        self.cache.clear()

    async def a_chatdelete(self, aid, chat):
        await self._cl(aid).delete_dialog(chat)
        self.cache.clear()

    def _dlg(self, ent):
        kind = 'user' if type(ent).__name__ == 'User' else ('channel' if getattr(ent, 'broadcast', False) else 'group')
        return {'id': utils.get_peer_id(ent), 'name': _name(ent), 'type': kind, 'username': getattr(ent, 'username', '') or '',
                'unread': 0, 'last': '', 'ts': 0, 'pinned': False, 'muted': False, 'out': False, 'bot': bool(getattr(ent, 'bot', False)),
                'verified': bool(getattr(ent, 'verified', False)), 'online': False, 'mentions': 0, 'read_out': 0, 'read_in': 0, 'self': False}

    async def a_resolve(self, aid, q):
        q = q.strip()
        if not q:
            raise ApiError('नाव / username द्या')
        q = re.sub(r'^https?://t\.me/', '', q).lstrip('@')
        try:
            ent = await self._cl(aid).get_entity(q if not q.lstrip('+').isdigit() or q.startswith('+') else int(q))
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(f'सापडलं नाही: {e}'[:140], 404)
        return self._dlg(ent)

    async def a_join(self, aid, q):
        cl = self._cl(aid)
        q = q.strip()
        m = re.search(r'(?:t\.me/\+|joinchat/)([\w-]+)', q)
        if m:
            upd = await cl(functions.messages.ImportChatInviteRequest(m.group(1)))
            ent = upd.chats[0] if getattr(upd, 'chats', None) else None
        else:
            ent = await cl.get_entity(re.sub(r'^https?://t\.me/', '', q).lstrip('@'))
            await cl(functions.channels.JoinChannelRequest(ent))
        if ent is None:
            raise ApiError('join झालं पण chat मिळाला नाही')
        self.cache.clear()
        return self._dlg(ent)

    async def a_contacts(self, aid):
        r = await self._cl(aid)(functions.contacts.GetContactsRequest(hash=0))
        return sorted(({'id': u.id, 'name': _name(u), 'username': getattr(u, 'username', '') or '', 'online': type(getattr(u, 'status', None)).__name__ == 'UserStatusOnline'}
                       for u in r.users), key=lambda x: x['name'].lower())

    async def a_chatinfo(self, aid, chat):
        cl = self._cl(aid)
        ent = await cl.get_entity(chat)
        kind = type(ent).__name__
        info = {'id': chat, 'name': _name(ent), 'username': getattr(ent, 'username', '') or '', 'phone': getattr(ent, 'phone', '') or '',
                'about': '', 'members': None, 'status': status_text(ent), 'type': 'user' if kind == 'User' else ('channel' if getattr(ent, 'broadcast', False) else 'group'),
                'bot': bool(getattr(ent, 'bot', False)), 'verified': bool(getattr(ent, 'verified', False)), 'self': bool(getattr(ent, 'is_self', False))}
        try:
            if kind == 'User':
                f = await cl(functions.users.GetFullUserRequest(ent))
                info['about'] = getattr(f.full_user, 'about', '') or ''
            elif kind == 'Channel':
                f = await cl(functions.channels.GetFullChannelRequest(ent))
                info['about'] = getattr(f.full_chat, 'about', '') or ''
                info['members'] = getattr(f.full_chat, 'participants_count', None)
            else:
                f = await cl(functions.messages.GetFullChatRequest(chat_id=ent.id))
                info['about'] = getattr(f.full_chat, 'about', '') or ''
                info['members'] = len(getattr(getattr(f.full_chat, 'participants', None), 'participants', []) or [])
        except Exception:
            pass
        if info['type'] == 'group' and info['members']:
            info['status'] = f"{info['members']} members"
        elif info['type'] == 'channel' and info['members']:
            info['status'] = f"{info['members']} subscribers"
        return info

    async def a_members(self, aid, chat, q):
        ps = await self._cl(aid).get_participants(chat, limit=100, search=q or '')
        return [{'id': p.id, 'name': _name(p), 'username': getattr(p, 'username', '') or '', 'bot': bool(getattr(p, 'bot', False)),
                 'status': status_text(p)} for p in ps]

    async def a_shared(self, aid, chat, kind, before):
        if kind not in FILTERS:
            raise ApiError('चुकीचा प्रकार')
        ms = await self._cl(aid).get_messages(chat, limit=30, offset_id=before or 0, filter=getattr(types, FILTERS[kind])())
        return [ser_msg(m) for m in ms]

    async def a_sessions(self, aid):
        r = await self._cl(aid)(functions.account.GetAuthorizationsRequest())
        return [{'hash': str(a.hash), 'device': a.device_model, 'platform': a.platform, 'app': f'{a.app_name} {a.app_version}',
                 'ip': a.ip, 'country': a.country, 'created': _ts(a.date_created), 'active': _ts(a.date_active), 'current': bool(a.current)}
                for a in r.authorizations]

    async def a_terminate(self, aid, h):
        await self._cl(aid)(functions.account.ResetAuthorizationRequest(hash=int(h)))

    async def a_setme(self, aid, first, last, about):
        await self._cl(aid)(functions.account.UpdateProfileRequest(first_name=first or None, last_name=last, about=about))

    async def a_avatar(self, aid, chat):
        key = (aid, chat)
        if key in self.avatars:
            return self.avatars[key]
        async with self.avatar_sem:
            cl = self._cl(aid)
            peer = await cl.get_input_entity(chat)
            data = await cl.download_profile_photo(peer, file=bytes, download_big=False)
        _lru_put(self.avatars, key, data or b'', 500, 20 * MB)
        return data or b''

    async def a_thumb(self, aid, chat, mid):
        key = (aid, chat, mid)
        if key in self.thumbs:
            return self.thumbs[key]
        cl = self._cl(aid)
        m = await cl.get_messages(chat, ids=mid)
        info = media_info(m) if m else None
        if not info:
            raise ApiError('media नाही', 404)
        idx = _thumb_index(m)
        if info['kind'] == 'sticker' and 0 < info['size'] < 400 * 1024 and 'webp' in info['mime']:
            data = (await cl.download_media(m, file=bytes),)
        elif idx is not None:
            data = (await cl.download_media(m, file=bytes, thumb=idx),)
        elif info['kind'] == 'photo' and 0 < info['size'] < 300 * 1024:
            data = (await cl.download_media(m, file=bytes),)
        else:
            raise ApiError('thumb नाही', 404)
        if not data[0]:
            raise ApiError('thumb अयशस्वी', 502)
        _lru_put(self.thumbs, key, data[0], 400, 16 * MB)
        return data[0]

    async def a_info(self, aid, chat, mid):
        m = await self._cl(aid).get_messages(chat, ids=mid)
        info = media_info(m) if m else None
        if not info:
            raise ApiError('media सापडला नाही', 404)
        return info

    async def a_iter(self, aid, chat, mid, offset, size):
        cl = self._cl(aid)
        m = await cl.get_messages(chat, ids=mid)
        it = cl.iter_download(m.media, offset=offset, chunk_size=REQ, request_size=REQ, file_size=size)
        return it.__aiter__()

    async def a_next(self, it):
        try:
            return await it.__anext__()
        except StopAsyncIteration:
            return None

    async def a_close(self, it):
        try:
            c = getattr(it, 'close', None)
            if c:
                r = c()
                if asyncio.iscoroutine(r):
                    await r
        except Exception:
            pass

    # --- मोठी फाइल Telegram → server disk (background, progress, अर्धवट असेल तर पुढे चालू)
    async def a_download(self, key):
        with DL_LOCK:
            j = DL.get(key)
        if not j:
            return
        final = _cpath(key)
        part = final + '.part'
        def ev(state, **kw):
            self.push(j['acc'], 'dl', j['chat'], None, None, mid=j['id'], state=state, done=j['done'], total=j['size'], **kw)
        try:
            cl = self._cl(j['acc'])
            m = await cl.get_messages(j['chat'], ids=j['id'])
            if not m or not getattr(m, 'media', None):
                raise ApiError('media सापडला नाही', 404)
            size, done = j['size'], 0
            if os.path.exists(part):
                n = os.path.getsize(part)
                done = n - n % REQ                       # REQ च्या पटीत → Telegram ला चालतो
            with open(part, 'r+b' if os.path.exists(part) else 'wb') as f:
                f.truncate(done)
                f.seek(done)
                j['done'] = done
                last = 0.0
                async for chunk in cl.iter_download(m.media, offset=done, chunk_size=REQ, request_size=REQ, file_size=size):
                    f.write(chunk)
                    done += len(chunk)
                    j['done'] = done
                    if time.time() - last > 0.7:
                        last = time.time()
                        ev('downloading')
                    if j.get('cancel'):
                        raise asyncio.CancelledError()
            if done < size:
                raise IOError(f'अपूर्ण download ({done}/{size})')
            os.replace(part, final)
            j.update(state='cached', done=size)
            ev('cached')
            await asyncio.get_running_loop().run_in_executor(None, cache_evict, 0)
        except asyncio.CancelledError:
            j['state'] = 'none'
            ev('none')
            raise
        except Exception as e:
            j.update(state='error', err=f'{e}'[:140])
            ev('error', err=j['err'])
        finally:
            self.dl_tasks.pop(key, None)

    # --- मोठ्या फाइलचा upload (disk वरून Telegram ला, progress सकट)
    async def a_send_file(self, up_id):
        with UP_LOCK:
            up = UP.get(up_id)
        if not up:
            return
        last = [0.0]
        def prog(sent, total):
            up['sent'] = int(sent)
            if time.time() - last[0] > 0.7:
                last[0] = time.time()
                self.push(up['acc'], 'up', up['chat'], None, None, uid=up_id, sent=int(sent), total=int(total), state='sending', name=up['name'])
        try:
            kw = {}
            if up.get('schedule'):
                kw['schedule'] = datetime.fromtimestamp(int(up['schedule']), tz=timezone.utc)
            m = await self._cl(up['acc']).send_file(
                up['chat'], up['path'], caption=up.get('caption') or None, force_document=bool(up.get('as_file')),
                reply_to=up.get('reply_to') or None, silent=bool(up.get('silent')), supports_streaming=True,
                parse_mode='md' if up.get('md') else None, progress_callback=prog, **kw)
            m = m[0] if isinstance(m, list) else m
            bw_add(up['size'] + 500)
            up.update(state='done', msg=ser_msg(m))
            self.push(up['acc'], 'up', up['chat'], up['msg'], None, uid=up_id, sent=up['size'], total=up['size'], state='done', name=up['name'])
        except asyncio.CancelledError:
            up['state'] = 'canceled'
            self.push(up['acc'], 'up', up['chat'], None, None, uid=up_id, state='canceled', name=up['name'])
            raise
        except Exception as e:
            up.update(state='error', err=f'{e}'[:160])
            self.push(up['acc'], 'up', up['chat'], None, None, uid=up_id, state='error', err=up['err'], name=up['name'])
        finally:
            shutil.rmtree(os.path.dirname(up['path']), ignore_errors=True)
            self.up_tasks.pop(up_id, None)

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
        self.premium[aid] = bool(getattr(me, 'premium', False))
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
                        'uid': d.get('uid'), 'enabled': d.get('enabled', True), 'state': st['state'], 'err': st.get('err', ''),
                        'premium': self.premium.get(d['_id'], False), 'max_upload': self.limit_for(d['_id'])})
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

def _sec_headers():
    return {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; connect-src 'self'; img-src 'self' data: blob:; "
                                       "media-src 'self' blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}

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
        abort(404)
    if request.method == 'POST':
        if request.headers.get('X-Requested-With') != 'app':
            abort(404)
        o = request.headers.get('Origin')
        if o and o.split('://', 1)[-1] != request.host:
            abort(404)
    return None

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
            del TOKENS[hit]
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

def _int(name, default=None, src=None):
    v = (src if src is not None else request.values).get(name, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(f'{name} चुकीचा')

def _json():
    return request.get_json(silent=True) or {}

def _acc(d=None):
    aid = (d or {}).get('acc') or request.values.get('acc')
    if not aid or not re.fullmatch(r'a_[0-9a-f]{8}', str(aid)):
        raise ApiError('account निवडा')
    return aid

def _ids(d, key='ids'):
    v = d.get(key)
    if not isinstance(v, list) or not v or len(v) > 100:
        raise ApiError('ids चुकीचे')
    return [int(x) for x in v]

@bp.get('', strict_slashes=False)
def ui():
    return Response(UI_PAGE, mimetype='text/html', headers=_sec_headers())

@bp.get('/api/state')
@api
def api_state():
    return jsonify(accounts=M.accounts_view(), bw=bw_status(), default_api=bool(DEF_API_ID and DEF_API_HASH),
                   chunk=CHUNK, seq=M.seq)

@bp.get('/api/dialogs')
@api
def api_dialogs():
    return jsonify(dialogs=M.call(M.a_dialogs(_acc(), request.args.get('archived') == '1')))

@bp.get('/api/messages')
@api
def api_messages():
    lim = max(1, min(_int('limit', 30), 60))
    return jsonify(M.call(M.a_messages(_acc(), _int('chat'), _int('before', 0), lim)))

@bp.get('/api/around')
@api
def api_around():
    return jsonify(messages=M.call(M.a_around(_acc(), _int('chat'), _int('id'))))

@bp.get('/api/search')
@api
def api_search():
    q = request.args.get('q', '').strip()[:100]
    if len(q) < 2:
        raise ApiError('कमीत कमी 2 अक्षरं')
    chat = request.args.get('chat')
    return jsonify(messages=M.call(M.a_search(_acc(), q, int(chat) if chat else None)))

@bp.get('/api/chatinfo')
@api
def api_chatinfo():
    return jsonify(info=M.call(M.a_chatinfo(_acc(), _int('chat'))))

@bp.get('/api/members')
@api
def api_members():
    return jsonify(members=M.call(M.a_members(_acc(), _int('chat'), request.args.get('q', '')[:50])))

@bp.get('/api/shared')
@api
def api_shared():
    return jsonify(messages=M.call(M.a_shared(_acc(), _int('chat'), request.args.get('kind', ''), _int('before', 0))))

@bp.get('/api/contacts')
@api
def api_contacts():
    return jsonify(contacts=M.call(M.a_contacts(_acc())))

@bp.get('/api/sessions')
@api
def api_sessions():
    return jsonify(sessions=M.call(M.a_sessions(_acc())))

@bp.post('/api/sessions/terminate')
@api
def api_terminate():
    d = _json()
    if not str(d.get('hash', '')).lstrip('-').isdigit():
        raise ApiError('hash चुकीचा')
    M.call(M.a_terminate(_acc(d), d['hash']))
    return jsonify(ok=1)

@bp.post('/api/send')
@api
def api_send():
    d = _json()
    text = str(d.get('text', ''))
    if not text.strip() or len(text) > 4096:
        raise ApiError('text रिकामा किंवा 4096 पेक्षा मोठा')
    return jsonify(message=M.call(M.a_send(_acc(d), int(d['chat']), text, d.get('reply_to'), d.get('md'), d.get('silent'),
                                           d.get('schedule'), d.get('preview', True))))

@bp.post('/api/edit')
@api
def api_edit():
    d = _json()
    text = str(d.get('text', ''))
    if not text.strip() or len(text) > 4096:
        raise ApiError('text रिकामा किंवा मोठा')
    return jsonify(message=M.call(M.a_edit(_acc(d), int(d['chat']), int(d['id']), text, d.get('md'))))

@bp.post('/api/delete')
@api
def api_delete():
    d = _json()
    M.call(M.a_delete(_acc(d), int(d['chat']), _ids(d), d.get('revoke', True)))
    return jsonify(ok=1)

@bp.post('/api/forward')
@api
def api_forward():
    d = _json()
    return jsonify(messages=M.call(M.a_forward(_acc(d), int(d['from_chat']), _ids(d), int(d['to_chat']), d.get('hide_author'))))

@bp.post('/api/pin')
@api
def api_pin():
    d = _json()
    M.call(M.a_pin(_acc(d), int(d['chat']), int(d['id']), d.get('unpin')))
    return jsonify(ok=1)

@bp.post('/api/react')
@api
def api_react():
    d = _json()
    em = str(d.get('emoji') or '')[:8]
    return jsonify(message=M.call(M.a_react(_acc(d), int(d['chat']), int(d['id']), em)))

@bp.post('/api/vote')
@api
def api_vote():
    d = _json()
    return jsonify(message=M.call(M.a_vote(_acc(d), int(d['chat']), int(d['id']), [int(x) for x in d.get('options', [])][:10])))

@bp.post('/api/click')
@api
def api_click():
    d = _json()
    return jsonify(M.call(M.a_click(_acc(d), int(d['chat']), int(d['id']), int(d['i']), int(d['j']))))

@bp.post('/api/read')
@api
def api_read():
    d = _json()
    M.call(M.a_read(_acc(d), int(d['chat']), int(d.get('max_id', 0))))
    return jsonify(ok=1)

@bp.post('/api/typing')
@api
def api_typing():
    d = _json()
    M.call(M.a_typing(_acc(d), int(d['chat'])))
    return jsonify(ok=1)

@bp.post('/api/chat/mute')
@api
def api_chat_mute():
    d = _json()
    M.call(M.a_mute(_acc(d), int(d['chat']), d.get('mute')))
    return jsonify(ok=1)

@bp.post('/api/chat/pin')
@api
def api_chat_pin():
    d = _json()
    M.call(M.a_chatpin(_acc(d), int(d['chat']), d.get('pin')))
    return jsonify(ok=1)

@bp.post('/api/chat/archive')
@api
def api_chat_archive():
    d = _json()
    M.call(M.a_archive(_acc(d), int(d['chat']), d.get('archive')))
    return jsonify(ok=1)

@bp.post('/api/chat/delete')
@api
def api_chat_delete():
    d = _json()
    M.call(M.a_chatdelete(_acc(d), int(d['chat'])))
    return jsonify(ok=1)

@bp.post('/api/resolve')
@api
def api_resolve():
    d = _json()
    return jsonify(dialog=M.call(M.a_resolve(_acc(d), str(d.get('q', '')))))

@bp.post('/api/join')
@api
def api_join():
    d = _json()
    return jsonify(dialog=M.call(M.a_join(_acc(d), str(d.get('q', '')))))

@bp.post('/api/me')
@api
def api_me():
    d = _json()
    M.call(M.a_setme(_acc(d), str(d.get('first', ''))[:64], str(d.get('last', ''))[:64], str(d.get('about', ''))[:140]))
    return jsonify(ok=1)

# --- images: avatar / thumb
def _img(data, mime, days=7):
    return Response(data, mimetype=mime, headers={'Cache-Control': f'private, max-age={days * 86400}',
                                                  'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; sandbox"})

@bp.get('/api/avatar')
@api
def api_avatar():
    data = M.call(M.a_avatar(_acc(), _int('chat')))
    if not data:
        return Response(status=204, headers={'Cache-Control': 'private, max-age=3600'})
    return _img(data, 'image/jpeg')

@bp.get('/api/thumb')
@api
def api_thumb():
    data = M.call(M.a_thumb(_acc(), _int('chat'), _int('id')), 90)
    mime = 'image/webp' if data[:4] == b'RIFF' else ('image/png' if data[:4] == b'\x89PNG' else 'image/jpeg')
    return _img(data, mime, 1)

# --- मोठी फाइल: streaming + Range
INLINE_OK = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'video/mp4', 'video/webm', 'audio/mpeg', 'audio/ogg',
             'audio/mp4', 'audio/webm', 'audio/wav', 'audio/x-wav', 'audio/aac'}

def parse_range(h, size):
    if not h:
        return 0, size - 1, False
    m = re.fullmatch(r'bytes=(\d*)-(\d*)', h.strip())
    if not m or (m.group(1) == '' and m.group(2) == ''):
        return 0, size - 1, False
    a, b = m.groups()
    if a == '':
        start, end = max(0, size - int(b)), size - 1
    else:
        start, end = int(a), (int(b) if b else size - 1)
    if start >= size or start > end:
        raise ApiError('range चुकीचा', 416)
    return start, min(end, size - 1), True

def _file_headers(info, mid, start, end, size, partial):
    length = end - start + 1
    mime = info['mime'] if info['mime'] in INLINE_OK else 'application/octet-stream'
    dl = request.args.get('dl') == '1'
    disp = 'attachment' if dl or mime not in INLINE_OK else 'inline'
    fname = urllib.parse.quote(re.sub(r'[\r\n"\\]', '_', info['name'] or f'file_{mid}'))
    h = {'Content-Length': str(length), 'Accept-Ranges': 'bytes', 'Content-Disposition': f"{disp}; filename*=UTF-8''{fname}",
         'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, max-age=3600',
         'Content-Security-Policy': "default-src 'none'; sandbox", 'X-BW-Counted': '1'}
    if partial:
        h['Content-Range'] = f'bytes {start}-{end}/{size}'
    return mime, h

@bp.get('/api/file')
@api
def api_file():
    aid, chat, mid = _acc(), _int('chat'), _int('id')
    info = M.call(M.a_info(aid, chat, mid))
    size = info['size']
    if size <= 0:
        raise ApiError('फाइल रिकामी आहे', 404)
    if size > MAX_MEDIA:
        raise ApiError(f'फाइल {MAX_MEDIA // MB} MB पेक्षा मोठी आहे', 413)
    start, end, partial = parse_range(request.headers.get('Range'), size)
    length = end - start + 1
    st = bw_status()
    if st['used'] + length > st['limit']:
        raise ApiError('या महिन्याची bandwidth मर्यादा संपली — फक्त text चालतो', 429)
    mime, h = _file_headers(info, mid, start, end, size, partial)
    cp = _cpath(_ckey(aid, chat, mid))
    if os.path.isfile(cp) and os.path.getsize(cp) == size:           # server च्या disk वर आधीच आहे → Telegram ला विचारायचं नाही
        try:
            os.utime(cp)                                                # LRU: नुकतीच वापरली
        except OSError:
            pass
        def gen_disk():
            remaining = length
            with open(cp, 'rb') as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    bw_add(len(chunk))
                    yield chunk
        return Response(gen_disk(), status=206 if partial else 200, mimetype=mime, headers=h, direct_passthrough=True)
    if not STREAMS.acquire(timeout=20):
        raise ApiError('खूप downloads चालू आहेत — थोड्या वेळाने', 503)
    try:
        it = M.call(M.a_iter(aid, chat, mid, start - start % REQ, size))
    except Exception:
        STREAMS.release()
        raise

    def gen():
        skip, remaining = start % REQ, length
        try:
            while remaining > 0:
                chunk = M.call(M.a_next(it), 120)
                if not chunk:
                    break
                if skip:
                    chunk, skip = chunk[skip:], 0
                chunk = chunk[:remaining]
                remaining -= len(chunk)
                bw_add(len(chunk))
                yield chunk
        finally:
            try:
                M.call(M.a_close(it), 10)
            except Exception:
                pass
            STREAMS.release()

    return Response(gen(), status=206 if partial else 200, mimetype=mime, headers=h, direct_passthrough=True)

# --- Server disk cache: Telegram → server (background), मग तिथून
def _dl_state(aid, chat, mid):
    key = _ckey(aid, chat, mid)
    if os.path.isfile(_cpath(key)):
        return {'state': 'cached', 'done': os.path.getsize(_cpath(key)), 'size': os.path.getsize(_cpath(key))}
    with DL_LOCK:
        j = DL.get(key)
    if j and j['state'] in ('downloading', 'error'):
        return {'state': j['state'], 'done': j['done'], 'size': j['size'], 'err': j.get('err', '')}
    return {'state': 'none', 'done': 0, 'size': 0}

@bp.post('/api/dl/start')
@api
def api_dl_start():
    d = _json()
    aid, chat, mid = _acc(d), int(d['chat']), int(d['id'])
    cur = _dl_state(aid, chat, mid)
    if cur['state'] in ('cached', 'downloading'):
        return jsonify(cur)
    info = M.call(M.a_info(aid, chat, mid))
    size = info['size']
    if size <= 0:
        raise ApiError('फाइल रिकामी आहे', 404)
    if size > DISK_CACHE:
        raise ApiError(f'फाइल server cache (DISK_CACHE_GB = {DISK_CACHE // MB} MB) पेक्षा मोठी आहे — थेट stream करा', 413)
    with DL_LOCK:
        if sum(1 for j in DL.values() if j['state'] == 'downloading') >= 2:
            raise ApiError('आधीचे 2 downloads पूर्ण होऊ द्या', 429)
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_evict(size)                                                   # जागा करतो (जुन्या फाइल हटवून)
    if shutil.disk_usage(CACHE_DIR).free < size * 1.02 + 300 * MB:
        raise ApiError('server वर disk जागा कमी — cache साफ करा किंवा थेट stream करा', 507)
    key = _ckey(aid, chat, mid)
    with DL_LOCK:
        DL[key] = {'acc': aid, 'chat': chat, 'id': mid, 'size': size, 'done': 0, 'state': 'downloading', 'err': ''}
    M.dl_tasks[key] = asyncio.run_coroutine_threadsafe(M.a_download(key), M.loop)
    return jsonify(state='downloading', done=0, size=size)

@bp.post('/api/dl/cancel')
@api
def api_dl_cancel():
    d = _json()
    key = _ckey(_acc(d), int(d['chat']), int(d['id']))
    with DL_LOCK:
        j = DL.get(key)
    if j:
        j['cancel'] = True
    return jsonify(ok=1)

@bp.get('/api/dl/status')
@api
def api_dl_status():
    aid, chat = _acc(), _int('chat')
    ids = [int(x) for x in request.args.get('ids', '').split(',') if x.strip().isdigit()][:60]
    return jsonify(items={str(i): _dl_state(aid, chat, i) for i in ids})

@bp.get('/api/cache')
@api
def api_cache():
    used, n = cache_usage()
    return jsonify(used=used, limit=DISK_CACHE, files=n)

@bp.post('/api/cache/clear')
@api
def api_cache_clear():
    done, _ = cache_files()
    for _, _, p in done:
        try:
            os.remove(p)
        except OSError:
            pass
    return jsonify(ok=1, removed=len(done))

# --- मोठ्या फाइलचा upload: 8 MB तुकडे → disk → Telegram
@bp.post('/api/up/start')
@api
def api_up_start():
    d = _json()
    aid, chat = _acc(d), int(d['chat'])
    size = int(d.get('size', 0))
    name = re.sub(r'[^\w.\- ()\[\]]', '_', os.path.basename(str(d.get('name', 'file'))))[:120] or 'file'
    if aid not in M.clients:
        raise ApiError('हा account सध्या online नाही', 409)
    lim = M.limit_for(aid)
    if size <= 0:
        raise ApiError('फाइल रिकामी आहे')
    if size > lim:
        raise ApiError(f'फाइल {lim // MB} MB पेक्षा मोठी आहे (या account ची Telegram मर्यादा)', 413)
    st = bw_status()
    if st['used'] + size > st['limit']:
        raise ApiError('या महिन्याची bandwidth मर्यादा संपली', 429)
    with UP_LOCK:
        if sum(1 for u in UP.values() if u['state'] in ('receiving', 'sending')) >= 3:
            raise ApiError('आधीचे upload पूर्ण होऊ द्या (एका वेळी 3)', 429)
    os.makedirs(UPDIR, exist_ok=True)
    if shutil.disk_usage(UPDIR).free < size * 1.05 + 200 * MB:
        raise ApiError('server वर जागा कमी (disk) — लहान फाइल पाठवा', 507)
    uid = secrets.token_hex(6)
    folder = os.path.join(UPDIR, uid)
    os.makedirs(folder)
    with UP_LOCK:
        UP[uid] = {'acc': aid, 'chat': chat, 'name': name, 'size': size, 'written': 0, 'sent': 0, 'path': os.path.join(folder, name),
                   'caption': str(d.get('caption', ''))[:1000], 'reply_to': d.get('reply_to'), 'as_file': bool(d.get('as_file')),
                   'silent': bool(d.get('silent')), 'schedule': d.get('schedule'), 'md': bool(d.get('md')), 'state': 'receiving', 'err': ''}
    return jsonify(id=uid, chunk=CHUNK)

def _up(uid):
    with UP_LOCK:
        up = UP.get(uid)
    if not up:
        raise ApiError('upload सापडला नाही', 404)
    return up

@bp.post('/api/up/chunk')
@api
def api_up_chunk():
    up = _up(request.args.get('id', ''))
    off = _int('off')
    if up['state'] != 'receiving':
        raise ApiError('upload आता चालू नाही', 409)
    if off != up['written']:
        return jsonify(error='offset जुळत नाही', written=up['written']), 409
    data = request.get_data()
    if not data or up['written'] + len(data) > up['size']:
        raise ApiError('तुकडा चुकीचा')
    with open(up['path'], 'ab') as f:
        f.write(data)
    up['written'] += len(data)
    return jsonify(written=up['written'])

@bp.post('/api/up/finish')
@api
def api_up_finish():
    up_id = str(_json().get('id', ''))
    up = _up(up_id)
    if up['written'] != up['size']:
        raise ApiError('फाइल पूर्ण आलेली नाही', 409)
    up['state'] = 'sending'
    M.up_tasks[up_id] = asyncio.run_coroutine_threadsafe(M.a_send_file(up_id), M.loop)
    return jsonify(ok=1)

@bp.post('/api/up/cancel')
@api
def api_up_cancel():
    up_id = str(_json().get('id', ''))
    up = _up(up_id)
    fut = M.up_tasks.pop(up_id, None)
    if fut:
        fut.cancel()
    up['state'] = 'canceled'
    shutil.rmtree(os.path.dirname(up['path']), ignore_errors=True)
    return jsonify(ok=1)

@bp.get('/api/up/status')
@api
def api_up_status():
    up = _up(request.args.get('id', ''))
    return jsonify(state=up['state'], written=up['written'], sent=up['sent'], size=up['size'], err=up['err'], message=up.get('msg'))

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
        if request.path.startswith('/app'):
            if resp.headers.pop('X-BW-Counted', None):
                return resp                              # stream: तुकड्यांनुसार आधीच मोजलं
            if resp.content_length:
                bw_add(resp.content_length)
    except Exception:
        pass
    return resp


# ───────────────────────── init ─────────────────────────
def init(app, db, notify):
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
