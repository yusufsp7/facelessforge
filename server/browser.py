"""Central Chrome resolution — one answer for generator, posters, and setup.

Priority (mirrors what actually exists on the target VM):
  1. FORGE_CHROME / CHROME env (explicit override)
  2. puppeteer cache: ~/.cache/puppeteer/chrome/*/chrome-linux64/chrome
     (Chrome for Testing — proven to work for capture AND Playwright)
  3. platform-standard installs (Windows paths, /usr/bin/*, snap)

Returns the path or None. ``launch_env()`` additionally returns the env
extras capture.mjs needs (CHROME + NO_PROXY so skill python httpx never
routes localhost through a proxy).
"""
import glob
import os
import sys


def find_chrome() -> str | None:
    env = os.environ.get("FORGE_CHROME") or os.environ.get("CHROME")
    if env and os.path.isfile(env):
        return env
    home = os.path.expanduser("~")
    for pat in (os.path.join(home, ".cache", "puppeteer", "chrome", "*",
                             "chrome-linux64", "chrome"),
                os.path.join(home, ".cache", "puppeteer", "chrome", "*",
                             "chrome-linux", "chrome")):
        hits = sorted(glob.glob(pat), reverse=True)
        if hits:
            return hits[0]
    if sys.platform.startswith("win"):
        for cand in (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                     r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                     r"%LocalAppData%\Google\Chrome\Application\chrome.exe"):
            p = os.path.expandvars(cand)
            if os.path.isfile(p):
                return p
    else:
        for cand in ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
                     "/usr/bin/chromium", "/usr/bin/chromium-browser",
                     "/snap/bin/chromium"):
            if os.path.isfile(cand):
                return cand
    return shutil_which_chrome()


def shutil_which_chrome() -> str | None:
    import shutil
    return shutil.which("chrome") or shutil.which("google-chrome")


def launch_env() -> dict:
    """Env extras for capture.mjs / skill processes on this machine."""
    env = {}
    chrome = find_chrome()
    if chrome:
        env["CHROME"] = chrome
    # skill python (httpx/urllib) must bypass any proxy for localhost
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["no_proxy"] = "localhost,127.0.0.1"
    return env
