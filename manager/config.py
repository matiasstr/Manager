"""Rutas, proveedores y API keys."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

DATA_DIR = Path(os.environ.get("MANAGER_DATA") or ROOT / "data")
MODELS_DIR = Path(os.environ.get("MANAGER_MODELS") or Path.home() / ".manager" / "models")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "manager.db"
KEYS_PATH = DATA_DIR / "keys.json"

# auto | fastembed | hash
EMBEDDINGS = os.environ.get("MANAGER_EMBEDDINGS", "auto")
EMBED_MODEL = os.environ.get("MANAGER_EMBED_MODEL", "")


def providers() -> list[dict]:
    return json.loads((ROOT / "proveedores.json").read_text("utf-8"))


def provider(pid: str) -> dict | None:
    return next((p for p in providers() if p["id"] == pid), None)


def _keys() -> dict:
    if KEYS_PATH.exists():
        try:
            return json.loads(KEYS_PATH.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def get_key(pid: str) -> str | None:
    """Key guardada desde la interfaz, o variable de entorno / .env."""
    k = _keys().get(pid)
    if k:
        return k
    p = provider(pid)
    if p and p.get("env"):
        return os.environ.get(p["env"]) or None
    return None


def set_key(pid: str, key: str | None) -> None:
    keys = _keys()
    if key:
        keys[pid] = key.strip()
    else:
        keys.pop(pid, None)
    KEYS_PATH.write_text(json.dumps(keys, indent=2), "utf-8")
