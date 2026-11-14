"""Instagram poster (web composer, post-now only in v1).

Instagram web has no native scheduling, so this poster posts immediately.
Scheduling via Meta Business Suite is the follow-up (driver 'meta_business').
"""
import time

from .base import BasePoster, UploadError, UploadResult
from .tiktok import build_caption

LOGIN_CHECK = (
    "(!document.querySelector('input[name=\"username\"]')) && "
    "(!!document.querySelector('svg[aria-label*=\"Beranda\"], "
    "svg[aria-label*=\"Home\"], a[href=\"/explore/\"]'))"
)
FILE_INPUT = "input[type='file'][accept*='video'], input[type='file']"


class InstagramPoster(BasePoster):
    platform = "instagram"
    upload_url = "https://www.instagram.com/"

    def login_check_js(self) -> str:
        return LOGIN_CHECK

    def upload(self, *, asset_path, asset_kind, title, description, tags,
               schedule_at, visibility, progress_cb,
               made_for_kids=False, channel_id="") -> UploadResult:
        self._preflight(asset_path)
        if schedule_at:
            raise UploadError("schedule",
                              "IG web tidak punya native scheduling — "
                              "gunakan jadwal worker (post saat waktu "
                              "tiba) atau Meta Business Suite (menyusul)")
        cap = build_caption(title, description, tags)
        from .base import PlaywrightSession
        with self._session() as sess:
            progress_cb(5)
            try:
                sess.navigate(self.upload_url)
            except Exception:
                raise UploadError("preflight", "IG: gagal buka instagram.com")
            if not sess.wait_for_selector("a[href='/explore/'], "
                                          "svg[aria-label*='Home']", 30_000):
                raise UploadError("session",
                                  "IG: tidak login — Hubungkan akun dulu")
            progress_cb(10)
            # open the create dialog
            create = sess.eval_js(
                "() => { var cands = document.querySelectorAll("
                "'div[role=\"button\"], button, a');"
                " for (var e of cands){ var t=(e.innerText||'').trim();"
                " var al=(e.getAttribute('aria-label')||'').trim();"
                " var s=(t+' '+al).toLowerCase();"
                " if (/create|buat|new post|buat postingan/.test(s)"
                " && s.length < 40){ var r=e.getBoundingClientRect();"
                " if(r.width>0) return {cx:r.x+r.width/2, cy:r.y+r.height/2,"
                " label:s}; } } return null; }")
            if not create:
                raise UploadError("menu",
                                  "IG: tombol Create tidak ditemukan")
            sess.mouse_click(create["cx"], create["cy"])
            time.sleep(2.0)
            if not sess.wait_for_selector(FILE_INPUT, 15_000):
                raise UploadError("dialog",
                                  "IG: file input composer tidak muncul")
            sess.upload_file(FILE_INPUT, asset_path)
            progress_cb(40)
            time.sleep(4)
            # Next through crop/filter steps (EN + ID labels)
            for step_label in ("Next", "Berikutnya", "Selanjutnya"):
                if sess.click_by_text(step_label, match_prefix=True,
                                      timeout_s=6):
                    time.sleep(2.0)
            progress_cb(60)
            # caption step: contenteditable in the share dialog
            cap_sel = ("div[role='dialog'] textarea[placeholder], "
                       "div[role='dialog'] div[contenteditable='true']")
            if not sess.wait_for_selector(cap_sel, 20_000):
                raise UploadError("caption",
                                  "IG: kolom caption tidak muncul")
            sess.click(cap_sel)
            sess.insert_text(cap)
            progress_cb(80)
            if not (sess.click_by_text("Share", match_prefix=True,
                                       timeout_s=6)
                    or sess.click_by_text("Bagikan", match_prefix=True,
                                          timeout_s=6)):
                raise UploadError("publish", "IG: tombol Share tidak ada")
            progress_cb(90)
            time.sleep(5)
            body = sess.page.locator("body").inner_text()[:4000]
            if "posted" in body.lower() or "terbit" in body.lower():
                progress_cb(100)
                return UploadResult(ok=True, note="posted")
            # dialog gone = success signal too
            if not sess.wait_for_selector("div[role='dialog']", 20_000):
                progress_cb(100)
                return UploadResult(ok=True, note="posted (dialog closed)")
            raise UploadError("publish",
                              "IG: konfirmasi posting tidak terdeteksi")
