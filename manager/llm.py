"""Un solo cliente para cualquier API compatible con OpenAI, más un proveedor de demostración.

Los nombres de modelos gratuitos cambian todo el tiempo, así que no hay ninguno fijo en el código:
se consulta la lista real de cada proveedor y se elige según las preferencias de proveedores.json.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Callable

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI

from . import config


class LLMError(Exception):
    pass


@dataclass
class Result:
    text: str
    provider: str
    model: str
    ms: int


# Modelos que aparecen en /models pero no sirven para conversar.
_NOT_CHAT = re.compile(
    r"whisper|tts|embed|guard|rerank|moderation|imagen|image|veo|audio|speech|transcri|lyria|aqa|ocr|"
    r"safeguard|learnlm|robotics|computer-use|-live|voxtral|orpheus|playai|compound|bge|nv-|clip", re.I)

_clients: dict[tuple[str, str], AsyncOpenAI] = {}
_models: dict[str, tuple[float, list[str]]] = {}
_local_up: dict[str, tuple[float, bool]] = {}


def _client(pid: str) -> AsyncOpenAI:
    p = config.provider(pid)
    if not p:
        raise LLMError(f"Proveedor desconocido: {pid}")
    key = config.get_key(pid) or ("local" if p.get("local") else "")
    if not key:
        raise LLMError(f"{p['name']}: falta la API key")
    if (pid, key) not in _clients:
        _clients[(pid, key)] = AsyncOpenAI(base_url=p["base_url"], api_key=key, timeout=90, max_retries=0)
    return _clients[(pid, key)]


async def _is_local_up(p: dict) -> bool:
    ts, up = _local_up.get(p["id"], (0.0, False))
    if time.time() - ts < 20:
        return up
    try:
        probe = AsyncOpenAI(base_url=p["base_url"], api_key="local", timeout=1.0, max_retries=0)
        await probe.models.list()
        await probe.close()
        up = True
    except Exception:
        up = False
    _local_up[p["id"]] = (time.time(), up)
    return up


async def status() -> list[dict]:
    out = []
    for p in config.providers():
        key = config.get_key(p["id"])
        if p.get("kind") == "mock":
            ready = True
        elif p.get("local"):
            ready = await _is_local_up(p)
        else:
            ready = bool(key)
        out.append({
            "id": p["id"], "name": p["name"], "note": p.get("note", ""), "signup": p.get("signup", ""),
            "env": p.get("env", ""), "local": bool(p.get("local")), "demo": p.get("kind") == "mock",
            "ready": ready, "key_hint": ("…" + key[-4:]) if key and len(key) > 8 else "",
        })
    return out


async def available() -> list[str]:
    """Proveedores reales que se pueden usar ahora (sin contar la demo)."""
    return [s["id"] for s in await status() if s["ready"] and not s["demo"]]


def _explain(p: dict, model: str, e: Exception) -> str:
    name = p["name"]
    if isinstance(e, APIStatusError):
        code = e.status_code
        if code in (401, 403):
            return f"{name}: la API key no es válida o no tiene permiso ({code})"
        if code == 404:
            return f"{name}: el modelo «{model}» no existe o ya no está disponible"
        if code == 413:
            return f"{name}: el pedido es demasiado grande para el plan gratuito"
        if code == 429:
            return f"{name}: se alcanzó el límite de uso gratuito (429)"
        msg = ""
        try:
            body = e.response.json()
            err = body.get("error", body) if isinstance(body, dict) else body
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
        except Exception:
            msg = str(e)
        return f"{name}: error {code}. {msg[:180]}".strip()
    if isinstance(e, APITimeoutError):
        return f"{name}: no respondió a tiempo"
    if isinstance(e, APIConnectionError):
        return f"{name}: no se pudo conectar" + (" (¿Ollama está abierto?)" if p.get("local") else "")
    return f"{name}: {str(e)[:180]}"


def _rank(ids: list[str], prefer: list[str]) -> list[str]:
    """Primero los que coinciden con cada preferencia (versión más nueva antes), después el resto."""
    out: list[str] = []
    for pat in prefer:
        rx = re.compile(pat, re.I)
        out += sorted((m for m in ids if rx.search(m) and m not in out), reverse=True)
    return out + sorted(m for m in ids if m not in out)


async def list_models(pid: str, force: bool = False) -> list[str]:
    p = config.provider(pid)
    if not p:
        raise LLMError(f"Proveedor desconocido: {pid}")
    if p.get("kind") == "mock":
        return ["demo-1", "demo-2", "demo-3"]
    ts, cached = _models.get(pid, (0.0, []))
    if cached and not force and time.time() - ts < 600:
        return cached
    try:
        page = await _client(pid).models.list()
        ids = [m.id.removeprefix("models/") for m in page.data]
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(_explain(p, "", e)) from None
    if p.get("only"):
        rx = re.compile(p["only"], re.I)
        ids = [m for m in ids if rx.search(m)]
    ids = _rank([m for m in ids if not _NOT_CHAT.search(m)], p.get("prefer", []))
    _models[pid] = (time.time(), ids)
    return ids


async def resolve(pid: str, model: str, slot: int = 0) -> str:
    """'auto' → un modelo real. `slot` reparte modelos distintos entre agentes del mismo proveedor."""
    if model and model != "auto":
        return model
    p = config.provider(pid) or {}
    if p.get("kind") == "mock":
        return f"demo-{slot % 3 + 1}"
    try:
        ids = await list_models(pid)
    except LLMError:
        if p.get("default_model"):
            return p["default_model"]
        raise
    if not ids:
        if p.get("default_model"):
            return p["default_model"]
        raise LLMError(f"{p.get('name', pid)}: no hay modelos de chat disponibles")
    top = ids[:4]
    return top[slot % len(top)]


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
    purpose: str = "",
    slot: int = 0,
    meta: dict | None = None,
) -> Result:
    p = config.provider(pid)
    if not p:
        raise LLMError(f"Proveedor desconocido: {pid}")
    t0 = time.perf_counter()
    model = await resolve(pid, model, slot)

    if p.get("kind") == "mock":
        text = await _mock(purpose, meta or {}, on_delta)
        return Result(text, pid, model, int((time.perf_counter() - t0) * 1000))

    client = _client(pid)
    kwargs: dict = dict(model=model, messages=messages, temperature=temperature,
                        max_tokens=max(max_tokens, int(p.get("max_tokens", 0))), stream=True)
    kwargs.update(p.get("extra", {}))
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    for attempt in range(4):
        shown = ""
        try:
            raw = ""
            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                piece = getattr(chunk.choices[0].delta, "content", None) or ""
                if not piece:
                    continue
                raw += piece
                clean = strip_think(raw)
                if on_delta and len(clean) > len(shown) and clean.startswith(shown):
                    on_delta(clean[len(shown):])
                    shown = clean
            text = strip_think(raw).strip()
            if not text:
                raise LLMError(f"{p['name']} ({model}) devolvió una respuesta vacía")
            return Result(text, pid, model, int((time.perf_counter() - t0) * 1000))
        except APIStatusError as e:
            if shown and on_reset:
                on_reset()
            code = e.status_code
            last = attempt == 3
            if code == 400 and "response_format" in kwargs:
                kwargs.pop("response_format")  # el modelo no soporta modo JSON: se parsea a mano
                continue
            if code == 400 and (p.get("extra") or "temperature" in kwargs) and not last:
                for k in list(p.get("extra", {})) + ["temperature"]:
                    kwargs.pop(k, None)
                continue
            if code == 413 and kwargs["max_tokens"] > 500 and not last:
                kwargs["max_tokens"] //= 2
                continue
            if code in (429, 500, 502, 503, 529) and attempt < 2:
                await asyncio.sleep(2 + 3 * attempt)
                continue
            raise LLMError(_explain(p, model, e)) from None
        except (APIConnectionError, APITimeoutError, APIError) as e:
            if shown and on_reset:
                on_reset()
            if attempt < 1:
                await asyncio.sleep(1.5)
                continue
            raise LLMError(_explain(p, model, e)) from None
    raise LLMError(f"{p['name']}: no se pudo completar el pedido")


async def test(pid: str) -> dict:
    """Prueba real: lista modelos y pide una respuesta mínima."""
    models = await list_models(pid, force=True)
    r = await chat(pid, "auto", [{"role": "user", "content": "Respondé solo con la palabra: OK"}],
                   max_tokens=200, temperature=0, purpose="test")
    return {"ok": True, "model": r.model, "reply": r.text[:80], "ms": r.ms, "models": models}


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
