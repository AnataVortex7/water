import subprocess, os, sys, uuid, time, threading, asyncio, requests, re

KOYEB_URL = os.environ.get("KOYEB_URL") or "https://sick-nikaniki-shubham8208989-6d7c863a.koyeb.app"
COLAB_SECRET = os.environ.get("COLAB_SECRET")
UPLOAD_BOT_TOKEN = os.environ.get("UPLOAD_BOT_TOKEN")
POLL_INTERVAL  = int(os.environ.get('POLL_INTERVAL', '5'))
FFMPEG_TIMEOUT = int(os.environ.get('FFMPEG_TIMEOUT', '14400'))

# Only ffmpeg (CPU-heavy) is limited — download/upload stay fully parallel
FFMPEG_CONCURRENCY = int(os.environ.get('FFMPEG_CONCURRENCY', '1'))
_FFMPEG_SEMAPHORE  = threading.Semaphore(FFMPEG_CONCURRENCY)

# Tracks how many downloads are actively transferring right now — the ffmpeg
# CPU governor (below) reads this to decide whether to yield a core.
_ACTIVE_DOWNLOADS = 0
_ACTIVE_DOWNLOADS_LOCK = threading.Lock()

def _download_started():
    global _ACTIVE_DOWNLOADS
    with _ACTIVE_DOWNLOADS_LOCK:
        _ACTIVE_DOWNLOADS += 1

def _download_finished():
    global _ACTIVE_DOWNLOADS
    with _ACTIVE_DOWNLOADS_LOCK:
        _ACTIVE_DOWNLOADS = max(0, _ACTIVE_DOWNLOADS - 1)

HEADERS   = {'X-Secret-Key': COLAB_SECRET}
WORKER_ID = str(uuid.uuid4())[:12]

_CLIENTS = {}

# ⚠️ Must be asyncio.Lock(), never threading.Lock() — threading.Lock blocks
# the whole event-loop thread and deadlocks two concurrent jobs here.
_CLIENT_LOCK = None

_WARMED_PEERS = set()

_TG_LOOP = None
_TG_LOOP_THREAD = None
_TG_LOOP_LOCK = threading.Lock()

# Pyrogram client is bound to the loop that started it — reuse one loop
# across all jobs (via run_coroutine_threadsafe) instead of asyncio.run().
def get_tg_loop():
    """Telegram operations साठी dedicated persistent event loop+thread."""
    global _TG_LOOP, _TG_LOOP_THREAD
    with _TG_LOOP_LOCK:
        if _TG_LOOP is None or not _TG_LOOP_THREAD.is_alive():
            _TG_LOOP = asyncio.new_event_loop()

            def _run_loop():
                asyncio.set_event_loop(_TG_LOOP)
                _TG_LOOP.run_forever()

            _TG_LOOP_THREAD = threading.Thread(target=_run_loop, daemon=True)
            _TG_LOOP_THREAD.start()
            print('✅ Telegram dedicated event loop सुरू झाला')
        return _TG_LOOP

def run_on_tg_loop(coro, timeout=None):
    """Coroutine ला persistent Telegram loop वर पाठवतो आणि result परत आणतो."""
    loop = get_tg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)

def stop_all_clients():
    """Shutdown वेळी cache मधले सगळे persistent clients (download + upload
    bot, दोन्ही) best-effort बंद करतो."""
    if _TG_LOOP is None:
        return
    for token, app in list(_CLIENTS.items()):
        try:
            run_on_tg_loop(app.stop(), timeout=10)
        except Exception:
            pass

print(f'🆔 Worker ID : {WORKER_ID}')
print(f'🌐 Koyeb URL : {KOYEB_URL}')
print(f'⏱ Poll every: {POLL_INTERVAL}s')
print(f'🎞 FFmpeg slots (parallel encodes): {FFMPEG_CONCURRENCY}')

try:
    from pyrogram import Client
    from pyrogram.errors import FloodWait

    # [FIX] Monkey-patch Pyrogram 2.0.106 to support forum topics
    # In older Pyrogram, copy_message sets reply_to_msg_id but leaves top_msg_id empty.
    # This causes the bot to post in the "General" topic instead of the specific topic.
    from pyrogram.raw.functions.messages import SendMedia, SendMessage

    import tgcrypto
except ImportError:
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'telethon'], check=True)
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import DocumentAttributeVideo

FFMPEG_BIN = 'ffmpeg'

def _test_encode(extra_input_args, encode_args):
    try:
        cmd = (
            [FFMPEG_BIN]
            + extra_input_args
            + ['-f', 'lavfi', '-i', 'nullsrc=s=320x240:d=1']
            + encode_args
            + ['-f', 'null', '-', '-y']
        )
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False

# Tier1: CUDA hwaccel+nvenc, Tier2: nvenc only, Tier3: libx264 CPU fallback
def detect_encoder():
    """
    Tier 1: hwaccel cuda + h264_nvenc  — full GPU pipeline
    Tier 2: h264_nvenc only            — CPU decode + GPU encode
    Tier 3: libx264 ultrafast          — CPU all cores
    """
    if _test_encode(
        ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'],
        ['-c:v', 'h264_nvenc', '-preset', 'p1', '-b:v', '2M']
    ):
        print('✅ Encoder: Tier 1 — CUDA hwaccel + h264_nvenc (full GPU)')
        return 'tier1'

    if _test_encode(
        [],
        ['-c:v', 'h264_nvenc', '-preset', 'p1', '-b:v', '2M']
    ):
        print('✅ Encoder: Tier 2 — h264_nvenc (GPU encode, CPU decode)')
        return 'tier2'

    print('✅ Encoder: Tier 3 — libx264 ultrafast (CPU all cores)')
    return 'tier3'

ENCODER_TIER = detect_encoder()
print(f'✅ Encoder mode ready: {ENCODER_TIER}')

def make_bar(pct, width=20):
    filled = int(width * pct / 100)
    return f"[{'█'*filled}{'░'*(width-filled)}] {pct:.1f}%"

def get_video_meta(path):
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,duration',
             '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=30
        )
        parts = r.stdout.strip().split(',')
        w   = int(parts[0])   if len(parts) > 0 and parts[0].strip().isdigit() else 1280
        h   = int(parts[1])   if len(parts) > 1 and parts[1].strip().isdigit() else 720
        dur = float(parts[2]) if len(parts) > 2 and parts[2].strip() not in ('', 'N/A') else 0.0
        if dur <= 0:
            r2 = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', path],
                capture_output=True, text=True, timeout=30
            )
            try:
                dur = float(r2.stdout.strip())
            except Exception:
                dur = 0.0
        return int(dur), w, h
    except Exception as e:
        print(f'⚠️ get_video_meta error: {e}')
        return 0, 1280, 720

def extract_thumb_from_video(video_path, duration):
    try:
        seek = max(1, int(duration * 0.2)) if duration > 0 else 5
        thumb_path = video_path + '_thumb.jpg'
        r = subprocess.run(
            ['ffmpeg', '-ss', str(seek), '-i', video_path,
             '-vframes', '1', '-q:v', '2', '-y', thumb_path],
            capture_output=True, timeout=30
        )
        if r.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            print(f'✅ Thumbnail extracted at {seek}s')
            return thumb_path
        print(f'⚠️ Thumbnail extract failed (returncode={r.returncode})')
    except Exception as e:
        print(f'⚠️ extract_thumb error: {e}')
    return None

def get_thumb(job, video_path, duration):
    thumb_url = job.get('thumbnail_url') or job.get('thumb_url')
    if thumb_url:
        try:
            custom_path = video_path + '_custom_thumb.jpg'
            r = subprocess.run(['wget', '-q', '-O', custom_path, thumb_url], timeout=30)
            if r.returncode == 0 and os.path.exists(custom_path) and os.path.getsize(custom_path) > 0:
                print('✅ Custom thumbnail downloaded')
                return custom_path
            print('⚠️ Custom thumb fail → video extract fallback')
        except Exception as e:
            print(f'⚠️ Thumb URL error: {e}')
    return extract_thumb_from_video(video_path, duration)

_progress_seq = {}

def koyeb_progress(job_id, msg, seq=None):
    if seq is None:
        _progress_seq[job_id] = _progress_seq.get(job_id, 0) + 1
        seq = _progress_seq[job_id]
    try:
        r = requests.post(f'{KOYEB_URL}/api/colab/progress',
                      json={'job_id': job_id, 'message': msg,
                            'worker_id': WORKER_ID, 'seq': seq},
                      headers=HEADERS, timeout=10)
        if r.status_code != 200:
            print(f'⚠️ koyeb_progress [job {job_id}] non-200: {r.status_code} — {r.text[:200]}')
    except Exception as e:
        print(f'⚠️ koyeb_progress [job {job_id}] request failed: {e}')

def koyeb_progress_async(job_id, msg):
    """koyeb_progress() हा blocking (synchronous requests.post) call आहे.
    Download/upload चे progress callbacks आपल्या dedicated Telegram event-loop
    thread वरच चालतात — तिथून थेट blocking HTTP call केला की तो पूर्ण होईपर्यंत
    त्याच thread वरचं actual file-transfer I/O सुद्धा थांबतं (asyncio single-thread
    असल्यामुळे). त्यामुळे प्रत्येक progress update वर काही सेकंद transfer थांबायचा,
    speed मोठ्या प्रमाणात कमी व्हायची. Fire-and-forget साठी वेगळ्या daemon thread
    वर पाठवतो — event loop कधीच block होत नाही.
    seq इथेच (calling thread मध्ये, synchronously) assign करतो — नाहीतर वेगवेगळे
    background threads वेगळ्या वेळी पूर्ण झाले की seq चा order चुकू शकतो, आणि Go
    server (out-of-order seq skip करतो) काही updates गाळेल."""
    _progress_seq[job_id] = _progress_seq.get(job_id, 0) + 1
    seq = _progress_seq[job_id]
    threading.Thread(target=koyeb_progress, args=(job_id, msg, seq), daemon=True).start()

def koyeb_done(job_id, log_channel_msg_id=None):
    try:
        payload = {'job_id': job_id}
        if log_channel_msg_id:
            payload['log_channel_msg_id'] = log_channel_msg_id
        requests.post(f'{KOYEB_URL}/api/colab/done',
                      json=payload,
                      headers=HEADERS, timeout=10)
    except:
        pass

def koyeb_failed(job_id, error):
    try:
        requests.post(f'{KOYEB_URL}/api/colab/failed',
                      json={'job_id': job_id, 'error': str(error)},
                      headers=HEADERS, timeout=10)
    except:
        pass

def extract_flood_wait_seconds(err):
    """
    Pyrogram FloodWait exception (.value attribute) किंवा raw error string
    ("A wait of 242 seconds is required") दोन्हीतून seconds काढतो.
    FloodWait नसेल तर None return करतो.
    """
    try:
        from pyrogram.errors import FloodWait
        if isinstance(err, FloodWait):
            return int(err.value)
    except Exception:
        pass

    err_str = str(err)
    if 'FLOOD_WAIT' not in err_str.upper() and 'flood' not in err_str.lower():
        return None

    m = re.search(r'wait of (\d+) seconds', err_str, re.IGNORECASE)
    if m:
        return int(m.group(1))

    m = re.search(r'FLOOD_WAIT_(\d+)', err_str.upper())
    if m:
        return int(m.group(1))

    return None

def wait_with_progress(job_id, fname, total_seconds, reason='FloodWait'):
    """
    FloodWait संपेपर्यंत थांबतो आणि दर 30 sec ला Koyeb la remaining time
    progress म्हणून पाठवतो. job कधीच failed मानला जात नाही — फक्त paused.
    """
    print(f'⏳ {reason}: {total_seconds}s थांबतोय (job: {job_id})')
    remaining = total_seconds
    while remaining > 0:
        mins, secs = divmod(remaining, 60)
        koyeb_progress(
            job_id,
            f"⏳ *{reason} — Telegram limit*\n📄 `{fname}`\n"
            f"⏱ Remaining: `{mins}m {secs}s`\n"
            f"🔁 आपोआप resume होईल, काही करायची गरज नाही."
        )
        sleep_chunk = min(30, remaining)
        time.sleep(sleep_chunk)
        remaining -= sleep_chunk
    print(f'✅ {reason} संपला — resume करतोय (job: {job_id})')

def poll_job():
    try:
        r = requests.get(f'{KOYEB_URL}/api/colab/poll',
                         params={'worker_id': WORKER_ID},
                         headers=HEADERS, timeout=15)
        data = r.json()
        return data.get('job'), data.get('api_id'), data.get('api_hash'), data.get('stop', False)
    except Exception as e:
        print(f'⚠️ poll error: {e}')
        return None, None, None, False

def build_ffmpeg_cmd(input_path, output_path, drawtext, tier):
    if tier == 'tier1':
        return (
            [FFMPEG_BIN,
             '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
             '-extra_hw_frames', '4',
             '-i', input_path,
             '-vf', f'hwdownload,format=nv12,{drawtext},hwupload_cuda',
             '-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'll',
             '-b:v', '1M', '-maxrate', '2M', '-bufsize', '4M',
             '-gpu', '0', '-c:a', 'copy', '-movflags', '+faststart',
             '-progress', 'pipe:2', '-stats_period', '3', '-y', output_path],
            'GPU full (hwaccel+nvenc)'
        )
    elif tier == 'tier2':
        return (
            [FFMPEG_BIN,
             '-i', input_path,
             '-vf', drawtext,
             '-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'll',
             '-b:v', '1M', '-maxrate', '2M', '-bufsize', '4M',
             '-gpu', '0', '-c:a', 'copy', '-movflags', '+faststart',
             '-progress', 'pipe:2', '-stats_period', '3', '-y', output_path],
            'GPU encode (nvenc)'
        )
    else:
        return (
            [FFMPEG_BIN,
             '-i', input_path,
             '-vf', drawtext,
             '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
             '-threads', '0', '-tune', 'fastdecode',
             '-c:a', 'copy', '-movflags', '+faststart',
             '-progress', 'pipe:2', '-stats_period', '3', '-y', output_path],
            'CPU libx264 ultrafast'
        )

# Env-gated experiment: pins this download's OS thread to a dedicated core
# (mirrors what _ffmpeg_cpu_governor does for ffmpeg, but in reverse — here
# it's the download getting a reserved core + higher scheduling priority)
# and boosts its nice priority, so tumhi test karu shakta ki pipe route cha
# bottleneck khare CPU/scheduling cha aahe ki server/network cha.
# Toggle: DOWNLOAD_CPU_BOOST=0 environment variable to disable. Default ON.
DOWNLOAD_CPU_BOOST = os.environ.get('DOWNLOAD_CPU_BOOST', '1') == '1'

def _boost_download_thread():
    if not DOWNLOAD_CPU_BOOST:
        return
    try:
        tid = threading.get_native_id()
    except Exception:
        return
    try:
        total = os.cpu_count() or 2
        if total >= 2 and hasattr(os, 'sched_setaffinity'):
            # last core reserved exclusively for this download thread
            os.sched_setaffinity(tid, {total - 1})
            print(f'🧵 [download] thread {tid} pinned to core {total-1}')
    except Exception as e:
        print(f'⚠️ [download] affinity pin failed: {e}')
    try:
        if hasattr(os, 'setpriority'):
            os.setpriority(os.PRIO_PROCESS, tid, -10)  # higher scheduling priority
            print(f'🧵 [download] thread {tid} priority boosted (-10)')
    except Exception as e:
        print(f'⚠️ [download] priority boost failed (needs root usually): {e}')

def download_file(file_url, dest_path, job_id, job):
    fname = job['file_name']
    total = job.get('file_size', 0)
    koyeb_progress(job_id,
        f"📥 *Downloading...STEP.1/3*\n📄 `{fname}`\n📌 Msg: `{job['msg_id']}`\n{make_bar(0)}")
    _download_started()
    _boost_download_thread()
    try:
        r = requests.get(file_url, headers=HEADERS, stream=True, timeout=60)
        r.raise_for_status()
        content_len = int(r.headers.get('Content-Length', total or 0))
        downloaded, start, last_update = 0, time.time(), time.time()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=512 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if time.time() - last_update >= 3 and content_len > 0:
                        pct   = min(downloaded / content_len * 100, 99)
                        speed = (downloaded / 1024 / 1024) / (time.time() - start)
                        koyeb_progress(job_id,
                            f"📥 *Downloading...STEP.1/3*\n📄 `{fname}`\n{make_bar(pct)}\n"
                            f"📌 Msg: `{job['msg_id']}`\n"
                            f"`{downloaded/1024/1024:.1f} / {content_len/1024/1024:.1f} MB`\n"
                            f"🚀 `{speed:.2f} MB/s`")
                        last_update = time.time()
        elapsed = time.time() - start
        print(f'✅ Downloaded: {downloaded/1024/1024:.1f} MB in {elapsed:.0f}s')
        return True
    except Exception as e:
        return False, str(e)
    finally:
        _download_finished()

async def _download_direct_async(api_id, api_hash, download_bot_token, channel_id, channel_access_hash, msg_id, dest_path, job_id, fname):
    print(f'🔐 [job {job_id}] Telegram login सुरू (bot ...{download_bot_token[-6:]})...')
    try:
        app = await asyncio.wait_for(
            get_persistent_client(api_id, api_hash, download_bot_token),
            timeout=60
        )
    except asyncio.TimeoutError:
        raise Exception("bot login timeout (60s) — कदाचित FloodWait मध्ये अडकलंय")
    print(f'🔐 [job {job_id}] login झालं ✅')

    channel_id_int = int(channel_id)
    if channel_id_int < 0:
        chat_id = channel_id_int
    else:
        chat_id = int(f"-100{channel_id_int}")

    if channel_access_hash:
        try:
            await asyncio.wait_for(
                app.storage.update_peers([(chat_id, int(channel_access_hash), "channel", None, None)]),
                timeout=15
            )
            print(f'📌 [job {job_id}] peer cache seed झालं (chat_id={chat_id})')
        except Exception as seed_err:
            print(f'⚠️ [job {job_id}] Peer cache seed failed (पुढेही प्रयत्न करतोय): {seed_err}')

    print(f'🔎 [job {job_id}] source message resolve करतोय (chat_id={chat_id}, msg_id={msg_id})...')
    try:
        message = await asyncio.wait_for(app.get_messages(chat_id, msg_id), timeout=60)
    except asyncio.TimeoutError:
        raise Exception("get_messages timeout (60s) — peer resolve अडकलं (FloodWait असू शकतो)")
    if message is None or getattr(message, "empty", False):
        raise Exception("source message सापडला नाही (deleted किंवा bot ला channel access नाही)")
    print(f'🔎 [job {job_id}] message सापडला ✅ — download सुरू करतोय...')

    start = time.time()
    last_update = {'t': 0}

    def progress(current, total):
        now = time.time()
        if now - last_update['t'] >= 3 and total:
            pct = min(current / total * 100, 99)
            speed = (current / 1024 / 1024) / max(now - start, 0.001)
            print(f'📥 [job {job_id}] {current/1024/1024:.1f}/{total/1024/1024:.1f} MB | {speed:.2f} MB/s | {pct:.0f}%')
            koyeb_progress_async(job_id,
                f"📥 *Downloading...STEP.1/3*\n📄 `{fname}`\n{make_bar(pct)}\n"
                f"📌 Msg: `{msg_id}`\n"
                f"`{current/1024/1024:.1f} / {total/1024/1024:.1f} MB`\n"
                f"🚀 `{speed:.2f} MB/s`")
            last_update['t'] = now

    await app.download_media(message, file_name=dest_path, progress=progress)

def download_file_direct(api_id, api_hash, download_bot_token, channel_id, channel_access_hash, msg_id, dest_path, job_id, job):
    """Main server ला काहीही न विचारता — worker स्वतःच्या Pyrogram session ने
    (download_bot_token, bot pool मधून round-robin निवडलेला) थेट source
    channel मधून file download करतो. Course offline-token flow साठीच वापरला
    जातो — /userbotdl (admin) flow अजूनही जुना file_url/pipe मार्ग वापरतो."""
    fname = job['file_name']
    koyeb_progress(job_id,
        f"📥 *Downloading...STEP 1/3*\n📄 `{fname}`\n📌 Msg: `{msg_id}`\n{make_bar(0)}")
    try:
        _download_started()
        try:
            run_on_tg_loop(
                _download_direct_async(api_id, api_hash, download_bot_token, channel_id, channel_access_hash, msg_id, dest_path, job_id, fname),
                timeout=FFMPEG_TIMEOUT
            )
        finally:
            _download_finished()
        if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
            return False, "downloaded file empty/missing"
        print(f'✅ Direct download (own Telegram session) complete: {fname}')
        return True
    except Exception as e:
        print(f'❌ [job {job_id}] direct download failed: {e}')
        return False, str(e)

def get_duration(input_path):
    for cmd in [
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', input_path],
        [FFMPEG_BIN, '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', input_path],
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            val = float(r.stdout.strip())
            if val > 0:
                return val
        except:
            pass
    return 0

def _lower_priority():
    try:
        os.nice(10)
    except Exception:
        pass

def _ffmpeg_cpu_governor(pid, stop_flag):
    """ffmpeg चालू असताना दर सेकंदाला चेक करतो — कुठला download active आहे का.
    Active असेल तर ffmpeg ला 1 core वर बांधतो (download ला दुसरा core मोकळा
    मिळतो), download नसेल तर ffmpeg ला परत दोन्ही cores देतो. Linux-only
    (sched_setaffinity); support नसेल तर आपोआप बंद होतो."""
    if not hasattr(os, 'sched_setaffinity'):
        return
    total = os.cpu_count() or 2
    if total < 2:
        return
    all_cores  = set(range(total))
    busy_cores = set(range(total - 1))  # एक core (शेवटचा) downloads साठी राखीव
    current = None
    while not stop_flag['done']:
        target = busy_cores if _ACTIVE_DOWNLOADS > 0 else all_cores
        if target != current:
            try:
                os.sched_setaffinity(pid, target)
                current = target
            except (ProcessLookupError, PermissionError):
                return
            except Exception:
                return
        time.sleep(1)

def run_ffmpeg_with_progress(cmd, label, total_dur, job_id, job):
    fname   = job['file_name']
    process = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, text=True,
                               preexec_fn=_lower_priority if os.name == 'posix' else None)
    start, cur_t, last_upd = time.time(), 0.0, time.time()

    gov_stop = {'done': False}
    gov_thread = threading.Thread(target=_ffmpeg_cpu_governor, args=(process.pid, gov_stop), daemon=True)
    gov_thread.start()

    try:
        for line in process.stderr:
            if line.strip().startswith('out_time_ms='):
                try:
                    val = int(line.split('=')[1])
                    if val > 0:
                        cur_t = val / 1_000_000
                except:
                    pass
            if time.time() - last_upd >= 4:
                elapsed = time.time() - start
                if total_dur > 0 and cur_t > 0:
                    pct = min(cur_t / total_dur * 100, 99)
                    eta = (elapsed / pct * (100 - pct)) if pct > 0 else 0
                    koyeb_progress(job_id,
                        f"💧 *Watermark applying...STEP 2/3*\n📄 `{fname}`\n📌 Msg: `{job['msg_id']}`\n"
                        f"⚙️ `{label}`\n{make_bar(pct)}\n"
                        f"⏱ `{int(elapsed//60)}m{int(elapsed%60)}s` | ETA: `{int(eta//60)}m{int(eta%60)}s`")
                else:
                    koyeb_progress(job_id,
                        f"💧 *Watermark applying...STEP 2/3*\n📄 `{fname}`\n📌 Msg: `{job['msg_id']}`\n"
                        f"⚙️ `{label}`\n⏱ `{int(elapsed//60)}m{int(elapsed%60)}s` elapsed...")
                last_upd = time.time()
    except Exception as e:
        print(f'⚠️ stderr read error: {e}')

    gov_stop['done'] = True

    try:
        process.wait(timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        return False, 'FFmpeg timeout (4hr)'

    if process.returncode != 0:
        return False, f'FFmpeg exit: {process.returncode}'

    print(f'✅ FFmpeg done: {time.time()-start:.0f}s ({label})')
    return True, None

def run_ffmpeg_remux_only(input_path, output_path, job_id, job):
    """Watermark text नसेल तर — फक्त remux (-c copy), re-encode नाही. खूप जलद."""
    koyeb_progress(job_id,
        f"📦 *Processing (no watermark)...*\n📄 `{job['file_name']}`\n"
        f"📌 Msg: `{job['msg_id']}`\n⚙️ `remux (copy)`")
    cmd = [FFMPEG_BIN, '-i', input_path, '-c', 'copy', '-movflags', '+faststart',
           '-y', output_path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            print('✅ Remux (copy) done — re-encode skip झाला')
            return True, None
        err = r.stderr.decode(errors='ignore')[-500:] if r.stderr else 'unknown'
        print(f'⚠️ Remux copy failed: {err} → fallback to plain file copy')
        import shutil
        shutil.copyfile(input_path, output_path)
        return True, None
    except Exception as e:
        print(f'⚠️ Remux exception: {e} → fallback to plain file copy')
        try:
            import shutil
            shutil.copyfile(input_path, output_path)
            return True, None
        except Exception as e2:
            return False, str(e2)

def run_ffmpeg(input_path, output_path, wm_text, job_id, job):
    if not wm_text or not wm_text.strip():
        return run_ffmpeg_remux_only(input_path, output_path, job_id, job)

    total_dur = get_duration(input_path)
    safe_text = wm_text.replace("'", "\\'").replace(':', '\\:')
    drawtext  = (f"drawtext=text='{safe_text}':fontcolor=white@0.45:fontsize=22:"
                 f"line_spacing=4:shadowcolor=black@0.35:shadowx=1:shadowy=1:"
                 f"x=w-tw-20:y=(h-th)/2")

    current_tier = ENCODER_TIER
    while True:
        cmd, label = build_ffmpeg_cmd(input_path, output_path, drawtext, current_tier)
        koyeb_progress(job_id,
            f"💧 *Watermark applying...STEP 2/3*\n📄 `{job['file_name']}`\n"
            f"📌 Msg: `{job['msg_id']}`\n⚙️ `{label}`\n{make_bar(0)}")

        ok, err = run_ffmpeg_with_progress(cmd, label, total_dur, job_id, job)
        if ok:
            return True, None

        print(f'⚠️ {label} failed: {err}')
        if current_tier == 'tier1':
            koyeb_progress(job_id, f"⚠️ GPU full failed → GPU encode only...\n📄 `{job['file_name']}`")
            if os.path.exists(output_path): os.remove(output_path)
            current_tier = 'tier2'
        elif current_tier == 'tier2':
            koyeb_progress(job_id, f"⚠️ GPU encode failed → CPU fallback...\n📄 `{job['file_name']}`")
            if os.path.exists(output_path): os.remove(output_path)
            current_tier = 'tier3'
        else:
            return False, f'सगळे encoders fail: {err}'

async def get_persistent_client(api_id, api_hash, bot_token):
    """
    Prataयek distinct bot_token साठी स्वतःचा persistent Pyrogram Client ठेवतो
    (dict cache) — navin client फक्त त्या token साठी पहिल्यांदाच बनतो, पुढच्या
    वेळी तोच reuse होतो. यामुळे download-bot आणि upload-bot वेगळे token
    असले तरी प्रत्येक job वर re-login (auth.ImportBotAuthorization → FloodWait
    risk) होत नाही.
    """
    global _CLIENTS, _CLIENT_LOCK

    if _CLIENT_LOCK is None:
        _CLIENT_LOCK = asyncio.Lock()

    async with _CLIENT_LOCK:
        app = _CLIENTS.get(bot_token)
        if app is not None:
            return app

        from pyrogram import Client
        session_name = f"worker_{abs(hash(bot_token)) % 10_000_000}"
        app = Client(
            session_name,
            api_id=int(api_id),
            api_hash=api_hash,
            bot_token=bot_token,
            in_memory=True,
            max_concurrent_transmissions=8,
        )
        await app.start()
        _CLIENTS[bot_token] = app
        print(f'✅ Persistent Telegram session सुरू झाला (bot token ...{bot_token[-6:]}, max_concurrent_transmissions=8)')
        return app

def telegram_send(output_path, job_id, job, api_id, api_hash):

    from pyrogram import Client
    from pyrogram.raw.types import InputChannel
    from pyrogram.raw.functions.messages import SendMedia
    import tgcrypto

    fname = job['file_name']
    bot_token = UPLOAD_BOT_TOKEN or job['bot_token']
    send_chat_id = job['send_to_chat_id']
    access_hash = job.get('send_to_access_hash', 0)
    topic_id = job.get('send_to_topic_id') or None
    caption = job.get('caption') or fname
    log_chat_id = job.get('log_chat_id') or None
    log_access_hash = job.get('log_access_hash', 0)
    userbot_log_chat_id = job.get('userbot_log_chat_id') or None
    userbot_log_access_hash = job.get('userbot_log_access_hash', 0)
    userbot_log_topic_id = job.get('userbot_log_topic_id') or None
    protect_content = bool(job.get('protect_content', False))
    log_only = bool(job.get('log_only', False))

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f'📤 Upload start: {size_mb:.1f}MB → chat {send_chat_id}')

    dur, w, h = get_video_meta(output_path)
    thumb_path = get_thumb(job, output_path, dur)

    start = time.time()
    last_update = [0]

    def progress(current, total):
        if time.time() - last_update[0] >= 3:
            pct = current / total * 100 if total else 0
            speed = (current / 1024 / 1024) / (time.time() - start)

            koyeb_progress_async(
                job_id,
                f"📤 Uploading...STEP.3/3\n{make_bar(pct)}\n🚀 `{speed:.2f} MB/s`"
            )
            last_update[0] = time.time()

    async def warm_peer(app, chat_id, ahash):
        if not ahash:
            return
        try:
            from pyrogram.raw.types import InputChannel as RawInputChannel
            from pyrogram.raw.functions.channels import GetChannels
            channel_id = abs(chat_id) - 1000000000000
            raw_peer = RawInputChannel(channel_id=channel_id, access_hash=ahash)
            await app.invoke(GetChannels(id=[raw_peer]))
            print(f'✅ Peer resolved via access_hash: {channel_id}')
        except Exception as pe:
            print(f'⚠️ Peer pre-resolve warning ({chat_id}): {pe}')

    async def ensure_warmed(app, chat_id, ahash):
        """
        Pratyek distinct chat_id sathi worker lifetime madhe EKDACH warm_peer
        call karto (set madhe cache karun). Yamule:
          - Pratyek upload var extra API round-trip nahi (jar aधीच warm zala)
          - Pan navin/cold chat sathi peer ALWAYS warm hoईl, exception cha
            fragile string-match (PEER_ID_INVALID text format pratyek
            pyrogram version/path madhe sarkha nasto) var depend na karता.
        """
        cache_key = (chat_id, bot_token)
        if not ahash or cache_key in _WARMED_PEERS:
            return
        await warm_peer(app, chat_id, ahash)
        _WARMED_PEERS.add(cache_key)

    async def upload():
        app = await get_persistent_client(api_id, api_hash, bot_token)

        try:
            if log_only:
                if not log_chat_id:
                    return False, 'log_only=true पण log_chat_id दिलेला नाही', None
                await ensure_warmed(app, log_chat_id, log_access_hash)
                try:
                    master_msg = await app.send_video(
                        chat_id=log_chat_id,
                        video=output_path,
                        caption=caption,
                        duration=dur,
                        width=w,
                        height=h,
                        thumb=thumb_path if thumb_path else None,
                        supports_streaming=True,
                        protect_content=False,
                        progress=progress
                    )
                except Exception as send_err:
                    if log_access_hash:
                        print(f'⚠️ log_only send failed ({send_err}) → force re-resolve करून retry करतोय')
                        _WARMED_PEERS.discard((log_chat_id, bot_token))
                        await warm_peer(app, log_chat_id, log_access_hash)
                        _WARMED_PEERS.add((log_chat_id, bot_token))
                        master_msg = await app.send_video(
                            chat_id=log_chat_id,
                            video=output_path,
                            caption=caption,
                            duration=dur,
                            width=w,
                            height=h,
                            thumb=thumb_path if thumb_path else None,
                            supports_streaming=True,
                            protect_content=False,
                            progress=progress
                        )
                    else:
                        raise
                print(f'✅ Silent pre-download → LOG_CHANNEL मध्ये save झालं (student ला काहीच पाठवलं नाही)')
                return True, None, master_msg.id

            use_log_first = bool(protect_content and log_chat_id)

            await ensure_warmed(app, send_chat_id, access_hash)
            if use_log_first:
                await ensure_warmed(app, log_chat_id, log_access_hash)

            first_chat_id = log_chat_id if use_log_first else send_chat_id
            first_ahash = log_access_hash if use_log_first else access_hash
            first_protect = False if use_log_first else protect_content

            try:
                master_msg = await app.send_video(
                    chat_id=first_chat_id,
                    video=output_path,
                    caption=caption,
                    duration=dur,
                    width=w,
                    height=h,
                    thumb=thumb_path if thumb_path else None,
                    supports_streaming=True,
                    protect_content=first_protect,
                    progress=progress
                )
            except Exception as send_err:
                if first_ahash:
                    print(f'⚠️ Send failed ({send_err}) → access_hash ने force re-resolve करून retry करतोय')
                    _WARMED_PEERS.discard((first_chat_id, bot_token))
                    await warm_peer(app, first_chat_id, first_ahash)
                    _WARMED_PEERS.add((first_chat_id, bot_token))
                    master_msg = await app.send_video(
                        chat_id=first_chat_id,
                        video=output_path,
                        caption=caption,
                        duration=dur,
                        width=w,
                        height=h,
                        thumb=thumb_path if thumb_path else None,
                        supports_streaming=True,
                        protect_content=first_protect,
                        progress=progress
                    )
                else:
                    raise

            if use_log_first:
                sent_msg = None
                try:
                    sent_msg = await app.copy_message(
                        chat_id=send_chat_id,
                        from_chat_id=log_chat_id,
                        message_id=master_msg.id,
                        caption=caption,
                        protect_content=True,
                    )
                except Exception as copy_err:
                    if access_hash:
                        _WARMED_PEERS.discard((send_chat_id, bot_token))
                        await warm_peer(app, send_chat_id, access_hash)
                        _WARMED_PEERS.add((send_chat_id, bot_token))
                        sent_msg = await app.copy_message(
                            chat_id=send_chat_id,
                            from_chat_id=log_chat_id,
                            message_id=master_msg.id,
                            caption=caption,
                            protect_content=True,
                        )
                    else:
                        raise
                print(f'✅ Student ला protected copy पाठवली (log master कडून) → {send_chat_id}')
                return True, None, master_msg.id

            sent_msg = master_msg
            log_channel_msg_id = None
            print(f'ℹ️ log_chat_id from job: {log_chat_id!r} (access_hash present: {bool(log_access_hash)})')
            if log_chat_id:
                try:
                    await ensure_warmed(app, log_chat_id, log_access_hash)
                    try:
                        log_copy_msg = await app.copy_message(
                            chat_id=log_chat_id,
                            from_chat_id=send_chat_id,
                            message_id=sent_msg.id,
                            caption=caption,
                        )
                    except Exception as copy_err:
                        if log_access_hash:
                            _WARMED_PEERS.discard((log_chat_id, bot_token))
                            await warm_peer(app, log_chat_id, log_access_hash)
                            _WARMED_PEERS.add((log_chat_id, bot_token))
                            log_copy_msg = await app.copy_message(
                                chat_id=log_chat_id,
                                from_chat_id=send_chat_id,
                                message_id=sent_msg.id,
                                caption=caption,
                            )
                        else:
                            raise
                    log_channel_msg_id = log_copy_msg.id
                    print(f'✅ Log channel backup copied → {log_chat_id}')
                except Exception as le:
                    import traceback
                    print(f'⚠️ Log channel backup failed: {le}')
                    traceback.print_exc()
            else:
                print('⚠️ log_chat_id job मध्ये नाहीये/empty — backup skip')

            print(f'ℹ️ userbot_log_chat_id={userbot_log_chat_id}, userbot_log_topic_id={userbot_log_topic_id}')
            if userbot_log_chat_id:
                try:
                    await ensure_warmed(app, userbot_log_chat_id, userbot_log_access_hash)
                    try:
                        await app.copy_message(
                            chat_id=userbot_log_chat_id,
                            from_chat_id=send_chat_id,
                            message_id=sent_msg.id,
                            caption=caption,
                            reply_to_message_id=userbot_log_topic_id if userbot_log_topic_id else None
                        )
                    except Exception as copy_err:
                        print(f'⚠️ Userbot log channel backup retry: {copy_err}')
                        await warm_peer(app, userbot_log_chat_id, userbot_log_access_hash)
                        await app.copy_message(
                            chat_id=userbot_log_chat_id,
                            from_chat_id=send_chat_id,
                            message_id=sent_msg.id,
                            caption=caption,
                            reply_to_message_id=userbot_log_topic_id if userbot_log_topic_id else None
                        )
                    print(f'✅ Userbot log channel backup copied → {userbot_log_chat_id}')
                except Exception as le:
                    import traceback
                    print(f'⚠️ Userbot log channel backup failed: {le}')
                    traceback.print_exc()
            
            return True, None, log_channel_msg_id

        except Exception as e:
            return False, str(e), None

    try:
        result = run_on_tg_loop(upload(), timeout=FFMPEG_TIMEOUT)

        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)

        if not result or len(result) != 3:
            return False, "invalid upload response", None

        return result

    except Exception as e:
        return False, str(e), None

def do_upload_phase(job_id, fname, output_path, job, api_id, api_hash):
    upload_ok = False
    err = None
    log_channel_msg_id = None
    try:
        attempt = 1
        max_attempts = 2
        flood_retries = 0
        max_flood_retries = 20

        while attempt <= max_attempts:
            if attempt > 1:
                koyeb_progress(job_id,
                    f"🔄 *Upload retry {attempt}/{max_attempts}...*\n📄 `{fname}`")
                time.sleep(15)

            ok, err, log_channel_msg_id = telegram_send(output_path, job_id, job, api_id, api_hash)
            if ok:
                upload_ok = True
                break

            wait_secs = extract_flood_wait_seconds(err)
            if wait_secs is not None:
                flood_retries += 1
                if flood_retries > max_flood_retries:
                    print(f'❌ FloodWait खूप वेळा आला ({flood_retries}x) — job fail मानतोय')
                    break
                wait_with_progress(job_id, fname, wait_secs + 5, reason='FloodWait')
                continue

            print(f'❌ Upload attempt {attempt} failed: {err}')
            attempt += 1

        if not upload_ok:
            koyeb_failed(job_id, err)
            koyeb_progress(job_id,
                f'❌ Upload failed\n📄 `{fname}`\n`{err}`')
            return

        koyeb_done(job_id, log_channel_msg_id)
        koyeb_progress(job_id,
            f"✅ *Done!*\n📄 `{fname}`\n💧 Watermark applied\n📤 Telegram ला पाठवलं")
        print(f'✅ Job {job_id} done!')

    except Exception as e:
        print(f'❌ Upload phase exception: {e}')
        koyeb_failed(job_id, str(e))
        koyeb_progress(job_id, f'❌ Upload error\n📄 `{fname}`\n`{e}`')
    finally:
        if upload_ok:
            try:
                if os.path.exists(output_path): os.remove(output_path)
            except: pass

def process_job_download_ffmpeg(job, api_id, api_hash):
    job_id = job['job_id']
    fname  = job['file_name']
    furl   = job.get('file_url', '')
    wm     = job['watermark_text']

    channel_id        = job.get('channel_id') or 0
    channel_access_hash = job.get('channel_access_hash') or 0
    download_bot_token = job.get('download_bot_token') or ''
    direct_download    = bool(channel_id) and bool(download_bot_token)

    tier_label = {'tier1': 'GPU full', 'tier2': 'GPU encode', 'tier3': 'CPU'}.get(ENCODER_TIER, ENCODER_TIER)
    mode_label = 'direct-TG' if direct_download else 'pipe'
    print(f'\n🎯 Job: {job_id} | {fname} | WM: {wm} | Encoder: {tier_label} | Download: {mode_label}')

    ext         = os.path.splitext(fname)[1] or '.mp4'
    input_path  = f'/tmp/wm_input_{job_id}{ext}'
    output_path = f'/tmp/wm_output_{job_id}{ext}'

    try:
        if direct_download:
            ok = download_file_direct(api_id, api_hash, download_bot_token, channel_id, channel_access_hash, job['msg_id'], input_path, job_id, job)
        else:
            ok = download_file(furl, input_path, job_id, job)
        if ok is not True:
            err = ok[1] if isinstance(ok, tuple) else 'download failed'
            koyeb_failed(job_id, err)
            koyeb_progress(job_id, f'❌ Download failed\n📄 `{fname}`\n`{err}`')
            return None, job_id, fname

        if not _FFMPEG_SEMAPHORE.acquire(blocking=False):
            koyeb_progress(job_id,
                f"⏳ *Watermark queue...*\n📄 `{fname}`\n📌 Msg: `{job['msg_id']}`\n"
                f"🔁 दुसरा video encode होतोय, स्लॉट मोकळा होताच सुरू होईल.")
            _FFMPEG_SEMAPHORE.acquire()
        try:
            ok, err = run_ffmpeg(input_path, output_path, wm, job_id, job)
        finally:
            _FFMPEG_SEMAPHORE.release()

        if not ok:
            koyeb_failed(job_id, err)
            koyeb_progress(job_id, f'❌ FFmpeg failed\n📄 `{fname}`\n`{err}`')
            return None, job_id, fname

        return output_path, job_id, fname

    except Exception as e:
        print(f'❌ Exception (download/ffmpeg): {e}')
        koyeb_failed(job_id, str(e))
        koyeb_progress(job_id, f'❌ Error\n📄 `{fname}`\n`{e}`')
        return None, job_id, fname

    finally:
        try:
            if os.path.exists(input_path): os.remove(input_path)
        except: pass

def handle_job(job, api_id, api_hash):
    output_path, job_id, fname = process_job_download_ffmpeg(job, api_id, api_hash)
    if output_path:
        do_upload_phase(job_id, fname, output_path, job, api_id, api_hash)

def main():
    print(f'\n🚀 Worker started (parallel jobs, no cap)!')
    print(f'   Koyeb  : {KOYEB_URL}')
    print(f'   Encoder: {ENCODER_TIER}')
    print(f'   Poll   : {POLL_INTERVAL}s\n')

    job_count    = 0
    alive_tick   = 0
    active_threads = []

    import signal
    shutdown_requested = {'flag': False}

    # GitHub Actions sends SIGTERM on redeploy — finish current job, don't kill it
    def _handle_sigterm(signum, frame):
        print('\n🛑 SIGTERM मिळाला (नवीन runner सुरू झाला असेल) — चालू job पूर्ण करून थांबतोय...')
        shutdown_requested['flag'] = True

    signal.signal(signal.SIGTERM, _handle_sigterm)

    while True:
        try:
            if shutdown_requested['flag']:
                running = [t for t in active_threads if t.is_alive()]
                if running:
                    print(f'⏳ चालू असलेले {len(running)} job(s) पूर्ण होण्याची वाट बघतोय (max ~30 min each)...')
                    for t in running:
                        t.join(timeout=1800)
                stop_all_clients()
                print('✅ Worker gracefully stopped (SIGTERM).')
                break

            job, api_id, api_hash, stop = poll_job()

            if stop:
                running = [t for t in active_threads if t.is_alive()]
                print(f'🛑 Stop command received — चालू {len(running)} job(s) पूर्ण करून थांबतोय...')
                for t in running:
                    t.join()
                stop_all_clients()
                print('✅ Worker stopped by server command.')
                break

            if job and api_id and api_hash:
                job_count += 1
                running_now = sum(1 for t in active_threads if t.is_alive())
                print(f'📦 Job #{job_count}: {job["job_id"]} (सध्या parallel चालू: {running_now + 1})')

                t = threading.Thread(
                    target=handle_job,
                    args=(job, api_id, api_hash),
                    daemon=True,
                )
                active_threads.append(t)
                t.start()

                active_threads[:] = [th for th in active_threads if th.is_alive()]
            else:
                alive_tick += 1
                if alive_tick % (60 // POLL_INTERVAL) == 0:
                    running_now = sum(1 for t in active_threads if t.is_alive())
                    print(f'💓 Alive | Done: {job_count} | Running: {running_now} | {ENCODER_TIER} | {time.strftime("%H:%M:%S")}')

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print('\n🛑 KeyboardInterrupt — stopping...')
            for t in active_threads:
                if t.is_alive():
                    t.join(timeout=60)
            break
        except Exception as e:
            print(f'❌ Loop error: {e}')
            time.sleep(POLL_INTERVAL * 2)

if __name__ == '__main__':
    main()
