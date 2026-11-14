"""TikTok Studio poster — port of AutoClip's verified tiktok_studio flow.

Single-page composer at /tiktokstudio/upload:
  1. Navigate (cookie-imported / persistent profile stays logged in).
  2. set_input_files the clip — the editor mounts (reload-retry x2 on throttle).
  3. Visibility, schedule, caption while the video transfers.
  4. Wait for Post to enable, best-effort cover, verify, click; handle the
     "Continue to post?" confirmation; success = redirect off /upload.

Notes honoured from AutoClip's archaeology:
  - read live <input> values with input_value(), NOT get_attribute("value")
  - TikTok custom radio/select/cover components ignore JS clicks → mouse_click
  - CDP Input.insertText types emoji captions safely
"""
import datetime
import time

from .base import BasePoster, PlaywrightSession, UploadError, UploadResult

# -- verified selectors (EN + ID) -------------------------------------------

FILE_INPUT = "input[type='file'][accept*='video'], input[type='file']"
CAPTION_SEL = ("div[data-e2e='caption_container'] div.public-DraftEditor-content"
               "[contenteditable='true'], div.public-DraftEditor-content"
               "[contenteditable='true']")
POST_BUTTON = "button[data-e2e='post_video_button']"
SCHEDULE_CONTAINER = "div[data-e2e='schedule_container']"
VISIBILITY_CONTAINER = "div[data-e2e='video_visibility_container']"
STATUS_CONTAINER = "div[data-e2e='upload_status_container']"

LOGIN_CHECK = (
    "!!document.querySelector('div[data-e2e=\"profile-icon\"]') || "
    "!!document.querySelector('a[data-e2e=\"nav-profile\"]')"
)

# dialog label classification (ported from AutoClip tt_classify_dialog_label)
DENY_PHRASES = [
    "discard", "buang", "dibuang", "delete", "hapus", "dihapus", "leave",
    "keluar", "lost", "hilang", "not be saved", "tidak disimpan",
    "tidak akan disimpan", "tidak tersimpan", "don't allow", "do not allow",
]
SCHEDULE_ALLOW_LABELS = [
    "schedule post", "allow scheduling", "allow schedule post",
    "izinkan penjadwalan", "jadwalkan postingan", "izinkan jadwal posting",
]
ACK_LABELS = ["i understand", "konfirmasi", "understood", "saya mengerti",
              "ok", "oke", "confirm", "selesai", "done"]
NOW_PHRASES = ["now", "sekarang", "immediately", "langsung", "instead",
               "right away", "segera", "posting sekarang", "publish now",
               "publish immediately", "unggah sekarang"]


def tt_norm_label(label: str) -> str:
    """NFKC-ish lowercase unify for label matching (port of tt_norm_label)."""
    import unicodedata
    s = unicodedata.normalize("NFKC", label or "").lower()
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    import re as _re
    s = _re.sub(r"[^\w\s']", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return s


def tt_classify_dialog_label(label: str) -> str:
    """'deny' | 'allow' | 'ack' | 'neutral' — deny checked first."""
    norm = tt_norm_label(label)
    for p in DENY_PHRASES:
        import re as _re
        if _re.search(r"(?<![\w'])" + _re.escape(p) + r"(?![\w'])", norm):
            return "deny"
    words = norm.split()
    for a in SCHEDULE_ALLOW_LABELS:
        if words == tt_norm_label(a).split():
            return "allow"
    for a in ACK_LABELS:
        if words == tt_norm_label(a).split():
            return "ack"
    return "neutral"


# calendar / time-picker JS (ported from AutoClip, trimmed to what we need)

CAL_SELECT_DAY_JS = r"""
(want) => {
  function vis(e){ if(!e) return false; var r=e.getBoundingClientRect();
    if(r.width<4||r.height<4) return false; var s=getComputedStyle(e);
    if(s.visibility==='hidden'||s.display==='none'||parseFloat(s.opacity||'1')<0.05) return false;
    var p=e; while(p&&p!==document.body){ if(/(^|[\\s_-])invisible([\\s_-]|$)/i.test((p.className||'').toString())) return false; p=p.parentElement; }
    return true; }
  function leafText(e){ for(var i=0;i<e.children.length;i++){ if(e.children[i].tagName!=='svg'&&(e.children[i].innerText||'').trim()) return null; }
    return (e.innerText||'').trim(); }
  function badCell(e, stop){ var p=e; while(p&&p!==stop){ if(p.getAttribute&&(p.getAttribute('aria-disabled')==='true'||p.disabled)) return true;
    var cl=' '+(p.className||'').toString().toLowerCase().replace(/[_-]+/g,' ')+' ';
    if(/ (disabled|invalid|outside|other|othermonth|out|prev|next|grey|gray|muted|past) /.test(cl)) return true; p=p.parentElement; } return false; }
  function calendarGrid(){
    var all=document.querySelectorAll('body *'), sets=new Map();
    for(var i=0;i<all.length;i++){ var e=all[i]; var t=leafText(e); if(!t||!/^\d{1,2}$/.test(t)) continue;
      var n=+t; if(n<1||n>31||!vis(e)) continue;
      var p=e.parentElement, d=0; while(p&&p!==document.body&&d<6){ if(!sets.has(p)) sets.set(p,new Set()); sets.get(p).add(n); p=p.parentElement; d++; } }
    var best=null, bestN=1e9;
    sets.forEach(function(v,k){ if(v.size>=28){ var n=k.querySelectorAll('*').length; if(n<bestN){best=k;bestN=n;} } });
    return best; }
  function popRoot(e){ var p=e, d=0; while(p&&p!==document.body&&d<8){ var s=getComputedStyle(p);
    if(s.position==='absolute'||s.position==='fixed'||p.getAttribute('role')==='dialog') return p; p=p.parentElement; d++; } return e.parentElement||e; }
  var g=calendarGrid(); if(!g) return {err:'no-grid'};
  var cells=[].filter.call(g.querySelectorAll('*'),function(e){ return leafText(e)===want && vis(e); });
  var ok=cells.filter(function(e){ return !badCell(e,g); });
  if(!ok.length) return {err:'day-not-selectable', n:cells.length};
  var e=(+want<=14)?ok[0]:ok[ok.length-1];
  e.scrollIntoView({block:'center'}); var r=e.getBoundingClientRect();
  var h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
  if(!(h&&(e.contains(h)||h.contains(e)))) return {err:'day-covered'};
  return {xy:[r.x+r.width/2,r.y+r.height/2]};
}
"""

TIME_COL_JS = r"""
() => {
  function vis(e){ var r=e.getBoundingClientRect();
    if(r.width<4||r.height<4) return false; var s=getComputedStyle(e);
    return s.visibility!=='hidden'&&s.display!=='none'; }
  function leafText(e){ for(var i=0;i<e.children.length;i++){ if(e.children[i].tagName!=='svg'&&(e.children[i].innerText||'').trim()) return null; }
    return (e.innerText||'').trim(); }
  function scroller(e){ var p=e.parentElement, d=0; while(p&&p!==document.body&&d<6){ var s=getComputedStyle(p);
    if((s.overflowY==='auto'||s.overflowY==='scroll')&&p.scrollHeight>p.clientHeight+2) return p;
    if(/option-list|column|listbox/i.test((p.className||'').toString()+' '+(p.getAttribute('role')||''))) return p; }
    return null; }
  var all=document.querySelectorAll('body *'), cols=[], items=[];
  for(var i=0;i<all.length;i++){ var e=all[i]; var t=leafText(e); if(!t||!/^(\d{1,2}|am|pm|a\.m\.|p\.m\.)$/i.test(t)) continue;
    var sc=scroller(e); if(!sc||!vis(sc)) continue;
    var k=cols.indexOf(sc); if(k<0){cols.push(sc); items.push([]); k=cols.length-1;} items[k].push(t.toLowerCase()); }
  var out=[]; for(var j=0;j<cols.length;j++){ var r=cols[j].getBoundingClientRect(); out.push({i:j, x:r.x, values:items[j]}); }
  out.sort(function(a,b){return a.x-b.x;}); return out;
}
"""

TIME_CLICK_JS = r"""
(args) => {
  var colIdx=args[0], want=String(args[1]).toLowerCase();
  function leafText(e){ for(var i=0;i<e.children.length;i++){ if(e.children[i].tagName!=='svg'&&(e.children[i].innerText||'').trim()) return null; }
    return (e.innerText||'').trim(); }
  function scroller(e){ var p=e.parentElement, d=0; while(p&&p!==document.body&&d<6){ var s=getComputedStyle(p);
    if((s.overflowY==='auto'||s.overflowY==='scroll')&&p.scrollHeight>p.clientHeight+2) return p;
    if(/option-list|column|listbox/i.test((p.className||'').toString()+' '+(p.getAttribute('role')||''))) return p; }
    return null; }
  var all=document.querySelectorAll('body *'), cols=[];
  for(var i=0;i<all.length;i++){ var e=all[i]; var t=leafText(e);
    if(t===null||!/^(\d{1,2}|am|pm|a\.m\.|p\.m\.)$/i.test(t)) continue;
    var sc=scroller(e); if(!sc||!vis(sc)) continue; if(cols.indexOf(sc)<0) cols.push(sc); }
  function vis(e){ var r=e.getBoundingClientRect();
    if(r.width<4||r.height<4) return false; var s=getComputedStyle(e);
    return s.visibility!=='hidden'&&s.display!=='none'; }
  cols.sort(function(a,b){return a.getBoundingClientRect().x-b.getBoundingClientRect().x;});
  var col=cols[colIdx]; if(!col) return null;
  var hit=[].filter.call(col.querySelectorAll('*'),function(e){ var t=leafText(e); return t!==null&&t.toLowerCase()===want; });
  if(!hit.length) return null; var e=hit[0];
  var opt=e.closest("[role='option'],li,[class*='item' i]")||e; opt.scrollIntoView({block:'center'});
  var r=opt.getBoundingClientRect(); return [r.x+r.width/2, r.y+r.height/2];
}
"""


class TikTokPoster(BasePoster):
    platform = "tiktok"
    upload_url = "https://www.tiktokstudio.com/upload?enter_from=page&lang=en"

    def login_check_js(self) -> str:
        return LOGIN_CHECK

    def _classify_and_click_dialogs(self, sess: PlaywrightSession,
                                    post_sel: str, sched_sel: str) -> dict | None:
        """Scan visible modals; click the right button (ported semantics)."""
        dialogs = sess.eval_js(f"""
        (args) => {{
          var sel = "[role='dialog'],[role='alertdialog'],[aria-modal='true'],"
                  + "[class*='TUXModal'],[class*='Modal'],[class*='Dialog']";
          var all = Array.prototype.slice.call(document.querySelectorAll(sel));
          function vis(e){{
            var r=e.getBoundingClientRect(); if(r.width<40||r.height<20) return false;
            var cs=getComputedStyle(e);
            return cs.display!=='none' && cs.visibility!=='hidden' && Number(cs.opacity||1)>0.05;
          }}
          var recovery=/wasn.t saved|was not saved|tidak tersimpan|you were editing|sedang anda edit/i;
          var cands = all.filter(function(e){{
            if(!vis(e)) return false;
            var t=(e.innerText||'').trim();
            if(!t || t.length>2000 || recovery.test(t)) return false;
            if(args[0] && e.querySelector(args[0])) return false;
            if(args[1] && e.querySelector(args[1])) return false;
            return true;
          }});
          cands = cands.filter(function(e){{
            return !cands.some(function(o){{ return o!==e && o.contains(e); }});
          }});
          var out=[];
          cands.slice(-4).forEach(function(m){{
            var r=m.getBoundingClientRect();
            var hit=document.elementFromPoint(r.x+r.width/2, r.y+Math.min(r.height/2, 60));
            var top = !!hit && (m.contains(hit) || hit.contains(m));
            var btns=[];
            m.querySelectorAll("button,[role='button']").forEach(function(b){{
              if(btns.length>=8) return;
              var br=b.getBoundingClientRect();
              if(br.width===0||br.height===0||br.width>400) return;
              if(b.disabled||b.getAttribute('aria-disabled')==='true') return;
              var lb=((b.innerText||'').trim()||b.getAttribute('aria-label')||'').replace(/\\s+/g,' ');
              if(!lb) return;
              btns.push({{label:lb.slice(0,60), cx:br.x+br.width/2, cy:br.y+br.height/2}});
            }});
            if(btns.length<1||btns.length>6) return;
            out.push({{text:(m.innerText||'').replace(/\\s+/g,' ').trim().slice(0,300),
                      buttons:btns, top:top}});
          }});
          return out;
        }}""", [post_sel, sched_sel])
        if not dialogs:
            return None
        for m in dialogs:
            for b in m.get("buttons", []):
                verdict = tt_classify_dialog_label(b["label"])
                if verdict == "deny":
                    continue
                if verdict in ("allow", "ack"):
                    sess.mouse_click(b["cx"], b["cy"])
                    time.sleep(1.0)
                    return {"clicked": b["label"], "verdict": verdict}
                if verdict == "neutral" and m.get("top"):
                    norm = tt_norm_label(b["label"])
                    if any(p in norm for p in NOW_PHRASES):
                        continue  # "post now instead" — never accept
                    sess.mouse_click(b["cx"], b["cy"])
                    time.sleep(1.0)
                    return {"clicked": b["label"], "verdict": "neutral"}
        return None

    def _set_schedule(self, sess: PlaywrightSession, when: str) -> bool:
        """Set the native TikTok schedule for ISO `when` (already local-ish).

        TikTok Studio's schedule dialog: click 'Schedule post' toggle →
        date grid → click target day → time scroll columns → set hour/min.
        """
        target = datetime.datetime.fromisoformat(when)
        target = target.astimezone()
        day = f"{target.day}"
        hour = f"{target.hour}" if target.hour else "12"
        minute = f"{target.minute:02d}"
        # open schedule mode: click the schedule radio (text label)
        for label in ("Schedule", "Jadwalkan", "Schedule post",
                      "Jadwalkan postingan"):
            if sess.click_by_text(label, match_prefix=True, timeout_s=3):
                break
        time.sleep(1.0)
        if not sess.wait_for_selector(SCHEDULE_CONTAINER, 10_000):
            raise UploadError("schedule",
                              "TikTok: container jadwal tidak muncul")
        # pick day on the calendar
        hit = sess.eval_js(CAL_SELECT_DAY_JS, day)
        if not isinstance(hit, dict) or "xy" not in (hit or {}):
            raise UploadError("schedule",
                              f"TikTok: tanggal {day} tak bisa dipilih ({hit})")
        sess.mouse_click(*hit["xy"])
        time.sleep(1.0)
        # time columns: [hour, minute, am/pm?]
        cols = sess.eval_js(TIME_COL_JS) or []
        if cols:
            h_idx = 0
            sess.eval_js("1")  # keepalive noop
            xy = sess.eval_js(TIME_CLICK_JS, [h_idx, hour])
            if isinstance(xy, list):
                sess.mouse_click(*xy)
                time.sleep(0.5)
            xy = sess.eval_js(TIME_CLICK_JS, [h_idx + 1, minute])
            if isinstance(xy, list):
                sess.mouse_click(*xy)
                time.sleep(0.5)
            if len(cols) >= 3:  # am/pm column present
                ampm = "am" if target.hour < 12 else "pm"
                xy = sess.eval_js(TIME_CLICK_JS, [h_idx + 2, ampm])
                if isinstance(xy, list):
                    sess.mouse_click(*xy)
        # confirm
        for label in ("Set schedule", "Tetapkan jadwal", "OK", "Confirm",
                      "Konfirmasi", "Done", "Selesai"):
            if sess.click_by_text(label, match_prefix=True, timeout_s=2):
                break
        return True

    def upload(self, *, asset_path, asset_kind, title, description, tags,
               schedule_at, visibility, progress_cb,
               made_for_kids=False, channel_id="") -> UploadResult:
        self._preflight(asset_path)
        if asset_kind != "video":
            raise UploadError("preflight",
                              "TikTok: asset bukan video (v1: video only)")
        cap = build_caption(title, description, tags)
        with self._session() as sess:
            progress_cb(5)
            try:
                sess.navigate(self.upload_url)
            except Exception:
                raise UploadError("preflight",
                                  "TikTok: gagal buka TikTok Studio (login?)")
            if not sess.wait_for_selector(FILE_INPUT, 30_000):
                raise UploadError("session",
                                  "TikTok: tidak login — Hubungkan akun dulu")
            progress_cb(10)
            sess.upload_file(FILE_INPUT, asset_path)
            progress_cb(25)

            deadline = time.monotonic() + 180
            caption_done = False
            while time.monotonic() < deadline:
                dlg = self._classify_and_click_dialogs(
                    sess, POST_BUTTON, SCHEDULE_CONTAINER)
                if not caption_done and sess.wait_for_selector(CAPTION_SEL, 2_000):
                    try:
                        sess.click(CAPTION_SEL)
                        sess.select_all()
                        sess.page.keyboard.press("Delete")
                        sess.insert_text(cap)
                        caption_done = True
                        progress_cb(45)
                    except Exception:
                        pass
                if sess.eval_js(
                    f"() => {{ var b=document.querySelector(\"{POST_BUTTON}\");"
                    "return b ? !b.disabled && b.getAttribute('aria-disabled')"
                    "!=='true' : false; }"):
                    break
                time.sleep(1.5)
            else:
                raise UploadError("stall", "TikTok: upload tidak selesai "
                                  "dalam 180s")

            if not caption_done:
                sess.click(CAPTION_SEL)
                sess.insert_text(cap)
            progress_cb(70)

            if schedule_at:
                self._set_schedule(sess, schedule_at)
            progress_cb(85)

            url_before = sess.current_url()
            # final dialog(s) may appear (e.g. 'Continue to post?')
            self._classify_and_click_dialogs(sess, POST_BUTTON,
                                             SCHEDULE_CONTAINER)
            btn = sess.eval_js(
                f"() => {{ var b=document.querySelector(\"{POST_BUTTON}\");"
                "if(!b) return null; var r=b.getBoundingClientRect();"
                "return {t:b.innerText, cx:r.x+r.width/2, cy:r.y+r.height/2,"
                "dis:b.disabled||b.getAttribute('aria-disabled')==='true'}; }")
            if not btn:
                raise UploadError("publish", "TikTok: tombol Post hilang")
            if btn.get("dis"):
                raise UploadError("publish",
                                  "TikTok: tombol Post tidak aktif (upload "
                                  "belum selesai / caption kosong?)")
            sess.mouse_click(btn["cx"], btn["cy"])
            time.sleep(2)
            # handle confirmation dialogs after clicking Post
            for _ in range(4):
                self._classify_and_click_dialogs(sess, POST_BUTTON,
                                                 SCHEDULE_CONTAINER)
                time.sleep(1.5)
                cur = sess.current_url()
                if "/upload" not in cur or cur != url_before:
                    break
            if "/upload" in sess.current_url() and sess.current_url() == url_before:
                raise UploadError("publish",
                                  "TikTok: tidak ada konfirmasi redirect — "
                                  "status posting tidak pasti")
            progress_cb(100)
            return UploadResult(ok=True,
                                note="posted" + (" (scheduled)" if schedule_at
                                                 else ""))


def build_caption(title: str, description: str, tags) -> str:
    parts = [p for p in (title, description) if p]
    tag_list = [t if t.startswith("#") else "#" + t for t in (tags or [])]
    cap = "\n".join(parts).strip()
    if tag_list:
        cap = (cap + "\n" + " ".join(tag_list)).strip()
    return cap
