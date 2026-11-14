"""Live API: profil encoder, start/stop, status, health, broadcast, replay."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, live, live_broadcast
from .worker import PLATFORM_LOCKS

router = APIRouter(prefix="/api")


def _acct(account_key: str):
    a = db.one("SELECT * FROM accounts WHERE key = ?", (account_key,))
    if not a:
        raise HTTPException(404, f"akun {account_key} tidak ada")
    return a


@router.get("/live")
def api_live_list():
    out = []
    for r in db.rows("SELECT account_key FROM live_profiles"):
        p = live.public_profile(r["account_key"])
        if p:
            p["status"] = live.status(r["account_key"])
            out.append(p)
    return out


class LiveProfileBody(BaseModel):
    stream_key: str | None = None
    ingest_url: str | None = None
    ingest_backup: str | None = None
    media_path: str | None = None
    loop: bool | None = None
    desired_on: bool | None = None
    broadcast_id: str | None = None


@router.put("/live/{account_key}")
def api_live_save(account_key: str, body: LiveProfileBody):
    _acct(account_key)
    p = live.save_profile(account_key, body.model_dump())
    return {"ok": True, "profile": {**live.public_profile(account_key)}}


@router.get("/live/{account_key}")
def api_live_get(account_key: str):
    _acct(account_key)
    p = live.public_profile(account_key)
    if not p:
        raise HTTPException(404, "profil live belum dibuat")
    p["status"] = live.status(account_key)
    return p


@router.get("/live/{account_key}/log")
def api_live_log(account_key: str, n: int = 40):
    _acct(account_key)
    return {"lines": live.log_tail(account_key, n)}


@router.post("/live/{account_key}/start")
def api_live_start(account_key: str):
    _acct(account_key)
    live.save_profile(account_key, {"desired_on": 1})
    r = live.start(account_key)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return r


@router.post("/live/{account_key}/stop")
def api_live_stop(account_key: str):
    _acct(account_key)
    return live.stop(account_key, clear_desired=True)


@router.post("/live/{account_key}/ensure-broadcast")
def api_live_ensure_broadcast(account_key: str,
                              title: str = "LIVE 24/7",
                              description: str = "",
                              privacy: str = "public"):
    a = _acct(account_key)
    try:
        return live_broadcast.ensure_broadcast(
            account_key, title, description, privacy,
            a.get("channel_id") or "")
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/live/{account_key}/health")
def api_live_health(account_key: str):
    a = _acct(account_key)
    enc = live.status(account_key)
    yt = {"live": None, "note": "pakai endpoint ensure-broadcast/health "
          "berbasis browser bila perlu — dijalankan manual via UI agar "
          "tidak berebut sesi"}
    return {"encoder": enc, "youtube": yt,
            "healthy": enc["encoder_alive"] and enc["data_flowing"]}


@router.post("/live/{account_key}/youtube-health")
def api_live_youtube_health(account_key: str):
    a = _acct(account_key)
    lock = PLATFORM_LOCKS.get("youtube")
    with lock:
        r = live_broadcast.youtube_side_health(
            account_key, a.get("channel_id") or "")
    return r


@router.post("/live/{account_key}/replay")
def api_live_replay(account_key: str, keep_hours: float = 1.0):
    a = _acct(account_key)
    lock = PLATFORM_LOCKS.get("youtube")
    with lock:
        return live_broadcast.apply_replay_policy(
            account_key, a.get("channel_id") or "", keep_hours)
