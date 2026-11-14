"""Screenshot what the connect flow actually shows the user.

Opens a throwaway persistent profile with the SAME launch args/stealth as
AccountManager, navigates to a platform login page, screenshots it.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FORGE_DATA", os.path.join(os.environ["TEMP"], "forge-diag"))

from playwright.sync_api import sync_playwright

from server.accounts import platforms
from server.accounts.manager import LAUNCH_ARGS, STEALTH_JS, find_chrome


def main(platform: str, out: str, wait_s: int = 8):
    spec = platforms.get(platform)
    exe = find_chrome()
    profile = os.path.join(os.environ["FORGE_DATA"], "shots", platform)
    os.makedirs(profile, exist_ok=True)
    with sync_playwright() as pw:
        kwargs = dict(user_data_dir=profile, headless=False,
                      args=LAUNCH_ARGS, viewport=None)
        if exe:
            kwargs["executable_path"] = exe
        else:
            kwargs["channel"] = "chrome"
        ctx = pw.chromium.launch_persistent_context(**kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.add_init_script(STEALTH_JS)
        page.goto(spec.login_url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(wait_s)
        page.screenshot(path=out, full_page=False)
        print("shot:", out, "| url:", page.url[:90])
        ctx.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tiktok",
         sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.environ["TEMP"], "login_shot.png"))
