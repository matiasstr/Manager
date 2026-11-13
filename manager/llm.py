"""Un solo cliente para cualquier API compatible con OpenAI, más un proveedor de demostración.

Los planes gratuitos cambian todas las semanas: modelos que aparecen, desaparecen o pasan a ser pagos.
Por eso no hay ningún nombre de modelo fijo en el código. Se consulta la lista real de cada proveedor,
se elige según las preferencias de proveedores.json, y lo que falla se saca de rotación por un rato.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI, Omit

from . import config


class LLMError(Exception):
    def __init__(self, message: str, model_level: bool = False):
        super().__init__(message)
        self.model_level = model_level  # el problema es de ese modelo, no de la cuenta ni de la conexión


@dataclass
class Result:
    text: str
    provider: str
    model: str  # el que respondió de verdad: un enrutador puede elegir otro distinto del pedido
    ms: int


# Aparecen en /models pero no conversan: audio, imagen, embeddings, moderación y modelos de un solo oficio.
_NOT_CHAT = re.compile(
    r"whisper|tts|embed|guard|rerank|moderation|imagen|image|veo|audio|speech|transcri|lyria|aqa|ocr|"
    r"safety|learnlm|robotics|computer-use|-live|voxtral|orpheus|playai|compound|bge|clip|diffusion|"
    r"parse|reward|detector|calibration|deplot|kosmos|fuyu|neva|vila|cosmos|topic-control", re.I)
# Conversan, pero son de nicho: quedan al final de la lista y no se eligen solos.
_SPECIAL = re.compile(r"code|devstral|leanstral|laguna|translate|chatqa|palmyra-(med|fin)|flash-fin", re.I)
# En un proveedor local se respeta lo que el usuario instaló; solo se sacan los que no generan texto.
_NOT_LOCAL = re.compile(r"embed|bge|rerank|whisper|tts|clip|diffusion", re.I)
_PREVIEW = re.compile(r"preview|beta|exp(\b|-)", re.I)
_GONE = re.compile(r"unavailable|not available|decommission|deprecat|does not exist|not found|no longer|not supported", re.I)

_clients: dict[tuple[str, str, str], AsyncOpenAI] = {}
_models: dict[str, tuple[float, list[str]]] = {}
_local_up: dict[str, tuple[float, bool]] = {}
_key_ok: set[tuple[str, str]] = set()  # keys que ya respondieron bien al menos una vez
_dead: dict[tuple[str, str], tuple[float, str]] = {}  # (proveedor, modelo) → (hasta cuándo saltearlo, motivo)


# ───────────────────────── Proveedores ─────────────────────────

def _key(p: dict) -> str:
    return config.get_key(p["id"]) or ("local" if p.get("local") else "")


def is_anon(p: dict) -> bool:
    """Proveedor sin key funcionando en modo anónimo, con los límites propios de ese modo."""
    return bool(p.get("keyless")) and config.get_key(p["id"]) == config.ANON


def _limits(p: dict) -> dict:
    """De dónde salen cupos y lista fija de modelos: del bloque `keyless` en modo anónimo, si no del proveedor."""
    return p["keyless"] if is_anon(p) and isinstance(p.get("keyless"), dict) else p


def _client(pid: str) -> AsyncOpenAI:
    p = config.provider(pid)
    if not p:
        raise LLMError(f"Proveedor desconocido: {pid}")
    key = _key(p)
    if not key:
        raise LLMError(f"{p['name']}: " + ("está desactivado" if p.get("keyless") else "falta la API key"))
    missing = config.missing_vars(p)
    if missing:
        raise LLMError(f"{p['name']}: falta completar " + ", ".join(missing).lower())
    url = config.base_url(p)
    if (pid, key, url) not in _clients:
        common = dict(base_url=url, timeout=90, max_retries=0)
        if key != config.ANON:
            client = AsyncOpenAI(api_key=key, **common)
        elif _limits(p).get("key"):  # acceso anónimo con una key de relleno documentada por el proveedor
            client = AsyncOpenAI(api_key=_limits(p)["key"], **common)
        else:  # acceso anónimo: la cabecera Authorization se omite en cada pedido (ver _no_auth)
            client = AsyncOpenAI(api_key="sin-key", **common)
        _clients[(pid, key, url)] = client
    return _clients[(pid, key, url)]


def _no_auth(p: dict) -> dict:
    """Argumentos para que el SDK no mande Authorization cuando el acceso es anónimo y sin key de relleno."""
    if is_anon(p) and not _limits(p).get("key"):
        return {"extra_headers": {"Authorization": Omit()}}
    return {}


async def _is_local_up(p: dict) -> bool:
    ts, up = _local_up.get(p["id"], (0.0, False))
    if time.time() - ts < 20:
        return up
    try:
        probe = AsyncOpenAI(base_url=p["base_url"], api_key="local", timeout=1.0, max_retries=0)
        page = await probe.models.list()
        await probe.close()
        up = isinstance(page.data, list)  # en ese puerto podría haber otro programa
    except Exception:
        up = False
    _local_up[p["id"]] = (time.time(), up)
    return up


async def status() -> list[dict]:
    out = []
    for p in config.providers():
        key = config.get_key(p["id"])
        anon = key == config.ANON
        if p.get("kind") == "mock":
            ready = True
        elif p.get("local"):
            ready = await _is_local_up(p)
        else:
            ready = bool(key) and not config.missing_vars(p)
        out.append({
            "id": p["id"], "name": p["name"], "group": p.get("group", "email"), "note": p.get("note", ""),
            "free": p.get("free", ""), "signup": p.get("signup", ""), "env": p.get("env", ""),
            "local": bool(p.get("local")), "demo": p.get("kind") == "mock",
            "keyless": bool(p.get("keyless")), "anon": anon, "token": bool(p.get("token", True)) and not p.get("local"),
            "vars": [{"name": n, "label": s.get("label", n), "set": bool(config.get_var(p["id"], n))} for n, s in (p.get("vars") or {}).items()],
            "ready": ready, "key_hint": ("…" + key[-4:]) if key and not anon and len(key) > 8 else "",
        })
    return out


async def available() -> list[str]:
    """Proveedores reales que se pueden usar ahora, sin la demo. Primero los que tienen key, después
    los anónimos (más limitados) y al final los locales."""
    ready = [s for s in await status() if s["ready"] and not s["demo"]]
    return [s["id"] for s in sorted(ready, key=lambda s: (s["local"], s["anon"]))]


# ───────────────────────── Errores y modelos caídos ─────────────────────────

def _message(e: APIStatusError) -> str:
    try:
        body = e.response.json()
        if isinstance(body, list) and body:
            body = body[0]
        err = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or err.get("errors") or err)
        return str(err)
    except Exception:
        return str(e)


def _bad_key(code: int, msg: str) -> bool:
    # Gemini contesta 400 (no 401) cuando la key es inválida.
    return code in (401, 403) or (code == 400 and bool(re.search(r"api[ _-]?key", msg, re.I)))


def _explain(p: dict, model: str, e: Exception) -> str:
    name = p["name"]
    if isinstance(e, APIStatusError):
        code, msg = e.status_code, _message(e)
        if _bad_key(code, msg):
            if is_anon(p):
                return f"{name}: rechazó el acceso sin key ({code})"
            return f"{name}: la API key no es válida o no tiene permiso ({code})"
        if code == 402:
            return f"{name}: «{model}» requiere un plan pago (402)"
        if code == 404 or (code == 400 and _GONE.search(msg)):
            return f"{name}: el modelo «{model}» no existe o no está disponible ahora"
        if code == 413:
            return f"{name}: el pedido es demasiado grande para el plan gratuito"
        if code == 429:
            return f"{name}: se alcanzó el límite de uso gratuito (429)"
        return f"{name}: error {code}. {msg[:180]}".strip()
    if isinstance(e, APITimeoutError):
        return f"{name}: no respondió a tiempo"
    if isinstance(e, APIConnectionError):
        return f"{name}: no se pudo conectar" + (f" (¿{name} está abierto?)" if p.get("local") else "")
    return f"{name}: {str(e)[:180]}"


def mark_dead(pid: str, model: str, minutes: float, why: str) -> None:
    _dead[(pid, model)] = (time.time() + minutes * 60, why)


def _alive(pid: str, models: list[str]) -> list[str]:
    now = time.time()
    return [m for m in models if _dead.get((pid, m), (0.0, ""))[0] < now]


def skipped(pid: str) -> list[dict]:
    """Modelos que se están salteando ahora en ese proveedor, con el motivo."""
    now = time.time()
    return [{"model": m, "why": why, "minutes": round((until - now) / 60)} for (q, m), (until, why) in _dead.items() if q == pid and until > now]


def _fail(p: dict, model: str, e: Exception, key: str) -> LLMError:
    """Arma el error. Si el problema es de ese modelo (y no de la cuenta), lo saca de rotación un rato."""
    pid = p["id"]
    if not isinstance(e, APIStatusError):
        return LLMError(_explain(p, model, e))
    code, msg = e.status_code, _message(e)
    if _bad_key(code, msg):
        # Si la misma key ya funcionó (o el acceso es anónimo), el rechazo es para este modelo en particular.
        if is_anon(p) or (pid, key) in _key_ok:
            mark_dead(pid, model, 60, "no incluido en este acceso")
            where = "el acceso sin key" if is_anon(p) else "tu plan"
            return LLMError(f"{p['name']}: «{model}» no está incluido en {where}", model_level=True)
        return LLMError(_explain(p, model, e))
    if code == 404 or (code == 400 and _GONE.search(msg)):
        mark_dead(pid, model, 30, "no disponible")
    elif code == 402:
        mark_dead(pid, model, 60, "requiere pago")
    elif code == 429:
        mark_dead(pid, model, 2, "límite de uso")
    elif code >= 500:
        mark_dead(pid, model, 3, "error del proveedor")
    return LLMError(_explain(p, model, e), model_level=code in (402, 404, 429) or code >= 500 or bool(_GONE.search(msg)))


def _retry_after(e: APIStatusError) -> float | None:
    """Segundos que el proveedor pide esperar, si lo dice en un formato simple."""
    for h in ("retry-after", "ratelimit-reset", "x-ratelimit-reset"):
        try:
            v = float(e.response.headers.get(h, ""))
            if 0 <= v <= 3600:
                return v
        except (TypeError, ValueError):
            continue
    return None


# ───────────────────────── Elección de modelos ─────────────────────────

def _size(model: str) -> float:
    """Miles de millones de parámetros según el nombre (…-70b → 70). 0 si el nombre no lo dice."""
    nums = [float(x) for x in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)b(?![a-z])", model.lower())]
    return max(nums) if nums else 0.0


def _family(model: str) -> str:
    """«openai/gpt-oss-120b» → gpt, «Meta-Llama-3_3-70B» → llama. Para repartir familias distintas."""
    name = re.sub(r"^meta-", "", model.split("/")[-1].lower())
    m = re.match(r"[a-z]+", name)
    return m.group(0) if m else name


def _natural(model: str) -> list:
    return [(0, int(t)) if t.isdigit() else (1, t) for t in re.findall(r"\d+|\D+", model.lower())]


def _order(models: list[str]) -> list[str]:
    """Más grande y más nuevo primero; las versiones preliminares, al final."""
    models = sorted(models, key=_natural, reverse=True)
    models.sort(key=lambda m: -(_size(m) or 40.0))  # sin tamaño en el nombre: se asume mediano
    models.sort(key=lambda m: bool(_PREVIEW.search(m)))
    return models


def _rank(ids: list[str], prefer: list[str]) -> list[str]:
    """Primero lo que coincide con cada preferencia, después el resto, y al final los de nicho."""
    general = [m for m in ids if not _SPECIAL.search(m)]
    out: list[str] = []
    for pat in prefer:
        rx = re.compile(pat, re.I)
        out += _order([m for m in general if rx.search(m) and m not in out])
    out += _order([m for m in general if m not in out])
    return out + _order([m for m in ids if m not in out])


def _rotation(ranked: list[str], n: int = 4) -> list[str]:
    """Los modelos que se reparten solos entre agentes: primero uno por familia, para que debatan LLM distintas."""
    good = [m for m in ranked if not _SPECIAL.search(m) and not 0 < _size(m) < 4] or ranked
    seen: set[str] = set()
    first, rest = [], []
    for m in good:
        fam = _family(m)
        (rest if fam in seen else first).append(m)
        seen.add(fam)
    return (first + rest)[:n]


async def list_models(pid: str, force: bool = False) -> list[str]:
    """Modelos de conversación del proveedor, del más recomendable al menos."""
    p = config.provider(pid)
    if not p:
        raise LLMError(f"Proveedor desconocido: {pid}")
    if p.get("kind") == "mock":
        return ["demo-1", "demo-2", "demo-3"]
    ts, cached = _models.get(pid, (0.0, []))
    if cached and not force and time.time() - ts < 600:
        return cached
    client = _client(pid)  # valida que haya key y datos completos
    lim = _limits(p)
    if lim.get("models") and (lim is not p or p.get("list") is False):
        ids = list(lim["models"])  # lista fija: se respeta su orden
    else:
        try:
            page = await client.models.list(**_no_auth(p))
            ids = [m.id.removeprefix("models/") for m in page.data]
            if p.get("only"):
                rx = re.compile(p["only"], re.I)
                ids = [m for m in ids if rx.search(m)]
            skip = _NOT_LOCAL if p.get("local") else _NOT_CHAT
            ids = _rank([m for m in ids if not skip.search(m)], p.get("prefer", []))
        except Exception as e:
            if not p.get("models"):
                raise LLMError(_explain(p, "", e)) from None
            ids = list(p["models"])  # el proveedor no lista sus modelos: se usa la lista de respaldo
    _models[pid] = (time.time(), ids)
    return ids


async def _rotation_for(pid: str) -> list[str]:
    p = config.provider(pid) or {}
    try:
        ids = await list_models(pid)
        rot = _rotation(_alive(pid, ids) or ids, int(p.get("rotate", 4)))
    except LLMError:
        if p.get("fallback_model"):
            return [p["fallback_model"]]
        raise
    if not rot:
        if p.get("fallback_model"):
            return [p["fallback_model"]]
        raise LLMError(f"{p.get('name', pid)}: no hay modelos de chat disponibles")
    return rot


async def resolve(pid: str, model: str, slot: int = 0) -> str:
    """'auto' → un modelo real. `slot` reparte modelos distintos entre agentes del mismo proveedor."""
    if model and model != "auto":
        return model
    p = config.provider(pid) or {}
    if p.get("kind") == "mock":
        return f"demo-{slot % 3 + 1}"
    rot = await _rotation_for(pid)
    return rot[slot % len(rot)]


# ───────────────────────── Cupos ─────────────────────────

class _Window:
    """Como máximo `n` llamadas por minuto. Si no hay cupo, espera en vez de recibir un rechazo."""

    def __init__(self, n: int):
        self.n = n
        self.times: deque[float] = deque()
        self.lock = asyncio.Lock()

    def wait_time(self) -> float:
        now = time.monotonic()
        while self.times and now - self.times[0] >= 60:
            self.times.popleft()
        return 0.0 if len(self.times) < self.n else 60 - (now - self.times[0]) + 1.0

    async def acquire(self, on_wait: Callable[[int], None] | None = None) -> None:
        async with self.lock:
            wait = self.wait_time()
            if wait > 0:
                if on_wait:
                    on_wait(round(wait))
                await asyncio.sleep(wait)
                self.wait_time()
            self.times.append(time.monotonic())


_windows: dict[tuple[str, str], _Window] = {}
_sems: dict[str, asyncio.Semaphore] = {}


def _gates(p: dict, model: str) -> list[_Window]:
    """Cupos por minuto que hay que respetar: del proveedor entero (`rpm`) y por modelo (`rpm_model`)."""
    lim, out = _limits(p), []
    if lim.get("rpm"):
        out.append(_windows.setdefault((p["id"], ""), _Window(int(lim["rpm"]))))
    if lim.get("rpm_model"):
        out.append(_windows.setdefault((p["id"], model), _Window(int(lim["rpm_model"]))))
    return out


def _sem(p: dict) -> asyncio.Semaphore | None:
    n = int(_limits(p).get("concurrent", 0) or 0)
    return _sems.setdefault(p["id"], asyncio.Semaphore(n)) if n else None


async def pick_free(pid: str, avoid: set[str] | frozenset = frozenset(), helper: bool = False) -> str:
    """El mejor modelo que no esté en `avoid`. Con cupo por minuto, el que pueda responder antes.
    `helper`: para tareas de apoyo (evaluar, destilar) se prefieren los modelos rápidos que el
    proveedor marque en "helper", si los tiene."""
    p = config.provider(pid) or {}
    if p.get("kind") == "mock":
        return "demo-1"
    rot = await _rotation_for(pid)
    cands = [m for m in rot if m not in avoid] or rot
    if helper and p.get("helper"):
        rx = re.compile(p["helper"], re.I)
        cands = [m for m in cands if rx.search(m)] + [m for m in cands if not rx.search(m)]
    return min(cands, key=lambda m: (max([g.wait_time() for g in _gates(p, m)], default=0.0), cands.index(m)))


def unfence(text: str) -> str:
    """Algunos modelos envuelven toda la respuesta en ```markdown … ```: se quita el envoltorio."""
    m = re.fullmatch(r"\s*```(?:markdown|md)?[ \t]*\n(.*?)\n```\s*", text, flags=re.S)
    return m.group(1) if m and "```" not in m.group(1) else text


# ───────────────────────── Conversación ─────────────────────────

def strip_think(text: str) -> str:
    """Quita el razonamiento interno que algunos modelos mandan dentro de <think>."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return re.sub(r"<think>.*$", "", text, flags=re.S)


def parse_json(text: str):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(t)
    except Exception:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    end = max(t.rfind("}"), t.rfind("]"))
    if starts and end > min(starts):
        try:
            return json.loads(t[min(starts):end + 1])
        except Exception:
            return None
    return None


async def chat(
    pid: str,
    model: str,
    messages: list[dict],
    *,
    temperature: float = 0.7,
    max_tokens: int = 1500,
    json_mode: bool = False,
    on_delta: Callable[[str], None] | None = None,
    on_reset: Callable[[], None] | None = None,
    on_think: Callable[[], None] | None = None,
    on_wait: Callable[[int], None] | None = None,
    purpose: str = "",
    slot: int = 0,
    meta: dict | None = None,
) -> Result:
    """Una respuesta en streaming. `on_think` avisa que el modelo razona antes de escribir;
    `on_wait`, que hay que esperar cupo del proveedor."""
    p = config.provider(pid)
    if not p:
        raise LLMError(f"Proveedor desconocido: {pid}")
    t0 = time.perf_counter()
    model = await resolve(pid, model, slot)

    if p.get("kind") == "mock":
        text = await _mock(purpose, meta or {}, on_delta)
        return Result(text, pid, model, int((time.perf_counter() - t0) * 1000))

    client, key = _client(pid), _key(p)
    gates, sem = _gates(p, model), _sem(p)
    kwargs: dict = dict(model=model, messages=messages, temperature=temperature,
                        max_tokens=max(max_tokens, int(p.get("max_tokens", 0))), stream=True)
    kwargs.update(p.get("extra", {}))
    if json_mode and p.get("json_mode", True):
        # En algunos servidores el modo JSON forzado se queda generando hasta el tope de tokens;
        # ahí se desactiva con "json_mode": false y se confía en el prompt y en parse_json.
        kwargs["response_format"] = {"type": "json_object"}

    for attempt in range(4):
        shown, last = "", attempt == 3
        try:
            for gate in gates:
                await gate.acquire(on_wait)
            if sem:
                await sem.acquire()
            try:
                raw, served, thought = "", model, False
                stream = await client.chat.completions.create(**kwargs, **_no_auth(p))
                async for chunk in stream:
                    if getattr(chunk, "model", None):
                        served = chunk.model.removeprefix("models/")
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    piece = getattr(delta, "content", None) or ""
                    if not piece:
                        if not thought and (getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)):
                            thought = True
                            if on_think:
                                on_think()
                        continue
                    raw += piece
                    clean = strip_think(raw)
                    if not clean and not thought and "<think>" in raw:
                        thought = True
                        if on_think:
                            on_think()
                    if on_delta and len(clean) > len(shown) and clean.startswith(shown):
                        on_delta(clean[len(shown):])
                        shown = clean
            finally:
                if sem:
                    sem.release()
            _key_ok.add((pid, key))
            text = strip_think(raw).strip()
            if not text:
                if thought and not last and kwargs["max_tokens"] < 8000:
                    kwargs["max_tokens"] *= 2  # se le fue todo el cupo en razonar: se le da más aire
                    continue
                mark_dead(pid, model, 5, "respuesta vacía")
                raise LLMError(f"{p['name']} ({model}) devolvió una respuesta vacía", model_level=True)
            return Result(text, pid, served or model, int((time.perf_counter() - t0) * 1000))
        except APIStatusError as e:
            if shown and on_reset:
                on_reset()
            code, msg = e.status_code, _message(e)
            if _bad_key(code, msg):
                raise _fail(p, model, e, key) from None
            if code == 400 and "response_format" in kwargs and not _GONE.search(msg):
                kwargs.pop("response_format")  # el modelo no soporta modo JSON: se parsea a mano
                continue
            optional = [k for k in [*p.get("extra", {}), "temperature"] if k in kwargs]
            if code == 400 and optional and not last and not _GONE.search(msg):
                for k in optional:
                    kwargs.pop(k)
                continue
            if code == 413 and kwargs["max_tokens"] > 500 and not last:
                kwargs["max_tokens"] //= 2
                continue
            if code == 429 and attempt < 2:
                wait = _retry_after(e)
                if wait is None:
                    wait = 2.0 + 3 * attempt
                if wait <= 20 or (gates and wait <= 65):  # si pide esperar mucho, mejor pasar a un respaldo
                    if wait > 5 and on_wait:
                        on_wait(round(wait))
                    await asyncio.sleep(wait + 0.5)
                    continue
            if code in (500, 502, 503, 529) and attempt < 2:
                await asyncio.sleep(2 + 3 * attempt)
                continue
            raise _fail(p, model, e, key) from None
        except (APIConnectionError, APITimeoutError, APIError) as e:
            if shown and on_reset:
                on_reset()
            if attempt < 1:
                await asyncio.sleep(1.5)
                continue
            raise _fail(p, model, e, key) from None
    raise LLMError(f"{p['name']}: no se pudo completar el pedido")


async def test(pid: str) -> dict:
    """Prueba real: lista modelos y pide una respuesta mínima. Si un modelo falla por ser él
    (no existe, no está incluido), prueba el siguiente y cuenta cuáles salteó."""
    models = await list_models(pid, force=True)
    tried: list[dict] = []
    ask = [{"role": "user", "content": "Respondé solo con la palabra: OK"}]
    for _ in range(4):
        model = await pick_free(pid, {t["model"] for t in tried})
        if any(t["model"] == model for t in tried):
            break
        try:
            r = await chat(pid, model, ask, max_tokens=200, temperature=0, purpose="test")
            return {"ok": True, "model": r.model, "reply": r.text[:80], "ms": r.ms, "models": models, "skipped": tried}
        except LLMError as e:
            tried.append({"model": model, "error": str(e)})
            if not e.model_level:
                break
    return {"ok": False, "error": tried[-1]["error"] if tried else "sin modelos para probar", "models": models, "skipped": tried[:-1]}


# ───────────────────────── Proveedor de demostración ─────────────────────────

_DEMO = {
    "Analista": [
        "Separo el problema en tres partes: qué se quiere lograr, qué restricciones hay y cómo se va a medir "
        "que funcionó. Con lo que hay en el contexto, la opción más sólida es avanzar por etapas y validar "
        "cada una antes de seguir.\n\n**Razonamiento clave:** cada etapa reduce la incertidumbre de la siguiente.\n\n"
        "**Dudas:** falta un criterio de éxito medible.",
        "Tomo la objeción del Crítico: sin criterio de éxito no se puede saber si una etapa cerró. Ajusto mi "
        "postura y agrego una métrica por etapa. Coincido con el Pragmático en empezar por la parte de menor costo.\n\n"
        "POSTURA FINAL: avanzar por etapas, cada una con una métrica definida antes de empezar.",
    ],
    "Crítico": [
        "Veo tres puntos débiles. Primero, se asume que el contexto está completo y no lo sabemos. Segundo, "
        "nadie definió qué pasa si la primera etapa falla. Tercero, el plan no tiene un criterio para detenerse.\n\n"
        "**Riesgo principal:** avanzar con confianza sobre un supuesto que nadie verificó.",
        "El Analista respondió a mi objeción con una métrica por etapa, y eso alcanza. Mantengo una reserva: "
        "todavía no hay plan si la primera etapa falla.\n\n"
        "POSTURA FINAL: de acuerdo con avanzar por etapas, siempre que exista un plan de salida explícito.",
    ],
    "Pragmático": [
        "Lo más barato es probar la parte chica primero, con lo que ya existe y sin construir nada nuevo. "
        "Si eso funciona en una semana, se sigue; si no, se descarta con poco costo.\n\n"
        "**Pasos:** 1) elegir el caso más simple, 2) probarlo, 3) decidir con el resultado en la mano.",
        "Las métricas del Analista y el plan de salida del Crítico caben en mi propuesta sin agregar costo: "
        "se definen antes de la prueba chica.\n\n"
        "POSTURA FINAL: prueba chica de una semana, con métrica y plan de salida definidos de antemano.",
    ],
    "Creativo": [
        "Propongo dar vuelta la pregunta: en vez de buscar la mejor solución, listar las formas en que esto "
        "puede salir mal y diseñar para evitarlas. Es especulativo, pero suele mostrar restricciones ocultas.",
        "Mi enfoque inverso coincide con el plan de salida que pide el Crítico. Lo sumo como paso previo.\n\n"
        "POSTURA FINAL: hacer primero el ejercicio de fallas y después la prueba chica.",
    ],
}


async def _mock(purpose: str, meta: dict, on_delta) -> str:
    name, rnd = meta.get("name", "Agente"), int(meta.get("round", 1))
    problem = (meta.get("problem") or "el problema planteado").strip()
    short = problem if len(problem) <= 90 else problem[:87] + "…"
    if purpose == "test":
        return "OK"
    if purpose == "judge":
        await asyncio.sleep(0.5)
        return json.dumps({
            "consenso": round(min(0.95, 0.45 + 0.25 * rnd), 2),
            "acuerdos": ["Conviene avanzar por etapas", "Hay que empezar por la parte de menor costo"],
            "desacuerdos": [] if rnd > 1 else ["No hay criterio de éxito definido", "Falta un plan si la primera etapa falla"],
        }, ensure_ascii=False)
    if purpose == "distill":
        await asyncio.sleep(0.6)
        return json.dumps({"tarjetas": [
            {"pregunta": problem, "respuesta": "Respuesta de demostración: avanzar por etapas, empezando por una prueba chica "
             "de bajo costo, con una métrica de éxito y un plan de salida definidos antes de empezar.",
             "tipo": "decision", "etiquetas": ["demo"], "variantes": [f"¿Qué se decidió sobre: {short}?"]},
            {"pregunta": "¿Qué hay que definir antes de empezar una etapa?",
             "respuesta": "Una métrica de éxito medible y un plan de salida por si la etapa falla.",
             "tipo": "procedimiento", "etiquetas": ["demo", "método"], "variantes": ["¿Qué necesito antes de arrancar una etapa?"]},
            {"pregunta": "¿Cuál es el riesgo principal señalado en la discusión?",
             "respuesta": "Avanzar con confianza sobre un supuesto que nadie verificó.",
             "tipo": "advertencia", "etiquetas": ["demo", "riesgo"], "variantes": ["¿Qué puede salir mal?"]},
        ]}, ensure_ascii=False)
    if purpose == "synth":
        text = (
            f"> Síntesis simulada (modo demo, sin IA real) sobre: *{short}*\n\n"
            "## Respuesta acordada\nAvanzar por etapas, empezando por una prueba chica de bajo costo.\n\n"
            "## Puntos de acuerdo\n- Empezar por la parte más simple y barata.\n- Definir una métrica de éxito antes de cada etapa.\n\n"
            "## Desacuerdos que persisten\n- Cuánto detalle necesita el plan de salida.\n\n"
            "## Próximos pasos\n1. Elegir el caso más simple.\n2. Definir métrica y plan de salida.\n3. Probar y decidir con el resultado."
        )
    elif purpose == "answer":
        text = "Respuesta simulada redactada a partir de las tarjetas recuperadas."
    else:
        parts = _DEMO.get(name) or _DEMO["Analista"]
        body = parts[0] if rnd == 1 else parts[1]
        text = (f"*(Demo sin IA real)* Sobre «{short}»:\n\n" if rnd == 1 else "") + body
    out = ""
    for word in re.findall(r"\S+\s*", text):
        out += word
        if on_delta:
            on_delta(word)
        await asyncio.sleep(0.03)  # por debajo de ~16 ms Windows no espera nada
    return out.strip()
