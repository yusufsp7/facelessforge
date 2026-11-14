"""faceless-shorts generator — the ONLY content generation system.

Drives the agent skill bundled at ``vendor/faceless-shorts/`` (the user's
Google Drive file): narrated faceless shorts made 100% in code — script.json
→ local Kokoro voice → Whisper word timings → shots drawn in JS → headless
Chrome capture → ffmpeg MP4. No AI images/video, no API keys, $0.

Pipeline per generation (mirrors the skill's SKILL.md §3):
  1. ``new_video.sh <style> <name> --dir <data>/videos``  → fresh project from
     the style's working example (renders as-is when nothing is rewritten)
  2. optional LLM writer hook (FORGE_WRITER_HOOK / FORGE_MUSE_HOOK): a command
     that gets the generation spec as JSON on stdin and emits
     ``{"script": {...script.json...}, "shots_js": "<full src/shots.js>"}`` —
     the skill requires script.json AND shots.js to be rewritten together
     (``Wd(word, line)`` throws when a word is missing), so a hook is the only
     way to make a video about the user's topic. Without a hook we render the
     style's example project untouched and label the asset as a style demo.
  3. ``scripts/py scripts/voice.py <proj>`` → narration.wav (Kokoro)
  4. ``scripts/py scripts/words.py <proj>`` → word timings (Whisper small.en)
  5. ``node tools/capture.mjs check`` → CHECK OK
  6. ``node tools/capture.mjs cues`` → data/cues.json
  7. ``scripts/py tools/mix.py .`` → data/mix.wav (procedural sound + music)
  8. ``node tools/capture.mjs video renders/<name>.mp4`` → the MP4

All bash invocations run through Git Bash (the skill's documented Windows
path). Setup (one-time, ~0.9 GB free downloads) runs ``setup.sh``.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time

from . import db, paths

SKILL_DIR = os.path.join(paths.PROJECT_ROOT, "vendor", "faceless-shorts")
VIDEOS_DIR = paths.data_root() / "videos"
SETUP_LOG = paths.data_root() / "gen-setup.log"

# id → (label, description) — mirrors SKILL.md's style table
STYLES = {
    "deep-dive": (
        "Deep Dive 3D",
        "Flagship ala Zack D. Films: manusia 3D di set terang, tubuh x-ray "
        "menyala, kamera menyelam ke pembuluh darah. \u201cWhat happens if\u2026?\u201d "
        "tubuh & sains. 30-45s \u00b7 9:16",
    ),
    "night-file": (
        "Night File 3D",
        "Misteri tak terpecahkan / true crime: ruang 3D gelap satu lampu, "
        "orang tanpa wajah (subjek menyala merah), tanggal mesin ketik. "
        "30-45s \u00b7 9:16",
    ),
    "inside-out": (
        "Inside Out",
        "Explainer 2D x-ray: satu hero sederhana, potongan x-ray, caption "
        "besar 1-3 kata, cut cepat. 25-40s \u00b7 9:16",
    ),
    "stick-story": (
        "Stick Story",
        "Langkah bertahan hidup ala stick figure: orang stick dengan mata "
        "ekspresif, ruangan flat siang/malam, kartu langkah emas. 30-45s \u00b7 9:16",
    ),
    "paper-atlas": (
        "Paper Atlas",
        "Sejarah gaya kertas gunting: peta parchment, rute merah crimson, "
        "pita tanggal emas, figur masa lalu. 30-45s \u00b7 9:16",
    ),
    "tall-tale": (
        "Tall Tale 3D",
        "Cerita nyata lucu: orang kartun 2D (kepala besar, outline tinta) "
        "beraksi di dunia kota kecil 3D yang cerah, kamera selalu bergerak, "
        "caption satu kata. 40-60s \u00b7 9:16",
    ),
}


def skill_scripts_ok() -> bool:
    return os.path.isfile(os.path.join(SKILL_DIR, "scripts", "setup.sh"))


PREVIEWS_DIR = paths.data_root() / "previews"


def _preview_params() -> tuple[int, int]:
    # (seconds, fps) — cheaper on GPU-less VMs so hover-wait stays sane
    if software_gl():
        return 4, 10
    return 5, 16


def list_voices() -> list[dict]:
    """Parse the skill's GOOD voices dict (stays in sync with the skill)."""
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
    if not out:  # hardcoded fallback mirrors voice.py GOOD
        out = [{"id": "am_michael", "desc": "US male, steady explainer (default)"},
               {"id": "af_heart", "desc": "US female, warm (best overall)"}]
    return out


# ---------------------------------------------------------------- previews

def preview_path(style_id: str) -> str:
    return str(PREVIEWS_DIR / f"{style_id}.mp4")


def preview_ready(style_id: str) -> bool:
    return os.path.isfile(preview_path(style_id))


_preview_lock = threading.Lock()
_preview_thread: threading.Thread | None = None
_preview_queue: list[str] = []


def ensure_preview(style_id: str) -> None:
    """Queue a style preview render (background, cached on disk)."""
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
    """Short draft render (no audio) of the style's example project."""
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
        env = _render_env(fps, True)
        proj = _win_path(str(tmp_dir))
        rc, out = _bash(f'node "{proj}/tools/capture.mjs" video '
                        f'"{proj}/renders/preview.mp4" 0 {seconds} '
                        f'--draft', cwd=proj, timeout=7200, env_extra=env)
        if rc != 0:
            raise RuntimeError(f"capture: {out[-300]}")
        src = os.path.join(str(tmp_dir), "renders", "preview.mp4")
        if not os.path.isfile(src):
            raise RuntimeError("preview.mp4 tidak dihasilkan")
        shutil.copy2(src, preview_path(style_id))
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
    db.log_event(None, "preview_ready", {"style": style_id})


def _bash(cmd: str, cwd: str | None = None, timeout: int | None = None,
          env_extra: dict | None = None) -> tuple[int, str]:
    """Run a command through Git Bash (the skill's Windows path)."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["bash", "-lc", cmd], cwd=cwd, env=env, timeout=timeout,
        capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def _win_path(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/")


# ------------------------------------------------------- machine profile

def software_gl() -> bool:
    """True when Chrome must render WebGL on CPU (no usable GPU).

    The skill prescribes `SOFTWARE_GL=1 QS='&res=0.6&aa=0&shadow=1024' FPS=24`
    for GPU-less machines (SKILL.md §3D styles, ≈2 s/frame). FORGE_SOFTWARE_GL
    overrides the auto-detection: =1 forces CPU, =0 forces hardware.
    """
    env = os.environ.get("FORGE_SOFTWARE_GL")
    if env == "1":
        return True
    if env == "0":
        return False
    if sys.platform.startswith("linux"):
        return not os.path.exists("/dev/dri")  # GPU-less VM (EPYC no iGPU)
    return False  # Windows/macOS dev boxes: let Chrome use its normal path


def _render_env(fps: int, draft: bool) -> dict | None:
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
    return 2 if cores >= 4 else 1  # 4 vCPU shared → usually 1.5 core


def _proj_dir(gen_id: str) -> str:
    return str(VIDEOS_DIR / f"forge-{gen_id}")


# ------------------------------------------------------------------ status

def status() -> dict:
    """Machine + skill readiness, for the UI."""
    out = {"skill": skill_scripts_ok(), "node": False, "node_v": "",
           "ffmpeg": False, "chrome": False, "python_env": False,
           "voice": False, "whisper": False, "hint": ""}
    if not out["skill"]:
        out["hint"] = "vendor/faceless-shorts hilang — ekstrak ulang paket Drive."
        return out
    rc, so = _bash("node -v", timeout=15)
    if rc == 0:
        out["node_v"] = so.strip().splitlines()[-1]
        major = int("".join(ch for ch in out["node_v"].split(".")[0]
                            if ch.isdigit()) or 0)
        out["node"] = major >= 22
    rc, _ = _bash("command -v ffmpeg >/dev/null && command -v ffprobe",
                  timeout=15)
    out["ffmpeg"] = rc == 0
    out["chrome"] = os.path.isfile(
        r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    fh = os.environ.get("FACELESS_HOME", os.path.expanduser("~/.faceless-shorts"))
    py = os.path.join(fh, "venv", "Scripts", "python.exe")
    if not os.path.isfile(py):
        py = os.path.join(fh, "venv", "bin", "python")
    out["python_env"] = os.path.isfile(py)
    if out["python_env"]:
        pw = _win_path(py)
        rc, _ = _bash(f'"{pw}" -c "import kokoro_onnx"', timeout=30)
        out["voice"] = rc == 0 and _voice_model_ok(fh)
        rc, _ = _bash(f'"{pw}" -c "from faster_whisper import WhisperModel',
                      timeout=30)
        out["whisper"] = rc == 0
    missing = [k for k, ok in (("node 22+", out["node"]),
                               ("ffmpeg", out["ffmpeg"]),
                               ("chrome", out["chrome"]),
                               ("python-env", out["python_env"]),
                               ("voice (Kokoro)", out["voice"]),
                               ("whisper", out["whisper"])) if not ok]
    out["ready"] = not missing and skill_scripts_ok()
    out["software_gl"] = software_gl()
    out["max_concurrency"] = max_concurrency()
    out["data_dir"] = str(paths.data_root())
    if missing:
        out["hint"] = ("Belum siap: " + ", ".join(missing) +
                       " — klik 'Setup Generator' (unduh ~0.9GB, sekali saja).")
    elif out["software_gl"]:
        out["hint"] = ("Siap (mode software-GL: render 3D jalan di CPU, "
                       "±2s/frame — video 40s bisa ±40 menit).")
    else:
        out["hint"] = ""
    return out


def _voice_model_ok(fh: str) -> bool:
    return bool(glob.glob(os.path.join(fh, "kokoro", "*.onnx"))) or \
        bool(glob.glob(os.path.join(fh, "kokoro", "*", "*.onnx")))


_setup_thread: threading.Thread | None = None


def run_setup(on_line=None) -> dict:
    """Run the skill's one-time setup.sh in the background."""
    global _setup_thread
    if _setup_thread and _setup_thread.is_alive():
        return {"started": False, "detail": "setup sedang berjalan"}
    if not skill_scripts_ok():
        return {"started": False, "detail": "skill tidak ada"}

    def _run():
        cmd = f'bash "{_win_path(os.path.join(SKILL_DIR, "scripts", "setup.sh"))}"'
        proc = subprocess.Popen(
            ["bash", "-lc", cmd], cwd=SKILL_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        with open(SETUP_LOG, "w", encoding="utf-8") as log:
            for line in proc.stdout or []:
                log.write(line)
                log.flush()
                on_line and on_line(line.rstrip())
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
        out.append({
            "id": sid, "label": label, "description": desc,
            "sheet": "/api/styles/%s/sheet.png" % sid
            if os.path.isfile(sheet) else "",
        })
    return out


def style_sheet_path(style_id: str) -> str | None:
    p = os.path.join(SKILL_DIR, "styles", style_id, "style-sheet.png")
    return p if os.path.isfile(p) else None


# ------------------------------------------------------------------ writer

def _writer_hook() -> str | None:
    return (os.environ.get("FORGE_WRITER_HOOK")
            or os.environ.get("FORGE_MUSE_HOOK"))


def _write_script(spec: dict, proj_dir: str) -> dict:
    """Ask the writer hook for script.json + shots.js; None → keep example."""
    hook = _writer_hook()
    if not hook:
        return {"mode": "example"}
    payload = json.dumps({
        "task": "faceless-shorts script+shots",
        "style": spec["style"],
        "style_md": os.path.join(SKILL_DIR, "styles", spec["style"],
                                 "STYLE.md"),
        "topic": spec.get("topic", ""),
        "length_s": spec.get("length_s", 35),
        "voice": spec.get("voice", "am_michael"),
        "end_line": spec.get("end_line", ""),
        "project_dir": proj_dir,
        "rules": "Return ONLY JSON: {\"script\": <script.json object>, "
                 "\"shots_js\": \"<full src/shots.js>\"}. Follow the style's "
                 "STYLE.md; Wd(word, line) must match the script words.",
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


# ------------------------------------------------------------------ generate

_GENS_LOCK = threading.Lock()
GENS: dict[str, dict] = {}


def _set(gen_id: str, **fields) -> None:
    with _GENS_LOCK:
        g = GENS.setdefault(gen_id, {"id": gen_id})
        g.update(fields)
        g["elapsed"] = int(time.time() - g.get("started_at", time.time()))


def get_gen(gen_id: str) -> dict | None:
    with _GENS_LOCK:
        g = GENS.get(gen_id)
        return dict(g) if g else None


def list_gens() -> list[dict]:
    with _GENS_LOCK:
        return [dict(g) for g in GENS.values()]


def generate(spec: dict) -> str:
    gen_id = os.urandom(6).hex()
    spec = {
        "style": spec.get("style") or "deep-dive",
        "topic": spec.get("topic") or "",
        "voice": spec.get("voice") or "am_michael",
        "length_s": int(spec.get("length_s") or 35),
        "end_line": spec.get("end_line") or "",
    }
    if spec["style"] not in STYLES:
        raise ValueError(f"style tak dikenal: {spec['style']}")
    _set(gen_id, style=spec["style"], topic=spec["topic"], status="queued",
         detail="", progress=2, started_at=time.time(), asset_id=None,
         error=None)
    threading.Thread(target=_run_gen, args=(gen_id, spec), daemon=True,
                     name=f"forge-gen-{gen_id}").start()
    return gen_id


def _run_gen(gen_id: str, spec: dict) -> None:
    try:
        _pipeline(gen_id, spec)
    except Exception as e:
        _set(gen_id, status="failed", error=repr(e)[:500])
        db.log_event(None, "generate_failed",
                     {"gen": gen_id, "error": repr(e)[:300]})


def _pipeline(gen_id: str, spec: dict) -> None:
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    skill = _win_path(SKILL_DIR)
    proj = _proj_dir(gen_id)
    projw = _win_path(proj)

    _set(gen_id, status="scaffolding", detail="Membuat project dari style…")
    rc, out = _bash(f'bash "{skill}/scripts/new_video.sh" "{spec["style"]}" '
                    f'"forge-{gen_id}" --dir "{_win_path(str(VIDEOS_DIR))}"',
                    timeout=120)
    if rc != 0:
        raise RuntimeError(f"new_video gagal: {out[-300]}")

    _set(gen_id, status="writing", detail="Menulis script…")
    mode = _write_script(spec, proj)["mode"]

    _set(gen_id, status="voice", detail="Rekaman suara AI (Kokoro)…")
    rc, out = _bash(f'bash "{skill}/scripts/py" "{skill}/scripts/voice.py" '
                    f'"{projw}"', timeout=900)
    if rc != 0:
        raise RuntimeError(f"voice gagal: {out[-300]}")

    _set(gen_id, status="timing", detail="Word timing (Whisper)…")
    rc, out = _bash(f'bash "{skill}/scripts/py" "{skill}/scripts/words.py" '
                    f'"{projw}"', timeout=900)
    if rc != 0:
        raise RuntimeError(f"words gagal: {out[-300]}")

    _set(gen_id, status="checking", detail="Capture check…")
    rc, out = _bash(f'node "{projw}/tools/capture.mjs" check', cwd=projw,
                    timeout=600, env_extra=_render_env(30, True))
    if rc != 0:
        raise RuntimeError(f"capture check gagal: {out[-400]}")

    _set(gen_id, status="cues", detail="Menyusun sound cues…")
    rc, out = _bash(f'node "{projw}/tools/capture.mjs" cues', cwd=projw,
                    timeout=600, env_extra=_render_env(30, True))
    if rc != 0:
        raise RuntimeError(f"cues gagal: {out[-300]}")

    _set(gen_id, status="mix", detail="Soundtrack (procedural)…")
    rc, out = _bash(f'bash "{skill}/scripts/py" "{projw}/tools/mix.py" .',
                    cwd=projw, timeout=900)
    if rc != 0:
        raise RuntimeError(f"mix gagal: {out[-300]}")

    # Software-GL (GPU-less VM): 24 fps per the skill's recipe; hardware: 30.
    render_fps = 24 if software_gl() else 30
    _set(gen_id, status="rendering",
         detail="Render video (Chrome+ffmpeg" +
                (", software GL — bisa ±2s/frame di VM tanpa GPU)"
                 if software_gl() else ")…"),
         progress=60)
    rc, out = _bash(f'node "{projw}/tools/capture.mjs" video '
                    f'"{projw}/renders/forge-{gen_id}.mp4"', cwd=projw,
                    timeout=7200, env_extra=_render_env(render_fps, False))
    if rc != 0:
        raise RuntimeError(f"render gagal: {out[-400]}")

    src = os.path.join(proj, "renders", f"forge-{gen_id}.mp4")
    if not os.path.isfile(src):
        raise RuntimeError("MP4 tidak ditemukan setelah render")
    media_dir = paths.media_dir()
    dst = os.path.join(str(media_dir), f"short-{gen_id}.mp4")
    shutil.copy2(src, dst)

    _set(gen_id, status="done", progress=100, detail="Selesai")
    title = (spec["topic"] or f"{spec['style']} short")[:80]
    asset_id = os.urandom(8).hex()
    db.insert("assets", {
        "id": asset_id, "kind": "video", "title": title, "path": dst,
        "engine": f"faceless-shorts:{spec['style']}",
        "meta_json": json.dumps({"mode": mode, "project": proj},
                                ensure_ascii=False),
        "created_at": db.now_iso(),
    })
    db.log_event(None, "generated", {"gen": gen_id, "asset": asset_id,
                                     "style": spec["style"], "mode": mode})
    _set(gen_id, asset_id=asset_id)
