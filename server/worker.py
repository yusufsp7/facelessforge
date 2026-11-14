"""Upload worker — AutoClip-style scheduler core.

Runs queued jobs with CROSS-PLATFORM PARALLELISM but ONE browser per platform:
- jobs are grouped per ACCOUNT; each account's batch runs on its own thread
- a per-platform lock lets only one browser per platform run at a time, so two
  TikTok accounts never post in parallel from one IP (bot-detection)
- a global concurrency cap bounds total browsers so weak machines don't thrash
- all DB writes go through db.* connect-per-call helpers (thread-safe)

Failure policy: 1 automatic retry (job goes back to 'queued' +10 minutes),
then 'failed' with a diagnostic screenshot saved by the poster.
"""
import datetime
import json
import logging
import os
import threading
import time

from . import db, paths

log = logging.getLogger("forge.worker")

PACING_DELAY_S = float(os.environ.get("FORGE_PACING", "20"))  # between uploads
RETRY_DELAY_MIN = 10


class PlatformLocks:
    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, platform: str) -> threading.Lock:
        with self._guard:
            if platform not in self._locks:
                self._locks[platform] = threading.Lock()
            return self._locks[platform]


PLATFORM_LOCKS = PlatformLocks()


class ConcurrencyCap:
    def __init__(self, limit: int):
        self.sem = threading.Semaphore(max(1, limit))  # honor tuning (E28)


def _default_cap() -> ConcurrencyCap:
    from .generator import max_concurrency
    return ConcurrencyCap(max_concurrency())


class Worker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap = _default_cap()
        self._active: dict[str, dict] = {}  # key -> status snapshot for UI
        self._active_lock = threading.Lock()

    # -- poster resolution ----------------------------------------------

    def _poster_for(self, platform: str):
        from .posters import registry
        return registry.get(platform)

    # -- main loop -------------------------------------------------------

    def recover_stale(self) -> int:
        """Requeue jobs stuck 'running' after a crash/reboot (B11)."""
        stale = db.rows("SELECT id FROM jobs WHERE status = 'running'")
        for j in stale:
            db.update("jobs", j["id"], {
                "status": "queued", "schedule_at": db.now_iso(),
                "error": "dipulihkan setelah restart",
                "updated_at": db.now_iso()}, key_col="id")
        if stale:
            db.log_event(None, "jobs_recovered",
                         {"count": len(stale), "ids": [j["id"] for j in stale]})
        return len(stale)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.recover_stale()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="forge-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def bind_app(self, app) -> None:  # UI hooks
        self.app = app

    def snapshot(self) -> list[dict]:
        with self._active_lock:
            return list(self._active.values())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("worker tick failed")
            self._stop.wait(1.0)

    def _tick(self) -> None:
        due = db.rows(
            "SELECT * FROM jobs WHERE status = 'queued' AND schedule_at <= ? "
            "ORDER BY schedule_at LIMIT 8", (db.now_iso(),))
        for job in due:
            acct = db.one("SELECT * FROM accounts WHERE key = ?",
                          (job["account_key"],))
            if not acct or not acct["connected"]:
                db.update("jobs", job["id"], {
                    "status": "failed",
                    "error": "akun tidak terhubung / tidak ada",
                    "updated_at": db.now_iso(),
                }, key_col="id")
                db.log_event(job["id"], "job_failed",
                             {"error": "account missing"})
                continue
            thread = threading.Thread(
                target=self._run_job, args=(job,), daemon=True,
                name=f"forge-job-{job['id'][:8]}")
            thread.start()

    # -- job execution ---------------------------------------------------

    def _run_job(self, job: dict) -> None:
        db.update("jobs", job["id"], {"status": "running",
                                      "updated_at": db.now_iso()},
                  key_col="id")
        db.log_event(job["id"], "job_started", {})
        acct = db.one("SELECT * FROM accounts WHERE key = ?",
                      (job["account_key"],))
        asset = db.one("SELECT * FROM assets WHERE id = ?", (job["asset_id"],))
        platform = acct["platform"]
        cap_ok = self._cap.sem.acquire(timeout=600)
        try:
            lock = PLATFORM_LOCKS.get(platform)
            with lock:  # one browser per platform
                poster_cls = self._poster_for(platform)
                if poster_cls is None:
                    raise RuntimeError(f"poster untuk {platform} belum ada")
                meta = json.loads(job["caption_json"] or "{}")
                schedule_at = job["schedule_at"]
                # Native scheduling: only hand the platform a future time when
                # the caption asked for it; otherwise upload immediately.
                native_when = (meta.get("schedule_native") and schedule_at
                               and schedule_at > db.now_iso())
                poster = poster_cls(acct["profile_key"])
                result = poster.upload(
                    asset_path=asset["path"] if asset else None,
                    asset_kind=asset["kind"] if asset else "text",
                    title=meta.get("title", ""),
                    description=meta.get("description", ""),
                    tags=meta.get("tags", []),
                    schedule_at=native_when,
                    visibility=job["visibility"],
                    made_for_kids=bool(meta.get("made_for_kids", False)),
                    channel_id=(acct.get("channel_id") or ""),
                    progress_cb=lambda pct: self._progress(job["id"], pct),
                )
                db.update("jobs", job["id"], {
                    "status": "done" if result.ok else "failed",
                    "public_url": result.public_url or "",
                    "platform_post_id": result.platform_post_id or "",
                    "error": "" if result.ok else result.note,
                    "progress": 100 if result.ok else job["progress"],
                    "updated_at": db.now_iso(),
                }, key_col="id")
                db.log_event(job["id"], "job_finished",
                             {"ok": result.ok, "note": result.note,
                              "url": result.public_url})
                time.sleep(PACING_DELAY_S)
        except Exception as e:
            self._fail_or_retry(job, e)
        finally:
            if cap_ok:
                self._cap.sem.release()
            with self._active_lock:
                self._active.pop(job["id"], None)

    def _progress(self, job_id: str, pct: int) -> None:
        db.update("jobs", job_id, {"progress": int(pct),
                                   "updated_at": db.now_iso()},
                  key_col="id")
        with self._active_lock:
            self._active[job_id] = {"id": job_id, "progress": int(pct)}

    def _fail_or_retry(self, job: dict, err: Exception) -> None:
        attempts = (job["attempts"] or 0) + 1
        if attempts <= 1:
            nxt = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(minutes=RETRY_DELAY_MIN)).isoformat(
                       timespec="seconds")
            db.update("jobs", job["id"], {
                "status": "queued", "attempts": attempts,
                "error": repr(err)[:500], "schedule_at": nxt,
                "updated_at": db.now_iso(),
            }, key_col="id")
            db.log_event(job["id"], "job_retry",
                         {"error": repr(err)[:300], "next": nxt})
        else:
            db.update("jobs", job["id"], {
                "status": "failed", "attempts": attempts,
                "error": repr(err)[:500], "updated_at": db.now_iso(),
            }, key_col="id")
            db.log_event(job["id"], "job_failed", {"error": repr(err)[:300]})


WORKER = Worker()
