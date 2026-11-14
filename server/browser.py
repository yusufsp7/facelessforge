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
import sys


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


def proxy_options() -> dict | None:
    """Baca ulang env setiap launch (kredensial proxy bisa berotasi).
    Mendukung https_proxy/HTTPS_PROXY/HTTP_PROXY/ALL_PROXY lengkap dengan
    username:password di URL-nya."""
    import urllib.parse
    raw = None
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
              "all_proxy", "ALL_PROXY"):
        raw = os.environ.get(k)
        if raw:
            break
    if not raw:
        return None
    p = urllib.parse.urlparse(raw if "://" in raw else "http://" + raw)
    proxy = {"server": f"{p.scheme or 'http'}://{p.hostname}:{p.port or 80}"}
    if p.username:
        proxy["username"] = urllib.parse.unquote(p.username)
    if p.password:
        proxy["password"] = urllib.parse.unquote(p.password)
    return proxy


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
