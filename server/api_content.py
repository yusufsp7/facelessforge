"""Content API: faceless-shorts generator, assets, jobs, history, settings."""
import json
import os
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, generator, paths

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- generator

@router.get("/styles")
def api_styles():
    return generator.list_styles()


@router.get("/styles/{style_id}/sheet.png")
def api_style_sheet(style_id: str):
    p = generator.style_sheet_path(style_id)
    if not p:
        raise HTTPException(404, "style sheet tidak ada")
    return FileResponse(p, media_type="image/png")


@router.get("/styles/{style_id}/preview.mp4")
def api_style_preview(style_id: str):
    if style_id not in generator.STYLES:
        raise HTTPException(404, "style tidak ada")
    p = generator.preview_path(style_id)
    if not os.path.isfile(p):
        generator.ensure_preview(style_id)  # render in background
        raise HTTPException(404, "preview belum siap — dirender di background")
    return FileResponse(p, media_type="video/mp4")


@router.get("/generator/voices")
def api_generator_voices():
    return generator.list_voices()


@router.get("/generator/status")
def api_generator_status():
    st = generator.status()
    st["setup_running"] = bool(
        generator._setup_thread and generator._setup_thread.is_alive())
    st["setup_log"] = generator.setup_log_tail()
    return st


@router.post("/generator/setup")
def api_generator_setup():
    return generator.run_setup()


@router.get("/generations")
def api_generations():
    return generator.list_gens()


@router.get("/generations/{gen_id}")
def api_generation(gen_id: str):
    g = generator.get_gen(gen_id)
    if not g:
        raise HTTPException(404, "generation tidak ada")
    return g


class GenerateBody(BaseModel):
    style: str
    topic: str
    voice: str = "am_michael"
    length_s: int = 35
    end_line: str = ""


@router.post("/generate")
def api_generate(body: GenerateBody):
    try:
        gen_id = generator.generate(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"started": True, "gen_id": gen_id}


# ---------------------------------------------------------------- assets

@router.get("/assets")
def api_assets():
    return db.rows("SELECT * FROM assets ORDER BY created_at DESC LIMIT 200")


@router.get("/media/{asset_id}")
def api_media(asset_id: str):
    a = db.one("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not a or not a["path"]:
        raise HTTPException(404, "asset tidak ada")
    import os
    if not os.path.isfile(a["path"]):
        raise HTTPException(404, "file hilang")
    return FileResponse(a["path"])


@router.delete("/assets/{asset_id}")
def api_asset_delete(asset_id: str):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------- jobs

class JobBody(BaseModel):
    asset_id: str
    account_keys: list[str]
    mode: str = "now"            # now | at
    schedule_at: str | None = None   # ISO local
    visibility: str = "public"
    caption: dict = {}           # {title, description, tags, schedule_native}


@router.post("/jobs")
def api_create_jobs(body: JobBody):
    asset = db.one("SELECT * FROM assets WHERE id = ?", (body.asset_id,))
    if not asset:
        raise HTTPException(404, "asset tidak ada")
    if body.mode == "at":
        if not body.schedule_at:
            raise HTTPException(400, "schedule_at wajib untuk mode 'at'")
        try:
            local = datetime.fromisoformat(body.schedule_at)
        except ValueError:
            raise HTTPException(400, "schedule_at bukan ISO valid")
        if local.tzinfo is None:
            local = local.astimezone()
        schedule_at = local.astimezone(timezone.utc).isoformat(
            timespec="seconds")
    else:
        schedule_at = db.now_iso()
    created = []
    for key in body.account_keys:
        acct = db.one("SELECT * FROM accounts WHERE key = ?", (key,))
        if not acct:
            raise HTTPException(404, f"akun {key} tidak ada")
        jid = uuid.uuid4().hex[:16]
        db.insert("jobs", {
            "id": jid,
            "asset_id": body.asset_id,
            "account_key": key,
            "status": "queued",
            "schedule_at": schedule_at,
            "visibility": body.visibility,
            "caption_json": json.dumps(body.caption, ensure_ascii=False),
            "created_at": db.now_iso(),
            "updated_at": db.now_iso(),
        })
        db.log_event(jid, "job_created",
                     {"account": key, "schedule_at": schedule_at})
        created.append(jid)
    return {"created": created}


@router.get("/jobs")
def api_jobs(status: str | None = None):
    if status:
        return db.rows("SELECT * FROM jobs WHERE status = ? "
                       "ORDER BY schedule_at DESC LIMIT 200", (status,))
    return db.rows("SELECT * FROM jobs ORDER BY schedule_at DESC LIMIT 200")


@router.post("/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    db.update("jobs", job_id, {"status": "cancelled",
                               "updated_at": db.now_iso()}, key_col="id")
    return {"ok": True}


@router.post("/jobs/{job_id}/retry")
def api_job_retry(job_id: str):
    db.update("jobs", job_id, {"status": "queued", "attempts": 0,
                               "schedule_at": db.now_iso(),
                               "error": "",
                               "updated_at": db.now_iso()}, key_col="id")
    return {"ok": True}


# ---------------------------------------------------------------- worker status

@router.get("/worker")
def api_worker():
    from .worker import WORKER
    return {"active": WORKER.snapshot()}


# ---------------------------------------------------------------- history

@router.get("/history")
def api_history(limit: int = 100):
    return db.rows("SELECT * FROM history ORDER BY id DESC LIMIT ?",
                   (min(limit, 500),))


# ---------------------------------------------------------------- settings

@router.get("/settings")
def api_settings():
    return {r["k"]: r["v"] for r in db.rows("SELECT * FROM settings")}


class SettingsBody(BaseModel):
    values: dict[str, str]


@router.post("/settings")
def api_settings_set(body: SettingsBody):
    for k, v in body.values.items():
        db.upsert("settings", {"k": k, "v": v}, conflict_col="k")
    return {"ok": True}
