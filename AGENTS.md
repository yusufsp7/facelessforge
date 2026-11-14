# AGENTS.md — ContentForge

> Guide for any AI agent (or human) opening this codebase. Read this before
> changing anything. Keep it updated when architecture changes.

## What this is

FacelessForge = social-media account manager + AI content generator + upload
scheduler in ONE process. Runs on a laptop, VM, VPS, or any self-host box —
it's a plain web app (FastAPI serving a vanilla-JS UI at `http://localhost:8790`.
The app name is FacelessForge (was ContentForge — renamed when the
faceless-shorts generator became the product's core).).

Three user-facing pillars:
1. **Accounts** — connect social accounts by logging in manually in a real
   Chrome window; sessions persist on disk per account (no OAuth, no API keys).
2. **Generator** — pluggable AI engines that produce assets (audio/text/…).
3. **Schedule** — queue assets for upload to connected accounts, now or later.

## Provenance (why the code looks the way it does)

- **Login/upload architecture is reverse-engineered from AutoClip**
  (`C:\Program Files\AutoClip`, a PyInstaller Python backend). We extracted
  its PYZ and ported its verified approach: Playwright
  `launch_persistent_context` on the user's REAL installed Chrome, one
  on-disk profile dir per account, stealth JS + `--disable-blink-features=AutomationControlled`,
  login by manual user login + poll loop, upload through the native platform
  composers (TikTok Studio, YouTube Studio, FB Reels composer, …).
  Research artifacts: `../_research/autoclip-extract/` (extracted backend source).
- **The ONLY generation system is the `faceless-shorts` skill** bundled at
  `vendor/faceless-shorts/` (the user's second Google Drive file). Earlier
  generation systems (yue2 music engine + its `vendor/yue2` runtime,
  gemini_web, muse stubs) were **deleted at the user's request on 2026-11-14
  — do not re-add them**; same for rea-video.
  Pipeline (see `server/generator.py`): new_video.sh → optional
  FORGE_WRITER_HOOK (LLM writes script.json AND src/shots.js together —
  the skill's `Wd(word, line)` throws when a rewritten script's words are
  missing from shots) → voice.py (Kokoro) → words.py (Whisper) →
  capture.mjs check/cues → mix.py → capture.mjs video. Style previews =
  `styles/<id>/style-sheet.png` served at /api/styles/{id}/sheet.png.

## Layout

```
server/
  main.py            FastAPI app; accounts/connects endpoints; static UI mount
  api_content.py     /api engines|generate|assets|media|jobs|worker|history|settings
  db.py              SQLite (connect-per-call, WAL); tables: accounts, assets,
                     jobs, history, settings
  paths.py           data layout (FORGE_DATA override; defaults ./data)
  accounts/
    platforms.py     PLATFORMS registry: login_url, upload_url, login_check_js,
                     account_name_js per platform (tiktok/youtube/facebook/
                     instagram/threads)
    manager.py       AccountManager: connect (blocking, poll loop, auto-relaunch
                     on window close), is_connected probe, cookies import/export,
                     in-flight connect status (for /api/connects)
  posters/
    base.py          PlaywrightSession (one Chrome session per upload call) +
                     BasePoster; UploadError(stage, msg), UploadResult
    tiktok.py / youtube.py / facebook.py / instagram.py / threads.py
  engines/
  generator.py       THE generation system: faceless-shorts skill runner
                     (style catalog + previews, readiness status, setup.sh
                     runner, generate pipeline thread, writer hook)
  worker.py          Worker thread: claims queued jobs due now, ONE browser per
                     platform (PlatformLocks), per-account execution, pacing delay,
                     1 auto-retry, failure screenshots to data/media/diag/
  ui/                index.html + app.js + style.css — vanilla JS, no build step
tools/               diagnostics (login-check JS smoke test, login screenshot)
tests/               pytest suite (25 tests)
run.py               uvicorn entry (FORGE_PORT, default 8790)
start.bat / stop.bat Windows launch/stop (stop kills the FORGE_PORT listener;
                     never touches Chrome)
```

## Running / testing

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt  # Windows
.venv/Scripts/python run.py          # serves http://localhost:8790
.venv/Scripts/python -m pytest tests/ -q
```

Env vars: `FORGE_PORT` (8790), `FORGE_HEADLESS` (1 = headless for VPS),
`FORGE_MUSE` (1 = enable muse engine), `FORGE_MUSE_HOOK`, `FORGE_DATA`
(data dir override), `FORGE_PACING` (seconds between uploads, default 20).

Do NOT leave multiple `run.py` instances running — ports collide silently
(we've been bitten: leftover processes kept serving stale code).

## Hard invariants — do not break

1. **Playwright sync API is thread-affine.** Never share contexts/pages across
   threads. Every public method opens `sync_playwright()` fresh and stays on
   one thread start-to-finish (the worker pins each job to one thread).
2. **One browser per platform at a time** (`worker.PlatformLocks`). This is
   bot-detection defense; do not parallelize same-platform uploads.
3. **Login is manual + polled.** `AccountManager.connect` never fills
   credentials. It polls `login_check_js` on EVERY open tab every 3 s
   (timeout 600 s), auto-relaunches Chrome (≤5×) if the user closes the
   window, and persists the session in the profile dir on success.
4. **Live inputs**: read values with `input_value()`, never
   `get_attribute("value")` (React/Polymer set properties, not attributes).
5. **Emoji-safe typing**: rich editors get text via CDP `Input.insertText`
   line-by-line (`session.insert_text`), never `keyboard.type` (non-BMP
   chars break DraftJS/Lexical).
6. **Stealth**: keep the stealth JS + launch args exactly as in
   `manager.py`; user's real Chrome via `find_chrome()` (fallback
   `channel="chrome"`), never the bundled Chromium.
7. **Selectors carry EN + ID variants** ("Next"/"Berikutnya"/"Selanjutnya",
   "Share"/"Bagikan", …). Platform UIs change constantly — when a poster
   fails, look at the diagnostic screenshot in `data/media/diag/` first.
8. **DB access is connect-per-call** (`db.connect()` each helper call). Never
   share a `sqlite3.Connection` across threads. `db.connect()` self-inits;
   `init_db()` deliberately opens a raw connection to avoid a lock deadlock —
   don't "simplify" it back to `connect()`.
9. **Engine contract**: `Engine.generate(params, on_progress) -> dict` with
   keys `{kind, title, path, engine, meta}` or `{"error": ...}`. Heavy work
   must run in a thread (API spawns one) and never block the event loop.
10. **UI has no build step** — plain HTML/JS. `node --check server/ui/app.js`
    after editing JS. Users must hard-refresh (Ctrl+F5) after JS changes
    (no cache busting yet).

## Known quirks / open issues

- `tests/test_posters.py` doesn't set `FORGE_DATA`, so its preflight test
  creates `data/secrets/profiles/tiktok/unit_test` in the default data dir
  (harmless artifact; delete freely).
- Generation needs a one-time `setup.sh` (Node 22+, ffmpeg, Chrome checked;
  Kokoro ~340 MB + Whisper small.en ~0.5 GB downloaded to
  `~/.faceless-shorts/`). Without FORGE_WRITER_HOOK the generator renders
  each style's example project as-is (labeled demo), because shots.js must
  be rewritten together with script.json.
- Scripts run through Git Bash (`bash -lc`), matching the skill's Windows
  docs. `FORGE_SOFTWARE_GL=1` forces low-quality software GL for machines
  without GPU acceleration (much slower renders).
- Instagram & Threads posters are post-now only (no native web scheduling);
  the worker's own queue handles timed posting instead.
- The in-app browser used by the owner may not show `prompt()` reliably —
  UI avoids `prompt()` entirely (inline inputs only).
- No git repo yet; no CI. Run pytest before handing off changes.

## How to extend

- **New platform**: add a `PlatformSpec` in `accounts/platforms.py`
  (login/upload URLs, check JS with EN+ID variants), add a poster in
  `posters/` subclassing `BasePoster` (override `login_check_js` + `upload`),
  register in `posters/__init__.py`.
- **New faceless-shorts style**: follow the skill's own SKILL.md §"Adding a
  new style" (style-sheet.png + STYLE.md + working `example/`), then add a
  row to `generator.STYLES` so the UI picks it up.

## Self-updater (header Update button)

`server/updater.py` pulls this repo and restarts the process in place:
`git pull --ff-only` → `pip install -r requirements.txt` → `os.execv(run.py)`.
- Modes: manual (default) or auto (`settings.auto_update`, checkbox next to
  the Update button; loop interval `FORGE_UPDATE_INTERVAL_MIN`, default 30m).
  Auto-apply skips cycles while any upload job is running.
- `data/` is gitignored — an update never touches login profiles/db/media.
- Requires a git clone (`.git/` present); zip installs hide the button.
- Apply is ff-only (local commits block it — expected). The API response
  returns before the exec; the UI polls `/api/update` until `local_head`
  changes then reloads the page.

## Target VM (supported baseline)

Owner runs this on Ubuntu 24.04, ~2 shared vCPU (1.5 effective cores),
7.7 GB RAM, no GPU, ephemeral 7.5 GB root + persistent 150 GB home. The app
MUST keep working there — meaning:
- everything persistent lives under the project dir in HOME (never write
  state to /opt, /tmp, or root FS)
- `generator.software_gl()` auto-detects GPU-less Linux (no /dev/dri) and
  applies the skill's software-GL recipe (FPS 24, res 0.6, aa 0, shadow
  1024) — do not hardcode SOFTWARE_GL at call sites, always go through
  `_render_env()`
- default browser concurrency on ≤2-core machines is 1 (`max_concurrency()`),
  env `FORGE_MAX_CONCURRENCY` overrides
- preview MP4s render lazily on first hover on software-GL machines (no
  boot-time render storm); expect ~2s/frame renders there and set generous
  timeouts (render timeout is already 7200 s)
- Chrome runs headless with `--disable-dev-shm-usage` (+`--no-sandbox` when
  root) — keep those in `LAUNCH_ARGS`

## Conduct

- The owner runs this on their own accounts; automation may violate platform
  ToS — that risk is theirs. Don't add credential-harvesting, mass-follower
  or evasion features beyond the stealth needed to keep sessions working.
- UI strings are Indonesian; code identifiers and comments are English.
- When you change architecture, update this file AND `README.md`.
