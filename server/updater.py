"""Self-updater — pull the latest code from GitHub and restart in place.

The app is deployed as a public git clone (yusufsp7/facelessforge). The
header's Update button checks origin for new commits and applies them:
``git pull --ff-only`` → ``pip install -r requirements.txt`` → execv restart
of this very process. ``data/`` (login profiles, db, media) is gitignored, so
an update never touches user state.

Modes (checkbox in the UI, persisted in settings.auto_update):
  - manual (default): the button only fetches/compares; applying needs a click
  - auto: a background loop checks every FORGE_UPDATE_INTERVAL_MIN minutes and
    applies updates when no upload job is running

Repo-less installs (zip, no .git) report supported=False and the UI hides the
button.
"""
import json
import os
import subprocess
import sys
import threading
import time

from . import db, paths

INTERVAL_MIN = int(os.environ.get("FORGE_UPDATE_INTERVAL_MIN", "30"))
PROJECT = paths.PROJECT_ROOT

_lock = threading.Lock()
_updating = False
_last_result: dict = {}
_loop_started = False


def supported() -> bool:
    return os.path.isdir(os.path.join(PROJECT, ".git"))


def _git(args: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(PROJECT),
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, repr(e)


def _branch() -> str:
    rc, out = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip() or "main" if rc == 0 else "main"


def _behind() -> tuple[int, str]:
    branch = _branch()
    rc, out = _git(["rev-list", "--count", f"HEAD..origin/{branch}"])
    if rc != 0:
        return 0, ""
    n = int(out.strip() or 0)
    rc, head = _git(["rev-parse", "--short", f"origin/{branch}"])
    return n, head.strip()


def check(force: bool = False) -> dict:
    """Fetch origin and report how many commits we're behind."""
    global _last_result
    if not supported():
        return {"supported": False, "updates": False}
    with _lock:
        if _updating:
            return {"supported": True, "updating": True, "updates": False,
                    **_last_result}
        if force or time.time() - _state_last_checked() > 300 or not _last_result:
            rc, out = _git(["fetch", "origin", "--quiet"], timeout=60)
            if rc != 0:
                _last_result = {"supported": True, "error": out[-200:],
                                "updates": False}
            else:
                behind, remote = _behind()
                _last_result = {"supported": True, "updates": behind > 0,
                                "behind": behind, "remote_head": remote}
            rc, local = _git(["rev-parse", "--short", "HEAD"])
            _last_result.setdefault("local_head", local.strip() if rc == 0 else "")
            _last_result["auto"] = auto_enabled()
            _last_result["checked_at"] = time.strftime("%H:%M:%S")
            _touch_last_checked()
        return {"supported": True, "updating": False, **_last_result}


_state_file = paths.data_root() / ".update-state.json"


def _state_last_checked() -> float:
    try:
        with open(_state_file, encoding="utf-8") as f:
            return json.load(f).get("checked_at_ts", 0)
    except Exception:
        return 0


def _touch_last_checked() -> None:
    try:
        with open(_state_file, "w", encoding="utf-8") as f:
            json.dump({"checked_at_ts": time.time()}, f)
    except Exception:
        pass


def auto_enabled() -> bool:
    row = db.one("SELECT v FROM settings WHERE k = 'auto_update'")
    return bool(row and row["v"] == "1")


def set_auto(enabled: bool) -> None:
    db.upsert("settings", {"k": "auto_update", "v": "1" if enabled else "0"},
              conflict_col="k")


def apply() -> dict:
    """Pull, install deps, restart this process. Runs in a thread so the HTTP
    response can be sent first; the UI then polls /api/update until the new
    process reports a new local_head."""
    global _updating
    if not supported():
        return {"ok": False, "error": "bukan git clone — update via git tidak tersedia"}
    with _lock:
        if _updating:
            return {"ok": False, "error": "update sedang berjalan"}
        _updating = True
    threading.Thread(target=_do_apply, daemon=True, name="forge-update").start()
    return {"ok": True, "restarting": True}


def _do_apply() -> None:
    try:
        branch = _branch()
        rc, out = _git(["pull", "--ff-only", "origin", branch], timeout=180)
        if rc != 0:
            db.log_event(None, "update_failed", {"error": out[-300:]})
            with _lock:
                _last_result = {"error": out[-200:], "updates": False}
                _updating = False
            return
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r",
             str(PROJECT / "requirements.txt")],
            cwd=str(PROJECT), capture_output=True, text=True, timeout=600)
        if pip.returncode != 0:
            db.log_event(None, "update_deps_failed",
                         {"error": (pip.stderr or "")[-300:]})
        db.log_event(None, "updated", {"branch": branch})
        with _lock:
            _last_result = {"updates": False, "applied": True}
        _exec_restart()
    finally:
        pass


def _exec_restart() -> None:
    """Restart in place: spawn a detached fresh run.py, then exit.

    os.execv is deliberately avoided — on Windows it mangles arguments with
    spaces ("Software project" split into two) and the new process dies
    instantly. A detached child + os._exit works on Windows and Linux.
    """
    time.sleep(1.2)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    log = open(paths.data_root() / "forge-restart.log", "w",
               encoding="utf-8")
    subprocess.Popen([sys.executable, str(PROJECT / "run.py")],
                     cwd=str(PROJECT), stdout=log, stderr=log,
                     creationflags=flags, close_fds=True)
    os._exit(0)


def _no_running_jobs() -> bool:
    running = db.rows("SELECT id FROM jobs WHERE status = 'running' LIMIT 1")
    return not running


def start_auto_loop() -> None:
    """Background loop: auto-apply updates when the checkbox is on."""
    global _loop_started
    if _loop_started or not supported():
        return
    _loop_started = True

    def _loop():
        while True:
            time.sleep(INTERVAL_MIN * 60)
            try:
                if not auto_enabled() or _no_running_jobs() is False:
                    continue
                st = check(force=True)
                if st.get("updates"):
                    db.log_event(None, "auto_update_start",
                                 {"behind": st.get("behind")})
                    apply()
            except Exception:
                continue

    threading.Thread(target=_loop, daemon=True, name="forge-autoupdate").start()
