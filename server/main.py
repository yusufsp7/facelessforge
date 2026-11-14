"""ContentForge FastAPI server.

Single process: static UI + JSON API. Blocking work (browser login, upload,
generation) always runs on plain threads — FastAPI endpoints just kick them
off and the UI polls status, so the event loop never blocks on Playwright.
"""
import threading
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, generator, paths
from .accounts import platforms
from .accounts.manager import AccountError, MANAGER
from .worker import WORKER

app = FastAPI(title="ContentForge", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    WORKER.start()
    from . import updater
    updater.start_auto_loop()
    # Pre-render hover previews — but NOT on GPU-less machines (software GL
    # burns the few shared cores for many minutes on boot); there they render
    # lazily on first hover instead.
    if not generator.software_gl():
        for sid in generator.STYLES:
            generator.ensure_preview(sid)
    else:
        import logging
        logging.getLogger("forge").info(
            "software-GL machine: style previews render lazily on hover")


@app.on_event("shutdown")
def _shutdown() -> None:
    WORKER.stop()


# ---------------------------------------------------------------- platforms

@app.get("/api/platforms")
def api_platforms():
    return platforms.public_list()


# ---------------------------------------------------------------- accounts

class ConnectBody(BaseModel):
    platform: str
    label: str | None = None


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


# routers added in later tasks
from .api_content import router as content_router  # noqa: E402

app.include_router(content_router)

WORKER.bind_app(app)
