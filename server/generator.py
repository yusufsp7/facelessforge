"""faceless-shorts generator — the ONLY content generation system.

Drives the agent skill bundled at ``vendor/faceless-shorts/`` (the user's
Google Drive file): narrated faceless shorts made 100% in code — script.json
→ local Kokoro voice → Whisper word timings → shots drawn in JS → headless
Chrome capture → ffmpeg MP4. No AI images/video, no API keys, $0.

Proven-reliable pipeline on the GPU-less VM (see AGENTS.md):
  - render is CHUNKED: capture.mjs video [start] [end] per 6s chunk, each
    ffprobe-verified, checkpointed (resume after crash/reboot), concatenated
    with ``ffmpeg -f concat -c copy +faststart``. One long Chrome invocation
    (>14 min wall) is proven to livelock SwiftShader — chunks keep every
    invocation minutes-long with per-chunk timeouts well under that.
  - Chrome path comes from server.browser (env override → puppeteer cache);
    capture.mjs never has to guess. NO_PROXY is always set for skill python.
  - generations are persisted (SQLite) so restarts/reboots lose nothing;
    stale ones resume from their checkpoint, two-phase "prepare" keeps
    scaffolded projects for an external writer (Muse/agent).
  - a single render runs at a time (software-GL machine); queued ones wait.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

from . import db, paths
from .browser import launch_env

SKILL_DIR = os.path.join(paths.PROJECT_ROOT, "vendor", "faceless-shorts")
VIDEOS_DIR = paths.data_root() / "videos"
PREVIEWS_DIR = paths.data_root() / "previews"
SETUP_LOG = paths.data_root() / "gen-setup.log"

CHUNK_SECONDS = 6           # render chunk length (4-8s band from the report)
CHUNK_TIMEOUT_S = 540       # 9 min — well under the 14-min livelock wall
CHUNK_RETRIES = 1
QC_MIN_WIDTH, QC_MIN_HEIGHT = 1080, 1920

# id → (label, description) — durations mirror the skill's STYLE.md practice
STYLES = {
    "deep-dive": ("Deep Dive 3D", "Flagship ala Zack D. Films: manusia 3D di set terang, tubuh x-ray menyala, kamera menyelam ke pembuluh darah. \u201cWhat happens if\u2026?\u201d tubuh & sains."),
    "night-file": ("Night File 3D", "Misteri tak terpecahkan / true crime: ruang 3D gelap satu lampu, orang tanpa wajah (subjek menyala merah), tanggal mesin ketik."),
    "inside-out": ("Inside Out", "Explainer 2D x-ray: satu hero sederhana, potongan x-ray, caption besar 1-3 kata, cut cepat."),
    "stick-story": ("Stick Story", "Langkah bertahan hidup ala stick figure: orang stick dengan mata ekspresif, ruangan flat siang/malam, kartu langkah emas."),
    "paper-atlas": ("Paper Atlas", "Sejarah gaya kertas gunting: peta parchment, rute merah crimson, pita tanggal emas, figur masa lalu."),
    "tall-tale": ("Tall Tale 3D", "Cerita nyata lucu: orang kartun 2D (kepala besar, outline tinta) beraksi di dunia kota kecil 3D yang cerah, kamera selalu bergerak, caption satu kata."),
}


def skill_scripts_ok() -> bool:
    return os.path.isfile(os.path.join(SKILL_DIR, "scripts", "setup.sh"))


def _bash(cmd: str, cwd: str | None = None, timeout: int | None = None,
          env_extra: dict | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    env.update(launch_env())
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(["bash", "-lc", cmd], cwd=cwd, env=env,
                          timeout=timeout, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _win_path(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/")


# ------------------------------------------------------- machine profile

def software_gl() -> bool:
    """True when Chrome must render WebGL on CPU (no usable GPU)."""
    env = os.environ.get("FORGE_SOFTWARE_GL")
    if env == "1":
        return True
    if env == "0":
        return False
    if sys.platform.startswith("linux"):
        return not os.path.exists("/dev/dri")
    return False


def _render_env(fps: int) -> dict:
    """Env for capture.mjs on this machine (skill's software-GL recipe)."""
    env = {"FPS": str(fps)}
    if software_gl():
        env.update(SOFTWARE_GL="1", QS="&res=0.6&aa=0&shadow=1024")
    return env


def max_concurrency() -> int:
    """Browsers allowed at once — tuned DOWN for small shared-core VMs."""
    env = os.environ.get("FORGE_MAX_CONCURRENCY")
    if env and env.isdigit():
        return max(1, int(env))
    cores = os.cpu_count() or 2
    return 2 if cores >= 4 else 1


def _proj_dir(gen_id: str) -> str:
    return str(VIDEOS_DIR / f"forge-{gen_id}")


def keep_projects() -> bool:
    return os.environ.get("FORGE_KEEP_PROJECTS") == "1"


# ------------------------------------------------------------------ status

def status() -> dict:
    out = {"skill": skill_scripts_ok(), "node": False, "node_v": "",
           "ffmpeg": False, "chrome": False, "chrome_path": "",
           "python_env": False, "voice": False, "whisper": False,
           "hint": "", "software_gl": software_gl(),
           "max_concurrency": max_concurrency(),
           "data_dir": str(paths.data_root())}
    if not out["skill"]:
        out["hint"] = "vendor/faceless-shorts hilang — ekstrak ulang paket Drive."
        return out
    from .browser import find_chrome
    rc, so = _bash("node -v", timeout=15)
    if rc == 0:
        out["node_v"] = so.strip().splitlines()[-1]
        major = int("".join(ch for ch in out["node_v"].split(".")[0]
                            if ch.isdigit()) or 0)
        out["node"] = major >= 22
    rc, _ = _bash("command -v ffmpeg >/dev/null && command -v ffprobe",
                  timeout=15)
    out["ffmpeg"] = rc == 0
    chrome = find_chrome()
    out["chrome"] = bool(chrome)
    out["chrome_path"] = chrome or ""
    fh = os.environ.get("FACELESS_HOME", os.path.expanduser("~/.faceless-shorts"))
    py = os.path.join(fh, "venv", "Scripts", "python.exe")
    if not os.path.isfile(py):
        py = os.path.join(fh, "venv", "bin", "python")
    out["python_env"] = os.path.isfile(py)
    if out["python_env"]:
        pw = _win_path(py)
        # quotes balanced on purpose — regression-tested (see tests)
        rc, _ = _bash(f'"{pw}" -c "import kokoro_onnx"', timeout=30)
        out["voice"] = rc == 0 and _voice_model_ok(fh)
        rc, _ = _bash(
            f'"{pw}" -c "from faster_whisper import WhisperModel"', timeout=30)
        out["whisper"] = rc == 0
    missing = [k for k, ok in (("node 22+", out["node"]),
                               ("ffmpeg", out["ffmpeg"]),
                               ("chrome", out["chrome"]),
                               ("python-env", out["python_env"]),
                               ("voice (Kokoro)", out["voice"]),
                               ("whisper", out["whisper"])) if not ok]
    out["ready"] = not missing and skill_scripts_ok()
    if missing:
        out["hint"] = ("Belum siap: " + ", ".join(missing) +
                       " — klik 'Setup Generator' (unduh ~0.9GB, sekali saja).")
    elif out["software_gl"]:
        out["hint"] = ("Siap (mode software-GL: render 3D di CPU, ±2s/frame "
                       "— video 40s bisa ±40 menit).")
    else:
        out["hint"] = ""
    return out


def _voice_model_ok(fh: str) -> bool:
    return bool(glob.glob(os.path.join(fh, "kokoro", "**", "*.onnx"),
                          recursive=True))


_setup_thread: threading.Thread | None = None


def run_setup() -> dict:
    global _setup_thread
    if _setup_thread and _setup_thread.is_alive():
        return {"started": False, "detail": "setup sedang berjalan"}
    if not skill_scripts_ok():
        return {"started": False, "detail": "skill tidak ada"}

    def _run():
        cmd = f'bash "{_win_path(os.path.join(SKILL_DIR, "scripts", "setup.sh"))}"'
        proc = subprocess.Popen(["bash", "-lc", cmd], cwd=SKILL_DIR,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                env={**os.environ, **launch_env()})
        with open(SETUP_LOG, "w", encoding="utf-8") as log:
            for line in proc.stdout or []:
                log.write(line)
                log.flush()
        proc.wait()

    _setup_thread = threading.Thread(target=_run, daemon=True,
                                     name="forge-setup")
    _setup_thread.start()
    return {"started": True, "log": str(SETUP_LOG)}


def setup_log_tail(n: int = 15) -> list[str]:
    try:
        with open(SETUP_LOG, encoding="utf-8") as f:
            lines = f.read().splitlines()
        return lines[-n:]
    except OSError:
        return []


# ------------------------------------------------------------------ styles

def list_styles() -> list[dict]:
    out = []
    if not skill_scripts_ok():
        return out
    for sid, (label, desc) in STYLES.items():
        sheet = os.path.join(SKILL_DIR, "styles", sid, "style-sheet.png")
        out.append({"id": sid, "label": label, "description": desc,
                    "sheet": f"/api/styles/{sid}/sheet.png"
                    if os.path.isfile(sheet) else "",
                    "preview_ready": preview_ready(sid)})
    return out


def style_sheet_path(style_id: str) -> str | None:
    p = os.path.join(SKILL_DIR, "styles", style_id, "style-sheet.png")
    return p if os.path.isfile(p) else None


# ------------------------------------------------------------------ voices

def list_voices() -> list[dict]:
    out = []
    try:
        src = open(os.path.join(SKILL_DIR, "scripts", "voice.py"),
                   encoding="utf-8").read()
        block = src.split("GOOD = {", 1)[1].split("}", 1)[0]
        import re
        for m in re.finditer(r"'([a-z_]+)':\s*'([^']*)'", block):
            out.append({"id": m.group(1), "desc": m.group(2)})
    except Exception:
        pass
    if not out:
        out = [{"id": "am_michael", "desc": "US male, steady explainer (default)"},
               {"id": "af_heart", "desc": "US female, warm (best overall)"}]
    return out


# ------------------------------------------------------------------ previews

def preview_path(style_id: str) -> str:
    return str(PREVIEWS_DIR / f"{style_id}.mp4")


def preview_ready(style_id: str) -> bool:
    return os.path.isfile(preview_path(style_id))


_preview_lock = threading.Lock()
_preview_thread: threading.Thread | None = None
_preview_queue: list[str] = []


def ensure_preview(style_id: str) -> None:
    with _preview_lock:
        if preview_ready(style_id) or style_id in _preview_queue:
            return
        _preview_queue.append(style_id)
    _start_preview_worker()


def _start_preview_worker() -> None:
    global _preview_thread
    if _preview_thread and _preview_thread.is_alive():
        return
    _preview_thread = threading.Thread(target=_preview_loop, daemon=True,
                                       name="forge-previews")
    _preview_thread.start()


def _preview_loop() -> None:
    while True:
        with _preview_lock:
            if not _preview_queue:
                return
            sid = _preview_queue.pop(0)
        if preview_ready(sid):
            continue
        try:
            _render_preview(sid)
        except Exception as e:
            db.log_event(None, "preview_failed",
                         {"style": sid, "error": repr(e)[:200]})


def _render_preview(style_id: str) -> None:
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = VIDEOS_DIR / f"preview-{style_id}"
    skill = _win_path(SKILL_DIR)
    rc, out = _bash(f'bash "{skill}/scripts/new_video.sh" "{style_id}" '
                    f'"preview-{style_id}" --dir "{_win_path(str(VIDEOS_DIR))}"',
                    timeout=120)
    if rc != 0:
        raise RuntimeError(f"new_video: {out[-200]}")
    try:
        seconds, fps = _preview_params()
        proj = _win_path(str(tmp_dir))
        rc, out = _bash(f'node "{proj}/tools/capture.mjs" video '
                        f'"{proj}/renders/preview.mp4" 0 {seconds} --draft',
                        cwd=proj, timeout=CHUNK_TIMEOUT_S,
                        env_extra=_render_env(fps))
        if rc != 0:
            raise RuntimeError(f"capture: {out[-300]}")
        src = os.path.join(str(tmp_dir), "renders", "preview.mp4")
        if not os.path.isfile(src):
            raise RuntimeError("preview.mp4 tidak dihasilkan")
        shutil.copy2(src, preview_path(style_id))
    finally:
        _cleanup_dir(str(tmp_dir))


def _cleanup_dir(path: str) -> None:
    if keep_projects():
        return
    shutil.rmtree(path, ignore_errors=True)


def _preview_params() -> tuple[int, int]:
    if software_gl():
        return 4, 10
    return 5, 16


# ------------------------------------------------------------------ writer

WRITER_PROTOCOL = (
    'Return ONLY JSON: {"script": <script.json object>, "shots_js": '
    '"<full src/shots.js content>"}. script.json schema: {"title": str, '
    '"voice": "am_michael", "speed": 1.05, "lead": 0.3, "outro": 1.2, '
    '"gap": 0.18, "music": "<bed|none|audio/file.mp3 — pakai nilai example>", '
    '"sources": [str, ...], "end_line": "<opsional>", "lines": [{"text": str, '
    '"caps": "<chunk caption dgn | pemisah, opsional>", "gap": float(ops)}]}. '
    "Wd(word, line) in shots.js must match the script words exactly."
)


def _writer_hook() -> str | None:
    return (os.environ.get("FORGE_WRITER_HOOK")
            or os.environ.get("FORGE_MUSE_HOOK"))


def _write_script(spec: dict, proj_dir: str) -> dict:
    hook = _writer_hook()
    if not hook:
        return {"mode": "example"}
    payload = json.dumps({
        "task": "faceless-shorts script+shots",
        "style": spec["style"],
        "style_md": os.path.join(SKILL_DIR, "styles", spec["style"],
                                 "STYLE.md"),
        "topic": spec.get("topic", ""),
        "title": spec.get("title", ""),
        "description": spec.get("description", ""),
        "tags": spec.get("tags", []),
        "length_s": spec.get("length_s", 35),
        "voice": spec.get("voice", "am_michael"),
        "end_line": spec.get("end_line", ""),
        "project_dir": proj_dir,
        "protocol": WRITER_PROTOCOL,
    })
    proc = subprocess.run(hook, input=payload, capture_output=True,
                          text=True, timeout=1800, shell=True)
    if proc.returncode != 0:
        raise RuntimeError(f"writer hook gagal: {proc.stderr[-300]}")
    data = json.loads(proc.stdout)
    if not data.get("script") or "shots_js" not in data:
        raise RuntimeError("writer hook output tidak lengkap (butuh "
                           "script + shots_js)")
    with open(os.path.join(proj_dir, "script.json"), "w",
              encoding="utf-8") as f:
        json.dump(data["script"], f, ensure_ascii=False, indent=2)
    with open(os.path.join(proj_dir, "src", "shots.js"), "w",
              encoding="utf-8") as f:
        f.write(data["shots_js"])
    return {"mode": "hook"}


# --------------------------------------------------------- persistence

def _gen_row(g: dict) -> dict:
    return {
        "id": g["id"], "style": g.get("style"), "topic": g.get("topic"),
        "status": g.get("status"), "detail": g.get("detail", ""),
        "progress": int(g.get("progress") or 0), "error": g.get("error") or "",
        "asset_id": g.get("asset_id"), "project_dir": g.get("project_dir"),
        "mode": g.get("mode", "auto"),
        "chunks_done": int(g.get("chunks_done") or 0),
        "chunks_total": int(g.get("chunks_total") or 0),
        "meta_json": json.dumps(g.get("meta") or {}, ensure_ascii=False),
        "created_at": g.get("created_at") or db.now_iso(),
        "updated_at": db.now_iso(),
    }


def _set(gen_id: str, **fields) -> None:
    row = db.one("SELECT * FROM generations WHERE id = ?", (gen_id,))
    if row:
        g = {**row, **fields}
        for k in ("meta_json",):
            if k in g and isinstance(g[k], str):
                try:
                    g["meta"] = json.loads(g[k])
                except Exception:
                    g["meta"] = {}
        g.pop("meta_json", None)
        if "started_at" not in g:
            g["started_at"] = time.time()
        g["elapsed"] = int(time.time() - g.get("started_at", time.time()))
        db.upsert("generations", _gen_row(g), conflict_col="id")
        return
    fields["started_at"] = time.time()
    fields.setdefault("created_at", db.now_iso())
    fields["elapsed"] = 0
    db.upsert("generations", _gen_row({"id": gen_id, **fields}),
              conflict_col="id")


def _load_gen(gen_id: str) -> dict | None:
    row = db.one("SELECT * FROM generations WHERE id = ?", (gen_id,))
    if not row:
        return None
    g = dict(row)
    try:
        g["meta"] = json.loads(g.pop("meta_json", "{}") or "{}")
    except Exception:
        g["meta"] = {}
    g.pop("meta_json", None)
    return g


def get_gen(gen_id: str) -> dict | None:
    return _load_gen(gen_id)


def list_gens() -> list[dict]:
    out = []
    for row in db.rows("SELECT * FROM generations "
                       "ORDER BY created_at DESC LIMIT 50"):
        g = dict(row)
        try:
            g["meta"] = json.loads(g.pop("meta_json", "{}") or "{}")
        except Exception:
            g["meta"] = {}
        g.pop("meta_json", None)
        out.append(g)
    return out


# --------------------------------------------------------- generation queue

_genq_lock = threading.Lock()
_genq: list[str] = []
_gen_worker_started = False


def generate(spec: dict) -> str:
    gen_id = uuid.uuid4().hex[:10]
    if spec.get("style") not in STYLES:
        raise ValueError(f"style tak dikenal: {spec.get('style')}")
    meta = {k: spec.get(k) for k in ("title", "description", "tags",
                                     "account_key", "topic_code",
                                     "planned_time", "voice", "length_s",
                                     "end_line") if spec.get(k)}
    mode = "prepare" if spec.get("mode") == "prepare" else "auto"
    _set(gen_id, style=spec.get("style"), topic=spec.get("topic") or "",
         status="queued", detail="", progress=2, asset_id=None,
         error=None, mode=mode, chunks_done=0, chunks_total=0, meta=meta)
    with _genq_lock:
        _genq.append(gen_id)
    _start_gen_worker()
    return gen_id


def continue_generation(gen_id: str) -> dict:
    """Two-phase: continue a prepared/halted pipeline with existing files."""
    g = _load_gen(gen_id)
    if not g:
        return {"error": "generation tidak ada"}
    non_terminal = ("prepared", "failed", "halted", "queued", "voice",
                    "timing", "checking", "cues", "mix", "rendering",
                    "scaffolding", "writing")
    if g["status"] not in non_terminal:
        return {"error": f"status {g['status']} tidak bisa dilanjutkan"}
    if not os.path.isdir(g.get("project_dir") or ""):
        return {"error": "project_dir hilang — tidak ada yang bisa dilanjutkan"}
    _set(gen_id, status="queued", error="", detail="")
    with _genq_lock:
        _genq.append(gen_id)
    _start_gen_worker()
    return {"ok": True, "gen_id": gen_id}


def resume_active_generations() -> int:
    """B5: generasi non-terminal yang mati karena restart/reboot diantrekan
    ulang saat startup — checkpoint chunk membuat render melanjutkan."""
    rows = db.rows("SELECT id, status FROM generations")
    terminal = ("done", "failed", "cancelled")
    n = 0
    for r in rows:
        if r["status"] not in terminal:
            with _genq_lock:
                _genq.append(r["id"])
            _set(r["id"], status="queued",
                 detail="dipulihkan setelah restart — lanjut checkpoint")
            n += 1
    if n:
        db.log_event(None, "generations_recovered", {"count": n})
    _start_gen_worker()
    return n


def _start_gen_worker() -> None:
    global _gen_worker_started
    if _gen_worker_started:
        return
    _gen_worker_started = True
    threading.Thread(target=_gen_queue_loop, daemon=True,
                     name="forge-gens").start()


def _gen_queue_loop() -> None:
    """One render at a time (honours max_concurrency for the machine)."""
    while True:
        with _genq_lock:
            gen_id = _genq.pop(0) if _genq else None
        if not gen_id:
            time.sleep(1.0)
            continue
        g = _load_gen(gen_id)
        if not g:
            continue
        try:
            _pipeline(g)
        except Exception as e:
            _set(gen_id, status="failed", error=repr(e)[:500])
            db.log_event(None, "generate_failed",
                         {"gen": gen_id, "error": repr(e)[:300]})


# --------------------------------------------------------- pipeline

def _stale_chunk_checkpoint(proj: str) -> dict:
    try:
        with open(os.path.join(proj, "renders", "checkpoint.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _pipeline(g: dict) -> None:
    gen_id = g["id"]
    spec = {"style": g["style"], "topic": g.get("topic") or "",
            "voice": (g.get("meta") or {}).get("voice", "am_michael"),
            "length_s": 35, "end_line": "", **(g.get("meta") or {})}
    skill = _win_path(SKILL_DIR)
    proj = g.get("project_dir") or _proj_dir(gen_id)
    projw = _win_path(proj)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    if g["status"] == "queued" and not os.path.isdir(proj):
        _set(gen_id, status="scaffolding",
             detail="Membuat project dari style…", project_dir=proj)
        rc, out = _bash(f'bash "{skill}/scripts/new_video.sh" '
                        f'"{spec["style"]}" "forge-{gen_id}" '
                        f'--dir "{_win_path(str(VIDEOS_DIR))}"', timeout=120)
        if rc != 0:
            raise RuntimeError(f"new_video gagal: {out[-300]}")
        if g.get("mode") == "prepare":
            _write_script(spec, proj)  # example-mode keeps example files
            _set(gen_id, status="prepared",
                 detail="Project siap — tulis script.json + src/shots.js, "
                        "lalu /continue", progress=10)
            return
        if _writer_hook():
            _set(gen_id, status="writing", detail="Menulis script (hook)…")
            _write_script(spec, proj)
    elif g["status"] == "prepared" or g.get("mode") == "prepare":
        # continue after external writer filled script.json + shots.js
        if not os.path.isfile(os.path.join(proj, "script.json")):
            raise RuntimeError("script.json belum ada di project")
        _set(gen_id, mode="continue")

    _set(gen_id, status="voice", detail="Rekaman suara AI (Kokoro)…",
         progress=20)
    rc, out = _bash(f'bash "{skill}/scripts/py" "{skill}/scripts/voice.py" '
                    f'"{projw}"', timeout=900)
    if rc != 0:
        raise RuntimeError(f"voice gagal: {out[-300]}")

    _set(gen_id, status="timing", detail="Word timing (Whisper)…",
         progress=30)
    rc, out = _bash(f'bash "{skill}/scripts/py" "{skill}/scripts/words.py" '
                    f'"{projw}"', timeout=900)
    if rc != 0:
        raise RuntimeError(f"words gagal: {out[-300]}")

    _set(gen_id, status="checking", detail="Capture check…", progress=40)
    rc, out = _bash(f'node "{projw}/tools/capture.mjs" check', cwd=projw,
                    timeout=600, env_extra=_render_env(30))
    if rc != 0:
        raise RuntimeError(f"capture check gagal: {out[-400]}")

    _set(gen_id, status="cues", detail="Menyusun sound cues…", progress=45)
    rc, out = _bash(f'node "{projw}/tools/capture.mjs" cues', cwd=projw,
                    timeout=600, env_extra=_render_env(30))
    if rc != 0:
        raise RuntimeError(f"cues gagal: {out[-300]}")

    _set(gen_id, status="mix", detail="Soundtrack (procedural)…", progress=50)
    rc, out = _bash(f'bash "{skill}/scripts/py" "{projw}/tools/mix.py" .',
                    cwd=projw, timeout=900)
    if rc != 0:
        raise RuntimeError(f"mix gagal: {out[-300]}")

    mp4 = _chunked_render(gen_id, proj, spec)
    dur, dur_source = _expected_duration(proj)
    _set(gen_id, status="qc", detail="QC (ffprobe + contact sheet)…",
         progress=92)

    dst = os.path.join(str(paths.media_dir()), f"short-{gen_id}.mp4")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(mp4, dst)
    qc = qc_validate(dst, expected_s=dur, dur_source=dur_source)
    qc_sheet = _contact_sheet(gen_id, projw)
    asset_id = os.urandom(8).hex()
    title = ((g.get("meta") or {}).get("title") or spec["topic"]
             or f"{spec['style']} short")[:100]
    db.insert("assets", {
        "id": asset_id, "kind": "video", "title": title, "path": dst,
        "engine": f"faceless-shorts:{spec['style']}",
        "meta_json": json.dumps({
            "mode": (g.get("mode") or "auto"), "project": proj,
            "style": spec["style"], "topic": spec["topic"],
            "description": (g.get("meta") or {}).get("description", ""),
            "tags": (g.get("meta") or {}).get("tags", []),
            "account_key": (g.get("meta") or {}).get("account_key", ""),
            "topic_code": (g.get("meta") or {}).get("topic_code", ""),
            "planned_time": (g.get("meta") or {}).get("planned_time", ""),
            "qc": qc,
            "qc_sheet": _persist_qc_sheet(qc_sheet, asset_id),
            "duration_s": qc.get("duration"),
            "expected_s": dur, "dur_source": dur_source,
        }, ensure_ascii=False),
        "qc_status": "pending",
        "created_at": db.now_iso(),
    })
    db.log_event(None, "generated", {"gen": gen_id, "asset": asset_id,
                                     "style": spec["style"],
                                     "qc": qc.get("ok")})
    _set(gen_id, status="done", progress=100, detail="Selesai (QC pending)",
         asset_id=asset_id)
    # B4: project dipertahankan sampai QC disetujui/ditolak — hapus hanya
    # saat approved (rejected ditahan untuk perbaikan; retensi manual).
    if qc.get("ok") and not keep_projects():
        shutil.rmtree(proj, ignore_errors=True)


def _chunked_render(gen_id: str, proj: str, spec: dict) -> str:
    """Render in 4-8s chunks with per-chunk ffprobe + checkpoint + resume."""
    projw = _win_path(proj)
    fps = 24 if software_gl() else 30
    dur, dur_source = _expected_duration(proj)  # B3: cues/mix, bukan estimasi
    if dur_source == "estimate-lines":
        db.log_event(None, "duration_fallback_warning",
                     {"gen": gen_id, "dur": dur})
    n_chunks = max(1, -(-int(dur * 100) // int(CHUNK_SECONDS * 100)))
    chunks_dir = os.path.join(proj, "renders", "chunks")
    os.makedirs(chunks_dir, exist_ok=True)
    cp_path = os.path.join(proj, "renders", "checkpoint.json")
    cp = _stale_chunk_checkpoint(proj)
    done = set(cp.get("done", []))
    _set(gen_id, status="rendering",
         detail=f"Render chunk (software-GL)…" if software_gl()
         else "Render video (Chrome+ffmpeg)…",
         progress=55, chunks_total=n_chunks, chunks_done=len(done))
    for i in range(n_chunks):
        key = f"chunk-{i:04d}"
        out = os.path.join(chunks_dir, f"{key}.mp4")
        if key in done and os.path.isfile(out):
            continue
        a0 = round(i * CHUNK_SECONDS, 2)
        a1 = round(min((i + 1) * CHUNK_SECONDS, dur), 2)
        if a1 <= a0:
            break
        attempt = 0
        while True:
            # full quality (CRF 18 / jpeg 0.93); kualitas draft hanya untuk
            # preview MP4, bukan render final.
            rc, output = _bash(f'node "{projw}/tools/capture.mjs" video '
                               f'"{_win_path(out)}" {a0} {a1}',
                               cwd=projw, timeout=CHUNK_TIMEOUT_S,
                               env_extra=_render_env(fps))
            if rc == 0 and _valid_mp4(out):
                break
            attempt += 1
            if attempt > CHUNK_RETRIES:
                raise RuntimeError(
                    f"chunk {i} ({a0}-{a1}s) gagal {attempt}x "
                    f"(livelock/capture error): {output[-300]}")
            db.log_event(None, "chunk_retry",
                         {"gen": gen_id, "chunk": i, "attempt": attempt})
        done.add(key)
        cp = {"done": sorted(done), "total": n_chunks, "fps": fps}
        with open(cp_path, "w", encoding="utf-8") as f:
            json.dump(cp, f)
        _set(gen_id, chunks_done=len(done),
             progress=55 + int(38 * len(done) / n_chunks),
             detail=f"Render chunk {len(done)}/{n_chunks}")
    # concat all chunks
    concat_list = os.path.join(chunks_dir, "list.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for i in range(n_chunks):
            f.write(f"file '{_win_path(os.path.join(chunks_dir, f'chunk-{i:04d}.mp4'))}'\n")
    final = os.path.join(proj, "renders", f"forge-{gen_id}.mp4")
    rc, out = _bash(f'ffmpeg -v error -y -f concat -safe 0 -i '
                    f'"{_win_path(concat_list)}" -c copy -movflags +faststart '
                    f'"{_win_path(final)}"', timeout=300)
    if rc != 0 or not _valid_mp4(final):
        raise RuntimeError(f"concat gagal: {out[-300]}")
    return final


def _expected_duration(proj: str) -> tuple[float, str]:
    """B3: durasi WAJIB dari data pasti pre-render — cues.json "duration"
    ( aktual), lalu ffprobe data/mix.wav. Estimasi baris hanya fallback
    terakhir (dengan warning dari pemanggil)."""
    try:
        cues = json.load(open(os.path.join(proj, "data", "cues.json"),
                              encoding="utf-8"))
        d = cues.get("duration")
        if d:
            return float(d) + 0.5, "cues"  # +tail outro kecil
    except Exception:
        pass
    d = _probe_duration(os.path.join(proj, "data", "mix.wav"))
    if d:
        return float(d), "mix"
    est = _script_duration(proj)
    if est:
        return est, "estimate-lines"
    return 40.0, "default"


def _script_duration(proj: str) -> float | None:
    try:
        s = json.load(open(os.path.join(proj, "script.json"), encoding="utf-8"))
        return float(len(s.get("lines", []))) * 2.2 + float(
            s.get("lead", 0.3)) + float(s.get("outro", 1.2))
    except Exception:
        return None


def _probe_duration(path: str) -> float | None:
    try:
        rc, out = _bash(f'ffprobe -v error -show_entries format=duration '
                        f'-of csv=p=0 "{_win_path(path)}"', timeout=30)
        return float(out.strip().splitlines()[-1]) if rc == 0 else None
    except Exception:
        return None


def _valid_mp4(path: str) -> bool:
    if not os.path.isfile(path) or os.path.getsize(path) < 20_000:
        return False
    try:
        rc, out = _bash(f'ffprobe -v error -select_streams v:0 '
                        f'-show_entries stream=codec_name -of csv=p=0 '
                        f'"{_win_path(path)}"', timeout=30)
        return rc == 0 and "h264" in out
    except Exception:
        return False


def qc_validate(path: str, expected_s: float | None = None,
                dur_source: str = "", spec: dict | None = None) -> dict:
    """ffprobe acceptance: 1080×1920 (≥), h264, audio, duration — dan (B3)
    durasi hasil render harus cocok durasi harapan (cues/mix) ±1 dtk atau
    ±3%; di luar itu qc.ok=false dengan alasan eksplisit."""
    rc, out = _bash(
        f'ffprobe -v error -show_entries stream=codec_type,codec_name,width,'
        f'height -show_entries format=duration -of json "{_win_path(path)}"',
        timeout=30)
    result = {"ok": False, "checks": {}}
    if rc != 0:
        result["error"] = out[-200:]
        return result
    try:
        data = json.loads(out)
        streams = data.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), {})
        a = next((s for s in streams if s.get("codec_type") == "audio"), {})
        duration = float(data.get("format", {}).get("duration") or 0)
        checks = {
            "resolution": (int(v.get("width") or 0) >= QC_MIN_WIDTH and
                           int(v.get("height") or 0) >= QC_MIN_HEIGHT),
            "h264": v.get("codec_name") == "h264",
            "audio": bool(a) and a.get("codec_name") in ("aac", "opus"),
            "duration": duration > 3,
        }
        if expected_s:
            tol = max(1.0, expected_s * 0.03)
            checks["duration_match"] = abs(duration - expected_s) <= tol
        result = {"ok": all(checks.values()), "checks": checks,
                  "width": v.get("width"), "height": v.get("height"),
                  "video_codec": v.get("codec_name"),
                  "audio_codec": (a or {}).get("codec_name"),
                  "duration": round(duration, 1)}
        if expected_s and not checks["duration_match"]:
            result["error"] = (f"durasi render {duration:.1f}s ≠ harapan "
                               f"{expected_s:.1f}s (sumber {dur_source})")
    except Exception as e:
        result["error"] = repr(e)[:200]
    return result


def _persist_qc_sheet(sheet_path: str, asset_id: str) -> str:
    """B4: sheet disalin ke media persisten — project boleh terhapus."""
    if sheet_path and os.path.isfile(sheet_path):
        dst = os.path.join(str(paths.media_dir()), f"qc-{asset_id}.jpg")
        try:
            shutil.copy2(sheet_path, dst)
            return dst
        except Exception:
            pass
    return ""


def _contact_sheet(gen_id: str, projw: str) -> str:
    rc, _ = _bash(f'node "{projw}/tools/capture.mjs" sheet '
                  f'"{projw}/review/all.jpg" 1 10', cwd=projw, timeout=900,
                  env_extra=_render_env(10))
    p = os.path.join(projw, "review", "all.jpg")
    return p if rc == 0 and os.path.isfile(p) else ""


# ------------------------------------------------------------------ QC gate

def asset_approved(asset_id: str) -> bool:
    row = db.one("SELECT qc_status FROM assets WHERE id = ?", (asset_id,))
    return bool(row) and row["qc_status"] == "approved"


def set_qc(asset_id: str, verdict: str) -> None:
    db.update("assets", asset_id, {"qc_status": verdict}, key_col="id")
    db.log_event(None, "asset_qc", {"asset": asset_id, "verdict": verdict})
