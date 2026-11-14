"""Broadcast lifecycle via the account's Studio session — same pattern as
posters: the account's own persistent-Chrome profile, per-platform queue
lock shared with upload (one browser per platform), never clicking the
avatar. Encoder restarts do NOT create a broadcast; an ended broadcast must
be re-created here.
"""
import json
import re
import time

from . import db, generator
from .worker import PLATFORM_LOCKS


def _profile_dir(account_key: str) -> str:
    acct = db.one("SELECT * FROM accounts WHERE key = ?", (account_key,))
    if not acct:
        raise RuntimeError(f"akun {account_key} tidak ada")
    from . import paths
    d = paths.profiles_dir() / "youtube" / (acct["profile_key"] or
                                            account_key.replace(":", "_"))
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _session(account_key: str):
    from .posters.base import PlaywrightSession
    return PlaywrightSession(_profile_dir(account_key))


def _no_active_render() -> bool:
    return not db.rows("SELECT id FROM generations WHERE status = "
                       "'rendering' LIMIT 1")


def broadcast_state(account_key: str, channel_id: str = "") -> dict:
    """Deteksi keadaan broadcast di Studio: none | offline | ended | live,
    beserta video id broadcast (kalau terbaca)."""
    lock = PLATFORM_LOCKS.get("youtube")
    with lock:
        with _session(account_key) as sess:
            url = ("https://studio.youtube.com/channel/"
                   f"{channel_id}/livestreaming" if channel_id.startswith("UC")
                   else "https://studio.youtube.com/livestreaming")
            sess.navigate(url)
            time.sleep(5)
            state = sess.eval_js(
                "() => { const t=(document.body.innerText||'');"
                " if (/Live streaming is not available|Tidak tersedia/"
                ".test(t)) return 'unavailable';"
                " if (/Stream is live|Sedang siaran|LIVE \\u2022|selesai siaran/"
                ".test(t) && /viewer|penonton/i.test(t)) return 'live';"
                " if (/Stream ended|Siaran berakhir|sudah berakhir/"
                ".test(t)) return 'ended';"
                " if (/Start streaming|Mulai streaming|Enable live/"
                ".test(t)) return 'offline';"
                " return 'unknown'; }")
            vid = sess.eval_js(
                "() => { const m=(document.body.innerHTML||'').match("
                "/[\\\"']([A-Za-z0-9_-]{11})[\\\"'].*?live/);"
                " const a=document.querySelector('a[href*=\\\"watch?v=\\\"]');"
                " if (a) { const mm=a.getAttribute('href')"
                ".match(/v=([A-Za-z0-9_-]{11})/); if (mm) return mm[1]; }"
                " return m ? m[1] : ''; }")
            return {"state": state, "broadcast_id": (vid or "").strip(),
                    "checked_at": db.now_iso()}


def ensure_broadcast(account_key: str, title: str, description: str,
                     privacy: str = "public", channel_id: str = "") -> dict:
    """Bila belum ada broadcast / yang lama sudah berakhir: buat broadcast
    baru lewat 'Go live → Stream' (judul, deskripsi, privasi, bukan untuk
    anak-anak) memakai stream key profil akun. Kembalikan video id."""
    lock = PLATFORM_LOCKS.get("youtube")
    st = broadcast_state(account_key, channel_id)
    if st["state"] == "live":
        return {"ok": True, "state": "live", "broadcast_id":
                st["broadcast_id"], "created": False}
    if st["state"] == "unavailable":
        return {"ok": False, "error": "fitur live tidak tersedia di channel "
                "ini (verifikasi 24 jam?)"}
    with lock:
        with _session(account_key) as sess:
            url = ("https://studio.youtube.com/channel/"
                   f"{channel_id}/livestreaming" if channel_id.startswith("UC")
                   else "https://studio.youtube.com/livestreaming")
            sess.navigate(url)
            time.sleep(4)
            if not (sess.click_by_text("Go live", match_prefix=True,
                                       timeout_s=6)
                    or sess.click_by_text("Siaran langsung",
                                          match_prefix=True, timeout_s=6)
                    or sess.click_by_text("Stream", match_prefix=True,
                                          timeout_s=6)):
                raise RuntimeError("tombol 'Go live' tidak ditemukan")
            time.sleep(4)
            # tab Stream (bukan webcam)
            sess.click_by_text("Streaming software", match_prefix=True,
                               timeout_s=6) \
                or sess.click_by_text("Stream", match_prefix=True,
                                      timeout_s=6)
            # judul/deskripsi/privasi/audience di dialog Settings
            if sess.wait_for_selector("input#textbox", 8_000):
                sess.page.locator("input#textbox").first.fill(title)
            if sess.click_by_text("More options", match_prefix=True,
                                  timeout_s=4) or sess.click_by_text(
                    "Opsi lainnya", match_prefix=True, timeout_s=4):
                time.sleep(1)
                if description and sess.wait_for_selector(
                        "div[contenteditable='true']", 6_000):
                    sess.page.locator("div[contenteditable='true']").first \
                        .fill(description)
                for label, target in (("Public", "PUBLIC"),
                                      ("Publik", "PUBLIC"),
                                      ("Unlisted", "UNLISTED"),
                                      ("Private", "PRIVATE"),
                                      ("Pribadi", "PRIVATE")):
                    if privacy.lower() in label.lower() and sess.click_by_text(
                            label, match_prefix=False, timeout_s=3):
                        break
                sess.click_by_text("No, it's not made for kids",
                                   match_prefix=True, timeout_s=4) \
                    or sess.click_by_text("Bukan untuk anak-anak",
                                          match_prefix=True, timeout_s=4)
                sess.click_by_text("Save", match_prefix=True, timeout_s=4) \
                    or sess.click_by_text("Simpan", match_prefix=True,
                                          timeout_s=4)
                time.sleep(2)
            vid = sess.eval_js(
                "() => { const a=document.querySelector('a[href*=' + "
                "JSON.stringify('watch?v=') + ']');"
                " if (a) { const m=a.getAttribute('href')"
                ".match(/v=([A-Za-z0-9_-]{11})/); if (m) return m[1]; }"
                " return ''; }")
            from . import live
            live.save_profile(account_key, {"broadcast_id": vid or ""})
            live.log_event(account_key, "broadcast_created",
                           {"broadcast_id": vid, "state": st["state"]})
            return {"ok": True, "state": "created", "broadcast_id": vid,
                    "created": True}


def youtube_side_health(account_key: str, channel_id: str = "") -> dict:
    """(b) sisi YouTube benar-benar LIVE — halaman Studio live dashboard."""
    lock = PLATFORM_LOCKS.get("youtube")
    with lock:
        with _session(account_key) as sess:
            url = ("https://studio.youtube.com/channel/"
                   f"{channel_id}/livestreaming" if channel_id.startswith("UC")
                   else "https://studio.youtube.com/livestreaming")
            sess.navigate(url)
            time.sleep(5)
            body = sess.page.locator("body").inner_text()[:4000]
            live = ("Stream is live" in body or "Sedang siaran" in body
                    or "LIVE" in body)
            return {"live": live, "checked_at": db.now_iso()}


def apply_replay_policy(account_key: str, channel_id: str = "",
                        keep_hours: float = 1.0) -> dict:
    """(5) temukan replay broadcast di Konten Studio, baca durasi, terapkan
    aturan (bawaan: >keep_hours SIMPAN, ≤keep_hours HAPUS), catat keputusan."""
    lock = PLATFORM_LOCKS.get("youtube")
    decisions = []
    with lock:
        with _session(account_key) as sess:
            from .posters.youtube import YouTubePoster
            poster = YouTubePoster.__new__(YouTubePoster)
            poster.platform = "youtube"
            poster.profile_dir = _profile_dir(account_key)
            from .posters.base import PlaywrightSession
            with poster._session() as s2:
                pass  # placeholder — gunakan sess utama
            sess.navigate("https://studio.youtube.com/livestreaming")
            time.sleep(4)
            rows = sess.eval_js(
                "() => Array.from(document.querySelectorAll"
                "('ytcp-video-row')).slice(0, 10).map(r => {"
                " const a=r.querySelector('a[href*=\"watch?v=\"]');"
                " const t=r.querySelector('#video-title');"
                " const d=(r.innerText||'').match(/(\\d+):(\\d{2}):(\\d{2})/);"
                " return {href: a?a.getAttribute('href'):'',"
                " title: t?t.innerText.trim():'',"
                " dur: d? (+d[1])*3600+(+d[2])*60+(+d[3]) : 0};})")
            for row in rows or []:
                m = re.search(r"v=([A-Za-z0-9_-]{11})",
                              row.get("href") or "")
                vid = m.group(1) if m else ""
                dur = float(row.get("dur") or 0)
                keep = dur > keep_hours * 3600
                decisions.append({"video_id": vid, "title": row.get("title"),
                                  "duration_s": dur,
                                  "decision": "keep" if keep else "delete"})
                if not keep and vid and _delete_video(sess, vid):
                    pass
    for d in decisions:
        db.log_event(None, "live_replay_decision", {"account": account_key,
                                                    **d})
    return {"decisions": decisions}


def _delete_video(sess, video_id: str) -> bool:
    """Hapus video lewat baris Konten (menu tiga titik → Hapus selamanya)."""
    hit = sess.eval_js(
        "(vid) => { const rows=document.querySelectorAll('ytcp-video-row');"
        " for (const r of rows) { const a=r.querySelector"
        "('a[href*=\"v=\" + vid]'); if (a) {"
        " const btn=r.querySelector('button[aria-label]');"
        " if (btn) { btn.click(); return true; } } } return false; }",
        video_id)
    if not hit:
        return False
    time.sleep(1.5)
    sess.click_by_text("Delete forever", match_prefix=True, timeout_s=5) \
        or sess.click_by_text("Hapus selamanya", match_prefix=True,
                              timeout_s=5)
    time.sleep(1.5)
    confirm = sess.click_by_text("Delete", match_prefix=True, timeout_s=5) \
        or sess.click_by_text("Hapus", match_prefix=True, timeout_s=5)
    return bool(confirm)
