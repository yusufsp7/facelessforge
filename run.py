"""Start ContentForge.   python run.py    (port dari FORGE_PORT, default 8790)"""
import os

import uvicorn

from server.main import app

if __name__ == "__main__":
    port = int(os.environ.get("FORGE_PORT", "8790"))
    host = os.environ.get("FORGE_BIND", "127.0.0.1")  # 0.0.0.0 = expose (pakai token!)
    uvicorn.run(app, host=host, port=port, log_level="info")
