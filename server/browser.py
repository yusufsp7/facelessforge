"""Central Chrome resolution — one answer for generator, posters, and setup.

Priority (mirrors what actually exists on the target VM):
  1. FORGE_CHROME / CHROME env (explicit override)
  2. puppeteer cache: ~/.cache/puppeteer/chrome/*/chrome-linux64/chrome
     (Chrome for Testing — proven to work for capture AND Playwright)
  3. platform-standard installs (Windows paths, /usr/bin/*, snap)

Returns the path or None. ``launch_env()`` additionally returns the env
extras capture.mjs needs (CHROME + NO_PROXY so skill python httpx never
routes localhost through a proxy).
"""
import glob
import os
import socket
import sys
import threading
import time


def find_chrome() -> str | None:
    env = os.environ.get("FORGE_CHROME") or os.environ.get("CHROME")
    if env and os.path.isfile(env):
        return env
    home = os.path.expanduser("~")
    for pat in (os.path.join(home, ".cache", "puppeteer", "chrome", "*",
                             "chrome-linux64", "chrome"),
                os.path.join(home, ".cache", "puppeteer", "chrome", "*",
                             "chrome-linux", "chrome")):
        hits = sorted(glob.glob(pat), reverse=True)
        if hits:
            return hits[0]
    if sys.platform.startswith("win"):
        for cand in (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                     r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                     r"%LocalAppData%\Google\Chrome\Application\chrome.exe"):
            p = os.path.expandvars(cand)
            if os.path.isfile(p):
                return p
    else:
        for cand in ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
                     "/usr/bin/chromium", "/usr/bin/chromium-browser",
                     "/snap/bin/chromium"):
            if os.path.isfile(cand):
                return cand
    return shutil_which_chrome()


def shutil_which_chrome() -> str | None:
    import shutil
    return shutil.which("chrome") or shutil.which("google-chrome")


def launch_env() -> dict:
    """Env extras for capture.mjs / skill processes on this machine."""
    env = {}
    chrome = find_chrome()
    if chrome:
        env["CHROME"] = chrome
    # skill python (httpx/urllib) must bypass any proxy for localhost
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["no_proxy"] = "localhost,127.0.0.1"
    return env


def _env_proxy_url() -> str | None:
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
              "all_proxy", "ALL_PROXY"):
        raw = os.environ.get(k)
        if raw:
            return raw
    return None


def proxy_options() -> dict | None:
    """R2: Chromium vs proxy egress tertentu gagal nego langsung (TUNNEL
    failed) walau curl/git/raw-socket sukses. Terbukti: relay lokal
    (Chromium → 127.0.0.1:port tanpa auth → raw CONNECT ke proxy env,
    dibaca ulang tiap koneksi — kredensial boleh rotasi). Default semua
    launch lewat relay; FORGE_PROXY_DIRECT=1 memaksa koneksi langsung."""
    import urllib.parse
    raw = _env_proxy_url()
    if not raw:
        return None
    if os.environ.get("FORGE_PROXY_DIRECT") == "1":
        p = urllib.parse.urlparse(raw if "://" in raw else "http://" + raw)
        proxy = {"server": f"{p.scheme or 'http'}://{p.hostname}:{p.port or 80}"}
        if p.username:
            proxy["username"] = urllib.parse.unquote(p.username)
        if p.password:
            proxy["password"] = urllib.parse.unquote(p.password)
        return proxy
    return {"server": f"http://127.0.0.1:{_chain_relay().port}"}


class _ChainRelay:
    """HTTP CONNECT chain proxy lokal: browser tanpa auth → raw CONNECT ke
    proxy env (dibaca ulang per koneksi)."""

    def __init__(self):
        self.port = 0
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(16)
        threading.Thread(target=self._serve, daemon=True,
                         name="forge-chain-proxy").start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,),
                             daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        import urllib.parse
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = client.recv(4096)
                if not chunk:
                    return
                req += chunk
            parts = req.split(b"\r\n", 1)[0].decode("latin-1").split()
            if parts[0] != "CONNECT":
                client.sendall(b"HTTP/1.1 400 only CONNECT supported\r\n\r\n")
                return
            host_port = parts[1]
            raw = _env_proxy_url()
            if not raw:
                h, _, p = host_port.partition(":")
                up = socket.create_connection((h, int(p or 443)), timeout=30)
            else:
                p = urllib.parse.urlparse(
                    raw if "://" in raw else "http://" + raw)
                up = socket.create_connection(
                    (p.hostname, p.port or 80), timeout=30)
                auth = ""
                if p.username:
                    import base64
                    cred = (f"{urllib.parse.unquote(p.username)}:"
                            f"{urllib.parse.unquote(p.password or '')}")
                    auth = ("Proxy-Authorization: Basic "
                            + base64.b64encode(cred.encode()).decode()
                            + "\r\n")
                up.sendall((f"CONNECT {host_port} HTTP/1.1\r\n"
                            f"Host: {host_port}\r\n{auth}\r\n").encode())
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = up.recv(4096)
                    if not chunk:
                        raise ConnectionError("proxy tutup koneksi")
                    resp += chunk
                if b" 200 " not in resp.split(b"\r\n")[0]:
                    client.sendall(
                        b"HTTP/1.1 502 upstream CONNECT failed\r\n\r\n")
                    return
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self._pipe(client, up)
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 tunnel error\r\n\r\n")
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        import select
        socks = [a, b]
        while True:
            ready, _, _ = select.select(socks, [], [], 10)
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


_chain_relay_obj: _ChainRelay | None = None
_chain_lock = threading.Lock()


def _chain_relay() -> _ChainRelay:
    global _chain_relay_obj
    with _chain_lock:
        if _chain_relay_obj is None:
            _chain_relay_obj = _ChainRelay()
        return _chain_relay_obj


def ignore_cert_errors() -> bool:
    return os.environ.get("FORGE_IGNORE_CERT_ERRORS") == "1"


def maybe_install_ca() -> str:
    """FORGE_CA_FILE: proxy MITM dengan CA privat — Chrome memakai store
    OS/NSS; coba pasang via certutil (best-effort, log hasil)."""
    ca = os.environ.get("FORGE_CA_FILE")
    if not ca or not os.path.isfile(ca):
        return ""
    nssdb = os.path.expanduser("~/.pki/nssdb")
    os.makedirs(nssdb, exist_ok=True)
    import subprocess
    p = subprocess.run(
        ["certutil", "-d", f"sql:{nssdb}", "-A", "-t", "CT,C,C",
         "-n", "forge-proxy-ca", "-i", ca],
        capture_output=True, text=True, timeout=30)
    if p.returncode == 0:
        return f"CA terpasang ke NSS ({nssdb})"
    return (f"certutil gagal ({(p.stderr or p.stdout)[-120:]}): pasang CA "
            "manual ke store OS/NSS")
