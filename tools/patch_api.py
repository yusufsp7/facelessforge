"""One-shot patcher: api_content.py two-phase/assets/QC/timezone (run once)."""
import sys

P = "server/api_content.py"
s = open(P, encoding="utf-8").read()
orig = s

s = s.replace(
    "import json\nimport os\nimport threading\nimport uuid\nfrom datetime import datetime, timezone",
    "import json\nimport os\nimport threading\nimport uuid\nfrom datetime import datetime\nfrom zoneinfo import ZoneInfo")

s = s.replace(
    "from fastapi import APIRouter, HTTPException",
    "from fastapi import APIRouter, File, Form, HTTPException, UploadFile")

s = s.replace(
    'class GenerateBody(BaseModel):\n    style: str\n    topic: str\n    voice: str = "am_michael"\n    length_s: int = 35\n    end_line: str = ""\n',
    'class GenerateBody(BaseModel):\n    style: str\n    topic: str\n    mode: str = "auto"            # auto | prepare\n    voice: str = "am_michael"\n    length_s: int = 35\n    end_line: str = ""\n    title: str = ""               # judul final (bukan topik)\n    description: str = ""\n    tags: list[str] = []\n    account_key: str = ""         # channel tujuan\n    topic_code: str = ""\n    planned_time: str = ""        # rencana jam tayang WIB (informatif)\n')

s = s.replace(
    '@router.post("/generations/{gen_id}")',
    '@router.post("/generations/{gen_id}/continue")\ndef api_generation_continue(gen_id: str):\n    r = generator.continue_generation(gen_id)\n    if r.get("error"):\n        raise HTTPException(400, r["error"])\n    return r\n\n\n@router.get("/generations/{gen_id}")')

s = s.replace(
    'def api_asset_delete(asset_id: str):\n    conn = db.connect()\n    try:\n        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))\n        conn.commit()\n    finally:\n        conn.close()\n    return {"ok": True}',
    'def api_asset_delete(asset_id: str, force: bool = False):\n    a = db.one("SELECT * FROM assets WHERE id = ?", (asset_id,))\n    if not a:\n        raise HTTPException(404, "asset tidak ada")\n    refs = db.rows("SELECT id FROM jobs WHERE asset_id = ?", (asset_id,))\n    if refs and not force:\n        raise HTTPException(400, f"masih direferensikan {len(refs)} job — "\n                                 "batal job dulu, atau pakai ?force=true")\n    if refs:\n        for j in refs:\n            db.update("jobs", j["id"], {"status": "cancelled",\n                                        "error": "asset dihapus",\n                                        "updated_at": db.now_iso()},\n                      key_col="id")\n    if a["path"] and os.path.isfile(a["path"]) and a["path"].startswith(\n            str(paths.media_dir())):\n        os.remove(a["path"])\n    conn = db.connect()\n    try:\n        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))\n        conn.commit()\n    finally:\n        conn.close()\n    return {"ok": True}\n\n\n@router.post("/assets")\ndef api_asset_register(file: UploadFile | None = File(default=None),\n                       path: str | None = Form(default=None),\n                       title: str = Form(default=""),\n                       description: str = Form(default=""),\n                       tags: str = Form(default=""),\n                       qc_approved: bool = Form(default=False)):\n    """Register external video (upload atau path lokal) untuk dijadwalkan."""\n    import shutil as _sh\n    os.makedirs(str(paths.media_dir()), exist_ok=True)\n    if file is not None:\n        asset_id = uuid.uuid4().hex[:10]\n        dst = str(paths.media_dir() / f"ext-{asset_id}.mp4")\n        with open(dst, "wb") as f:\n            _sh.copyfileobj(file.file, f)\n    elif path and os.path.isfile(path):\n        asset_id = uuid.uuid4().hex[:10]\n        dst = path\n    else:\n        raise HTTPException(400, "berikan file (upload) atau path lokal valid")\n    qc = generator.qc_validate(dst, {})\n    if not qc.get("ok"):\n        raise HTTPException(400, f"video tidak lolos ffprobe: {qc}")\n    db.insert("assets", {\n        "id": asset_id, "kind": "video",\n        "title": title or os.path.basename(dst), "path": dst,\n        "engine": "external",\n        "meta_json": json.dumps({"description": description,\n                                 "tags": [t.strip() for t in tags.split(",")\n                                          if t.strip()]}, ensure_ascii=False),\n        "qc_status": "approved" if qc_approved else "pending",\n        "created_at": db.now_iso(),\n    })\n    return {"ok": True, "id": asset_id, "qc": qc}\n\n\n@router.post("/assets/{asset_id}/qc")\ndef api_asset_qc(asset_id: str, approved: bool):\n    if not db.one("SELECT id FROM assets WHERE id = ?", (asset_id,)):\n        raise HTTPException(404, "asset tidak ada")\n    generator.set_qc(asset_id, "approved" if approved else "rejected")\n    return {"ok": True, "qc_status": "approved" if approved else "rejected"}')

s = s.replace(
    '''def api_create_jobs(body: JobBody):
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
    return {"created": created}''',
    '''def _schedule_tz() -> ZoneInfo:
    row = db.one("SELECT v FROM settings WHERE k = 'schedule_tz'")
    return ZoneInfo(row["v"] if row else "Asia/Jakarta")


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
    return {"created": created}''')

s = s.replace(
    '''def api_jobs(status: str | None = None):
    if status:
        return db.rows("SELECT * FROM jobs WHERE status = ? "
                       "ORDER BY schedule_at DESC LIMIT 200", (status,))
    return db.rows("SELECT * FROM jobs ORDER BY schedule_at DESC LIMIT 200")''',
    '''def api_jobs(status: str | None = None):
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
    return jobs''')

if s == orig:
    print("NOTHING CHANGED — anchors missing!")
    sys.exit(1)
open(P, "w", encoding="utf-8").write(s)
print("api_content patched")
