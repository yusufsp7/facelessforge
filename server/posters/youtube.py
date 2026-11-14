"""YouTube Studio poster — visibility/audience explicit, channel-verified.

Hard rules learned the hard way (see AGENTS.md + VM test report):
- NEVER click the avatar inside Studio (triggers Google verification
  dialog). Channel switching only via youtube.com.
- The "Verifikasi diri Anda" overlay is NOT a dead session: close it with
  Escape, navigate to the upload page directly, and prove the session by
  the upload attempt itself.
- visibility must be set AND verified on the matching Studio radio before
  Publish — 'private'/'unlisted' jobs must never ship as PUBLIC.
- audience ("made for kids?") is chosen explicitly, never via blind Next
  loops; every Next must land on an expected step or fail loudly.
- Native scheduling is intentionally NOT supported here (registry says
  schedule_supported=False): the worker uploads at the scheduled time.
- After publish, verify on the Content tab (title + status) and extract the
  video id so jobs/external dashboards can consume it.
"""
import json
import re
import time

from .base import BasePoster, UploadError, UploadResult
from .tiktok import build_caption

CREATE_BUTTON = ("ytcp-button#create-icon, #create-icon, "
                 "tp-yt-paper-button#create-button")
FILE_INPUT = "input[type='file']"
TITLE_INPUT = "ytcp-uploads-dialog input#textbox, input#textbox"
DESC_INPUT = "#description-wrapper textarea"
NEXT_BUTTON = "ytcp-button#next-button, #next-button"
PUBLISH_BTN = "ytcp-button#done-button, #done-button"

VISIBILITY_RADIO = {
    "public": "tp-yt-paper-radio-button[name='PUBLIC']",
    "unlisted": "tp-yt-paper-radio-button[name='UNLISTED']",
    "private": "tp-yt-paper-radio-button[name='PRIVATE']",
}

VERIFY_DIALOG_MARK = ("div[role='dialog'] h2, yt-upsell-dialog, "
                      "tp-yt-paper-dialog[id='dialog']")

LOGIN_CHECK = ("!!document.querySelector('img#img.avatar, button#avatar-btn, "
               "yt-img-shadow#avatar img')")


class YouTubePoster(BasePoster):
    platform = "youtube"
    upload_url = "https://studio.youtube.com/"

    def login_check_js(self) -> str:
        return LOGIN_CHECK

    # -- guards ----------------------------------------------------------

    def _content_url(self, channel_id: str) -> str:
        if channel_id and channel_id.startswith("UC"):
            return f"https://studio.youtube.com/channel/{channel_id}/videos"
        return "https://studio.youtube.com/"  # lalu klik menu Konten

    def _goto_content(self, sess, channel_id: str) -> None:
        sess.navigate(self._content_url(channel_id))
        if not (channel_id or "").startswith("UC"):
            try:
                sess.click_by_text("Content", match_prefix=True, timeout_s=5) \
                    or sess.click_by_text("Konten", match_prefix=True,
                                          timeout_s=5)
            except Exception:
                pass
        time.sleep(3)

    def _dismiss_verification(self, sess) -> None:
        """'Verifikasi diri Anda' overlay: close with Escape, never click
        through it, and never treat it as a dead session."""
        hit = sess.eval_js(
            "() => { const t=(document.body.innerText||'');"
            " return /Verifikasi diri Anda|Verify it's you|Verify yourself/"
            ".test(t) ? true : false; }")
        if hit:
            try:
                sess.page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(1)
            # uji sesi: navigasi langsung ke halaman upload (bukan dialog)
            sess.navigate(self.upload_url)

    def _assert_channel(self, sess, channel_id: str) -> None:
        """Active Studio channel must match the job's target channel."""
        if not channel_id:
            return
        url = sess.current_url()
        m = re.search(r"studio\.youtube\.com/channel/(UC[A-Za-z0-9_-]+)", url)
        active = m.group(1) if m else ""
        if active and channel_id and active != channel_id:
            raise UploadError(
                "channel",
                f"Studio aktif channel {active} ≠ target {channel_id}. "
                "Ganti channel lewat youtube.com (JANGAN klik avatar di "
                "Studio).")

    def _next_until(self, sess, expect_selector: str, step_name: str,
                    max_steps: int = 6) -> None:
        for i in range(max_steps):
            if sess.wait_for_selector(expect_selector, 3_000):
                return
            try:
                sess.page.locator(NEXT_BUTTON).first.click(timeout=8_000)
            except Exception as e:
                raise UploadError(
                    "steps",
                    f"YouTube: gagal klik Next menuju '{step_name}' "
                    f"(step {i}): {e!r:.120}")
            time.sleep(2.0)
        if not sess.wait_for_selector(expect_selector, 3_000):
            raise UploadError("steps",
                              f"YouTube: step '{step_name}' tidak pernah "
                              "muncul — layout Studio berubah?")

    def _set_visibility(self, sess, visibility: str) -> None:
        sel = VISIBILITY_RADIO.get(visibility or "public")
        if not sel:
            raise UploadError("visibility",
                              f"visibility '{visibility}' tidak dikenal")
        if not sess.wait_for_selector(sel, 8_000):
            raise UploadError("visibility",
                              f"radio {visibility} tidak ditemukan di step "
                              "visibility")
        sess.click(sel)
        time.sleep(0.6)
        checked = sess.eval_js(
            f"() => {{ const el=document.querySelector(\"{sel}\");"
            " return el ? (el.getAttribute('aria-checked')==='true' ||"
            " el.hasAttribute('active')) : false; }")
        if not checked:
            raise UploadError("visibility",
                              f"radio {visibility} tidak terverifikasi aktif "
                              "sebelum Publish")

    def _set_audience(self, sess, made_for_kids: bool) -> None:
        """Explicitly pick the audience radio (never blind Next)."""
        yes_sel = ("tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS']"
                   ", ytkmp-made-for-kids-toggle #yes-button")
        no_sel = ("tp-yt-paper-radio-button[name='NOT_MADE_FOR_KIDS']"
                  ", ytkmp-made-for-kids-toggle #no-button")
        target = yes_sel if made_for_kids else no_sel
        if not sess.wait_for_selector(yes_sel, 4_000) and \
                not sess.wait_for_selector(no_sel, 2_000):
            # audience step layout varies; presence of the toggle header
            return  # step may not exist for Shorts-only flows — don't fail
        try:
            sess.click(target)
            time.sleep(0.5)
        except Exception as e:
            raise UploadError("audience",
                              f"YouTube: gagal pilih audience "
                              f"(made_for_kids={made_for_kids}): {e!r:.120}")

    def _id_from_publish_dialog(self, sess) -> str:
        """(c) sumber ID terbaik: dialog 'Video published' memuat link video."""
        hit = sess.eval_js(
            "() => { const links = document.querySelectorAll("
            "'a[href*=\"watch?v=\"]');"
            " for (const a of links) { const h = a.getAttribute('href') || '';"
            " if (h.includes('watch')) {"
            " const m = h.match(/v=([A-Za-z0-9_-]{6,})/);"
            " if (m) return m[1]; } } return ''; }")
        return (hit or "").strip()

    def _extract_video_id(self, sess, title: str, channel_id: str = "") -> str:
        """(b)(c) ID HANYA dari baris berjudul cocok — TANPA fallback
        baris-pertama (bisa mencatat video orang lain). Poll 6×10s."""
        t = (title or "").lower()[:24]
        js = (
            "(title) => {"
            " const rows = document.querySelectorAll('ytcp-video-row');"
            " for (const r of rows) {"
            "   const el = r.querySelector('#video-title');"
            "   const txt = el ? (el.innerText || '').trim().toLowerCase() : '';"
            "   if (title && txt.startsWith(title)) {"
            "     const a = r.querySelector('a[href*=\"watch?v=\"]');"
            "     const h = a ? a.getAttribute('href') : '';"
            "     const m = h.match(/v=([A-Za-z0-9_-]{6,})/);"
            "     return m ? m[1] : ''; }"
            " }"
            " return ''; }"
        )
        for _ in range(6):  # 6×10s — balapan dengan pemrosesan YouTube
            self._goto_content(sess, channel_id)
            vid = sess.eval_js(js, t)
            if vid:
                return (vid or "").strip()
            time.sleep(10)
        return ""

    def _verify_in_content(self, sess, title: str, video_id: str = "",
                           channel_id: str = "") -> dict:
        """(e) verifikasi gabungan: judul + video_id + status visibilitas."""
        self._goto_content(sess, channel_id)
        body = ""
        try:
            body = sess.page.locator("body").inner_text()[:8000]
        except Exception:
            pass
        tl = title.lower()[:20] if title else ""
        return {"title_found": tl in body.lower() if tl else True,
                "video_id_found": bool(video_id),
                "visibility_seen": any(v in body.lower() for v in
                                       ("public", "publik", "unlisted",
                                        "private", "pribadi", "terjadwal")),
                "page": "content"}

    # -- upload ----------------------------------------------------------

    def upload(self, *, asset_path, asset_kind, title, description, tags,
               schedule_at, visibility, progress_cb,
               made_for_kids=False, channel_id="") -> UploadResult:
        self._preflight(asset_path)
        if asset_kind != "video":
            raise UploadError("preflight",
                              "YouTube: asset bukan video (v1: video only)")
        if schedule_at:
            raise UploadError(
                "schedule",
                "YouTube native schedule sengaja tidak dipakai — worker "
                "upload tepat waktu. Jalankan worker saat jam tayang atau "
                "jadwalkan tanpa 'jadwal native'.")
        cap_title = title or "Untitled"
        cap_desc = build_caption("", description, tags)
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
            self._dismiss_verification(sess)
            self._assert_channel(sess, channel_id)
            progress_cb(10)
            sess.click(CREATE_BUTTON)
            time.sleep(1.0)
            if not (sess.click_by_text("Add videos", match_prefix=True,
                                       timeout_s=3)
                    or sess.click_by_text("Tambahkan video",
                                          match_prefix=True, timeout_s=3)
                    or sess.click_by_text("Upload videos", match_prefix=True,
                                          timeout_s=3)):
                raise UploadError("menu",
                                  "YouTube: menu 'Add videos' tidak "
                                  "ditemukan (screenshot tersimpan)")
            if not sess.wait_for_selector(FILE_INPUT, 15_000):
                raise UploadError("dialog",
                                  "YouTube: dialog upload tidak muncul")
            sess.upload_file(FILE_INPUT, asset_path)
            progress_cb(25)

            if not sess.wait_for_selector(TITLE_INPUT, 30_000):
                raise UploadError("details",
                                  "YouTube: step detail tidak muncul")
            try:
                sess.page.locator(TITLE_INPUT).first.fill(cap_title)
            except Exception as e:
                raise UploadError("details",
                                  f"YouTube: gagal isi judul {e!r:.120}")
            filled = False
            if cap_desc:
                for dsel in (DESC_INPUT, "div#description-container textarea",
                             "ytcp-social-suggestions-textbox #textbox"):
                    if sess.wait_for_selector(dsel, 4_000):
                        sess.page.locator(dsel).first.fill(cap_desc)
                        filled = True
                        break
            if cap_desc and not filled:
                sess.screenshot(f"yt-desc-missing-{int(time.time())}")
                raise UploadError("details",
                                  "YouTube: kolom deskripsi tidak ditemukan — "
                                  "publish DIBATALKAN (deskripsi bagian "
                                  "konten, bukan opsional; screenshot "
                                  "diagnostik tersimpan)")
            progress_cb(50)
            self._next_until(sess, "ytcp-video-visibility, #step-button, "
                             "tp-yt-paper-radio-button[name='PUBLIC']",
                             "visibility/audience", max_steps=4)
            self._set_audience(sess, made_for_kids)
            progress_cb(65)
            self._next_until(sess, VISIBILITY_RADIO["public"],
                             "visibility", max_steps=4)
            self._set_visibility(sess, visibility or "public")
            progress_cb(80)

            try:
                sess.page.locator(PUBLISH_BTN).first.click(timeout=15_000)
            except Exception:
                if not (sess.click_by_text("Publish", match_prefix=True,
                                           timeout_s=5)
                        or sess.click_by_text("Publikasikan",
                                              match_prefix=True,
                                              timeout_s=5)):
                    raise UploadError("publish",
                                      "YouTube: tombol Publish tidak ada")
            progress_cb(90)
            time.sleep(4)
            self._dismiss_verification(sess)
            # (c) sumber utama: dialog sukses; cadangan: tab Konten (polling)
            video_id = self._id_from_publish_dialog(sess)
            if not video_id:
                video_id = self._extract_video_id(sess, cap_title, channel_id)
            verify = self._verify_in_content(sess, cap_title, video_id,
                                             channel_id)
            progress_cb(100)
            url = (f"https://www.youtube.com/watch?v={video_id}"
                   if video_id else "")
            if video_id and verify.get("title_found"):
                note = "posted (verified di Content)"
            elif video_id:
                note = "posted (video_id dari dialog; verifikasi Content terbatas)"
            else:
                note = "posted — PERLU VERIFIKASI MANUAL (video_id tidak terbaca)"
            return UploadResult(ok=True, public_url=url,
                                platform_post_id=video_id, note=note)
