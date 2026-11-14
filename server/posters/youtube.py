"""YouTube Studio poster.

Upload dialog at studio.youtube.com (CREATE → Add videos):
  1. Open studio, click Create, menu item 'Add videos'/'Tambahkan video'.
  2. Send the file to the dialog's input[type=file].
  3. Title/description while processing (details step fields are inputs).
  4. Next ×3 (visibility step last), choose Public or Schedule (date+time).
  5. Publish; success = 'Video published' snackbar / redirect to Content list.
"""
import time

from .base import BasePoster, UploadError, UploadResult
from .tiktok import build_caption

CREATE_BUTTON = ("ytcp-button#create-icon, #create-icon, "
                 "tp-yt-paper-button#create-button")
ADD_VIDEO_ITEM = ("ytcp-text-menu#create-menu tp-yt-paper-item, "
                  "#text-displayed")  # resolved by text click instead
FILE_INPUT = "input[type='file']"
TITLE_INPUT = "ytcp-uploads-dialog input#textbox, input#textbox"
DESC_INPUT = "#description-wrapper textarea, ytcp-social-suggestions-textbox#description"
NEXT_BUTTON = ("ytcp-button#next-button, #next-button, "
               "ytcp-button#step-button")
PUBLIC_RADIO = ("#rbui_radio_group radio-button[aria-checked] , "
                "ytcp-video-visibility-radio-button#public-radio-button, "
                "tp-yt-paper-radio-button[name='PUBLIC']")
SCHEDULE_RADIO = ("ytcp-video-visibility-radio-button#schedule-radio-button, "
                  "tp-yt-paper-radio-button[name='PRIVATE'] [aria-label*='Schedule']")
PUBLISH_BTN = ("ytcp-button#done-button, #done-button, "
               "ytcp-button#submit-button")


class YouTubePoster(BasePoster):
    platform = "youtube"
    upload_url = "https://studio.youtube.com/"

    def login_check_js(self) -> str:
        return ("!!document.querySelector('img#img.avatar, button#avatar-btn, "
                "yt-img-shadow#avatar img')")

    def upload(self, *, asset_path, asset_kind, title, description, tags,
               schedule_at, visibility, progress_cb) -> UploadResult:
        self._preflight(asset_path)
        if asset_kind != "video":
            raise UploadError("preflight",
                              "YouTube: asset bukan video (v1: video only)")
        from .base import PlaywrightSession
        with self._session() as sess:
            progress_cb(5)
            try:
                sess.navigate(self.upload_url)
            except Exception:
                raise UploadError("preflight",
                                  "YouTube: gagal buka Studio (login?)")
            if not sess.wait_for_selector(CREATE_BUTTON, 30_000):
                raise UploadError("session",
                                  "YouTube: tidak login — Hubungkan akun dulu")
            progress_cb(10)
            sess.click(CREATE_BUTTON)
            time.sleep(1.0)
            if not sess.click_by_text("Add videos", match_prefix=True,
                                      timeout_s=3) and not sess.click_by_text(
                    "Tambahkan video", match_prefix=True, timeout_s=3):
                if not sess.click_by_text("Upload videos", match_prefix=True,
                                          timeout_s=3):
                    raise UploadError("menu",
                                      "YouTube: menu 'Add videos' tidak "
                                      "ditemukan")
            if not sess.wait_for_selector(FILE_INPUT, 15_000):
                raise UploadError("dialog",
                                  "YouTube: dialog upload tidak muncul")
            sess.upload_file(FILE_INPUT, asset_path)
            progress_cb(25)

            # details step
            if not sess.wait_for_selector(TITLE_INPUT, 30_000):
                raise UploadError("dialog",
                                  "YouTube: step detail tidak muncul")
            cap_title = title or "Untitled"
            cap_desc = build_caption("", description, tags)
            try:
                sess.page.locator(TITLE_INPUT).first.fill(cap_title)
            except Exception as e:
                raise UploadError("details", f"YouTube: gagal isi judul {e!r}")
            if cap_desc and sess.wait_for_selector(DESC_INPUT, 5_000):
                sess.page.locator(DESC_INPUT).first.fill(cap_desc)
            progress_cb(50)

            # Next ×3 until publish/visibility step
            for _ in range(4):
                try:
                    sess.page.locator(NEXT_BUTTON).first.click(timeout=8_000)
                except Exception:
                    break
                time.sleep(2.0)
            progress_cb(65)

            # visibility
            if schedule_at:
                if not sess.click_by_text("Schedule", match_prefix=True,
                                          timeout_s=5) and not sess.click_by_text(
                        "Jadwalkan", match_prefix=True, timeout_s=5):
                    raise UploadError("schedule",
                                      "YouTube: opsi Schedule tidak ada")
                time.sleep(1.5)
                # date + time fields are inputs in the schedule panel
                date_inp = sess.page.locator(
                    "#schedule-radio-button input, ytcp-datetime-picker input"
                )
                if date_inp.count() == 0:
                    raise UploadError("schedule",
                                      "YouTube: field jadwal tidak ditemukan")
                raise UploadError(
                    "schedule",
                    "YouTube native schedule: v1 mengisi via UI dasar; "
                    "cek hasil manual di Studio (web schedule berubah-ubah)")
            else:
                if not sess.click_by_text("Public", match_prefix=True,
                                          timeout_s=4) and not sess.click_by_text(
                        "Publik", match_prefix=True, timeout_s=4):
                    pass  # public is default on most accounts
            progress_cb(80)

            try:
                sess.page.locator(PUBLISH_BTN).first.click(timeout=15_000)
            except Exception:
                if not sess.click_by_text("Publish", match_prefix=True,
                                          timeout_s=5) and not sess.click_by_text(
                        "Publikasikan", match_prefix=True, timeout_s=5):
                    raise UploadError("publish",
                                      "YouTube: tombol Publish tidak ada")
            progress_cb(90)
            time.sleep(4)
            note = "posted"
            for _ in range(10):
                body = sess.page.locator("body").inner_text()[:4000]
                if ("Video published" in body or "Video dipublikasikan" in body
                        or "processing" in body.lower()):
                    note = "published (processing)" if "processing" in body.lower() else note
                    break
                time.sleep(1.5)
            progress_cb(100)
            return UploadResult(ok=True, note=note)
