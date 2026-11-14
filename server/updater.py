"""Self-updater — pull the latest code from GitHub and restart in place. (patched r7)"""
import json
import os
import subprocess
import sys
import threading
import time

from . import db, paths

INTERVAL_MIN = int(os.environ.get("FORGE_UPDATE_INTERVAL_MIN", "30"))
PROJECT = paths.PROJECT_ROOT

_updating = False
_lock = threading.Lock()
_watchdog_started = False
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
                _last_result = {"supported": True, "error": out[-300:],
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


def _under_systemd() -> bool:
    return bool(os.environ.get("INVOCATION_ID"))


def _data_paths_in_diff(prev_head: str, new_head: str) -> list[str]:
    """R8: tolak update bila commit masuk menyentuh data/ (database,
    profil login, media) — git pull tidak boleh menimpa state pengguna."""
    rc, out = _git(["diff", "--name-only", f"{prev_head}..{new_head}",
                    "--", "data/"], timeout=60)
    if rc != 0:
        return []
    return [l for l in out.splitlines() if l.strip()]


def _backup_data(tag: str) -> str:
    """Snapshot forge.db (sqlite backup API — aman selama WAL aktif) +
    .api-token sebelum update. Balikin path folder backup."""
    import sqlite3
    bdir = paths.data_root() / "backups" / tag
    bdir.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(paths.db_path()))
    dst = sqlite3.connect(str(bdir / "forge.db"))
    src.backup(dst)
    dst.close()
    src.close()
    tok = paths.data_root() / ".api-token"
    if tok.is_file():
        (bdir / ".api-token").write_text(tok.read_text(encoding="utf-8"),
                                         encoding="utf-8")
    return str(bdir)


def _accounts_count(db_path) -> int:
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        n = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return -1


def _restore_accounts_from_backup(backup_dir: str) -> int:
    """Integritas pasca-update: kalau jumlah akun berkurang, kembalikan
    baris accounts/live_profiles/settings dari backup (INSERT OR IGNORE —
    tidak menimpa data yang lebih baru)."""
    import sqlite3
    src = sqlite3.connect(os.path.join(backup_dir, "forge.db"))
    dst = sqlite3.connect(str(paths.db_path()), timeout=30)
    n = 0
    for table in ("accounts", "live_profiles", "settings"):
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            cols = [d[1] for d in src.execute(f"PRAGMA table_info({table})")]
            pk = cols[0]
            marks = ",".join("?" * len(cols))
            sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks}) "
                   f"ON CONFLICT({pk}) DO NOTHING")
            for row in rows:
                cur = dst.execute(sql, row)
                n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except sqlite3.Error:
            continue
    dst.commit()
    dst.close()
    src.close()
    return n


def _no_active_work() -> bool:
    """Auto-apply aman hanya bila tak ada job running DAN tak ada generasi
    non-terminal (restart membunuh render — gen di-resume, tapi tetap
    kita hindari bila bisa)."""
    from . import generator
    if db.rows("SELECT id FROM jobs WHERE status = 'running' LIMIT 1"):
        return False
    terminal = ("done", "failed", "cancelled")
    busy = db.rows("SELECT id FROM generations WHERE status NOT IN "
                   f"({','.join(repr(t) for t in terminal)}) LIMIT 1")
    return not busy


def _post_pull_checks() -> tuple[bool, str]:
    """B6: jalankan pytest setelah pull — commit rusak dibatalkan."""
    py = sys.executable
    try:
        p = subprocess.run([py, "-m", "pytest", "-q"], cwd=str(PROJECT),
                           capture_output=True, text=True, timeout=900)
        return p.returncode == 0, (p.stdout or "")[-300:]
    except Exception as e:
        return False, repr(e)[:300]


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
    global _updating
    try:
        prev_head = _git(["rev-parse", "HEAD"])[1].strip()
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
        # B6 gate: pytest wajib lolos sebelum restart; gagal → rollback pull
        ok, detail = _post_pull_checks()
        if not ok:
            _git(["reset", "--hard", prev_head], timeout=60)
            db.log_event(None, "update_aborted_tests",
                         {"detail": detail, "rolled_back_to": prev_head[:7]})
            with _lock:
                _last_result = {"error": "pytest gagal — update dibatalkan",
                                "updates": False}
                _updating = False
            return
        # R8: guard — commit masuk tak boleh menyentuh data/
        new_head = _git(["rev-parse", "HEAD"])[1].strip()
        touching = _data_paths_in_diff(prev_head, new_head)
        if touching:
            _git(["reset", "--hard", prev_head], timeout=60)
            db.log_event(None, "update_aborted_data",
                         {"files": touching[:10]})
            with _lock:
                _last_result = {
                    "error": "commit menyentuh data/ — update dibatalkan",
                    "updates": False}
                _updating = False
            return
        # R8: backup database sebelum restart (akun/live/settings)
        backup_dir = _backup_data(prev_head[:7])
        accounts_before = _accounts_count(paths.db_path())
        db.log_event(None, "updated", {"branch": branch,
                                       "from": prev_head[:7],
                                       "backup": backup_dir,
                                       "accounts_before": accounts_before})
        with _lock:
            _last_result = {"updates": False, "applied": True}
        # B6 watchdog: proses baru wajib lulus /api/health; gagal → rollback
        marker = paths.data_root() / ".update-watchdog.json"
        marker.write_text(json.dumps({
            "prev_head": prev_head, "new_head": new_head,
            "backup_dir": backup_dir, "accounts_before": accounts_before,
            "until": time.time() + 180}), encoding="utf-8")
        if _under_systemd():
            # supervisor (Restart=always) yang menyalakan ulang — JANGAN spawn
            os._exit(0)
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


def start_watchdog() -> None:
    """R7: bila ada penanda update belum dikonfirmasi — probe health diri
    sendiri di thread terpisah, retry tiap 2 dtk sampai tenggat (180 dtk);
    rollback HANYA bila sampai tenggat tetap tidak sehat. Probe sekali-
    tembak tanpa tunggu selalu gagal (server sendiri belum naik) → semua
    update asli ter-rollback (bug terverifikasi di log riwayat)."""
    global _watchdog_started
    if _watchdog_started or not supported():
        return
    _watchdog_started = True
    try:
        marker = paths.data_root() / ".update-watchdog.json"
        if not marker.is_file():
            return
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return

    def _run():
        marker.unlink(missing_ok=True)
        until = data.get("until", 0)
        if time.time() > until:
            return
        port = os.environ.get("FORGE_PORT", "8790")
        import urllib.request
        healthy = False
        while time.time() < until:
            time.sleep(2)
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health",
                        timeout=5) as r:
                    if r.status == 200:
                        healthy = True
                        break
            except Exception:
                continue
        if healthy:
            db.log_event(None, "update_health_ok",
                         {"head": data.get("new_head", "")[:7]})
            # R8 integritas data: akun tidak boleh hilang setelah update
            before = int(data.get("accounts_before") or 0)
            backup_dir = data.get("backup_dir") or ""
            now = _accounts_count(paths.db_path())
            if before > 0 and now < before and backup_dir and \
                    os.path.isdir(backup_dir):
                restored = _restore_accounts_from_backup(backup_dir)
                db.log_event(None, "accounts_restored_after_update",
                             {"before": before, "after": now,
                              "restored": restored})
            return
        _git(["reset", "--hard", data["prev_head"]], timeout=60)
        db.log_event(None, "update_rolled_back",
                     {"to": data["prev_head"][:7]})
        # muat kode lama: systemd exit → supervisor restart; standalone
        # proses masih pegang kode baru → minta restart manual.
        if _under_systemd():
            os._exit(0)
        db.log_event(None, "update_rollback_manual_restart_needed",
                     {"to": data["prev_head"][:7]})

    threading.Thread(target=_run, daemon=True,
                     name="forge-update-watchdog").start()


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
                if not auto_enabled() or not _no_active_work():
                    continue
                st = check(force=True)
                if st.get("updates"):
                    db.log_event(None, "auto_update_start",
                                 {"behind": st.get("behind")})
                    apply()
            except Exception:
                continue

    threading.Thread(target=_loop, daemon=True,
                     name="forge-autoupdate").start()
