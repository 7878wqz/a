import os, json, secrets, subprocess, time, re
from flask import Flask, request, jsonify, redirect
from datetime import datetime, date, timedelta

try:
    import requests as req_lib
    USE_REQUESTS = True
except:
    USE_REQUESTS = False
    import urllib.request, urllib.parse

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# ===== RATE LIMIT (กันสแปมที่ Bot Hosting แทน Cloudflare Worker) =====
# นับ req ต่อ IP ในหน่วยความจำ + ล้างเก่าทุกนาที
# เกิน RL_LIMIT ครั้ง/นาที -> คืน 429 ชั่วคราว
import threading
_rl_lock = threading.Lock()
_rl_hits = {}  # ip -> [timestamps]
RL_LIMIT = 60
RL_WINDOW = 60  # วินาที

@app.before_request
def _ratelimit():
    # ไม่กันเส้นทางภายใน (เช่น static / เฮลธ์เช็ค)
    ip = request.headers.get('CF-Connecting-IP') or request.remote_addr or 'unknown'
    now = time.time()
    with _rl_lock:
        hits = _rl_hits.get(ip, [])
        hits = [t for t in hits if now - t < RL_WINDOW]
        hits.append(now)
        _rl_hits[ip] = hits
        if len(hits) > RL_LIMIT:
            return "Rate limited", 429, {'Retry-After': '60'}

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
WEBHOST_FILE = os.path.join(BASE_DIR, 'webhost.json')
INVITE_FILE  = os.path.join(BASE_DIR, 'current_invite.txt')  # บอทเขียน invite URL ลงที่นี่

@app.route('/getkey/')
@app.route('/getkey')
def getkey_landing():
    """อ่าน invite ล่าสุดที่บอทเขียนไว้ใน current_invite.txt แล้ว redirect ไปเลยทันที"""
    try:
        with open(INVITE_FILE, 'r', encoding='utf-8') as f:
            url = f.read().strip()
        if url.startswith('https://discord.gg/'):
            return redirect(url, code=302)
    except Exception as e:
        print(f"[/getkey] Error: {e}")
    return "ไม่สามารถสร้างลิงก์เชิญได้ในขณะนี้ กรุณาลองใหม่อีกครั้ง (บอทอาจยังไม่ได้รัน หรือยังไม่สร้างลิงก์แรก)", 503

# ===== TERMS / PRIVACY =====
# RENDER_URL ชี้ตัวเองบน Render (เช่น https://vectorhub-bot.onrender.com) — ตั้งใน env
RENDER_URL = os.environ.get('RENDER_URL', 'https://vectorhub.space').rstrip('/')
TERMS_URL   = os.environ.get('TERMS_URL',   RENDER_URL + '/api/terms')
PRIVACY_URL = os.environ.get('PRIVACY_URL', RENDER_URL + '/api/privacy')

# ===== LOOTLABS ANTI-BYPASS / POSTBACK =====
# Token ลับสำหรับ endpoint รับ postback จาก Lootlabs (ต่างจาก success token)
LOOTLABS_POSTBACK_TOKEN = os.environ.get('LOOTLABS_POSTBACK_TOKEN', 'VectorHubLootlabsPB2026')
# ชื่อ key ใน db ที่เก็บรายการ unique_id/ip ที่ผ่านโฆษณาแล้ว
FREEKEY_PASSED_KEY = "freekey_passed"

# ===== BOTS =====
# หมายเหตุ: RyuxRasin อยู่อันดับ 1 เสมอ ถ้าหน้าที่ซ้ำกับบอทตัวอื่น (เช่น airi)
# ให้ RyuxRasin เป็นตัวทำงานหลัก ตัวอื่นจะถูก autostart ตามหลัง
ADMIN_BOTS = {
    "ryuxrasin": {
        "name": "RyuxRasin Bot",
        "script": os.path.join(BASE_DIR, "bot_ryuxrasin.py"),
        "session": "admin_bot_ryuxrasin",
        "token_env": "RyuxRasin_TOKEN",
        "color": "#22D3EE",
        "icon": "👑",
        "description": "Key Management & AI Bot (Priority #1)",
        "public_path": "ryuxrasinproject",
    },
    "airi": {
        "name": "Airi Bot",
        "script": os.path.join(BASE_DIR, "bot_airi.py"),
        "session": "admin_bot_airi",
        "token_env": "AIRI_TOKEN",
        "color": "#6366F1",
        "icon": "🤖",
        "description": "Key Management & AI Bot",
        "public_path": "airiproject",
    },
    "nexa": {
        "name": "NEXA AutoMod",
        "script": os.path.join(BASE_DIR, "bot_nexa.py"),
        "session": "admin_bot_nexa",
        "token_env": "NEXA_TOKEN",
        "color": "#EF4444",
        "icon": "🛡️",
        "description": "AutoMod & Moderation Bot",
        "public_path": "nexaproject",
    },
    "emi": {
        "name": "Emi Music",
        "script": os.path.join(BASE_DIR, "bot_emi", "main.py"),
        "session": "admin_bot_emi",
        "token_env": "EMI_TOKEN",
        "color": "#A78BFA",
        "icon": "🎧",
        "description": "Music Bot (YouTube / Spotify / SoundCloud)",
        "public_path": "emiproject",
    },
}

ADMIN_BOT_TOKENS_FILE = os.path.join(BASE_DIR, "admin_bot_tokens.json")

def load_bot_tokens():
    if os.path.exists(ADMIN_BOT_TOKENS_FILE):
        with open(ADMIN_BOT_TOKENS_FILE) as f:
            return json.load(f)
    return {}

try:
    import psutil
    USE_PSUTIL = True
except ImportError:
    USE_PSUTIL = False

import sys, platform
IS_WINDOWS = platform.system() == "Windows"

# bot_id -> subprocess.Popen object (แทนการพึ่ง tmux ซึ่งใช้บน Windows ไม่ได้)
RUNNING_PROCS = {}


def _is_proc_alive(proc):
    return proc is not None and proc.poll() is None


def get_ram_kb(bot_id):
    proc = RUNNING_PROCS.get(bot_id)
    if not _is_proc_alive(proc) or not USE_PSUTIL:
        return 0
    try:
        p = psutil.Process(proc.pid)
        total = p.memory_info().rss
        for child in p.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total // 1024
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0


def get_bot_status():
    tokens = load_bot_tokens()
    result = {}
    for bot_id, cfg in ADMIN_BOTS.items():
        proc = RUNNING_PROCS.get(bot_id)
        online = _is_proc_alive(proc)
        ram = get_ram_kb(bot_id) if online else 0
        tok = tokens.get(bot_id, "")
        result[bot_id] = {**cfg, "online": online, "ram_kb": ram, "has_token": bool(tok)}
    return result


def start_bot(bot_id, cfg, token):
    """สตาร์ทบอทด้วย subprocess.Popen แบบข้าม OS (ไม่ใช้ tmux)"""
    stop_bot(bot_id)
    env = os.environ.copy()
    env[cfg.get("token_env", "DISCORD_TOKEN")] = token
    env["DISCORD_TOKEN"] = token  # เผื่อบอทตัวอื่นอ่านจากตัวแปรนี้

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    try:
        proc = subprocess.Popen(
            [sys.executable, cfg["script"]],
            cwd=os.path.dirname(cfg["script"]),
            env=env,
            creationflags=creationflags,
        )
        RUNNING_PROCS[bot_id] = proc
        print(f"[autostart] {cfg['name']} started (pid={proc.pid})")
        return True
    except Exception as e:
        print(f"[autostart] failed to start {cfg['name']}: {e}")
        return False


def stop_bot(bot_id):
    proc = RUNNING_PROCS.get(bot_id)
    if proc is None:
        return
    if _is_proc_alive(proc):
        try:
            if USE_PSUTIL:
                p = psutil.Process(proc.pid)
                for child in p.children(recursive=True):
                    try:
                        child.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as e:
            print(f"[stop_bot] error stopping {bot_id}: {e}")
    RUNNING_PROCS.pop(bot_id, None)


def autostart_bots():
    """สตาร์ทบอททุกตัวที่มีโทเคน ตามลำดับใน ADMIN_BOTS (RyuxRasin มาก่อนเสมอ)"""
    tokens = load_bot_tokens()
    for bot_id, cfg in ADMIN_BOTS.items():
        token = tokens.get(bot_id, '').strip()
        if not token or not os.path.exists(cfg["script"]):
            continue
        proc = RUNNING_PROCS.get(bot_id)
        if _is_proc_alive(proc):
            continue
        start_bot(bot_id, cfg, token)

# ===== WEBHOST HELPERS =====
def load_webhost():
    if os.path.exists(WEBHOST_FILE):
        with open(WEBHOST_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"sites": [], "orders": []}

def save_webhost(data):
    with open(WEBHOST_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ===== HOMEPAGE: สถานะบอท =====
HOME_HTML = '''<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BotHost — Bot Status</title>
<link href="https://fonts.googleapis.com/css2?family=Kanit:wght@300;400;700;900&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0f;color:#e8e8f0;font-family:"Kanit",sans-serif;min-height:100vh}}
.topbar{{background:rgba(10,10,15,.95);backdrop-filter:blur(16px);border-bottom:1px solid #1e1e2e;padding:16px 32px;display:flex;align-items:center;justify-content:space-between}}
.logo{{font-size:1.3rem;font-weight:900;background:linear-gradient(135deg,#00e5ff,#a855f7);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
.links{{display:flex;gap:16px}}
.links a{{color:#555;font-size:.78rem;text-decoration:none;transition:.2s}}
.links a:hover{{color:#00e5ff}}
.wrapper{{max-width:800px;margin:0 auto;padding:60px 24px}}
h1{{font-size:clamp(1.8rem,4vw,2.8rem);font-weight:900;text-align:center;margin-bottom:8px;background:linear-gradient(135deg,#fff,#00e5ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
.sub{{text-align:center;color:#555;font-size:.9rem;margin-bottom:48px}}
.bot-card{{background:#111118;border:1px solid #1e1e2e;border-radius:16px;padding:28px 32px;margin-bottom:20px;display:flex;align-items:center;gap:24px;transition:.2s;text-decoration:none;color:inherit}}
.bot-card:hover{{border-color:#2a2a3a;transform:translateY(-2px)}}
.bot-icon{{font-size:2.5rem;width:64px;height:64px;display:flex;align-items:center;justify-content:center;border-radius:16px;flex-shrink:0}}
.bot-info{{flex:1}}
.bot-name{{font-size:1.2rem;font-weight:900;margin-bottom:4px}}
.bot-desc{{color:#666;font-size:.85rem}}
.status{{display:flex;align-items:center;gap:8px;margin-top:10px}}
.dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.dot.on{{background:#00ff88;box-shadow:0 0 8px #00ff88}}
.dot.off{{background:#444}}
.status-text{{font-size:.8rem;font-family:"Space Mono",monospace}}
.status-text.on{{color:#00ff88}}
.status-text.off{{color:#444}}
.ram{{margin-left:auto;text-align:right}}
.ram .val{{font-family:"Space Mono",monospace;font-size:1rem;font-weight:700;color:#00e5ff}}
.ram .lbl{{font-size:.7rem;color:#444}}
.bot-url{{font-size:.75rem;color:#555;margin-top:6px;font-family:"Space Mono",monospace}}
footer{{text-align:center;padding:40px 24px;color:#333;font-size:.75rem}}
footer a{{color:#444;text-decoration:none}}
footer a:hover{{color:#00e5ff}}
</style></head>
<body>
<div class="topbar">
  <div class="logo">⚡ BotHost</div>
  <div class="links">
    <a href="{terms_url}">Terms of Service</a>
    <a href="{privacy_url}">Privacy Policy</a>
  </div>
</div>
<div class="wrapper">
  <h1>Bot Status</h1>
  <p class="sub">สถานะบอทแบบ Real-time</p>
  {bot_cards}
</div>
<footer>
  <a href="{terms_url}">Terms of Service</a> &nbsp;·&nbsp;
  <a href="{privacy_url}">Privacy Policy</a><br><br>
  © 2026 BotHost
</footer>
</body></html>'''

@app.route('/')
def index():
    # UA-split: Roblox ได้ Lua จริง (เพื่อ loadstring(HttpGet('https://vectorhub.space'))())
    # Browser ได้หน้า troll ล้อเลียน (เดิมทำใน Worker ย้ายมาไว้ที่นี่เลย ไม่พึ่ง Worker)
    ua = (request.headers.get('User-Agent') or '').lower()
    if 'roblox' in ua:
        return serve_lua('Loader')
    return serve_troll_page()

@app.route('/home')
def home():
    """หน้าแดชบอร์ดบอท (เคยอยู่ที่ root ก่อนย้าย UA-split มา app.py)"""
    bots = get_bot_status()
    cards = ''
    for bot_id, b in bots.items():
        ram_html = f'<div class="ram"><div class="val">{b["ram_kb"]/1024:.1f} MB</div><div class="lbl">RAM</div></div>' if b["online"] else ''
        cards += f'''
        <a class="bot-card" href="/{b['public_path']}">
          <div class="bot-icon" style="background:rgba(255,255,255,.05)">{b["icon"]}</div>
          <div class="bot-info">
            <div class="bot-name" style="color:{b["color"]}">{b["name"]}</div>
            <div class="bot-desc">{b["description"]}</div>
            <div class="status">
              <div class="dot {"on" if b["online"] else "off"}"></div>
              <span class="status-text {"on" if b["online"] else "off"}">{"● Online" if b["online"] else "○ Offline"}</span>
            </div>
            <div class="bot-url">{RENDER_URL}/api/{b["public_path"]}</div>
          </div>
          {ram_html}
        </a>'''
    return HOME_HTML.format(bot_cards=cards, terms_url=TERMS_URL, privacy_url=PRIVACY_URL)

# ===== LUAU SCRIPT SERVING (จากโฟลเดอร์ Lua/ — ไม่พึ่ง GitHub คนอื่น) =====
LUA_DIR = os.path.join(BASE_DIR, 'Lua')

@app.route('/Lua/<name>')
def serve_lua(name):
    """เสิร์ฟไฟล์ .lua จากโฟลเดอร์ Lua/ (Loader.lua, VD2.lua ฯลฯ)
    ใช้ text/plain + cache 4 ชม. เพื่อให้ Worker/CDN แคชได้"""
    # ป้องกัน path traversal: 只允许 ตัวอักษร a-z0-9_ เท่านั้น
    if not re.fullmatch(r'[A-Za-z0-9_]+', name):
        return "-- invalid name", 400
    # ไม่ใช้ whitelist ชื่อไฟล์ — หย่อน .lua ลงโฟลเดอร์ Lua/ ไหนก็เสิร์ฟได้
    # ความปลอดภัยอยู่ที่ regex บรรทัดบน (บังคับ a-z0-9_ เท่านั้น ห้าม / หรือ ..)
    lua_path = os.path.join(LUA_DIR, name + '.lua')
    if not os.path.isfile(lua_path):
        return "-- " + name + ".lua not found", 404
    try:
        with open(lua_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return "-- read error: " + str(e), 500
    resp = app.make_response(content)
    resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=14400'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

# ===== TROLL PAGE (ล้อเลียนคนเปิด browser ดู source) =====
TROLL_HTML = os.path.join(BASE_DIR, 'troll.html')
TROLL_SRC  = os.path.join(BASE_DIR, 'troll_source.lua')
DISCORD_INVITE = 'https://discord.gg/5Yv9d26PHu'  # TODO: แก้เป็น invite จริงถ้าผิด

@app.route('/web/troll')
def serve_troll_page():
    """หน้าล้อเลียนสำหรับ browser (คนรัน Roblox ได้ Lua จริงจาก Worker)"""
    try:
        with open(TROLL_HTML, 'r', encoding='utf-8') as f:
            html = f.read()
    except Exception as e:
        return "troll page missing: " + str(e), 500
    html = html.replace('DISCORD_INVITE_HERE', DISCORD_INVITE)
    resp = app.make_response(html)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

@app.route('/web/troll-src')
def serve_troll_src():
    """คืนโค้ดหลอก (obfuscated) ให้หน้า troll โชว์"""
    try:
        with open(TROLL_SRC, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return "-- troll source missing: " + str(e), 500
    resp = app.make_response(content)
    resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

# ===== TROLL GHOST IMAGES (popup jumpscare สำหรับ browser) =====
GHOST1 = os.path.join(BASE_DIR, 'Ghost.jpg')
GHOST2 = os.path.join(BASE_DIR, 'Ghost2.jpg')

def serve_image(path, content_type='image/jpeg'):
    """เสิร์ฟไฟล์รูปให้หน้า troll โชว์ (popup ผี)"""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception as e:
        return "image missing: " + str(e), 500
    resp = app.make_response(data)
    resp.headers['Content-Type'] = content_type
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/web/ghost')
def serve_ghost():
    return serve_image(GHOST1)

@app.route('/web/ghost2')
def serve_ghost2():
    return serve_image(GHOST2)

# ===== TROLL MONKEY IMAGE (แสดงในกรอบแทนโค้ด) =====
MONKEY = os.path.join(BASE_DIR, 'monkey.png')

@app.route('/web/monkey.png')
def serve_monkey():
    return serve_image(MONKEY, content_type='image/png')

# ===== BOT PUBLIC PAGES =====
def bot_page_html(bot_id):
    bots = get_bot_status()
    b = bots.get(bot_id)
    if not b:
        return "Not found", 404
    online = b["online"]
    status_text = "Online" if online else "Offline"
    status_color = "#00ff88" if online else "#ff4466"
    ram_text = f'{b["ram_kb"]/1024:.1f} MB' if online else "—"
    return f'''<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{b["name"]}</title>
<link href="https://fonts.googleapis.com/css2?family=Kanit:wght@300;400;700;900&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0f;color:#e8e8f0;font-family:"Kanit",sans-serif;min-height:100vh;display:flex;flex-direction:column}}
.topbar{{background:rgba(10,10,15,.95);backdrop-filter:blur(16px);border-bottom:1px solid #1e1e2e;padding:16px 32px;display:flex;align-items:center;justify-content:space-between}}
.logo{{font-size:1.1rem;font-weight:900;background:linear-gradient(135deg,#00e5ff,#a855f7);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none}}
.back{{color:#555;font-size:.82rem;text-decoration:none}}
.back:hover{{color:#00e5ff}}
.hero{{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px 24px;text-align:center}}
.icon{{font-size:4rem;margin-bottom:20px;width:96px;height:96px;display:flex;align-items:center;justify-content:center;border-radius:24px;background:rgba(255,255,255,.05);margin:0 auto 24px}}
h1{{font-size:clamp(2rem,5vw,3rem);font-weight:900;color:{b["color"]};margin-bottom:8px}}
.desc{{color:#666;margin-bottom:36px;font-size:.95rem}}
.status-badge{{display:inline-flex;align-items:center;gap:10px;background:#111118;border:1px solid #1e1e2e;border-radius:100px;padding:12px 28px;margin-bottom:16px}}
.dot{{width:10px;height:10px;border-radius:50%;background:{status_color};{"box-shadow:0 0 10px "+status_color if online else ""}}}
.status-label{{font-family:"Space Mono",monospace;font-size:.9rem;color:{status_color};font-weight:700}}{f"""
.pulse{{animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.5}}}}""" if online else ""}
.stats{{display:flex;gap:20px;justify-content:center;flex-wrap:wrap;margin-top:20px}}
.stat{{background:#111118;border:1px solid #1e1e2e;border-radius:12px;padding:16px 28px;text-align:center}}
.stat .val{{font-family:"Space Mono",monospace;font-size:1.3rem;font-weight:700;color:#00e5ff}}
.stat .lbl{{font-size:.72rem;color:#444;margin-top:4px}}
footer{{text-align:center;padding:32px;color:#333;font-size:.75rem}}
footer a{{color:#444;text-decoration:none}}
footer a:hover{{color:#00e5ff}}
</style></head>
<body>
<div class="topbar">
  <a class="logo" href="/">⚡ BotHost</a>
  <a class="back" href="/">← กลับหน้าหลัก</a>
</div>
<div class="hero">
  <div class="icon">{b["icon"]}</div>
  <h1>{b["name"]}</h1>
  <p class="desc">{b["description"]}</p>
  <div class="status-badge">
    <div class="dot{"  pulse" if online else ""}"></div>
    <span class="status-label">{status_text}</span>
  </div>
  <div class="stats">
    <div class="stat"><div class="val">{ram_text}</div><div class="lbl">RAM Usage</div></div>
    <div class="stat"><div class="val">{"▶ Running" if online else "■ Stopped"}</div><div class="lbl">Process</div></div>
  </div>
</div>
<footer>
  <a href="{TERMS_URL}">Terms of Service</a> &nbsp;·&nbsp;
  <a href="{PRIVACY_URL}">Privacy Policy</a>
</footer>
</body></html>'''

@app.route('/ryuxrasinproject')
def ryuxrasin_page():
    return bot_page_html("ryuxrasin")

@app.route('/airiproject')
def airi_page():
    return bot_page_html("airi")

@app.route('/nexaproject')
def nexa_page():
    return bot_page_html("nexa")


# ===== KEY & HWID VALIDATION API =====
CONFIG_FILE = os.path.join(BASE_DIR, "RyuxRasinconfig.json")
LOCK_FILE = CONFIG_FILE + ".lock"

def _acquire_config_lock(timeout=5):
    import time
    start = time.time()
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - start > timeout:
                try:
                    os.remove(LOCK_FILE)
                except OSError:
                    pass
            time.sleep(0.05)

def _release_config_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

# ── MONGO STORE (shared persistence) ──
try:
    from mongo_store import (
        load_config as _ms_load_config,
        save_config as _ms_save_config,
        load_keydb as _ms_load_keydb,
        save_keydb as _ms_save_keydb,
        load_freeips as _ms_load_freeips,
        save_freeips as _ms_save_freeips,
    )
    _HAVE_MONGO_STORE = True
except Exception as _mse:
    print(f"[mongo_store import ERROR] {_mse}")
    _HAVE_MONGO_STORE = False

def load_config() -> dict:
    if _HAVE_MONGO_STORE:
        try:
            return _ms_load_config()
        except Exception as e:
            print(f"[API Config] Mongo load error: {e}")
    # JSON fallback
    _acquire_config_lock()
    try:
        if os.path.exists(CONFIG_FILE):
            if os.path.getsize(CONFIG_FILE) > 0:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
    except Exception as e:
        print(f"[API Config] Load config error: {e}")
        BAK_FILE = CONFIG_FILE + ".bak"
        if os.path.exists(BAK_FILE) and os.path.getsize(BAK_FILE) > 0:
            try:
                with open(BAK_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    finally:
        _release_config_lock()
    return {}

def save_config(cfg: dict) -> bool:
    if _HAVE_MONGO_STORE:
        try:
            return _ms_save_config(cfg)
        except Exception as e:
            print(f"[API Config] Mongo save error: {e}")
    # JSON fallback
    _acquire_config_lock()
    try:
        import tempfile, shutil
        dir_name = os.path.dirname(CONFIG_FILE) or "."
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            json.dump(cfg, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        BAK_FILE = CONFIG_FILE + ".bak"
        if os.path.exists(CONFIG_FILE) and os.path.getsize(CONFIG_FILE) > 0:
            try:
                shutil.copy2(CONFIG_FILE, BAK_FILE)
            except Exception:
                pass
        shutil.move(tmp_path, CONFIG_FILE)
        return True
    except Exception as e:
        print(f"[API Config] Save config error: {e}")
        return False
    finally:
        _release_config_lock()

# ── Key DB helpers (separate doc so app.py save doesn't clobber bot keys) ──
def load_key_db() -> dict:
    if _HAVE_MONGO_STORE:
        try:
            return _ms_load_keydb()
        except Exception as e:
            print(f"[key_db] Mongo load error: {e}")
    cfg = load_config()
    return cfg.get("keyDatabase", {})

def save_key_db(key_db: dict) -> bool:
    if _HAVE_MONGO_STORE:
        try:
            return _ms_save_keydb(key_db)
        except Exception as e:
            print(f"[key_db] Mongo save error: {e}")
    cfg = load_config()
    cfg["keyDatabase"] = key_db
    return save_config(cfg)

def load_free_keys_ips() -> dict:
    if _HAVE_MONGO_STORE:
        try:
            return _ms_load_freeips()
        except Exception as e:
            print(f"[freeips] Mongo load error: {e}")
    return load_config().get("free_keys_ips", {}) or {}

def save_free_keys_ips(data: dict) -> bool:
    if _HAVE_MONGO_STORE:
        try:
            return _ms_save_freeips(data)
        except Exception as e:
            print(f"[freeips] Mongo save error: {e}")
    cfg = load_config()
    cfg["free_keys_ips"] = data
    return save_config(cfg)

# ── Discord Webhook Logger ──
def send_discord_log(title, description, color=0x3b82f6, fields=None):
    try:
        # Load config directly without nesting locks, using simple read
        if not os.path.exists(CONFIG_FILE) or os.path.getsize(CONFIG_FILE) == 0:
            return
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        webhook_url = cfg.get("log_webhook_url", "").strip()
        if not webhook_url:
            return

        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "fields": fields or [],
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }]
        }
        
        headers = {"Content-Type": "application/json"}
        import urllib.request
        req = urllib.request.Request(
            webhook_url, 
            data=json.dumps(payload).encode("utf-8"), 
            headers=headers, 
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            pass
    except Exception as e:
        print(f"[Webhook Log Error] {e}")

# ── Auto DB Cleaner Background Loop ──
def run_db_cleaner():
    import threading, time
    def _cleaner_loop():
        # wait 3 minutes after startup
        time.sleep(180)
        while True:
            try:
                key_db = load_key_db()
                free_keys_ips = load_free_keys_ips()
                if key_db or free_keys_ips:
                    custom_keys = key_db.get("keys_custom_days", {})
                    used_keys = key_db.get("used_keys", {})

                    now = datetime.now()
                    modified = False

                    # 1. Prune custom keys older than 30 hours
                    for ip, ip_data in list(free_keys_ips.items()):
                        try:
                            created_at = datetime.fromisoformat(ip_data.get("created_at"))
                            if now > created_at + timedelta(hours=30):
                                k = ip_data.get("key")
                                if k and k in custom_keys:
                                    custom_keys.pop(k, None)
                                free_keys_ips.pop(ip, None)
                                modified = True
                        except Exception:
                            pass

                    # 2. Prune used keys expired more than 7 days
                    expired_used = []
                    for key, key_data in list(used_keys.items()):
                        try:
                            expire_time = parse_key_time(key_data.get("หมดเวลา"))
                            if now > expire_time + timedelta(days=7):
                                expired_used.append(key)
                        except Exception:
                            pass

                    for key in expired_used:
                        used_keys.pop(key, None)
                        modified = True

                    if modified:
                        save_key_db(key_db)
                        save_free_keys_ips(free_keys_ips)
                        print(f"[Auto DB Cleaner] Cleaned up expired custom/used keys. DB saved.")
            except Exception as e:
                print(f"[Auto DB Cleaner Error] {e}")
            time.sleep(7200) # every 2 hours
            
    threading.Thread(target=_cleaner_loop, daemon=True).start()

# Start background cleaner on import / startup
run_db_cleaner()

def save_config(cfg: dict) -> bool:
    _acquire_config_lock()
    try:
        import tempfile, shutil
        dir_name = os.path.dirname(CONFIG_FILE) or "."
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            json.dump(cfg, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        
        # Write backup before replace
        BAK_FILE = CONFIG_FILE + ".bak"
        if os.path.exists(CONFIG_FILE) and os.path.getsize(CONFIG_FILE) > 0:
            try:
                shutil.copy2(CONFIG_FILE, BAK_FILE)
            except Exception:
                pass
                
        shutil.move(tmp_path, CONFIG_FILE)
        return True
    except Exception as e:
        print(f"[API Config] Save config error: {e}")
        return False
    finally:
        _release_config_lock()

def parse_key_time(t_str):
    t_str = t_str.replace("/24.00", "/23.59").replace("/24.59", "/23.59")
    parts = t_str.split("/")
    if len(parts) >= 3 and len(parts[2]) > 4:
        parts[2] = parts[2][:4]
        t_str = "/".join(parts)
    return datetime.strptime(t_str, "%d/%m/%Y/%H.%M")

# ===== KEY PREFIX (ต้องตรงกับ bot_ryuxrasin.py และ game_loader.lua) =====
VALID_KEY_PREFIXES = (
    "VECTORHUB7D-",
    "VECTORHUB30D-",
    "VECTORHUBPREMIUME-",
    "VECTORHUBCUSTOM-",
    "VECTORHUBFREE-",
    "FREE-",   # legacy — คีย์ในฐานข้อมูลเดิม
    "WL-",     # whitelist keys
    "AIRI-",   # legacy airi keys
)

def is_valid_key_prefix(key: str) -> bool:
    return any(key.startswith(p) for p in VALID_KEY_PREFIXES)

def is_free_key(key: str) -> bool:
    return key.startswith("VECTORHUBFREE-") or key.startswith("FREE-")

# ===== RATE LIMITING =====
from collections import defaultdict
from datetime import datetime, timedelta

RATE_LIMIT_STORE = defaultdict(list)
MAX_REQUESTS_PER_IP = 10
RATE_LIMIT_WINDOW = 60  # seconds

def check_rate_limit(ip):
    now = datetime.now()
    requests = RATE_LIMIT_STORE[ip]
    # Remove old requests outside the window
    RATE_LIMIT_STORE[ip] = [req_time for req_time in requests if now - req_time < timedelta(seconds=RATE_LIMIT_WINDOW)]
    if len(RATE_LIMIT_STORE[ip]) >= MAX_REQUESTS_PER_IP:
        return False
    RATE_LIMIT_STORE[ip].append(now)
    return True

@app.route('/validate', methods=['POST'])
def validate_key():
    # ── Check Auth Signature ──
    auth_key = os.environ.get("VH_AUTH_KEY", "VectorHubSecureKey2026")
    if request.headers.get("VH-Auth-Key") != auth_key:
        return jsonify({"valid": False, "reason": "unauthorized"}), 200
    
    # ── Rate Limit Check ──
    client_ip = request.headers.get("CF-Connecting-IP", request.headers.get("X-Forwarded-For", request.remote_addr))
    if client_ip and "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    if not client_ip:
        client_ip = request.remote_addr
    
    if not check_rate_limit(client_ip):
        return jsonify({"valid": False, "reason": "rate_limit_exceeded"}), 429
        
    data = request.json or {}
    key = data.get("key")
    hwid = data.get("hwid")

    if not key:
        return jsonify({"valid": False, "reason": "missing_key"}), 200

    # ── Key Prefix Validation ──
    if not is_valid_key_prefix(key):
        return jsonify({"valid": False, "reason": "invalid_key_prefix"}), 200

    # ── Key Format Validation ──
    if len(key) < 8 or len(key) > 64:
        return jsonify({"valid": False, "reason": "invalid_key_format"}), 200
    
    # Key must be alphanumeric with optional hyphens
    valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")
    if not all(c in valid_chars for c in key):
        return jsonify({"valid": False, "reason": "invalid_key_format"}), 200

    key_db = load_key_db()
    if not key_db:
        return jsonify({"valid": False, "reason": "corrupt_record"}), 200

    used_keys = key_db.get("used_keys", {})
    blacklist = key_db.get("blacklist", {})

    key_data = None
    # 1. Exact match ONLY (removed prefix match for security)
    if key in used_keys:
        key_data = used_keys[key]

    if not key_data:
        return jsonify({"valid": False, "reason": "key_not_found"}), 200

    # Check blacklist
    user_id = key_data.get("ผู้ใช้")
    if user_id and str(user_id) in blacklist:
        return jsonify({"valid": False, "reason": "blacklisted"}), 200

    # Check expiration
    end_time_str = key_data.get("หมดเวลา")
    if not end_time_str:
        return jsonify({"valid": False, "reason": "expired"}), 200

    try:
        expire_time = parse_key_time(end_time_str)
    except Exception as e:
        print(f"[API Validate] Error parsing expire time: {e}")
        return jsonify({"valid": False, "reason": "corrupt_record"}), 200

    now = datetime.now()
    if now > expire_time:
        return jsonify({"valid": False, "reason": "expired"}), 200

    remaining_seconds = int((expire_time - now).total_seconds())

    # Check HWID and Device Limit (1 device per key)
    saved_hwid = key_data.get("hwid")
    hwid_bound = False
    
    # บังคับให้ต้องมี HWID เสมอ
    if not hwid:
        return jsonify({"valid": False, "reason": "hwid_required"}), 200
    
    if saved_hwid:
        if saved_hwid != hwid:
            return jsonify({"valid": False, "reason": "hwid_mismatch"}), 200
        hwid_bound = True
    else:
        # HWID Auto-Bind: บันทึก HWID ครั้งแรกที่ validate
        key_data["hwid"] = hwid
        key_data["ip_address"] = client_ip
        key_data["last_device_update"] = datetime.now().strftime("%d/%m/%Y/%H.%M")
        save_key_db(key_db)
        hwid_bound = True

    return jsonify({
        "valid": True,
        "hwid_bound": hwid_bound,
        "remaining_seconds": remaining_seconds
    }), 200

@app.route('/api/save-hwid', methods=['POST'])
def save_hwid():
    # ── Check Auth Signature ──
    auth_key = os.environ.get("VH_AUTH_KEY", "VectorHubSecureKey2026")
    if request.headers.get("VH-Auth-Key") != auth_key:
        return jsonify({"success": False, "error": "Unauthorized request origin"}), 403
    
    # ── Rate Limit Check ──
    client_ip = request.headers.get("CF-Connecting-IP", request.headers.get("X-Forwarded-For", request.remote_addr))
    if client_ip and "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    if not client_ip:
        client_ip = request.remote_addr
    
    if not check_rate_limit(client_ip):
        return jsonify({"success": False, "error": "rate_limit_exceeded"}), 429
        
    import tempfile, shutil
    data = request.json or {}
    key = data.get("key")
    hwid = data.get("hwid")

    if not key or not hwid:
        return jsonify({"success": False, "error": "Missing key or hwid"}), 400

    # ── Key Format Validation ──
    if len(key) < 8 or len(key) > 64:
        return jsonify({"success": False, "error": "invalid_key_format"}), 400
    
    valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")
    if not all(c in valid_chars for c in key):
        return jsonify({"success": False, "error": "invalid_key_format"}), 400

    key_db = load_key_db()
    if not key_db:
        return jsonify({"success": False, "error": "Database not found"}), 500

    used_keys = key_db.get("used_keys", {})

    # Find the key (EXACT MATCH ONLY - removed prefix match for security)
    found_key = None
    if key in used_keys:
        found_key = key

    if not found_key:
        return jsonify({"success": False, "error": "Key not found"}), 404

    # Save HWID, IP, and device info
    used_keys[found_key]["hwid"] = hwid
    used_keys[found_key]["ip_address"] = client_ip
    used_keys[found_key]["last_device_update"] = datetime.now().strftime("%d/%m/%Y/%H.%M")

    if not save_key_db(key_db):
        return jsonify({"success": False, "error": "Database locked or saving failed"}), 500

    return jsonify({"success": True}), 200


@app.route('/api/redeem', methods=['POST'])
def redeem_key():
    # ── Check Auth Signature ──
    auth_key = os.environ.get("VH_AUTH_KEY", "VectorHubSecureKey2026")
    if request.headers.get("VH-Auth-Key") != auth_key:
        return jsonify({"success": False, "error": "Unauthorized request origin"}), 403
    
    # ── Rate Limit Check ──
    client_ip = request.headers.get("CF-Connecting-IP", request.headers.get("X-Forwarded-For", request.remote_addr))
    if client_ip and "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    if not client_ip:
        client_ip = request.remote_addr
    
    if not check_rate_limit(client_ip):
        return jsonify({"success": False, "error": "rate_limit_exceeded"}), 429
        
    import tempfile, shutil
    data = request.json or {}
    key = data.get("key", "").strip()
    hwid = data.get("hwid", "").strip()
    roblox_user_id = data.get("roblox_user_id", "").strip()
    roblox_username = data.get("roblox_username", "").strip()

    if not key:
        return jsonify({"success": False, "error": "กรุณากรอกคีย์"}), 200

    # ── Key Prefix Validation ──
    if not is_valid_key_prefix(key):
        return jsonify({"success": False, "error": "invalid_key_prefix"}), 200

    # ── Key Format Validation ──
    if len(key) < 8 or len(key) > 64:
        return jsonify({"success": False, "error": "invalid_key_format"}), 200
    
    valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")
    if not all(c in valid_chars for c in key):
        return jsonify({"success": False, "error": "invalid_key_format"}), 200

    key_db = load_key_db()
    used_keys = key_db.setdefault("used_keys", {})
    blacklist = key_db.setdefault("blacklist", {})

    # 1. Check if key is already in used_keys (EXACT MATCH ONLY)
    found_key = None
    if key in used_keys:
        found_key = key

    if found_key:
        existing_data = used_keys[found_key]
        
        # Check blacklist
        user_id = existing_data.get("ผู้ใช้")
        if user_id and str(user_id) in blacklist:
            return jsonify({"success": False, "error": "คีย์นี้ถูกระงับการใช้งาน"}), 200

        # Check expiration
        try:
            expire_time = parse_key_time(existing_data.get("หมดเวลา"))
            if datetime.now() > expire_time:
                return jsonify({"success": False, "error": "คีย์นี้หมดอายุแล้ว"}), 200
        except Exception:
            return jsonify({"success": False, "error": "คีย์นี้หมดอายุแล้ว"}), 200

        # Check HWID mismatch
        saved_hwid = existing_data.get("hwid")
        if saved_hwid and hwid and saved_hwid != hwid:
            return jsonify({"success": False, "error": "คีย์นี้ถูกผูกกับเครื่องอื่นแล้ว"}), 200

        # If valid, just return success
        return jsonify({"success": True, "message": "คีย์ของคุณใช้งานได้แล้วค่ะ"}), 200

    # 2. Check if key is in active unredeemed keys (EXACT MATCH ONLY)
    duration_days = None
    found_list_name = None
    target_key = key

    custom_keys = key_db.setdefault("keys_custom_days", {})
    if key in custom_keys:
        duration_days = custom_keys[key]
        found_list_name = "keys_custom_days"
    else:
        for list_name, days in [("keys_7_days", 7), ("keys_30_days", 30), ("keys_lifetime", 36500)]:
            key_list = key_db.setdefault(list_name, [])
            if key in key_list:
                duration_days = days
                found_list_name = list_name
                break
            # Removed prefix match for security

    if duration_days is None:
        return jsonify({"success": False, "error": "ไม่พบคีย์นี้ในระบบ หรือคีย์นี้ถูกเปิดใช้งานไปแล้ว"}), 200

    # 3. Redeem the key
    if found_list_name == "keys_custom_days":
        custom_keys.pop(target_key, None)
    else:
        key_db[found_list_name].remove(target_key)

    now = datetime.now()
    end = now + timedelta(days=duration_days)
    
    start_str = now.strftime("%d/%m/%Y/%H.%M")
    end_str = end.strftime("%d/%m/%Y/%H.%M")

    user_identifier = f"Roblox_{roblox_user_id}" if roblox_user_id else f"Roblox_{roblox_username}" if roblox_username else "Roblox_User"

    used_keys[target_key] = {
        "ผู้ใช้": user_identifier,
        "เริ่มใช้": start_str,
        "หมดเวลา": end_str,
        "คีย์ที่ใช้": target_key,
        "hwid": hwid,
        "ip_address": client_ip,
        "last_device_update": start_str,
        "free_key": is_free_key(target_key)
    }

    if not save_key_db(key_db):
        return jsonify({"success": False, "error": "เซิร์ฟเวอร์ยุ่งอยู่ Saving failed"}), 200

    # ── Send Webhook Log ──
    send_discord_log(
        title="✅ Key Redeemed In-Game",
        description="มีผู้เล่นเปิดใช้งานคีย์เคลมสคริปต์เข้าเล่นเกมเรียบร้อยแล้วค่ะ",
        color=0x10b981,
        fields=[
            {"name": "🔑 คีย์ที่ใช้", "value": f"`{target_key}`", "inline": True},
            {"name": "🆔 Roblox UserID", "value": f"`{roblox_user_id}`", "inline": True},
            {"name": "👤 Roblox Username", "value": f"`{roblox_username}`", "inline": True},
            {"name": "💻 HWID", "value": f"`{hwid}`", "inline": False},
            {"name": "⏳ วันหมดเวลาคีย์", "value": f"`{end_str}`", "inline": False}
        ]
    )

    return jsonify({"success": True, "message": "เปิดใช้งานคีย์สำเร็จแล้วค่ะ!"}), 200


@app.route('/api/getkey-urls', methods=['GET'])
def get_key_urls():
    cfg = load_config()
    if not cfg:
        return jsonify({"lootlabs_url": "", "linkvertise_url": ""}), 200
    try:
        return jsonify({
            "lootlabs_url": cfg.get("getkey_lootlabs_url", ""),
            "linkvertise_url": cfg.get("getkey_linkvertise_url", "")
        }), 200
    except Exception as e:
        return jsonify({"lootlabs_url": "", "linkvertise_url": ""}), 200



@app.route('/getkey/success', methods=['GET'])
@app.route('/api/getkey/success', methods=['GET'])
def getkey_success():
    # ── Check Bypass Protection (Referer / Token) ──
    referer = request.headers.get("Referer", "")
    token = request.args.get("token")

    # Get client IP early so the anti-bypass gate can use it
    user_ip = request.headers.get("CF-Connecting-IP", request.headers.get("X-Forwarded-For", request.remote_addr))
    if user_ip and "," in user_ip:
        user_ip = user_ip.split(",")[0].strip()
    if not user_ip:
        user_ip = "unknown"

    # Expanded referer list for lootlabs and linkvertise
    is_valid_referer = any(kw in referer.lower() for kw in [
        "loot", "linkvertise", "link-hub"
    ])
    is_valid_token = (token == "VectorHubSecureShortenerRedirectSecret2026")
    
    # Only allow: (a) valid Lootlabs/Linkvertise referer, or (b) correct secret token.
    # Direct access (empty referer) is BLOCKED to stop people copying the link.
    # IMPORTANT (method ก): the PUBLIC Lootlabs destination link MUST NOT contain
    # ?token=... — otherwise anyone who copies the link gets a key without the ad.
    # The token below is kept ONLY for local/debug testing (?token=... manually).
    is_same_origin = ("vectorhub.space" in referer.lower()) or ("sslip.io" in referer.lower())

    # Anti-Bypass gate: ต้องมี grant token ที่ถูก mint มาก่อน (single-use, ต่อคน)
    # ลบการเช็ค ip_passed ตามที่เจ้าของระบุ (WiFi เดียวกันจะได้คีย์คนละอัน)
    grant = request.args.get("grant")
    grants = _load_grants()
    grant_ok = False
    if grant and grant in grants:
        rec = grants[grant]
        if not rec.get("used"):
            grant_ok = True
            rec["used"] = True          # single-use: ใช้แล้วลบออก
            grants.pop(grant, None)
            _save_grants(grants)

    # ยังคงอนุญาต referer/token เดิมสำหรับกรณีทดสอบ (local/debug)
    if not (grant_ok or is_valid_referer or is_valid_token or is_same_origin):
        print(f"[Access Denied] Blocked Referer: '{referer}', Token: '{token}', Grant: '{grant}'")
        return '''<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Access Denied — RyuxRasin</title>
    <link href="https://fonts.googleapis.com/css2?family=Kanit:wght@400;600;800&family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        body {
            background: radial-gradient(circle at center, #0e1227 0%, #050712 100%);
            color: #e2e8f0;
            font-family: "Outfit", "Kanit", sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0;
        }
        .card {
            background: rgba(10, 12, 22, 0.75);
            backdrop-filter: blur(20px);
            border: 1px solid rgba(239, 68, 68, 0.2);
            border-radius: 24px;
            padding: 40px;
            width: 90%;
            max-width: 480px;
            text-align: center;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
        }
        .icon {
            font-size: 3rem;
            color: #ef4444;
            margin-bottom: 20px;
        }
        h1 {
            font-size: 1.6rem;
            margin-bottom: 12px;
            color: #ef4444;
        }
        p {
            color: #94a3b8;
            font-size: 0.95rem;
            line-height: 1.6;
            margin-bottom: 24px;
        }
        .btn {
            display: inline-block;
            background: linear-gradient(135deg, #374151 0%, #1f2937 100%);
            border: 1px solid rgba(255, 255, 255, 0.1);
            padding: 12px 24px;
            border-radius: 12px;
            color: white;
            text-decoration: none;
            font-weight: 600;
            transition: 0.2s;
        }
        .btn:hover {
            background: linear-gradient(135deg, #4b5563 0%, #374151 100%);
            transform: translateY(-1px);
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">🚫</div>
        <h1>เข้าใช้งานไม่ถูกต้อง</h1>
        <p>คุณไม่สามารถเข้าหน้านี้โดยตรงได้ค่ะ กรุณากดรับคีย์ (Get Key) ผ่านบอทดิสคอร์ด และดำเนินการข้ามลิงก์โฆษณาให้เสร็จสิ้นเพื่อรับสคริปต์นะคะ</p>
        <a href="https://discord.gg/x3UhBsnpeJ" class="btn">กลับเข้าดิสคอร์ด (Discord)</a>
    </div>
</body>
</html>''', 403

    import secrets, tempfile, shutil, base64
    from datetime import datetime, timedelta
    
    # 1. Helper function for Lua XOR/Base64 key obfuscation
    def obfuscate_key(start_time_str: str, end_time_str: str, key_value: str) -> str:
        raw_value = f"{start_time_str}|{end_time_str}|{key_value}"
        xor_seed = sum(ord(c) for c in key_value) % 256
        xored = bytes([ord(c) ^ ((xor_seed + i) % 256) for i, c in enumerate(raw_value)])
        b64 = base64.b64encode(xored).decode()
        chunks = [b64[i:i+4] for i in range(0, len(b64), 4)]
        lua_concat = "..".join([f'"{c}"' for c in chunks])
        lua_code = f'getgenv().Key="{key_value}";'
        return lua_code

    active_key = None
    start_time_dt = None
    end_time_dt = None

    try:
        # Load database
        key_db = load_key_db()
        free_keys_ips = load_free_keys_ips()
        custom_keys = key_db.setdefault("keys_custom_days", {})

        now = datetime.now()

        # 3. ถ้ามี grant token ที่ valid -> ดึงคีย์จาก grant record (ส่วนตัวต่อคน)
        if grant_ok and grant in grants:
            rec = grants[grant]
            active_key = rec.get("key")
            if rec.get("start_str") and rec.get("end_str"):
                try:
                    start_time_dt = datetime.strptime(rec["start_str"], "%d/%m/%Y/%H.%M")
                    end_time_dt = datetime.strptime(rec["end_str"], "%d/%m/%Y/%H.%M")
                except Exception:
                    start_time_dt = now
                    end_time_dt = now + timedelta(hours=6)
            else:
                start_time_dt = now
                end_time_dt = now + timedelta(hours=6)

        # 4. Fallback: ไม่มี grant (กรณีคนก๊อปปี้ลิงก์ success ดิบ) -> เช็ค IP แทน
        if not active_key:
            ip_record = free_keys_ips.get(user_ip)
            if ip_record:
                try:
                    created_at_dt = datetime.fromisoformat(ip_record["created_at"])
                    if now < created_at_dt + timedelta(hours=6):
                        existing_key = ip_record["key"]
                        if existing_key in custom_keys or existing_key in key_db.get("used_keys", {}):
                            active_key = existing_key
                            start_time_dt = created_at_dt
                            end_time_dt = created_at_dt + timedelta(hours=6)
                except Exception as ex:
                    print(f"[GetkeySuccess] IP Check Exception: {ex}")

        # 5. Generate new key if no active key found (fallback จาก IP หรือก๊อปปี้ลิงก์)
        if not active_key:
            active_key = f"VECTORHUBFREE-{secrets.token_hex(6).upper()}"
            start_time_dt = now
            end_time_dt = now + timedelta(hours=6)

            custom_keys[active_key] = 0.25
            free_keys_ips[user_ip] = {
                "key": active_key,
                "created_at": start_time_dt.isoformat()
            }

            if not save_key_db(key_db) or not save_free_keys_ips(free_keys_ips):
                return "เซิร์ฟเวอร์ยุ่งเกินไป กรุณารีเฟรชหน้านี้เพื่อรับคีย์อีกครั้ง", 500

    except Exception as e:
        print(f"[API GetkeySuccess] Error: {e}")
        return f"เกิดข้อผิดพลาดในการสร้างคีย์: {e}", 500

    # 6. Format key as Roblox script block
    start_str = start_time_dt.strftime("%d/%m/%Y/%H.%M")
    end_str = end_time_dt.strftime("%d/%m/%Y/%H.%M")

    # ── Send Webhook Log ──
    send_discord_log(
        title="🔑 Free Key Generated (6 Hrs)",
        description="มีการรับสคริปต์และสร้างคีย์ฟรี 6 ชั่วโมงผ่านหน้าเว็บแล้วค่ะ",
        color=0x22d3ee,
        fields=[
            {"name": "🔑 คีย์ที่ได้รับ", "value": f"`{active_key}`", "inline": True},
            {"name": "🌐 IP Address", "value": f"`{user_ip}`", "inline": True},
            {"name": "⏳ เวลาหมดอายุคีย์", "value": f"`{end_str}`", "inline": False}
        ]
    )
    
    # Generate the script (plaintext key ตามที่เจ้าของอนุญาตให้ลบ obfuscator)
    full_roblox_script = f'getgenv().Key="{active_key}";loadstring(game:HttpGet(\'{os.environ.get("SCRIPT_URL", RENDER_URL)}\'))()'

    expire_epoch = int(end_time_dt.timestamp() * 1000)

    # 6. Render HTML Page
    # Load HTML template + embed logo
    _tpl_path = os.path.join(BASE_DIR, "getkey_template.html")
    with open(_tpl_path, "r", encoding="utf-8") as _f:
        html_template = _f.read()
    _logo_path = os.path.join(BASE_DIR, "VectorHubLogo.png")
    _logo_b64 = base64.b64encode(
        open(_logo_path, "rb").read()
    ).decode() if os.path.exists(_logo_path) else ""
    html_template = html_template.replace("__LOGO_B64__", _logo_b64)
    html_template = html_template.replace("{roblox_script}", full_roblox_script).replace("{expire_epoch}", str(expire_epoch))

    return html_template


# ===== LOOTLABS POSTBACK (Advanced Anti-Bypass) =====
FREEKEY_PASSED_FILE = os.path.join(BASE_DIR, "freekey_passed.json")

def _load_passed() -> dict:
    """รายการ IP/unique_id ที่ผ่านโฆษณา Lootlabs แล้ว (จาก postback)"""
    try:
        if os.path.exists(FREEKEY_PASSED_FILE) and os.path.getsize(FREEKEY_PASSED_FILE) > 0:
            with open(FREEKEY_PASSED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[freekey_passed] load error: {e}")
    return {}

def _save_passed(data: dict) -> bool:
    try:
        tmp = FREEKEY_PASSED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, FREEKEY_PASSED_FILE)
        return True
    except Exception as e:
        print(f"[freekey_passed] save error: {e}")
        return False


@app.route('/api/lootlabs/postback', methods=['GET'])
def lootlabs_postback():
    # Lootlabs ยิงมาแบบ server-to-server พร้อม token ลับ + click_id/ip/unique_id
    token = request.args.get("token")
    if token != LOOTLABS_POSTBACK_TOKEN:
        return "invalid token", 403
    click_id = request.args.get("click_id", "")
    ip = request.args.get("ip", "")
    unique_id = request.args.get("unique_id", "")
    if not unique_id and not ip:
        return "missing params", 400
    passed = _load_passed()
    rec = {
        "click_id": click_id,
        "ip": ip,
        "ts": datetime.now().isoformat(),
    }
    if unique_id:
        passed[f"uid:{unique_id}"] = rec
    if ip:
        passed[f"ip:{ip}"] = rec
    _save_passed(passed)
    print(f"[Lootlabs Postback] passed unique_id={unique_id} ip={ip}")
    return "ok", 200


# ===== LOOTLABS ANTI-BYPASS — MINT ENDPOINT (per-user link) =====
# Token ลับสำหรับเรียก /api/lootlabs/mint (ป้องกันคนอื่น mint ลิ้งได้)
LOOTLABS_MINT_TOKEN = os.environ.get('LOOTLABS_MINT_TOKEN', 'VectorHubMint2026')
LOOTLABS_API_TOKEN = os.environ.get('LOOTLABS_API_TOKEN', '')  # ใส่ token จริงผ่าน env เท่านั้น
# Success URL ที่ Lootlabs จะเด้งกลับมาหลังดูโฆษณาจบ (หน้าเดียวกันรองรับทั้ง Discord+Loader)
SUCCESS_URL = os.environ.get(
    'FREEKEY_SUCCESS_URL',
    'https://vectorhub.space/api/getkey/success'
)
LOOTLABS_BASE_LINK = os.environ.get(
    'LOOTLABS_BASE_LINK',
    'https://links.lootlabs.gg/s?kNmrWrQM'
)
# ไฟล์เก็บ grant token (single-use, ต่อคน)
GRANT_FILE = os.path.join(BASE_DIR, 'freekey_grants.json')

def _load_grants() -> dict:
    try:
        if os.path.exists(GRANT_FILE) and os.path.getsize(GRANT_FILE) > 0:
            with open(GRANT_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[grants] load error: {e}")
    return {}

def _save_grants(data: dict) -> bool:
    try:
        tmp = GRANT_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, GRANT_FILE)
        return True
    except Exception as e:
        print(f"[grants] save error: {e}")
        return False

def _call_lootlabs_encryptor(destination_url: str) -> str | None:
    """เรียก LootLabs url_encryptor เพื่อเข้ารหัส destination_url -> ได้ &data="""
    if not LOOTLABS_API_TOKEN:
        print('[Lootlabs] ไม่มี LOOTLABS_API_TOKEN ไม่สามารถเข้ารหัสลิ้งได้')
        return None
    url = 'https://loot-link.com/api/url_encryptor'
    headers = {
        'Authorization': f'Bearer {LOOTLABS_API_TOKEN}',
        'Content-Type': 'application/json',
    }
    payload = {
        'destination_url': destination_url,
        'slug': LOOTLABS_BASE_LINK.split('?', 1)[0].split('/s?', 1)[0].rsplit('/', 1)[-1] if 'loot-link' in LOOTLABS_BASE_LINK or 'lootlabs' in LOOTLABS_BASE_LINK else 'kx',
    }
    try:
        if USE_REQUESTS:
            r = req_lib.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code == 200:
                d = r.json()
                return d.get('data') or d.get('encrypted_url') or d.get('url')
        else:
            import urllib.request, urllib.parse, json as _json
            req = urllib.request.Request(url, data=_json.dumps(payload).encode(), headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=15) as resp:
                d = _json.loads(resp.read().decode())
                return d.get('data') or d.get('encrypted_url') or d.get('url')
    except Exception as e:
        print(f'[Lootlabs] encryptor error: {e}')
    return None

@app.route('/api/lootlabs/mint', methods=['GET'])
def lootlabs_mint():
    """สร้างลิ้ง Lootlabs per-user (single-use ผ่าน grant token)
    สร้างคีย์ไว้ใน grant record ตั้งแต่ตอน mint เพื่อให้คีย์เป็นของส่วนตัวต่อคน
    (แม้อยู่ WiFi เดียวกันก็ได้คีย์คนละอัน เพราะ grant คนละ token)"""
    token = request.args.get('token')
    if token != LOOTLABS_MINT_TOKEN:
        return jsonify({'error': 'unauthorized'}), 403

    grant = secrets.token_urlsafe(32)
    # สร้างคีย์ฟรี 6 ชม. ไว้ใน grant record (ไม่ผูกกับ IP)
    new_key = f"VECTORHUBFREE-{secrets.token_hex(6).upper()}"
    now = datetime.now()
    start_str = now.strftime("%d/%m/%Y/%H.%M")
    end_str = (now + timedelta(hours=6)).strftime("%d/%m/%Y/%H.%M")
    grants = _load_grants()
    grants[grant] = {
        'used': False,
        'created_at': now.isoformat(),
        'key': new_key,
        'start_str': start_str,
        'end_str': end_str,
    }
    _save_grants(grants)

    dest = f'{SUCCESS_URL}?grant={grant}'
    enc = _call_lootlabs_encryptor(dest)
    if enc:
        # enc คือ data ที่เข้ารหัสแล้ว -> นำไปต่อกับ base link
        sep = '&' if '?' in LOOTLABS_BASE_LINK else '?'
        final_url = f'{LOOTLABS_BASE_LINK}{sep}data={enc}'
        return jsonify({'url': final_url}), 200
    # Fallback: ถ้าไม่มี API token ให้ส่ง dest ดิบ (ยังคงกันด้วย grant)
    return jsonify({'url': dest}), 200

@app.route('/getkey', methods=['GET'])
def getkey_web():
    """หน้าเว็บสำหรับ Loader กดปุ่ม Get Key -> เด้ง Lootlabs -> ได้คีย์"""
    token = LOOTLABS_MINT_TOKEN
    import urllib.parse
    mint_url = f'{request.host_url.rstrip("/")}/api/lootlabs/mint?token={urllib.parse.quote(token)}'
    html = f'''<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vector Hub — Get Key</title>
<link href="https://fonts.googleapis.com/css2?family=Kanit:wght@400;600;800&family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
<style>
body{{background:radial-gradient(circle at center,#0e1227 0%,#050712 100%);color:#e2e8f0;font-family:"Outfit","Kanit",sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0}}
.card{{background:rgba(10,12,22,0.75);backdrop-filter:blur(20px);border:1px solid rgba(56,182,255,0.2);border-radius:24px;padding:40px;width:90%;max-width:480px;text-align:center;box-shadow:0 20px 40px rgba(0,0,0,0.4)}}
h1{{font-size:1.8rem;margin-bottom:12px;color:#38b6ff}}
p{{color:#94a3b8;font-size:0.95rem;line-height:1.6;margin-bottom:24px}}
.btn{{display:inline-block;background:linear-gradient(135deg,#38b6ff 0%,#0a5adc 100%);border:none;padding:14px 32px;border-radius:12px;color:white;text-decoration:none;font-weight:600;font-size:1rem;transition:0.2s;cursor:pointer}}
.btn:hover{{transform:translateY(-2px);box-shadow:0 10px 20px rgba(56,182,255,0.3)}}
</style>
</head>
<body>
<div class="card">
<h1>🔑 Vector Hub — Get Key</h1>
<p>กดปุ่มด้านล่างเพื่อรับคีย์ฟรี คุณจะถูกนำไปยังโฆษณาสั้น ๆ จากนั้นระบบจะเด้งกลับมาพร้อมคีย์ของคุณ</p>
<button class="btn" onclick="startGetKey()">🟡 รับคีย์ฟรี (Lootlabs)</button>
</div>
<script>
async function startGetKey(){{
  try{{
    const r = await fetch('{mint_url}');
    const d = await r.json();
    if(d.url){{ window.location.href = d.url; }}
    else {{ alert('ไม่สามารถสร้างลิ้งได้ในขณะนี้'); }}
  }}catch(e){{ alert('เกิดข้อผิดพลาด: '+e); }}
}}
</script>
</body>
</html>'''
    return html


if __name__ == '__main__':
    # WEB_ONLY=1 => run as web server only (no bot autostart).
    # Discord Hosting / PaaS injects PORT env; fall back to 5000 for local/run.py.
    # On Discord Hosting, set WEB_ONLY=1 so the web process never tries to
    # spawn bot subprocesses (which would crash startup => Bad Gateway).
    if os.environ.get("WEB_ONLY") != "1":
        try:
            autostart_bots()
        except Exception as _ab_e:
            print(f"[app] autostart_bots skipped: {_ab_e}")
    # Discord Hosting / PaaS injects PORT env; fall back to 5000 for local/run.py.
    _port = int(os.environ.get("PORT", "5000"))
    app.run(host='0.0.0.0', port=_port)
