"""Threads poster (web composer, post-now only in v1).

Threads' composer is the same React stack Instagram uses: a dialog with a
contenteditable textbox and an optional file input for media.
"""
import time

from .base import BasePoster, UploadError, UploadResult
from .tiktok import build_caption

LOGIN_CHECK = (
    "(!document.querySelector('input[name=\"username\"]')) && "
    "(!!document.querySelector('a[href^=\"/inbox\"], "
    "[aria-label*=\"Balasan\"], [aria-label*=\"Replies\"]'))"
)
FILE_INPUT = "input[type='file']"
TEXTBOX = "div[role='dialog'] div[contenteditable='true']"


class ThreadsPoster(BasePoster):
    platform = "threads"
    upload_url = "https://www.threads.net/"

    def login_check_js(self) -> str:
        return LOGIN_CHECK

    def upload(self, *, asset_path, asset_kind, title, description, tags,
               schedule_at, visibility, progress_cb) -> UploadResult:
        self._preflight(asset_path)
        if schedule_at:
            raise UploadError("schedule",
                              "Threads web tidak punya scheduling — "
                              "gunakan jadwal worker (post saat waktu tiba)")
        cap = build_caption(title, description, tags)
        if asset_kind not in ("text", "video", "image"):
            raise UploadError("preflight",
                              f"Threads: jenis asset {asset_kind} tak didukung")
        if asset_kind == "text" and not cap:
            raise UploadError("preflight", "Threads: caption kosong")
        from .base import PlaywrightSession
        with self._session() as sess:
            progress_cb(5)
            try:
                sess.navigate(self.upload_url)
            except Exception:
                raise UploadError("preflight", "Threads: gagal buka threads.net")
            if not sess.wait_for_selector(
                    "a[href^='/inbox'], [aria-label*='Balasan']", 30_000):
                raise UploadError("session",
                                  "Threads: tidak login — Hubungkan akun dulu")
            progress_cb(10)
            if not (sess.click_by_text("Post", match_prefix=True,
                                       timeout_s=4)
                    or sess.click_by_text("Posting", match_prefix=True,
                                          timeout_s=4)
                    or sess.click_by_text("Create", match_prefix=True,
                                          timeout_s=4)
                    or sess.click_by_text("Buat", match_prefix=True,
                                          timeout_s=4)):
                # compose URL fallback
                sess.navigate("https://www.threads.net/intent/post?text="
                              "&plain=1")
            if not sess.wait_for_selector(TEXTBOX, 20_000):
                raise UploadError("dialog",
                                  "Threads: composer tidak muncul")
            progress_cb(30)
            sess.click(TEXTBOX)
            sess.insert_text(cap)
            if asset_path and asset_kind in ("video", "image"):
                if sess.wait_for_selector(FILE_INPUT, 8_000):
                    sess.upload_file(FILE_INPUT, asset_path)
                    progress_cb(60)
                    time.sleep(4)
            progress_cb(80)
            submitted = False
            for label in ("Post", "Posting", "Share", "Bagikan"):
                hit = sess.click_by_text(label, match_prefix=True,
                                         timeout_s=4)
                if hit:
                    submitted = True
                    break
            if not submitted:
                sess.page.keyboard.press("Control+Enter")
            progress_cb(90)
            time.sleep(4)
            if not sess.wait_for_selector("div[role='dialog']", 30_000):
                progress_cb(100)
                return UploadResult(ok=True, note="posted (composer closed)")
            raise UploadError("publish",
                              "Threads: composer masih terbuka — status "
                              "tidak pasti")
