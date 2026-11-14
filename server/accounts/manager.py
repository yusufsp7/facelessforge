"""Account manager — the AutoClip-style login core.

Login = Playwright ``launch_persistent_context`` pointing at the user's REAL
installed Chrome with one on-disk profile dir per account. The user logs in
manually in the visible browser; we poll ``login_check_js`` every few seconds
until the logged-in marker shows up, then close Chrome — cookies stay in the
profile dir, so the session survives restarts and later uploads never need an
API key.

Threading: Playwright's sync API is thread-affine. Every public method here is
called from its own thread (FastAPI endpoint thread / upload worker thread)
and opens ``sync_playwright()`` fresh, so no object ever crosses threads.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid

from .. import db, paths
from . import platforms

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
if (navigator.plugins && navigator.plugins.length === 0) {
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
}
if (navigator.languages && navigator.languages.length === 0) {
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
}
"""

LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--disable-features=Translate",
    "--disable-notifications",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1320,900",
    "--lang=en-US",
]

if sys.platform.startswith("linux"):
    # VMs/docker have tiny /dev/shm → Chrome tabs crash without this; and
    # most cloud VMs run as root, where Chrome refuses to start.
    LAUNCH_ARGS += ["--disable-dev-shm-usage"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        LAUNCH_ARGS += ["--no-sandbox"]



LOGIN_TIMEOUT_S = 600
LOGIN_POLL_S = 3.0


def find_chrome() -> str | None:
    """Single source of truth lives in server.browser (env override →
    puppeteer Chrome-for-Testing cache → standard installs)."""
    from .. import browser as _browser
    return _browser.find_chrome()


class AccountError(Exception):
    pass


class AccountManager:
    """Owns connect/probe/disconnect lifecycle for all accounts."""

    def __init__(self):
        # in-flight connect states for UI observability
        # key -> {platform, started_at, status, detail, elapsed}
        self.connecting: dict[str, dict] = {}
        self._conn_lock = threading.Lock()

    def _set_connecting(self, key: str, **fields) -> None:
        with self._conn_lock:
            st = self.connecting.setdefault(key, {})
            st.update(fields)
            if "started_at" in st:
                st["elapsed"] = int(time.time() - st["started_at"])

    def connect_status(self) -> list[dict]:
        with self._conn_lock:
            out = []
            for key, st in self.connecting.items():
                out.append({"key": key, **st})
            return out

    # -- profile helpers -------------------------------------------------

    def profile_dir(self, platform: str, key: str) -> str:
        safe = key.replace(":", "_").replace("/", "_").replace("\\", "_")
        d = paths.profiles_dir() / platform / safe
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def new_key(self, platform: str, label: str | None = None) -> str:
        existing = db.rows(
            "SELECT key FROM accounts WHERE platform = ?", (platform,))
        if not existing:
            return f"{platform}:default"
        return f"{platform}:{uuid.uuid4().hex[:6]}"

    # -- launch ----------------------------------------------------------

    def _launch(self, pw, url: str, profile: str, headless: bool = False):
        """Open a persistent Chrome context. Returns (context, page)."""
        exe = find_chrome()
        kwargs = dict(
            user_data_dir=profile,
            headless=headless,
            args=LAUNCH_ARGS,
            viewport=None,  # use --window-size, real chrome window
        )
        if exe:
            kwargs["executable_path"] = exe
        else:
            kwargs["channel"] = "chrome"
        context = pw.chromium.launch_persistent_context(**kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script(STEALTH_JS)
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        return context, page

    # -- public API ------------------------------------------------------

    def connect(self, platform: str, key: str | None = None,
                label: str | None = None, headless: bool = False,
                timeout_s: int = LOGIN_TIMEOUT_S) -> dict:
        """Blocking login flow. Call from a worker thread, not an event loop."""
        spec = platforms.get(platform)
        key = key or self.new_key(platform, label)
        profile = self.profile_dir(platform, key)
        self._set_connecting(key, platform=platform, status="opening_chrome",
                             detail="Membuka Chrome…", started_at=time.time())

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context, page = self._launch(pw, spec.login_url, profile, headless)
            relaunches = 0
            try:
                self._set_connecting(key, status="waiting_login",
                                     detail=f"Login {spec.name} di jendela "
                                            "Chrome yang terbuka…")
                deadline = time.monotonic() + timeout_s
                logged_in = False
                while time.monotonic() < deadline:
                    self._set_connecting(key, elapsed=int(
                        time.time() - self.connecting.get(key, {}).get(
                            "started_at", time.time())))
                    # evaluate the marker on EVERY open tab: the user may
                    # complete login in a different tab than pages[0]
                    pages = list(context.pages)
                    for p in pages:
                        try:
                            if p.evaluate(spec.login_check_js):
                                logged_in = True
                                page = p
                                break
                        except Exception:
                            continue
                    if logged_in:
                        break
                    if not pages:  # every tab closed
                        try:
                            page = context.new_page()
                            page.add_init_script(STEALTH_JS)
                            page.goto(spec.login_url,
                                      wait_until="domcontentloaded",
                                      timeout=60_000)
                        except Exception:
                            # the user closed the whole Chrome window — the
                            # browser process is gone. Relaunch on the same
                            # profile so a stray close doesn't kill the flow.
                            if relaunches >= 5:
                                raise AccountError(
                                    "Chrome ditutup berkali-kali. Klik "
                                    "Hubungkan lagi saat siap login.")
                            relaunches += 1
                            self._set_connecting(
                                key, status="relaunching",
                                detail="Chrome tertutup — membuka ulang…")
                            context, page = self._launch(
                                pw, spec.login_url, profile, headless)
                            self._set_connecting(
                                key, status="waiting_login",
                                detail=f"Login {spec.name} di jendela "
                                       "Chrome yang terbuka…")
                    time.sleep(LOGIN_POLL_S)
                if not logged_in:
                    raise AccountError(
                        f"Login {spec.name} tidak terdeteksi dalam "
                        f"{timeout_s}s. Selesaikan login di jendela Chrome "
                        "sebelum waktu habis, lalu klik Hubungkan lagi.")

                self._set_connecting(key, status="saving",
                                     detail="Login terdeteksi — menyimpan…")
                account_name, channel_id, handle = "", "", ""
                try:
                    page.goto(spec.probe_url, wait_until="domcontentloaded",
                              timeout=60_000)
                    time.sleep(2)
                    account_name = page.evaluate(spec.account_name_js) or ""
                except Exception:
                    pass
                if platform == "youtube":
                    # capture channel id/handle from Studio URL (never via
                    # avatar switching — that dialog is forbidden)
                    try:
                        page.goto("https://studio.youtube.com",
                                  wait_until="domcontentloaded", timeout=60_000)
                        time.sleep(3)
                        m = re.search(r"studio\.youtube\.com/channel/"
                                      r"(UC[A-Za-z0-9_-]+)", page.url)
                        channel_id = m.group(1) if m else ""
                        handle = account_name
                    except Exception:
                        pass

                db.upsert("accounts", {
                    "key": key,
                    "platform": platform,
                    "label": label or account_name or key,
                    "profile_key": key.replace(":", "_"),
                    "account_name": account_name,
                    "connected": 1,
                    "channel_id": channel_id,
                    "handle": handle,
                    "created_at": db.now_iso(),
                })
                db.log_event(None, "account_connected",
                             {"platform": platform, "key": key})
                with self._conn_lock:
                    self.connecting.pop(key, None)
                return {"key": key, "platform": platform,
                        "account_name": account_name}
            except AccountError:
                self._set_connecting(key, status="failed",
                                     detail=f"Timeout login {spec.name} — "
                                            "klik Hubungkan lagi")
                raise
            except Exception as e:
                self._set_connecting(key, status="failed",
                                     detail=f"Gagal membuka Chrome: {e!r:.150}")
                raise
            finally:
                context.close()

    def is_connected(self, key: str) -> tuple[bool, str]:
        """Cheap probe: open the platform's probe URL and run the check."""
        acct = db.one("SELECT * FROM accounts WHERE key = ?", (key,))
        if not acct or not acct["connected"]:
            return False, "akun belum dihubungkan"
        if not os.path.isdir(self.profile_dir(acct["platform"], key)):
            return False, "profil browser hilang"
        spec = platforms.get(acct["platform"])
        from playwright.sync_api import sync_playwright
        profile = self.profile_dir(acct["platform"], key)
        with sync_playwright() as pw:
            context, page = self._launch(pw, spec.probe_url, profile,
                                         headless=True)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        if page.evaluate(spec.login_check_js):
                            return True, ""
                    except Exception:
                        pass
                    time.sleep(1.0)
                url = page.url
                return False, f"sesi kedaluwarsa (di {url[:80]})"
            finally:
                context.close()

    def disconnect(self, key: str) -> None:
        db.update("accounts", key, {"connected": 0}, key_col="key")

    def wipe(self, key: str) -> None:
        acct = db.one("SELECT * FROM accounts WHERE key = ?", (key,))
        if acct:
            shutil.rmtree(self.profile_dir(acct["platform"], key),
                          ignore_errors=True)
        db.connect().close()
        conn = db.connect()
        try:
            conn.execute("DELETE FROM accounts WHERE key = ?", (key,))
            conn.commit()
        finally:
            conn.close()
        db.log_event(None, "account_wiped", {"key": key})

    def import_cookies(self, key: str, cookies: list[dict]) -> None:
        """Write cookies into the persistent profile without opening Chrome."""
        acct = db.one("SELECT * FROM accounts WHERE key = ?", (key,))
        if not acct:
            raise AccountError(f"akun {key} tidak ada")
        spec = platforms.get(acct["platform"])
        profile = self.profile_dir(acct["platform"], key)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context, page = self._launch(pw, spec.probe_url, profile,
                                         headless=True)
            try:
                context.add_cookies(cookies)
                page.reload(wait_until="domcontentloaded")
                time.sleep(3)
                ok = False
                try:
                    ok = bool(page.evaluate(spec.login_check_js))
                except Exception:
                    pass
                if not ok:
                    raise AccountError(
                        "cookies diimpor tapi login belum valid — cek ulang "
                        "cookiesnya (perlu cookies akun + sesi platform).")
                db.update("accounts", key,
                          {"connected": 1, "account_name": acct["account_name"] or key},
                          key_col="key")
            finally:
                context.close()

    def export_cookies(self, key: str) -> list[dict]:
        acct = db.one("SELECT * FROM accounts WHERE key = ?", (key,))
        if not acct:
            raise AccountError(f"akun {key} tidak ada")
        profile = self.profile_dir(acct["platform"], key)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context, _ = self._launch(pw, "", profile, headless=True)
            try:
                return context.cookies()
            finally:
                context.close()

    def list_accounts(self) -> list[dict]:
        out = []
        for r in db.rows("SELECT * FROM accounts ORDER BY platform, key"):
            out.append({
                "key": r["key"],
                "platform": r["platform"],
                "label": r["label"],
                "account_name": r["account_name"],
                "connected": bool(r["connected"]),
                "channel_id": r.get("channel_id") or "",
                "handle": r.get("handle") or "",
            })
        return out


MANAGER = AccountManager()
