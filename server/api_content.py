"""Content API: faceless-shorts generator, assets, jobs, history, settings."""
import json
import os
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
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


@router.post("/generations/{gen_id}/continue")
def api_generation_continue(gen_id: str):
    """Two-phase: lanjutkan pipeline (voice→…→render) dengan file yang
    sudah ada di project — juga untuk generasi yang terputus restart."""
    r = generator.continue_generation(gen_id)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return r


class GenerateBody(BaseModel):
    style: str
    topic: str
    mode: str = "auto"            # auto | prepare
    voice: str = "am_michael"
    length_s: int = 35
    end_line: str = ""
    title: str = ""               # judul final (bukan topik)
    description: str = ""
    tags: list[str] = []
    account_key: str = ""         # channel tujuan
    topic_code: str = ""
    planned_time: str = ""        # rencana jam tayang WIB (informatif)


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
def api_asset_delete(asset_id: str, force: bool = False):
    a = db.one("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not a:
        raise HTTPException(404, "asset tidak ada")
    refs = db.rows("SELECT id FROM jobs WHERE asset_id = ?", (asset_id,))
    if refs and not force:
        raise HTTPException(400, f"masih direferensikan {len(refs)} job — "
                                 "batal job dulu, atau pakai ?force=true")
    if refs:
        for j in refs:
            db.update("jobs", j["id"], {"status": "cancelled",
                                        "error": "asset dihapus",
                                        "updated_at": db.now_iso()},
                      key_col="id")
    if a["path"] and os.path.isfile(a["path"]) and a["path"].startswith(
            str(paths.media_dir())):
        os.remove(a["path"])
    conn = db.connect()
    try:
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.post("/assets")
def api_asset_register(file: UploadFile | None = File(default=None),
                       path: str | None = Form(default=None),
                       title: str = Form(default=""),
                       description: str = Form(default=""),
                       tags: str = Form(default=""),
                       qc_approved: bool = Form(default=False)):
    """Register external video (upload atau path lokal) untuk dijadwalkan."""
    import shutil as _sh
    os.makedirs(str(paths.media_dir()), exist_ok=True)
    if file is not None:
        asset_id = uuid.uuid4().hex[:10]
        dst = str(paths.media_dir() / f"ext-{asset_id}.mp4")
        with open(dst, "wb") as f:
            _sh.copyfileobj(file.file, f)
    elif path and os.path.isfile(path):
        asset_id = uuid.uuid4().hex[:10]
        dst = path
    else:
        raise HTTPException(400, "berikan file (upload) atau path lokal valid")
    qc = generator.qc_validate(dst, {})
    if not qc.get("ok"):
        raise HTTPException(400, f"video tidak lolos ffprobe: {qc}")
    db.insert("assets", {
        "id": asset_id, "kind": "video",
        "title": title or os.path.basename(dst), "path": dst,
        "engine": "external",
        "meta_json": json.dumps({"description": description,
                                 "tags": [t.strip() for t in tags.split(",")
                                          if t.strip()]}, ensure_ascii=False),
        "qc_status": "approved" if qc_approved else "pending",
        "created_at": db.now_iso(),
    })
    return {"ok": True, "id": asset_id, "qc": qc}


@router.post("/assets/{asset_id}/qc")
def api_asset_qc(asset_id: str, approved: bool):
    if not db.one("SELECT id FROM assets WHERE id = ?", (asset_id,)):
        raise HTTPException(404, "asset tidak ada")
    generator.set_qc(asset_id, "approved" if approved else "rejected")
    return {"ok": True, "qc_status": "approved" if approved else "rejected"}


# ---------------------------------------------------------------- jobs

class JobBody(BaseModel):
    asset_id: str
    account_keys: list[str]
    mode: str = "now"            # now | at
    schedule_at: str | None = None   # ISO local
    visibility: str = "public"
    caption: dict = {}           # {title, description, tags, schedule_native}


def _schedule_tz() -> ZoneInfo:
    row = db.one("SELECT v FROM settings WHERE k = 'schedule_tz'")
    return ZoneInfo(row["v"] if row else "Asia/Jakarta")


@router.post("/jobs")
def api_create_jobs(body: JobBody):
    asset = db.one("SELECT * FROM assets WHERE id = ?", (body.asset_id,))
    if not asset:
        raise HTTPException(404, "asset tidak ada")
    if asset["qc_status"] != "approved":
        raise HTTPException(400, f"asset belum lolos QC "
                                 f"(status: {asset['qc_status']}) — "
                                 "approve di tab Konten dulu")
    if body.mode == "at":
        if not body.schedule_at:
            raise HTTPException(400, "schedule_at wajib untuk mode 'at'")
        try:
            local = datetime.fromisoformat(body.schedule_at)
        except ValueError:
            raise HTTPException(400, "schedule_at bukan ISO valid")
        if local.tzinfo is None:
            # naive input = jam tayang di zona jadwal (default WIB), bukan jam server
            local = local.replace(tzinfo=_schedule_tz())
        schedule_at = local.astimezone(ZoneInfo("UTC")).isoformat(
            timespec="seconds")
    else:
        schedule_at = db.now_iso()
    meta = json.loads(asset["meta_json"] or "{}")
    caption = {**{k: meta[k] for k in ("title", "description", "tags")
                  if meta.get(k)}, **body.caption}
    caption["made_for_kids"] = body.caption.get("made_for_kids", False)
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
            "caption_json": json.dumps(caption, ensure_ascii=False),
            "created_at": db.now_iso(),
            "updated_at": db.now_iso(),
        })
        db.log_event(jid, "job_created",
                     {"account": key, "schedule_at": schedule_at,
                      "visibility": body.visibility})
        created.append(jid)
    return {"created": created}


@router.get("/jobs")
def api_jobs(status: str | None = None):
    tz = _schedule_tz()
    if status:
        jobs = db.rows("SELECT * FROM jobs WHERE status = ? "
                       "ORDER BY schedule_at DESC LIMIT 200", (status,))
    else:
        jobs = db.rows("SELECT * FROM jobs ORDER BY schedule_at DESC LIMIT 200")
    accs = {a["key"]: a for a in db.rows(
        "SELECT key, platform, channel_id, handle FROM accounts")}
    for j in jobs:
        try:
            j["schedule_tz"] = (datetime.fromisoformat(j["schedule_at"])
                                .astimezone(tz).isoformat(timespec="seconds"))
        except Exception:
            j["schedule_tz"] = j["schedule_at"]
        j["schedule_tz_name"] = str(tz)
        a = accs.get(j["account_key"]) or {}
        j["channel_id"] = a.get("channel_id", "")
        j["handle"] = a.get("handle", "")
    return jobs


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


# ---------------------------------------------------------------- update

@router.get("/update")
def api_update_status():
    from . import updater
    return updater.check()


@router.post("/update/check")
def api_update_check():
    from . import updater
    return updater.check(force=True)


@router.post("/update/apply")
def api_update_apply():
    from . import updater
    return updater.apply()


class AutoUpdateBody(BaseModel):
    enabled: bool


@router.post("/update/auto")
def api_update_auto(body: AutoUpdateBody):
    from . import updater
    updater.set_auto(body.enabled)
    return {"ok": True, "auto": body.enabled}


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


@router.get("/preview-file")
def api_preview_file(path: str):
    """Serve internal QC artifacts (contact sheets) — path must live in data/."""
    import os as _os
    p = _os.path.abspath(path)
    if not p.startswith(str(paths.data_root())):
        raise HTTPException(403, "path di luar data/")
    if not _os.path.isfile(p):
        raise HTTPException(404, "file tidak ada")
    return FileResponse(p)
