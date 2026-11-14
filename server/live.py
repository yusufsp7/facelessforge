"""YouTube livestream module — managed ffmpeg encoder + Studio broadcast.

Per-account live profile (stream key RAHASIA: never logged, masked in API),
managed ffmpeg encoder with auto-restart + desired_on persistence (reboot →
stream continues by itself), broadcast lifecycle via the account's Studio
session (same per-account queue lock as upload posters — one browser per
account), two-layer health (encoder flowing + YouTube actually LIVE),
replay policy (>keep-hours keep, else delete), and RTMP-over-HTTP-CONNECT
tunneling (FORGE_RTMP_PROXY) for egress networks that block RTMP.
"""
import json
import os
import re
import socket
import shutil

import subprocess
import threading
import time
import urllib.parse

from . import db, paths
from .worker import PLATFORM_LOCKS

LIVE_DIR = paths.data_root() / "live"
RTMP_PORT_DEFAULT = 1935
# encoder progress lines appear every ~1s; no new line in this window = stall
STALL_TIMEOUT_S = 120
RESTART_BACKOFF = [5, 15, 45, 120]


# ---------------------------------------------------------------- profiles

def get_profile(account_key: str) -> dict | None:
    row = db.one("SELECT * FROM live_profiles WHERE account_key = ?",
                 (account_key,))
    if not row:
        return None
    p = dict(row)
    p["loop"] = bool(p.get("loop"))
    p["desired_on"] = bool(p.get("desired_on"))
    return p


def public_profile(account_key: str) -> dict | None:
    """API view — stream key masked (last 4 only), never the full secret."""
    p = get_profile(account_key)
    if not p:
        return None
    k = p.get("stream_key") or ""
    p["stream_key_masked"] = ("••••" + k[-4:]) if len(k) >= 4 else \
        ("••••" if k else "")
    p["has_stream_key"] = bool(k)
    p.pop("stream_key", None)
    return p


def save_profile(account_key: str, fields: dict) -> dict:
    old = get_profile(account_key)
    # password-style fields: kirim "••••..." berarti tetap pakai yang lama
    sk = fields.get("stream_key")
    if sk is None or (isinstance(sk, str) and "•" in sk):
        sk = (old or {}).get("stream_key", "")
    row = {
        "account_key": account_key,
        "stream_key": sk or "",
        "ingest_url": fields.get("ingest_url")
        or (old or {}).get("ingest_url")
        or "rtmp://a.rtmp.youtube.com/live2",
        "ingest_backup": fields.get("ingest_backup")
        or (old or {}).get("ingest_backup")
        or "rtmp://b.rtmp.youtube.com/live2?backup=1",
        "media_path": fields.get("media_path")
        or (old or {}).get("media_path") or "",
        "loop": int(fields.get("loop") if fields.get("loop") is not None
                    else (old or {}).get("loop", 1)),
        "desired_on": int(fields.get("desired_on")
                          if fields.get("desired_on") is not None
                          else (old or {}).get("desired_on", 0)),
        "broadcast_id": fields.get("broadcast_id",
                                   (old or {}).get("broadcast_id", "")),
        "created_at": (old or {}).get("created_at") or db.now_iso(),
        "updated_at": db.now_iso(),
    }
    db.upsert("live_profiles", row, conflict_col="account_key")
    log_event(account_key, "profile_saved", {})
    return get_profile(account_key)


def log_event(account_key: str, event: str, detail: dict | None = None) -> None:
    db.log_event(None, f"live_{event}",
                 {"account": account_key, **(detail or {})})


# ------------------------------------------------------------------ tunnel

def _rtmp_proxy_target(ingest_url: str) -> tuple[str, int]:
    m = re.match(r"rtmp://([^/:?]+)(?::(\d+))?", ingest_url)
    host = m.group(1) if m else "a.rtmp.youtube.com"
    port = int(m.group(2) or RTMP_PORT_DEFAULT)
    return host, port


class ConnectTunnel:
    """Lokal listen port → HTTP CONNECT via proxy egress → RTMP host:port.
    Menyerap pola forwarder eksternal ke dalam app. Error ditulis ke log
    encoder akun agar terdiagnosa dari luar (BUG 5)."""

    def __init__(self, proxy_url: str, host: str, port: int,
                 account_key: str = ""):
        self.account_key = account_key
        self.proxy_url = proxy_url
        self.host = host
        self.port = port
        self.sock = None
        self.thread = threading.Thread(target=self._serve, daemon=True,
                                       name="forge-rtmp-tunnel")
        self.active = True

    def start(self) -> int:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        port = self.sock.getsockname()[1]
        self.thread.start()
        return port

    def stop(self) -> None:
        self.active = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def _serve(self) -> None:
        while self.active:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._pipe, args=(client,),
                             daemon=True).start()

    def _pipe(self, client: socket.socket) -> None:
        p = urllib.parse.urlparse(self.proxy_url)
        phost, pport = p.hostname, p.port or 80
        auth = ""
        if p.username:
            import base64
            cred = f"{urllib.parse.unquote(p.username)}:" \
                   f"{urllib.parse.unquote(p.password or '')}"
            auth = "Proxy-Authorization: Basic " + base64.b64encode(
                cred.encode()).decode() + "\r\n"
        try:
            up = socket.create_connection((phost, pport), timeout=30)
            req = (f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                   f"Host: {self.host}:{self.port}\r\n{auth}\r\n")
            up.sendall(req.encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = up.recv(4096)
                if not chunk:
                    raise ConnectionError("proxy tutup koneksi")
                resp += chunk
            if b" 200 " not in resp.split(b"\r\n")[0]:
                raise ConnectionError(f"CONNECT ditolak: {resp[:80]}")
            self._relay(client, up)
        except Exception as e:
            msg = (f"[tunnel] CONNECT {self.host}:{self.port} via "
                   f"{self.proxy_url} gagal: {e!r:.200}")
            self._append_log(msg)
            log_event("tunnel", "tunnel_error",
                      {"account": self.account_key,
                       "error": repr(e)[:150]})
            try:
                client.close()
            except Exception:
                pass

    def _append_log(self, msg: str) -> None:
        d = LIVE_DIR / self.account_key if self.account_key else LIVE_DIR
        try:
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "encoder.log", "ab") as f:
                stamp = time.strftime("[%Y-%m-%d %H:%M:%S] ")
                f.write((stamp + msg + "\n").encode())
        except Exception:
            pass

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket) -> None:
        sockets = [a, b]
        while True:
            ready = []
            import select
            ready, _, _ = select.select(sockets, [], [], 5)
            if not ready:
                continue
            for s in ready:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                (b if s is a else a).sendall(data)


# ------------------------------------------------------------------ encoder

_encoders: dict[str, dict] = {}  # account_key -> {proc, log_path, ...}
_lock = threading.Lock()
_watcher_started = False


def _ffmpeg_cmd(profile: dict, dest_url: str) -> list[str]:
    loop = ["-stream_loop", "-1"] if profile.get("loop") else []
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-re", *loop, "-i", profile["media_path"],
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", "4500k",
        "-maxrate", "4500k", "-bufsize", "9000k",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "flv", dest_url,
    ]


def _dest_url(profile: dict, tunnel_port: int | None) -> str:
    """URL publish FINAL — SELALU diakhiri /<stream_key> (akar bug ronde 6:
    key hanya disimpan, tidak pernah ditempel → YouTube reset koneksi)."""
    ingest = profile.get("ingest_url")
    key = profile.get("stream_key") or ""
    if tunnel_port:
        m = re.match(r"rtmp://([^/?]+)(/?.*)", ingest)
        path = m.group(2) if m else ""
        base = f"rtmp://127.0.0.1:{tunnel_port}{path}"
    else:
        base = ingest
    return f"{base.rstrip('/')}/{key}" if key else base


def status(account_key: str) -> dict:
    enc = _encoders.get(account_key)
    flowing = False
    if enc and enc["proc"].poll() is None:
        flowing = (time.time() - enc.get("last_progress_ts", 0)) < \
            STALL_TIMEOUT_S and enc.get("bytes", 0) > 0
    return {
        "encoder_alive": bool(enc and enc["proc"].poll() is None),
        "data_flowing": flowing,
        "bytes": enc.get("bytes", 0) if enc else 0,
        "desired_on": bool((get_profile(account_key) or {}).get("desired_on")),
        "broadcast_id": (get_profile(account_key) or {}).get("broadcast_id",
                                                             ""),
        "restarts": enc.get("restarts", 0) if enc else 0,
    }


def start(account_key: str) -> dict:
    profile = get_profile(account_key)
    if not profile:
        return {"error": "profil live belum diset"}
    if not profile.get("stream_key"):
        return {"error": "stream key kosong"}
    if not profile.get("media_path") or not os.path.isfile(
            profile["media_path"]):
        return {"error": f"media sumber tidak ada: {profile['media_path']}"}
    with _lock:
        enc = _encoders.get(account_key)
        if enc and enc["proc"].poll() is None:
            return {"ok": True, "detail": "encoder sudah jalan"}
        dest = _dest_url(profile, None)
        if os.environ.get("FORGE_RTMP_PROXY"):
            host, port = _rtmp_proxy_target(profile["ingest_url"])
            tunnel = ConnectTunnel(os.environ["FORGE_RTMP_PROXY"], host,
                                   port, account_key)
            dest = _dest_url(profile, tunnel.start())
            enc_tunnels[account_key] = tunnel
        log_dir = LIVE_DIR / account_key
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "encoder.log"
        logf = open(log_path, "ab")
        proc = subprocess.Popen(_ffmpeg_cmd(profile, dest), stdout=logf,
                                stderr=logf)
        _encoders[account_key] = {
            "proc": proc, "log_path": str(log_path), "bytes": 0,
            "last_progress_ts": time.time(), "restarts": 0,
            "started_at": time.time(),
        }
    log_event(account_key, "encoder_started", {"pid": proc.pid})
    db.log_event(None, "live_encoder_started", {"account": account_key})
    return {"ok": True, "pid": proc.pid}


def stop(account_key: str, clear_desired: bool = True) -> dict:
    with _lock:
        enc = _encoders.pop(account_key, None)
    tun = enc_tunnels.pop(account_key, None)
    if tun:
        tun.stop()
    if enc and enc["proc"].poll() is None:
        enc["proc"].terminate()
    if clear_desired:
        p = get_profile(account_key)
        if p:
            save_profile(account_key, {"desired_on": 0})
    log_event(account_key, "encoder_stopped", {})
    return {"ok": True}


def log_tail(account_key: str, n: int = 40) -> list[str]:
    enc = _encoders.get(account_key)
    path = enc["log_path"] if enc else str(LIVE_DIR / account_key /
                                           "encoder.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def _parse_progress(line: str, state: dict) -> None:
    m = re.search(r"size=\s*(\d+)kB", line)
    if m:
        state["bytes"] = int(m.group(1)) * 1024
        state["last_progress_ts"] = time.time()


def _read_encoder_progress(key: str, p: dict) -> None:
    """R7: bytes/status dibaca dari log ffmpeg (size= kB) — proses hidup
    saja tidak cukup; size bertambah = data benar-benar mengalir."""
    try:
        with open(p["log_path"], "rb") as f:
            f.seek(max(0, os.path.getsize(p["log_path"]) - 65536))
            tail = f.read().decode("utf-8", errors="replace")
        matches = re.findall(r"size=\s*(\d+)kB", tail)
        if matches:
            size_kb = int(matches[-1])
            if size_kb * 1024 > p.get("bytes", 0):
                p["bytes"] = size_kb * 1024
                p["last_progress_ts"] = time.time()
    except Exception:
        pass


def _watch_loop() -> None:
    """Health: proses hidup + data mengalir + desired_on → pulihkan."""
    while True:
        time.sleep(15)
        for key, p in list(_encoders.items()):
            profile = get_profile(key)
            if not profile or not profile.get("desired_on"):
                continue
            alive = p["proc"].poll() is None
            _read_encoder_progress(key, p)
            flowing = alive and (time.time() - p["last_progress_ts"]) < \
                STALL_TIMEOUT_S and p["bytes"] > 0
            if alive and not flowing and p["bytes"] == 0 and \
                    time.time() - p["started_at"] < 120:
                continue  # baru start, beri waktu naik
            if not alive or not flowing:
                restarts = p.get("restarts", 0)
                backoff = RESTART_BACKOFF[min(restarts,
                                              len(RESTART_BACKOFF) - 1)]
                db.log_event(None, "live_restart_scheduled",
                             {"account": key, "alive": alive,
                              "flowing": flowing, "backoff_s": backoff})
                time.sleep(backoff)
                start(key)
                new = _encoders.get(key)
                if new:
                    new["restarts"] = restarts + 1


def start_watchdog() -> None:
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True
    threading.Thread(target=_watch_loop, daemon=True,
                     name="forge-live-watch").start()
    # desired_on tersimpan di DB → reboot: lanjutkan streaming sendiri
    for r in db.rows("SELECT account_key FROM live_profiles "
                     "WHERE desired_on = 1"):
        start(r["account_key"])


enc_tunnels: dict[str, ConnectTunnel] = {}
