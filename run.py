"""Start ContentForge.   python run.py    (port dari FORGE_PORT, default 8790)"""
import os

import uvicorn

from server.main import app

if __name__ == "__main__":
    port = int(os.environ.get("FORGE_PORT", "8790"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
