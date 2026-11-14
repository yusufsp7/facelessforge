"""Facebook Reels poster — port of AutoClip's verified meta_facebook flow.

  1. Navigate facebook.com/reels/create (redirects to /reel/<id>); the file
     input mounts on the composer overlay.
  2. set_input_files → Next → 'Edit reel' step (caption = Lexical
     contenteditable div[aria-placeholder='Describe your reel...']).
  3. Next → 'Reel settings' (Public-only, 'Scheduling options', 'Post').
  4. SCHEDULE (optional): 'Scheduling options' popover — two role=combobox
     inputs (Date 'D MMM YYYY', Time 'HH:MM' 24h, no aria-label → positional)
     + 'Schedule for later' commit. Main button flips Post → Schedule.
  5. Wait for publish button enabled (= transfer 100%), trusted click, wait
     composer unmount (publish confirmed).

Emoji-safe captions via CDP insertText; selector pool covers EN + ID UI.
"""
import datetime
import re
import time

from .base import (BasePoster, PlaywrightSession, UploadError, UploadResult)
from .tiktok import build_caption

FILE_INPUT_ANY = "input[type='file'][accept*='video'], input[type='file']"
COVER_INPUT = ("input[type='file'][accept*='jpg'], "
               "input[type='file'][accept*='jpeg'],"
               "input[type='file'][accept*='png'], "
               "input[type='file'][accept*='image']")
CAPTION_SELS = (",").join([
    "div[contenteditable='true'][aria-placeholder^='Describe']",
    "div[contenteditable='true'][aria-placeholder^='Deskripsikan']",
    "div[contenteditable='true'][aria-placeholder^='Jelaskan']",
    "div[role='dialog'] div[role='textbox'][contenteditable='true']"
    "[data-lexical-editor='true']",
])
NEXT_LABELS = ["Next", "Berikutnya", "Selanjutnya"]
PUBLISH_LABELS = ["Post", "Schedule", "Publikasikan", "Jadwalkan", "Posting"]
LOGIN_CHECK = (
    "(!!document.querySelector('div[role=\"navigation\"]"
    "[aria-label=\"Facebook\"]') || "
    "!!document.querySelector('div[role=\"navigation\"] a[aria-label]')) && "
    "(!document.querySelector('input[name=\"email\"], #loginform, "
    "form[action*=\"login\"]'))"
)

REJECT_PHRASES = [
    "your file can't be uploaded", "file tidak dapat diunggah",
    "file couldn't be uploaded", "this video can't be uploaded",
]


class FacebookPoster(BasePoster):
    platform = "facebook"
    upload_url = "https://www.facebook.com/reels/create"

    def login_check_js(self) -> str:
        return LOGIN_CHECK

    def _advance(self, sess: PlaywrightSession, target_sel: str,
                 max_clicks: int = 12) -> bool:
        """Click Next until target appears (poll, not fixed waits)."""
        labels = NEXT_LABELS
        for _ in range(max_clicks):
            if sess.wait_for_selector(target_sel, 2_000):
                return True
            clicked = False
            for label in labels:
                if sess.click_by_text(label, match_prefix=True, timeout_s=2):
                    clicked = True
                    time.sleep(1.5)
                    break
            if not clicked:
                time.sleep(1.0)
        return bool(sess.wait_for_selector(target_sel, 3_000))

    def _detect_rejection(self, sess: PlaywrightSession) -> str | None:
        return sess.eval_js(
            "() => { var body = (document.body.innerText || '').toLowerCase();"
            " var phrases = " + repr(REJECT_PHRASES).replace("'", '"') + ";"
            " for (var p of phrases) { p = p.toLowerCase();"
            " if (body.includes(p)) return p; } return null; }")

    def _set_schedule(self, sess: PlaywrightSession, when: str) -> None:
        target = datetime.datetime.fromisoformat(when).astimezone()
        # open the popover
        if not (sess.click_by_text("Scheduling options", match_prefix=True,
                                   timeout_s=4)
                or sess.click_by_text("Opsi penjadwalan", match_prefix=True,
                                      timeout_s=4)):
            raise UploadError("schedule",
                              "FB: 'Scheduling options' tidak ditemukan")
        time.sleep(1.0)
        comboboxes = sess.eval_js("""
        () => {
            var dialogs = document.querySelectorAll('[role="dialog"]');
            for (var d of dialogs) {
                var r = d.getBoundingClientRect();
                if (r.width === 0) continue;
                if ((d.innerText || '').trim().indexOf('Scheduling options')
                        !== 0
                    && (d.innerText || '').trim().indexOf('Opsi penjadwalan')
                        !== 0) continue;
                var ins = d.querySelectorAll("input[role='combobox']");
                if (ins.length >= 2) {
                    return Array.from(ins).map(function(e) {
                        var r = e.getBoundingClientRect();
                        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                    });
                }
            }
            return null;
        }""")
        if not comboboxes or len(comboboxes) < 2:
            raise UploadError("schedule",
                              "FB: combobox jadwal tidak ditemukan")
        # click date combobox, select day via keyboard
        sess.mouse_click(comboboxes[0]["x"], comboboxes[0]["y"])
        time.sleep(0.8)
        date_str = target.strftime("%d/%m/%Y")
        sess.page.keyboard.type(date_str, delay=50)
        sess.page.keyboard.press("Enter")
        time.sleep(0.8)
        sess.mouse_click(comboboxes[1]["x"], comboboxes[1]["y"])
        time.sleep(0.8)
        time_str = target.strftime("%H:%M")
        sess.page.keyboard.type(time_str, delay=50)
        sess.page.keyboard.press("Enter")
        time.sleep(0.8)
        if not (sess.click_by_text("Schedule for later", match_prefix=True,
                                   timeout_s=4)
                or sess.click_by_text("Jadwalkan untuk nanti",
                                      match_prefix=True, timeout_s=4)):
            raise UploadError("schedule",
                              "FB: commit 'Schedule for later' tidak ada")

    def _wait_publish_enabled(self, sess: PlaywrightSession,
                              timeout_s: int = 600) -> dict:
        labels = repr(PUBLISH_LABELS)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            btn = sess.eval_js(
                "() => { var LABELS = " + labels + ";"
                " var dialogs = document.querySelectorAll('[role=\"dialog\"]');"
                " var roots = []; for (var d of dialogs){"
                " var r=d.getBoundingClientRect();"
                " if(r.width>0&&r.height>0) roots.push(d);} roots.push(document);"
                " for (var root of roots){"
                " var btns = root.querySelectorAll('div[role=\"button\"], button');"
                " for (var b of btns){ var t=(b.innerText||'').trim();"
                " if (LABELS.indexOf(t)===-1) continue;"
                " var br=b.getBoundingClientRect();"
                " if (br.width===0||br.height===0) continue;"
                " var s=getComputedStyle(b);"
                " var disabled = b.getAttribute('aria-disabled')==='true';"
                " var faded = parseFloat(s.opacity||'1') < 0.5;"
                " var nopt = s.pointerEvents==='none';"
                " if (disabled||faded||nopt) continue;"
                " return {text:t, cx:br.left+br.width/2, cy:br.top+br.height/2};"
                " } } return null; }")
            if btn:
                return btn
            time.sleep(2.0)
        raise UploadError("publish",
                          "FB: tombol publish tidak pernah aktif (upload "
                          "gagal / timeout)")

    def upload(self, *, asset_path, asset_kind, title, description, tags,
               schedule_at, visibility, progress_cb,
               made_for_kids=False, channel_id="") -> UploadResult:
        self._preflight(asset_path)
        if asset_kind != "video":
            raise UploadError("preflight",
                              "Facebook: asset bukan video (v1: Reels video)")
        cap = build_caption(title, description, tags)
        with self._session() as sess:
            progress_cb(5)
            try:
                sess.navigate(self.upload_url)
            except Exception:
                raise UploadError("preflight",
                                  "FB: gagal buka /reels/create (login?)")
            if not sess.wait_for_selector(FILE_INPUT_ANY, 30_000):
                raise UploadError("session",
                                  "FB: tidak login — Hubungkan akun dulu")
            progress_cb(10)
            sess.upload_file(FILE_INPUT_ANY, asset_path)
            progress_cb(25)

            if not self._advance(sess, CAPTION_SELS, max_clicks=14):
                raise UploadError("steps",
                                  "FB: tidak sampai step caption/Next habis")
            progress_cb(40)
            sess.click(CAPTION_SELS)
            sess.select_all()
            sess.page.keyboard.press("Delete")
            sess.insert_text(cap)
            progress_cb(55)

            # reach settings step: press Next once more; the settings panel
            # shows the Post button — verified via publish-poll below
            for label in NEXT_LABELS:
                if sess.click_by_text(label, match_prefix=True, timeout_s=2):
                    time.sleep(1.5)
                    break

            if schedule_at:
                self._set_schedule(sess, schedule_at)
            progress_cb(80)

            rej = self._detect_rejection(sess)
            if rej:
                raise UploadError("upload_rejected",
                                  f"FB menolak unggahan: {rej}")
            btn = self._wait_publish_enabled(sess)
            sess.mouse_click(btn["cx"], btn["cy"])
            progress_cb(90)
            time.sleep(2)
            # wait composer unmount = confirmed
            deadline = time.monotonic() + 120
            confirmed = False
            while time.monotonic() < deadline:
                gone = not sess.wait_for_selector(CAPTION_SELS, 1_000)
                if gone:
                    confirmed = True
                    break
                time.sleep(2)
            if not confirmed:
                raise UploadError("publish",
                                  "FB: composer tidak tertutup — status "
                                  "tidak pasti")
            progress_cb(100)
            return UploadResult(ok=True,
                                note="posted" + (" (scheduled)"
                                                 if schedule_at else ""))
