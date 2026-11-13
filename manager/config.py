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


# Algunos proveedores funcionan sin registrarse. No se usan hasta que el usuario los activa:
# activar uno guarda este marcador en el lugar de la key.
ANON = "@sin-key"


def set_enabled(pid: str, on: bool) -> None:
    set_key(pid, ANON if on else None)


# Algunos proveedores necesitan un dato más en la URL (Cloudflare: el ID de cuenta).
def get_var(pid: str, name: str) -> str:
    v = _keys().get(f"{pid}:{name}")
    if v:
        return v
    spec = ((provider(pid) or {}).get("vars") or {}).get(name) or {}
    return os.environ.get(spec.get("env", ""), "") if spec.get("env") else ""


def set_var(pid: str, name: str, value: str | None) -> None:
    keys = _keys()
    if value and value.strip():
        keys[f"{pid}:{name}"] = value.strip()
    else:
        keys.pop(f"{pid}:{name}", None)
    KEYS_PATH.write_text(json.dumps(keys, indent=2), "utf-8")


def missing_vars(p: dict) -> list[str]:
    return [spec.get("label", name) for name, spec in (p.get("vars") or {}).items() if not get_var(p["id"], name)]


def base_url(p: dict) -> str:
    url = p.get("base_url", "")
    for name in p.get("vars") or {}:
        url = url.replace("{" + name + "}", get_var(p["id"], name))
    return url


# Sin login, el servidor solo debe atender a esta máquina. Se relaja al arrancar con --host distinto.
LOCAL_ONLY = os.environ.get("MANAGER_LOCAL_ONLY", "1") != "0"
ALLOW_ORIGINS = {o.strip().rstrip("/") for o in os.environ.get("MANAGER_ALLOW_ORIGINS", "").split(",") if o.strip()}
