"""Poster base — the AutoClip upload-session core.

PlaywrightSession drives the user's real installed Chrome through a
persistent per-account profile (``data/secrets/profiles/{platform}/{key}``),
with the same stealth JS and launch flags AutoClip uses. Each ``upload()``
launches and tears Chrome down inside a single thread call (sync Playwright is
thread-affine); the worker's per-platform lock guarantees a platform never
runs two browsers at once.
"""
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field

from .. import paths
from ..accounts.manager import LAUNCH_ARGS, STEALTH_JS, find_chrome

DEFAULT_TIMEOUT_S = 120


class UploadError(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message


@dataclass
class UploadResult:
    ok: bool
    public_url: str = ""
    platform_post_id: str = ""
    note: str = ""
    schedule_requested: bool = False
    schedule_set: bool = False


# --- shared JS helpers (ported from AutoClip's verified drivers) ----------

CLICK_BY_TEXT_JS = """
(args) => {
    var target_text = args[0], match_prefix = args[1],
        case_sensitive = args[2], scope_sel = args[3];
    var scope = scope_sel ? document.querySelector(scope_sel) : document;
    if (!scope) return null;
    var candidates = scope.querySelectorAll(
        'button, a, [role="button"], [role="link"], [role="menuitem"], '
      + '[role="option"], [role="tab"], [role="menuitemradio"], '
      + '[role="radio"], div[tabindex], span[role]');
    var cmp_text = case_sensitive ? target_text : target_text.toLowerCase();
    for (var el of candidates) {
        var r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        var s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') continue;
        var t = (el.innerText || el.textContent || '').trim();
        if (!t) continue;
        var cmp = case_sensitive ? t : t.toLowerCase();
        var matched = match_prefix ? cmp.startsWith(cmp_text) : cmp === cmp_text;
        if (matched) return {text: t.slice(0, 80),
                             cx: r.left + r.width / 2,
                             cy: r.top + r.height / 2};
    }
    return null;
}
"""


class PlaywrightSession:
    """One persistent-Chrome browser session bound to one account profile."""

    def __init__(self, profile_dir: str, headless: bool | None = None):
        self.profile_dir = profile_dir
        env_headless = os.environ.get("FORGE_HEADLESS") == "1"
        self.headless = env_headless if headless is None else headless
        self._pw = None
        self._context = None
        self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        kwargs = dict(
            user_data_dir=self.profile_dir,
            headless=self.headless,
            args=LAUNCH_ARGS,
            viewport=None,
        )
        exe = find_chrome()
        if exe:
            kwargs["executable_path"] = exe
        else:
            kwargs["channel"] = "chrome"
        self._context = self._pw.chromium.launch_persistent_context(**kwargs)
        self.page = self._context.pages[0] if self._context.pages \
            else self._context.new_page()
        self.page.add_init_script(STEALTH_JS)
        self.page.set_default_timeout(30_000)
        return self

    def __exit__(self, *exc):
        try:
            if self._context:
                self._context.close()
        finally:
            if self._pw:
                self._pw.stop()
        self._context = None
        self._pw = None
        self.page = None
        return False

    # -- primitives ------------------------------------------------------

    def navigate(self, url: str, timeout_ms: int = 60_000) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def eval_js(self, js: str, arg=None):
        return self.page.evaluate(js, arg)

    def wait_for_selector(self, selector: str, timeout_ms: int = 20_000,
                          state: str = "visible") -> bool:
        try:
            self.page.wait_for_selector(selector, state=state,
                                        timeout=timeout_ms)
            return True
        except Exception:
            return False

    def click(self, selector: str, timeout_ms: int = 20_000) -> None:
        self.page.locator(selector).first.click(timeout=timeout_ms)

    def type_text(self, selector: str, text: str) -> None:
        self.page.locator(selector).first.fill(text)

    def insert_text(self, text: str) -> None:
        """Emoji-safe typing into the focused rich editor (CDP insertText).

        AutoClip learned the hard way that keyboard.type chokes on non-BMP
        chars (emoji) inside DraftJS/Lexical editors, so type line by line
        with Shift+Enter for newlines.
        """
        lines = text.split("\n")
        cdp = self.page.context.new_cdp_session(self.page)
        try:
            for i, line in enumerate(lines):
                if i:
                    self.page.keyboard.press("Shift+Enter")
                if line:
                    cdp.send("Input.insertText", {"text": line})
                time.sleep(0.05)
        finally:
            cdp.detach()

    def select_all(self) -> None:
        self.page.keyboard.press("Control+a")

    def mouse_click(self, cx: float, cy: float) -> None:
        """Trusted click at coordinates (custom components ignore JS clicks)."""
        self.page.mouse.click(cx, cy)

    def click_by_text(self, text: str, match_prefix: bool = False,
                      case_sensitive: bool = False,
                      scope: str | None = None,
                      timeout_s: int = 10) -> dict | None:
        """Find a visible element by its label text; trusted-click its center."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            hit = self.eval_js(CLICK_BY_TEXT_JS,
                               [text, match_prefix, case_sensitive, scope])
            if hit:
                self.mouse_click(hit["cx"], hit["cy"])
                return hit
            time.sleep(0.5)
        return None

    def upload_file(self, selector: str, path: str) -> None:
        self.page.locator(selector).first.set_input_files(path)

    def input_value(self, selector: str) -> str:
        """Read a live input's property (get_attribute('value') is stale)."""
        return self.page.locator(selector).first.input_value()

    def current_url(self) -> str:
        return self.page.url

    def get_text(self, selector: str, timeout_ms: int = 10_000) -> str:
        try:
            self.page.wait_for_selector(selector, timeout=timeout_ms)
            return self.page.locator(selector).first.inner_text()
        except Exception:
            return ""

    def screenshot(self, name: str) -> str:
        out = paths.diag_dir() / f"{name}.png"
        try:
            self.page.screenshot(path=str(out), full_page=True)
        except Exception:
            pass
        return str(out)


class BasePoster:
    """Base class for platform upload posters."""

    platform: str = ""
    upload_url: str = ""
    session_probe_url: str = ""

    def __init__(self, profile_key: str):
        from .. import paths as _p
        d = _p.profiles_dir() / self.platform / profile_key
        d.mkdir(parents=True, exist_ok=True)
        self.profile_dir = str(d)

    # -- helpers ---------------------------------------------------------

    def _session(self) -> PlaywrightSession:
        return PlaywrightSession(self.profile_dir)

    def _preflight(self, asset_path: str | None) -> None:
        if asset_path and not os.path.isfile(asset_path):
            raise UploadError("preflight",
                              f"{self.platform}: file tidak ada: {asset_path}")
        if asset_path and os.path.getsize(asset_path) > 4 * 1024**3:
            size_gb = os.path.getsize(asset_path) / 1024**3
            raise UploadError(
                "preflight",
                f"{self.platform}: file {size_gb:.1f} GB melebihi batas 4 GB")

    def verify_session(self) -> tuple[bool, str]:
        spec_probe = self.session_probe_url or self.upload_url
        with self._session() as sess:
            try:
                sess.navigate(spec_probe)
            except Exception as e:
                return False, f"tidak bisa membuka Chrome: {e!r:.80}"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if sess.eval_js(self.login_check_js()):
                        return True, ""
                except Exception:
                    pass
                time.sleep(1)
            return False, (f"{self.platform}: sesi tidak valid — login ulang "
                           "di tab Akun")

    def login_check_js(self) -> str:
        raise NotImplementedError

    def upload(self, *, profile_key: str, asset_path, asset_kind, title,
               description, tags, schedule_at, visibility, progress_cb,
               **kw) -> UploadResult:
        raise NotImplementedError
