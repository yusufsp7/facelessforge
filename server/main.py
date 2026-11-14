"""ContentForge FastAPI server.

Single process: static UI + JSON API. Blocking work (browser login, upload,
generation) always runs on plain threads — FastAPI endpoints just kick them
off and the UI polls status, so the event loop never blocks on Playwright.
"""
import secrets
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, generator, paths
from .accounts import platforms
from .accounts.manager import AccountError, MANAGER
from .worker import WORKER

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    WORKER.start()
    generator.resume_active_generations()  # B5: lanjutkan render terputus
    from . import live
    live.start_watchdog()  # live desired_on → lanjutkan streaming sendiri
    from . import updater
    updater.start_watchdog()  # ronde-7: probe health berulang (2s), rollback hanya di tenggat
    updater.start_auto_loop()
    yield
    WORKER.stop()


app = FastAPI(title="FacelessForge", version="0.3.0", lifespan=lifespan)

TOKEN_FILE = paths.data_root() / ".api-token"


def _api_token() -> str:
    """Token auth for /api — required for non-localhost clients (E26)."""
    if TOKEN_FILE.is_file():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    print(f"[FacelessForge] API TOKEN (header X-Forge-Token): {tok}",
          flush=True)
    return tok


@app.middleware("http")
async def token_guard(request: Request, call_next):
    path = request.url.path
    client = request.client.host if request.client else ""
    localhost = client in ("127.0.0.1", "::1", "localhost")
    if path.startswith("/api/") and path != "/api/health" and not localhost:
        tok = _api_token()
        if request.headers.get("X-Forge-Token", "") != tok:
            return JSONResponse(
                {"detail": "token tidak valid — header X-Forge-Token wajib"},
                status_code=401)
    return await call_next(request)


@app.exception_handler(AccountError)
async def account_error_handler(request: Request,
                                 exc: AccountError):
    """R6: pesan akun yang bisa dibaca (impor cookies, verify, connect)
    sampai ke UI/agen sebagai 4xx — bukan 500 mentah."""
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.get("/api/health")
def api_health():
    from . import updater
    return {"ok": True, "head": updater.check().get("local_head", "")}


# ---------------------------------------------------------------- platforms

@app.get("/api/platforms")
def api_platforms():
    return platforms.public_list()


# ---------------------------------------------------------------- accounts

class ConnectBody(BaseModel):
    platform: str
    label: str | None = None


class AddAccountBody(BaseModel):
    platform: str
    label: str
    channel_id: str = ""
    handle: str = ""


@app.post("/api/accounts/add")
def api_accounts_add(body: AddAccountBody):
    """Buat akun placeholder — impor cookies bisa jadi langkah PERTAMA
    tanpa login manual (channel_id/handle boleh menyusul)."""
    try:
        platforms.get(body.platform)
    except KeyError:
        raise HTTPException(400, f"platform tak dikenal: {body.platform}")
    key = MANAGER.new_key(body.platform, body.label)
    db.upsert("accounts", {
        "key": key, "platform": body.platform, "label": body.label,
        "profile_key": key.replace(":", "_"), "connected": 0,
        "channel_id": body.channel_id, "handle": body.handle,
        "created_at": db.now_iso()})
    db.log_event(None, "account_added", {"platform": body.platform,
                                         "key": key})
    return {"ok": True, "key": key}


@app.get("/api/accounts")
def api_accounts():
    return MANAGER.list_accounts()


@app.get("/api/connects")
def api_connects():
    return MANAGER.connect_status()


@app.post("/api/accounts/connect")
def api_accounts_connect(body: ConnectBody):
    try:
        platforms.get(body.platform)
    except KeyError:
        raise HTTPException(400, f"platform tak dikenal: {body.platform}")
    key = MANAGER.new_key(body.platform, body.label)
    label = body.label

    def _run():
        try:
            MANAGER.connect(body.platform, key, label)
        except AccountError as e:
            db.log_event(None, "connect_failed",
                         {"platform": body.platform, "key": key,
                          "error": str(e)})
        except Exception as e:  # playwright/browser launch issues
            db.log_event(None, "connect_failed",
                         {"platform": body.platform, "key": key,
                          "error": repr(e)})

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "key": key}


@app.post("/api/accounts/{key}/disconnect")
def api_accounts_disconnect(key: str):
    MANAGER.disconnect(key)
    return {"ok": True}


@app.post("/api/accounts/{key}/wipe")
def api_accounts_wipe(key: str):
    MANAGER.wipe(key)
    return {"ok": True}


@app.post("/api/accounts/{key}/verify")
def api_accounts_verify(key: str):
    ok, detail = MANAGER.is_connected(key)
    return {"ok": ok, "detail": detail}


@app.post("/api/accounts/{key}/cookies")
def api_accounts_cookies(key: str, cookies: list[dict]):
    MANAGER.import_cookies(key, cookies)
    return {"ok": True}


# ---------------------------------------------------------------- static UI

app.mount("/ui", StaticFiles(directory=str(paths.PROJECT_ROOT / "server" / "ui")),
          name="ui")


@app.get("/")
def index():
    return FileResponse(str(paths.PROJECT_ROOT / "server" / "ui" / "index.html"))


# routers
from .api_content import router as content_router  # noqa: E402
from .api_live import router as live_router  # noqa: E402

app.include_router(content_router)
app.include_router(live_router)

WORKER.bind_app(app)
