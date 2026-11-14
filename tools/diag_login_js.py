"""Diagnostic: run each platform's login_check_js on its real login page.

Prints URL, JS result, and any exception — proves the poll loop evaluates
cleanly while NOT logged in. Read-only; closes the browser at the end.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FORGE_DATA", os.path.join(os.environ["TEMP"], "forge-diag"))

from playwright.sync_api import sync_playwright

from server.accounts import platforms
from server.accounts.manager import LAUNCH_ARGS, STEALTH_JS, find_chrome


def main():
    exe = find_chrome()
    with sync_playwright() as pw:
        for pid, spec in platforms.all().items():
            profile = os.path.join(os.environ["FORGE_DATA"], "profiles", pid)
            os.makedirs(profile, exist_ok=True)
            kwargs = dict(user_data_dir=profile, headless=True,
                          args=LAUNCH_ARGS, viewport=None)
            if exe:
                kwargs["executable_path"] = exe
            else:
                kwargs["channel"] = "chrome"
            ctx = pw.chromium.launch_persistent_context(**kwargs)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.add_init_script(STEALTH_JS)
            try:
                page.goto(spec.login_url, wait_until="domcontentloaded",
                          timeout=60_000)
                time.sleep(3)
                url = page.url
                try:
                    result = page.evaluate(spec.login_check_js)
                    err = None
                except Exception as e:
                    result, err = None, repr(e)
                print(f"[{pid}] url={url[:70]}")
                print(f"    check_js -> {result!r}  err={err}")
                # also validate account_name_js syntax
                try:
                    page.evaluate(spec.account_name_js)
                    print(f"    account_name_js OK")
                except Exception as e:
                    print(f"    account_name_js ERR {e!r:.120}")
            finally:
                ctx.close()


if __name__ == "__main__":
    main()
