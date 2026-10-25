#!/usr/bin/env python3
"""
watermark.py  —  Worker Manager (PC permanent server mode)

फक्त एकदा:   python watermark.py
त्यानंतर:    PC चालू होताच (login/unlock शिवाय) आपोआप सुरू होतं.

हा file तुमचा जुना code (worker.py) चालवतो, crash झाला तर restart करतो,
internet गेलं तर वाट बघतो, आणि web panel (terminal + code editor + logs) देतो.

Commands:
  python watermark.py            → setup + install + start (पहिल्यांदा)
  python watermark.py status     → चालू आहे का
  python watermark.py uninstall  → auto-start काढतो
  python watermark.py run        → foreground मध्ये चालवतो (testing)
"""
import os, sys, subprocess, time, threading, json, re, socket, secrets, hmac, shutil
import py_compile, urllib.request, urllib.parse, urllib.error, collections, functools, logging
from pathlib import Path

SELF = Path(__file__).resolve()
BASE = SELF.parent
os.chdir(BASE)
IS_WIN = os.name == 'nt'
VENV = BASE / '.venv'
VENV_PY = VENV / ('Scripts/python.exe' if IS_WIN else 'bin/python')
VENV_PYW = VENV / ('Scripts/pythonw.exe' if IS_WIN else 'bin/python')
ENV_FILE = BASE / '.env'
WORKER = BASE / 'worker.py'          # ← तुमचा जुना code इथे ठेवा (नाव worker.py)
LOG_DIR = BASE / 'logs'
BACKUPS = BASE / 'backups'
STATE_FILE = BASE / 'state.json'
PID_FILE = BASE / 'worker.pid'
TASK = 'WatermarkWorker'
NO_WINDOW = 0x08000000 if IS_WIN else 0
REQS = ['flask', 'requests', 'pyaes', 'pysocks', 'pyrogram==2.0.106']


# ───────────────────────── bootstrap (venv + deps) ─────────────────────────
def bootstrap():
    """Private venv बनवतो — म्हणजे SYSTEM account सुद्धा सगळे packages बघू शकतो."""
    if Path(sys.prefix).resolve() == VENV.resolve():
        return
    if not VENV_PY.exists():
        print('📦 virtual environment बनवतोय...')
        subprocess.check_call([sys.executable, '-m', 'venv', str(VENV)])
    marker = VENV / '.deps_ok2'          # नवं नाव: जुने अर्धवट venv आपोआप दुरुस्त होतात
    if not marker.exists():
        py = str(VENV_PY)
        print('📦 packages install करतोय (पहिल्यांदाच, थोडा वेळ लागेल)...')
        subprocess.call([py, '-m', 'pip', 'install', '-q', '--upgrade', 'pip'])
        for r in REQS:
            if subprocess.call([py, '-m', 'pip', 'install', '-q', r]):
                print(f'⚠️  {r} install failed')
        # tgcrypto: compiler नको म्हणून फक्त तयार (wheel) package. Python 3.12/3.13 साठी
        # मूळ tgcrypto चा Windows wheel नाही → tgcrypto-pyrofork (तोही `import tgcrypto`).
        crypto_ok = False
        for pkg in ('tgcrypto', 'tgcrypto-pyrofork'):
            subprocess.call([py, '-m', 'pip', 'install', '-q', '--only-binary=:all:', pkg],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if subprocess.call([py, '-c', 'import tgcrypto'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0:
                crypto_ok = True
                print(f'✅ tgcrypto तयार ({pkg})')
                break
        if crypto_ok:
            marker.write_text('ok')
        else:
            print('⚠️  tgcrypto install झालं नाही — worker ला हे लागतं. Python 3.11/3.12 (64-bit) वापरा,'
                  ' मग `python watermark.py` परत चालवा.')
    sys.exit(subprocess.call([str(VENV_PY), str(SELF)] + sys.argv[1:]))


# ───────────────────────── .env ─────────────────────────
def read_env():
    d = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    return d

def load_env():
    os.environ.update(read_env())

def write_env(d):
    ENV_FILE.write_text(''.join(f'{k}={v}\n' for k, v in d.items()), encoding='utf-8')

def wizard():
    env = read_env()
    def ask(key, prompt, default=''):
        if env.get(key):
            return
        hint = f' [{default}]' if default else ''
        val = input(f'{prompt}{hint}: ').strip() or default
        if val:
            env[key] = val
    print('\n── पहिल्यांदाचं setup (.env) ──')
    print('ℹ️  COLAB_SECRET / bot tokens इथे नकोत — secret worker.py मध्ये hardcode करा.')
    print('   Terminal URL तुमच्या server ला आपोआप जाईल (/api/colab/terminal).')
    if not get_secret():
        import getpass
        sec = getpass.getpass('COLAB_SECRET टाका (टाइप करताना दिसणार नाही): ').strip()
        if sec:
            env['COLAB_SECRET'] = sec
    if not env.get('PANEL_PASSWORD'):
        pw = input('Panel password ठरवा (Enter = आपोआप बनवा): ').strip()
        env['PANEL_PASSWORD'] = pw or secrets.token_urlsafe(12)
        print(f'🔑 Panel password (लिहून ठेवा): {env["PANEL_PASSWORD"]}')
    env.setdefault('PANEL_PORT', '8765')
    write_env(env)
    print('✅ .env तयार\n')


# ───────────────────────── logging ─────────────────────────
LAST_W_LOG = [0.0]
INFO = {'encoder': '', 'last_job': ''}
LOG = collections.deque(maxlen=3000)
LOG_N = 0
LOG_LOCK = threading.Lock()
_logf = None

def log(line, src='M'):
    global LOG_N, _logf
    line = line.rstrip('\r\n')
    ts = time.strftime('%H:%M:%S')
    text = f'[{ts}]{"" if src == "W" else " [mgr]"} {line}'
    if src == 'W':
        LAST_W_LOG[0] = time.time()
        if 'Encoder mode ready:' in line:
            INFO['encoder'] = line.split(':', 1)[1].strip()
        if '📦 Job #' in line or ('Job ' in line and 'done!' in line):
            INFO['last_job'] = f'{ts} {line.strip()[:110]}'
    with LOG_LOCK:
        LOG.append((LOG_N, text))
        LOG_N += 1
        try:
            LOG_DIR.mkdir(exist_ok=True)
            f = LOG_DIR / 'worker.log'
            if f.exists() and f.stat().st_size > 5_000_000:
                f.replace(LOG_DIR / 'worker.log.1')
            with open(f, 'a', encoding='utf-8') as fh:
                fh.write(text + '\n')
        except Exception:
            pass


# ───────────────────────── helpers ─────────────────────────
STATE = {'url': '', 'started': time.time()}
PANEL_TOKENS = {}                  # bot ने दिलेले एकदाचे terminal tokens: token -> expiry
PANEL_TOKENS_LOCK = threading.Lock()
W = {'proc': None, 'want': True, 'started': 0.0, 'restarts': 0, 'last_exit': None, 'manual': False}

def save_state():
    try:
        STATE_FILE.write_text(json.dumps(STATE), encoding='utf-8')
    except Exception:
        pass

def internet_ok():
    for host in (('1.1.1.1', 443), ('8.8.8.8', 443), ('api.telegram.org', 443)):
        try:
            socket.create_connection(host, timeout=4).close()
            return True
        except OSError:
            continue
    return False

def wait_internet(keep_going=lambda: True):
    n = 0
    while keep_going():
        if internet_ok():
            return True
        if n % 12 == 0:
            log('🌐 internet नाहीये — येईपर्यंत वाट बघतोय...')
        n += 1
        time.sleep(5)
    return False

def notify(text):
    tok = os.environ.get('NOTIFY_BOT_TOKEN') or os.environ.get('UPLOAD_BOT_TOKEN')
    chat = os.environ.get('ADMIN_CHAT_ID')
    if not (tok and chat):
        return
    try:
        data = urllib.parse.urlencode({'chat_id': chat, 'text': text}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f'https://api.telegram.org/bot{tok}/sendMessage', data=data), timeout=15)
    except Exception as e:
        log(f'notify failed: {e}')

def kill_tree(pid, grace=30):
    if IS_WIN:
        subprocess.call(['taskkill', '/PID', str(pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    else:
        import signal
        try:
            os.killpg(pid, signal.SIGTERM)
            for _ in range(grace):
                time.sleep(1)
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    return
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ───────────────────────── worker supervisor ─────────────────────────
def worker_env():
    e = os.environ.copy()
    e['PYTHONUTF8'] = '1'
    e['PYTHONIOENCODING'] = 'utf-8'
    e['PYTHONUNBUFFERED'] = '1'
    if e.get('FFMPEG_DIR'):
        e['PATH'] = e['FFMPEG_DIR'] + os.pathsep + e.get('PATH', '')
    return e

def stop_worker():
    p = W['proc']
    if p and p.poll() is None:
        kill_tree(p.pid)

def kill_stale():
    """मागच्या manager crash झाल्यावर उरलेला worker असेल तर मारतो."""
    try:
        if PID_FILE.exists():
            kill_tree(int(PID_FILE.read_text().strip()), grace=3)
            PID_FILE.unlink()
    except Exception:
        pass

def run_worker_once():
    if IS_WIN:  # worker `/tmp/...` वापरतो → Windows वर <drive>:\tmp लागतो
        try:
            os.makedirs(os.path.join(os.path.splitdrive(str(BASE))[0] + os.sep, 'tmp'), exist_ok=True)
        except Exception:
            pass
    p = subprocess.Popen(
        [str(VENV_PY), '-u', str(WORKER)], cwd=BASE, env=worker_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
        errors='replace', bufsize=1, creationflags=NO_WINDOW,
        start_new_session=not IS_WIN)
    W['proc'], W['started'] = p, time.time()
    PID_FILE.write_text(str(p.pid))
    log(f'🚀 worker सुरू (pid {p.pid})')
    for line in p.stdout:
        log(line, 'W')
    p.wait()
    return p.returncode

def worker_loop():
    kill_stale()
    backoff = 5
    while True:
        if not W['want'] or not WORKER.exists():
            if W['want']:
                log('⚠️ worker.py सापडला नाही — panel मधून upload करा')
                time.sleep(10)
            time.sleep(1)
            continue
        if not wait_internet(lambda: W['want']):
            continue
        try:
            ensure_ffmpeg()
        except Exception as e:
            log(f'⚠️ ffmpeg download failed: {e} — 30s नंतर परत')
            time.sleep(30)
            continue
        t0 = time.time()
        try:
            rc = run_worker_once()
        except Exception as e:
            rc = -1
            log(f'❌ worker start error: {e}')
        W['last_exit'] = f'{rc} @ {time.strftime("%H:%M:%S")}'
        W['restarts'] += 1
        if not W['want']:
            continue
        ran = time.time() - t0
        if W['manual']:
            W['manual'] = False
            delay, backoff = 2, 5
        elif rc == 0:
            delay = 60   # worker स्वतः थांबला (server stop command) → 60s नंतर परत
        elif ran > 120:
            delay, backoff = 5, 5
        else:
            delay, backoff = backoff, min(backoff * 2, 120)
        log(f'⚠️ worker exit code {rc} — {delay}s नंतर restart')
        for _ in range(int(delay * 2)):
            if not W['want']:
                break
            time.sleep(0.5)


# ───────────────────────── terminal URL → तुमच्या server ला report ─────────────────────────
def _worker_src():
    try:
        return WORKER.read_text(encoding='utf-8')
    except Exception:
        return ''

def get_secret():
    """COLAB_SECRET: env → worker.py मधील hardcoded value (वेगळं कुठे ठेवायची गरज नाही)."""
    v = os.environ.get('COLAB_SECRET')
    if v:
        return v
    src = _worker_src()
    for pat in (r'COLAB_SECRET\s*=.*\bor\s*["\']([^"\']+)["\']', r'COLAB_SECRET\s*=\s*["\']([^"\']+)["\']'):
        m = re.search(pat, src)
        if m:
            return m.group(1)
    return ''

def get_koyeb_url():
    v = os.environ.get('KOYEB_URL')
    if v:
        return v.rstrip('/')
    m = re.search(r'KOYEB_URL\s*=.*\bor\s*["\'](https?://[^"\']+)["\']', _worker_src())
    return m.group(1).rstrip('/') if m else ''

_NET = {'t': 0.0, 'internet': True, 'koyeb': None}

def net_status():
    """internet + job-server reachable (दर 30s ला एकदा चेक)."""
    if time.time() - _NET['t'] > 30:
        _NET['internet'] = internet_ok()
        k, ok = get_koyeb_url(), None
        if k:
            try:
                urllib.request.urlopen(urllib.request.Request(k, headers={'User-Agent': 'worker-manager'}), timeout=6)
                ok = True
            except urllib.error.HTTPError:
                ok = True            # server ने उत्तर दिलं (404 इ.) = पोहोचतोय
            except Exception:
                ok = False
        _NET['koyeb'] = ok
        _NET['t'] = time.time()
    return _NET

def status_dict():
    p = W['proc']
    run = bool(p and p.poll() is None)
    n = net_status()
    return {'host': socket.gethostname(), 'url': STATE['url'], 'running': run, 'want': W['want'],
            'uptime': int(time.time() - W['started']) if run else 0, 'restarts': W['restarts'],
            'last_exit': W['last_exit'], 'internet': n['internet'], 'koyeb_ok': n['koyeb'],
            'koyeb_url': get_koyeb_url(),
            'log_age': int(time.time() - LAST_W_LOG[0]) if LAST_W_LOG[0] else None,
            'encoder': INFO['encoder'], 'last_job': INFO['last_job']}

def health_text():
    st = status_dict()
    with LOG_LOCK:
        recent = [t for _, t in list(LOG)[-100:]]
    poll_err = sum(1 for t in recent if 'poll error' in t)
    good = (st['running'] and st['internet'] and st['koyeb_ok'] is not False
            and st['log_age'] is not None and st['log_age'] < 150 and poll_err < 5)
    yn = lambda v: '❓' if v is None else ('✅' if v else '❌')
    return '\n'.join([
        ('✅ Worker बरोबर चालू आहे' if good else '⚠️ Worker मध्ये अडचण असू शकते'),
        f"Process: {yn(st['running'])}  (uptime {st['uptime'] // 60}m, restarts {st['restarts']})",
        f"Internet: {yn(st['internet'])}   Job-server: {yn(st['koyeb_ok'])}",
        f"Last worker log: {st['log_age']}s पूर्वी (heartbeat ~60s)" if st['log_age'] is not None else 'Last worker log: -',
        f"Poll errors (last 100 lines): {poll_err}",
        f"Encoder: {st['encoder'] or '-'}",
        f"Last job: {st['last_job'] or '-'}",
        f"Last exit: {st['last_exit'] or '-'}",
        f"Job URL: {st['koyeb_url'] or '-'}"])

def run_command(c):
    t, a = c.get('type'), str(c.get('arg', ''))
    if t == 'restart':
        W['want'] = True; W['manual'] = True; stop_worker()
        return '🔁 Worker restart होतोय'
    if t == 'stop':
        W['want'] = False; stop_worker()
        return '⏹ Worker थांबवला (Start दाबेपर्यंत बंद)'
    if t == 'start':
        W['want'] = True
        return '▶️ Worker सुरू करतोय'
    if t == 'logs':
        with LOG_LOCK:
            rows = [x for _, x in list(LOG)[-25:]]
        return '📜 Last logs:\n' + '\n'.join(rows)[-3300:]
    if t == 'check':
        return health_text()
    if t == 'seturl':
        if not re.match(r'^https?://[^\s]{4,200}$', a):
            return '❌ URL चुकीचा'
        a = a.rstrip('/')
        env = read_env(); env['KOYEB_URL'] = a; write_env(env)
        os.environ['KOYEB_URL'] = a
        _NET['t'] = 0
        if W['want']:
            W['manual'] = True; stop_worker()
        return f'✅ Job-server URL बदलला:\n{a}\n🔁 Worker नव्या URL ने restart होतोय'
    if t == 'ptoken':                       # bot ने दिलेला एकदाचा terminal token (2 मिनिटं)
        if re.fullmatch(r'[A-Za-z0-9_-]{20,64}', a):
            with PANEL_TOKENS_LOCK:
                now = time.time()
                for k in [k for k, v in PANEL_TOKENS.items() if v < now]:
                    del PANEL_TOKENS[k]
                PANEL_TOKENS[a] = now + 120
        return ''                           # रिकामा result = bot वर काहीच message नाही
    if t == 'rmgr':
        threading.Thread(target=lambda: (time.sleep(2), restart_manager()), daemon=True).start()
        return '♻️ Manager restart होतोय (1-2 मिनिटांत परत येईल)'
    return f'❓ अज्ञात command: {t}'

def _post_json(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        'Content-Type': 'application/json', 'User-Agent': 'worker-manager', **headers})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8') or '{}')

def sync_loop():
    """Render शी long-poll: status पाठवतो, bot मधून आलेले commands लगेच चालवतो."""
    pcid, key = os.environ.get('PC_ID'), os.environ.get('PC_KEY')
    base = os.environ.get('REPORT_BASE', '').rstrip('/')
    if not (pcid and key and base):
        log('ℹ️ Render sync बंद (PC_ID / PC_KEY / REPORT_BASE नाहीत)')
        return
    results, fail, connected = [], 0, False
    while True:
        t0 = time.time()
        try:
            body = status_dict()
            body['results'] = results
            d = _post_json(base + '/api/sync', body, {'X-PC-Id': pcid, 'X-PC-Key': key}, 40)
            results = []
            fail = 0
            if not connected:
                connected = True
                log('📡 Render शी जोडलं गेलं')
            for c in d.get('cmds', []):
                try:
                    txt = run_command(c)
                except Exception as e:
                    txt = f'❌ error: {e}'
                results.append({'id': c.get('id'), 'text': txt})
            if not results and time.time() - t0 < 2:
                time.sleep(2)
        except Exception as e:
            fail += 1
            connected = False
            if fail in (1, 10):
                log(f'⚠️ Render sync failed: {e}')
            time.sleep(min(5 * fail, 60))


# ───────────────────────── ffmpeg auto-download ─────────────────────────
FFMPEG_URL = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'

def ensure_ffmpeg():
    """ffmpeg/ffprobe नसतील तर आपोआप download करतो (एकदाच, ~150MB)."""
    exe = 'ffmpeg.exe' if IS_WIN else 'ffmpeg'
    local = BASE / 'ffmpeg' / 'bin'
    if (local / 'ffmpeg.exe').exists() and (local / 'ffprobe.exe').exists():
        os.environ['FFMPEG_DIR'] = str(local)
        return True
    d = os.environ.get('FFMPEG_DIR')
    if d and (Path(d) / exe).exists():
        return True
    if shutil.which('ffmpeg') and shutil.which('ffprobe'):
        return True
    if not IS_WIN:
        log('⚠️ ffmpeg नाहीये — install करा: sudo apt install ffmpeg')
        return False
    import zipfile
    msg = '⬇️ ffmpeg download करतोय (~150MB, एकदाच — थोडा वेळ लागेल)...'
    print(msg); log(msg)
    zp = BASE / '_ffmpeg.zip'
    try:
        urllib.request.urlretrieve(FFMPEG_URL, str(zp))
        local.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zp) as z:
            for n in z.namelist():
                b = n.rsplit('/', 1)[-1]
                if '/bin/' in n and b in ('ffmpeg.exe', 'ffprobe.exe'):
                    with z.open(n) as src, open(local / b, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
    finally:
        zp.unlink(missing_ok=True)
    ok = (local / 'ffmpeg.exe').exists() and (local / 'ffprobe.exe').exists()
    if ok:
        os.environ['FFMPEG_DIR'] = str(local)
        print('✅ ffmpeg तयार'); log('✅ ffmpeg तयार')
    return ok


# ───────────────────────── keep awake ─────────────────────────
def keep_awake():
    if not IS_WIN:
        return
    import ctypes
    ES = 0x80000000 | 0x00000001   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED (screen बंद झाली तरी चालेल)
    while True:
        ctypes.windll.kernel32.SetThreadExecutionState(ES)
        time.sleep(30)


# ───────────────────────── cloudflared tunnel ─────────────────────────
def ensure_cloudflared():
    p = shutil.which('cloudflared')
    if p:
        return p
    exe = BASE / ('cloudflared.exe' if IS_WIN else 'cloudflared')
    if not exe.exists():
        name = 'cloudflared-windows-amd64.exe' if IS_WIN else 'cloudflared-linux-amd64'
        log('⬇️ cloudflared download करतोय...')
        urllib.request.urlretrieve(
            f'https://github.com/cloudflare/cloudflared/releases/latest/download/{name}', str(exe))
        if not IS_WIN:
            exe.chmod(0o755)
    return str(exe)

def tunnel_loop():
    port = os.environ.get('PANEL_PORT', '8765')
    backoff = 5
    while True:
        try:
            wait_internet()
            exe = ensure_cloudflared()
            token = os.environ.get('CF_TUNNEL_TOKEN')
            cmd = [exe, 'tunnel', '--no-autoupdate'] + (['run', '--token', token] if token else ['--url', f'http://127.0.0.1:{port}'])
            if token and os.environ.get('PANEL_PUBLIC_URL'):
                STATE['url'] = os.environ['PANEL_PUBLIC_URL']
                save_state()
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
            t0 = time.time()
            for line in p.stdout:
                m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if m and m.group(0) != STATE['url']:
                    STATE['url'] = m.group(0)
                    save_state()
                    log(f'🌍 Panel URL: {STATE["url"]}')
                    notify(f'🖥 Worker PC panel: {STATE["url"]}')
            p.wait()
            backoff = 5 if time.time() - t0 > 120 else min(backoff * 2, 120)
        except Exception as e:
            log(f'tunnel error: {e}')
            backoff = min(backoff * 2, 120)
        time.sleep(backoff)


# ───────────────────────── web panel ─────────────────────────
FILES = {'worker.py': WORKER, 'watermark.py': SELF, '.env': ENV_FILE}

def apply_file(name, text):
    path = FILES[name]
    if name.endswith('.py'):
        tmp = BASE / f'_check_{name}'
        tmp.write_text(text, encoding='utf-8')
        try:
            py_compile.compile(str(tmp), doraise=True, cfile=str(tmp) + 'c')
        except py_compile.PyCompileError as e:
            return False, f'Syntax error — save झालं नाही:\n{e}'
        finally:
            tmp.unlink(missing_ok=True)
            Path(str(tmp) + 'c').unlink(missing_ok=True)
    BACKUPS.mkdir(exist_ok=True)
    if path.exists():
        shutil.copy2(path, BACKUPS / f'{name}.{time.strftime("%Y%m%d-%H%M%S")}.bak')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    if name == '.env':
        load_env()
    log(f'📝 {name} update झाला (backup घेतला)')
    return True, 'saved'

def task_installed():
    if not IS_WIN:
        return False
    return subprocess.call(['schtasks', '/Query', '/TN', TASK], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=NO_WINDOW) == 0

def restart_manager():
    time.sleep(1)
    stop_worker()
    if task_installed():
        subprocess.Popen(f'cmd /c "timeout /t 4 /nobreak >nul & schtasks /Run /TN {TASK}"', shell=True,
                         creationflags=NO_WINDOW | 0x00000008)
    else:
        subprocess.Popen([str(VENV_PY), str(SELF), 'run', '--delay', '4'], cwd=BASE,
                         creationflags=(NO_WINDOW | 0x00000008 | 0x00000200) if IS_WIN else 0,
                         start_new_session=not IS_WIN)
    os._exit(0)

def make_app():
    from flask import Flask, request, session, jsonify, Response, abort
    app = Flask(__name__)
    sf = BASE / '.secret'
    if not sf.exists():
        sf.write_text(secrets.token_hex(32))
    app.secret_key = sf.read_text()
    app.config.update(SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_HTTPONLY=True,
                      PERMANENT_SESSION_LIFETIME=86400 * 7)
    fails = {}
    cwd = [str(BASE)]

    def ip():
        return request.headers.get('CF-Connecting-IP') or request.remote_addr

    def is_remote():
        # Cloudflare tunnel वरून आलेल्या requests ना हे headers नेहमी असतात (बाहेरचा माणूस ते काढू शकत नाही)
        return bool(request.headers.get('Cf-Ray') or request.headers.get('Cf-Connecting-Ip'))

    def session_ok():
        return bool(session.get('ok')) and time.time() < session.get('exp', float('inf'))

    def auth(f):
        @functools.wraps(f)
        def w(*a, **k):
            if not session_ok():
                return jsonify(error='login'), 401
            return f(*a, **k)
        return w

    @app.before_request
    def gate():
        if not is_remote() or session_ok():
            return None                               # PC वरून स्वतः (127.0.0.1) → पूर्वीसारखा password login
        if request.path == '/' and request.method == 'GET' and request.args.get('k'):
            return Response(ENTER_PAGE, mimetype='text/html', headers={'Cache-Control': 'no-store'})
        if request.path == '/enter' and request.method == 'POST':
            return None
        abort(404)                                    # internet ला login पान सुद्धा नाही — रिकामा 404

    @app.errorhandler(404)
    def _nf(e):
        return '', 404

    @app.post('/enter')
    def enter():
        d = request.get_json(silent=True) or {}
        tok, k, now = str(d.get('k', '')), ip(), time.time()
        n, t = fails.get(k, (0, 0))
        if n >= 5 and now - t < 300:
            return '', 404
        hit = None
        with PANEL_TOKENS_LOCK:
            for t2 in [t2 for t2, v in PANEL_TOKENS.items() if v < now]:
                del PANEL_TOKENS[t2]
            for t2 in list(PANEL_TOKENS):
                if hmac.compare_digest(t2.encode(), tok.encode()):
                    hit = t2
            if hit:
                del PANEL_TOKENS[hit]                 # एकदाच वापरता येतो
        if not hit:
            fails[k] = (n + 1, now)
            time.sleep(1)
            return '', 404
        session.clear()
        session['ok'] = True
        session['exp'] = now + 7200                   # remote session 2 तास
        return jsonify(ok=1)

    @app.get('/')
    def index():
        return Response(PAGE, mimetype='text/html')

    @app.post('/login')
    def login():
        d = request.get_json(silent=True) or {}
        k = ip()
        n, t = fails.get(k, (0, 0))
        if n >= 5 and time.time() - t < 300:
            return jsonify(error='५ चुकीचे प्रयत्न — ५ मिनिटं थांबा'), 429
        pw = os.environ.get('PANEL_PASSWORD', '')
        if pw and hmac.compare_digest(str(d.get('password', '')).encode(), pw.encode()):
            session.permanent = True
            session['ok'] = True
            fails.pop(k, None)
            return jsonify(ok=1)
        fails[k] = (n + 1, time.time())
        time.sleep(1)
        return jsonify(error='चुकीचा password'), 403

    @app.get('/api/status')
    @auth
    def status():
        p = W['proc']
        run = bool(p and p.poll() is None)
        return jsonify(running=run, pid=p.pid if run else None, want=W['want'],
                       uptime=int(time.time() - W['started']) if run else 0,
                       restarts=W['restarts'], last_exit=W['last_exit'], url=STATE['url'],
                       internet=internet_ok())

    @app.get('/api/logs')
    @auth
    def logs():
        since = int(request.args.get('since', -1))
        with LOG_LOCK:
            rows = [(i, t) for i, t in LOG if i > since]
        return jsonify(rows=rows)

    @app.post('/api/worker/<act>')
    @auth
    def worker_ctl(act):
        if act == 'stop':
            W['want'] = False
            stop_worker()
        elif act == 'start':
            W['want'] = True
        elif act == 'restart':
            W['want'] = True
            W['manual'] = True
            stop_worker()
        return jsonify(ok=1)

    @app.post('/api/manager/restart')
    @auth
    def mgr_restart():
        threading.Thread(target=restart_manager, daemon=True).start()
        return jsonify(ok=1)

    @app.get('/api/file')
    @auth
    def get_file():
        n = request.args.get('name', 'worker.py')
        if n not in FILES:
            return jsonify(error='bad name'), 400
        p = FILES[n]
        return jsonify(text=p.read_text(encoding='utf-8') if p.exists() else '')

    @app.post('/api/file')
    @auth
    def save_file():
        d = request.get_json(force=True)
        n = d.get('name')
        if n not in FILES:
            return jsonify(error='bad name'), 400
        ok, msg = apply_file(n, d.get('text', ''))
        if ok and d.get('restart') and n in ('worker.py', '.env'):
            W['want'] = True
            W['manual'] = True
            stop_worker()
        return jsonify(ok=ok, msg=msg)

    @app.post('/api/fetch')
    @auth
    def fetch_url():
        d = request.get_json(force=True)
        url, n = d.get('url', ''), d.get('name', 'worker.py')
        if not url.startswith('https://') or n not in FILES:
            return jsonify(ok=False, msg='https URL लागतो'), 400
        try:
            text = urllib.request.urlopen(url, timeout=30).read().decode('utf-8')
        except Exception as e:
            return jsonify(ok=False, msg=f'download failed: {e}')
        ok, msg = apply_file(n, text)
        if ok and d.get('restart') and n in ('worker.py', '.env'):
            W['want'] = True
            W['manual'] = True
            stop_worker()
        return jsonify(ok=ok, msg=msg, text=text if ok else '')

    @app.post('/api/exec')
    @auth
    def exec_cmd():
        d = request.get_json(force=True)
        cmd = d.get('cmd', '').strip()
        sh = d.get('shell', 'cmd' if IS_WIN else 'sh')
        m = re.match(r'^cd\s+(.+)$', cmd, re.I)
        if m:
            new = os.path.normpath(os.path.join(cwd[0], m.group(1).strip('"')))
            if os.path.isdir(new):
                cwd[0] = new
                return jsonify(out='', cwd=cwd[0])
            return jsonify(out='folder सापडला नाही\n', cwd=cwd[0])
        try:
            if IS_WIN and sh == 'powershell':
                args, use_shell = ['powershell', '-NoProfile', '-NonInteractive', '-Command', cmd], False
            else:
                args, use_shell = cmd, True
            r = subprocess.run(args, shell=use_shell, cwd=cwd[0], capture_output=True, timeout=300,
                               stdin=subprocess.DEVNULL, env=worker_env(), creationflags=NO_WINDOW)
            raw = (r.stdout or b'') + (r.stderr or b'')
            try:
                out = raw.decode('utf-8')
            except UnicodeDecodeError:
                out = raw.decode('mbcs' if IS_WIN else 'latin1', errors='replace')
        except subprocess.TimeoutExpired:
            out = '⏱ 300s timeout — command थांबवला\n'
        except Exception as e:
            out = f'error: {e}\n'
        return jsonify(out=out, cwd=cwd[0])

    return app

def serve_panel():
    from werkzeug.serving import make_server
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app = make_app()
    port = int(os.environ.get('PANEL_PORT', '8765'))
    for _ in range(30):
        try:
            srv = make_server('127.0.0.1', port, app, threaded=True)
            break
        except OSError:
            time.sleep(1)
    else:
        sys.exit('panel port busy')
    log(f'🖥 Panel चालू: http://127.0.0.1:{port}')
    srv.serve_forever()


# ───────────────────────── commands ─────────────────────────
def panel_port():
    return int(read_env().get('PANEL_PORT', '8765'))

def port_open(port):
    try:
        socket.create_connection(('127.0.0.1', port), timeout=1).close()
        return True
    except OSError:
        return False

def cmd_run():
    if sys.stdout is None or sys.stderr is None:   # pythonw
        LOG_DIR.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(LOG_DIR / 'manager.out', 'a', encoding='utf-8', buffering=1)
    if '--delay' in sys.argv:
        time.sleep(int(sys.argv[sys.argv.index('--delay') + 1]))
    load_env()
    if not os.environ.get('PANEL_PASSWORD'):
        os.environ['PANEL_PASSWORD'] = secrets.token_urlsafe(12)
        env = read_env()
        env['PANEL_PASSWORD'] = os.environ['PANEL_PASSWORD']
        write_env(env)
    if port_open(int(os.environ.get('PANEL_PORT', '8765'))):
        print('आधीच एक manager चालू आहे — बाहेर पडतोय.')
        return
    log('════ Manager सुरू ════')
    threading.Thread(target=keep_awake, daemon=True).start()
    threading.Thread(target=worker_loop, daemon=True).start()
    if os.environ.get('TUNNEL', '1') != '0':
        threading.Thread(target=tunnel_loop, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()
    serve_panel()

def is_admin():
    if not IS_WIN:
        return os.geteuid() == 0
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())

def relaunch_admin(args):
    import ctypes
    params = ' '.join(f'"{a}"' for a in [str(SELF)] + args)
    ctypes.windll.shell32.ShellExecuteW(None, 'runas', str(VENV_PY), params, str(BASE), 1)

def install_windows():
    from xml.sax.saxutils import escape
    xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
    <TimeTrigger>
      <Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>2024-01-01T00:00:00</StartBoundary><Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="A"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="A"><Exec>
    <Command>{escape(str(VENV_PYW))}</Command>
    <Arguments>"{escape(str(SELF))}" run</Arguments>
    <WorkingDirectory>{escape(str(BASE))}</WorkingDirectory>
  </Exec></Actions>
</Task>'''
    xf = BASE / '_task.xml'
    xf.write_text(xml, encoding='utf-16')
    rc = subprocess.call(['schtasks', '/Create', '/TN', TASK, '/XML', str(xf), '/F'])
    xf.unlink(missing_ok=True)
    if rc:
        print('❌ Task बनवता आला नाही')
        return False
    for a in ('standby-timeout-ac', 'hibernate-timeout-ac'):   # AC वर PC कधीच sleep होणार नाही
        subprocess.call(['powercfg', '/change', a, '0'])
    subprocess.call(['schtasks', '/End', '/TN', TASK], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    subprocess.call(['schtasks', '/Run', '/TN', TASK])
    return True

def install_linux():
    unit = f'''[Unit]
Description=Watermark Worker Manager
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={VENV_PY} {SELF} run
WorkingDirectory={BASE}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
'''
    Path('/etc/systemd/system/watermark.service').write_text(unit)
    subprocess.call(['systemctl', 'daemon-reload'])
    subprocess.call(['systemctl', 'enable', '--now', 'watermark.service'])
    return True

def cmd_install(pause=False):
    ok = install_windows() if IS_WIN else install_linux()
    if ok:
        print('\n✅ Auto-start install झालं. PC restart झाला तरी login शिवाय चालू होईल.')
        print('⏳ Panel URL मिळेपर्यंत थांबतोय (~20s)...')
        port = panel_port()
        for _ in range(40):
            time.sleep(1)
            if port_open(port) and STATE_FILE.exists():
                try:
                    url = json.loads(STATE_FILE.read_text()).get('url')
                except Exception:
                    url = ''
                if url:
                    print(f'\n🌍 Panel URL : {url}')
                    break
        print(f'🖥 Local     : http://127.0.0.1:{port}')
        print(f'🔑 Password  : {read_env().get("PANEL_PASSWORD")}')
        print('ℹ️  Terminal URL आणि सगळं control तुमच्या Telegram bot वर दिसेल.')
    if pause:
        input('\nEnter दाबा...')

def cmd_default():
    if not WORKER.exists():
        sys.exit(f'❌ {WORKER.name} सापडला नाही.\n   तुमचा जुना code याच folder मध्ये "worker.py" नावाने save करा आणि परत चालवा.')
    load_env()
    if not ENV_FILE.exists() or not get_secret():
        wizard()
        load_env()
    try:
        ensure_ffmpeg()
    except Exception as e:
        print(f'⚠️ ffmpeg download failed: {e} (नंतर आपोआप परत प्रयत्न होईल)')
    if IS_WIN and not is_admin():
        print('🔐 Administrator permission मागतोय (auto-start साठी लागते)...')
        relaunch_admin(['install', '--pause'])
        return
    cmd_install()

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'run':
        return cmd_run()
    if cmd == 'install':
        if not is_admin():
            sys.exit('Administrator म्हणून चालवा')
        return cmd_install(pause='--pause' in sys.argv)
    if cmd == 'status':
        up = port_open(panel_port())
        print('✅ Manager चालू आहे' if up else '❌ Manager बंद आहे')
        if STATE_FILE.exists():
            print('URL:', json.loads(STATE_FILE.read_text()).get('url'))
        return
    if cmd == 'uninstall':
        if IS_WIN:
            subprocess.call(['schtasks', '/End', '/TN', TASK])
            subprocess.call(['schtasks', '/Delete', '/TN', TASK, '/F'])
        else:
            subprocess.call(['systemctl', 'disable', '--now', 'watermark.service'])
        return print('✅ Auto-start काढला')
    cmd_default()


ENTER_PAGE = r'''<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>…</title><body style="font:16px system-ui;background:#0f1115;color:#e6e8ee;padding:30px">⏳
<script>
const k=new URLSearchParams(location.search).get('k');history.replaceState(null,'','/');
fetch('/enter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({k})})
 .then(r=>r.ok?location.replace('/'):document.body.textContent='✖ Link संपला किंवा आधीच वापरला').catch(()=>document.body.textContent='✖ error');
</script>'''

PAGE = r'''<!doctype html><html lang="mr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Worker Panel</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ee;--mut:#8b93a7;--ac:#4f8cff;--ok:#2ecc71;--bad:#ff5c5c;--bd:#2a2f3a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:10px 14px;background:var(--card);border-bottom:1px solid var(--bd);flex-wrap:wrap}
.dot{width:10px;height:10px;border-radius:50%;background:var(--bad)}.dot.on{background:var(--ok)}
nav{display:flex;gap:6px;padding:8px 14px}nav button,.b{background:var(--card);color:var(--fg);border:1px solid var(--bd);padding:7px 12px;border-radius:7px;cursor:pointer}
nav button.act,.b.p{background:var(--ac);border-color:var(--ac);color:#fff}.b.r{border-color:var(--bad);color:var(--bad)}
main{padding:0 14px 14px}pre,textarea,input,select{background:#0b0d11;color:var(--fg);border:1px solid var(--bd);border-radius:7px;font:12.5px ui-monospace,Consolas,monospace}
pre{padding:10px;margin:0;height:70vh;overflow:auto;white-space:pre-wrap;word-break:break-all}
textarea{width:100%;height:65vh;padding:10px;tab-size:4}input,select{padding:8px}.row{display:flex;gap:8px;margin:8px 0;flex-wrap:wrap}.row input{flex:1;min-width:160px}
.tab{display:none}.tab.act{display:block}#login{max-width:320px;margin:18vh auto;display:none}#msg{color:var(--mut);margin-left:8px}
</style></head><body>
<div id="login"><h3>🔐 Worker Panel</h3><div class="row"><input id="pw" type="password" placeholder="Password"><button class="b p" onclick="login()">Login</button></div><div id="lerr" style="color:var(--bad)"></div></div>
<div id="app" style="display:none">
<header><span class="dot" id="dot"></span><b id="st">...</b><span id="inf" style="color:var(--mut)"></span></header>
<nav id="nav"></nav><main>
<section class="tab act" id="t-logs"><pre id="log"></pre></section>
<section class="tab" id="t-term"><pre id="tout" style="height:60vh"></pre>
<div class="row"><select id="sh"><option>cmd</option><option>powershell</option></select><input id="cmd" placeholder="command... (Enter)" autocomplete="off"><button class="b p" onclick="run()">Run</button></div><div id="cwd" style="color:var(--mut)"></div></section>
<section class="tab" id="t-edit"><div class="row"><select id="fn" onchange="loadF()"><option>worker.py</option><option>.env</option><option>watermark.py</option></select>
<button class="b p" onclick="saveF(true)">💾 Save + Restart worker</button><button class="b" onclick="saveF(false)">Save only</button><span id="msg"></span></div>
<div class="row"><input id="url" placeholder="https://raw.githubusercontent.com/.../worker.py"><button class="b" onclick="fetchU()">⬇️ URL वरून update</button></div>
<textarea id="ed" spellcheck="false"></textarea></section>
<section class="tab" id="t-ctl"><div class="row"><button class="b p" onclick="ctl('start')">▶ Start</button><button class="b" onclick="ctl('restart')">🔄 Restart worker</button><button class="b r" onclick="ctl('stop')">⏹ Stop</button>
<button class="b r" onclick="if(confirm('Manager restart करायचा?'))post('/api/manager/restart',{})">♻️ Restart manager</button></div></section>
</main></div>
<script>
const $=s=>document.querySelector(s);let since=-1;
const TABS=[['logs','📜 Logs'],['term','⌨️ Terminal'],['edit','📝 Code'],['ctl','⚙️ Control']];
$('#nav').innerHTML=TABS.map(t=>`<button data-t="${t[0]}" onclick="tab('${t[0]}')">${t[1]}</button>`).join('');
function tab(n){document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('act',e.id=='t-'+n));
 document.querySelectorAll('nav button').forEach(e=>e.classList.toggle('act',e.dataset.t==n));if(n=='edit'&&!$('#ed').value)loadF()}
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 if(r.status==401){showLogin();throw 0}return r.json()}
async function get(p){const r=await fetch(p);if(r.status==401){showLogin();throw 0}return r.json()}
function showLogin(){$('#login').style.display='block';$('#app').style.display='none'}
async function login(){const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('#pw').value})});
 const d=await r.json();if(d.ok){$('#login').style.display='none';$('#app').style.display='block';start()}else $('#lerr').textContent=d.error}
$('#pw').addEventListener('keydown',e=>e.key=='Enter'&&login());
async function poll(){try{const s=await get('/api/status');$('#dot').className='dot'+(s.running?' on':'');
 $('#st').textContent=s.running?'Worker चालू':(s.want?'Worker सुरू होतोय...':'Worker थांबवला');
 $('#inf').textContent=` pid ${s.pid||'-'} · uptime ${Math.floor(s.uptime/60)}m · restarts ${s.restarts} · internet ${s.internet?'✅':'❌'}`;
 const l=await get('/api/logs?since='+since);if(l.rows.length){const el=$('#log'),b=el.scrollTop+el.clientHeight>=el.scrollHeight-40;
 el.textContent+=l.rows.map(r=>r[1]).join('\n')+'\n';since=l.rows[l.rows.length-1][0];if(el.textContent.length>300000)el.textContent=el.textContent.slice(-200000);if(b)el.scrollTop=el.scrollHeight}}catch(e){}}
function start(){tab('logs');poll();setInterval(poll,2500)}
async function run(){const c=$('#cmd').value;if(!c)return;$('#cmd').value='';$('#tout').textContent+=`\n$ ${c}\n`;
 const d=await post('/api/exec',{cmd:c,shell:$('#sh').value});$('#tout').textContent+=d.out;$('#cwd').textContent=d.cwd;$('#tout').scrollTop=1e9}
$('#cmd').addEventListener('keydown',e=>e.key=='Enter'&&run());
async function loadF(){const d=await get('/api/file?name='+$('#fn').value);$('#ed').value=d.text;$('#msg').textContent=''}
async function saveF(rs){const d=await post('/api/file',{name:$('#fn').value,text:$('#ed').value,restart:rs});$('#msg').textContent=d.ok?'✅ saved'+(rs?' + restart':''):'❌ '+d.msg}
async function fetchU(){const d=await post('/api/fetch',{url:$('#url').value,name:$('#fn').value,restart:true});$('#msg').textContent=d.ok?'✅ updated + restart':'❌ '+d.msg;if(d.ok)$('#ed').value=d.text}
async function ctl(a){await post('/api/worker/'+a,{})}
get('/api/status').then(()=>{$('#app').style.display='block';start()}).catch(()=>showLogin());
</script></body></html>'''


if __name__ == '__main__':
    bootstrap()
    main()
