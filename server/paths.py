"""Filesystem layout for ContentForge.

Everything lives under the project root (override with FORGE_DATA), so the
install is portable: zip it, move it to a VPS, run it there.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_root() -> Path:
    override = os.environ.get("FORGE_DATA")
    base = Path(override) if override else PROJECT_ROOT / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_root() / "forge.db"


def profiles_dir() -> Path:
    d = data_root() / "secrets" / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def media_dir() -> Path:
    d = data_root() / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def diag_dir() -> Path:
    d = data_root() / "media" / "diag"
    d.mkdir(parents=True, exist_ok=True)
    return d
