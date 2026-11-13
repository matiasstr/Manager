"""Ejercita el camino real de llamadas (SDK de OpenAI, streaming, reintentos, respaldos, acceso sin key)
contra un servidor falso compatible con OpenAI. No usa internet ni keys reales.

    python tests/prueba_proveedores.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

tmp = tempfile.mkdtemp(prefix="manager_prov_")
os.environ["MANAGER_DATA"] = tmp
os.environ["MANAGER_EMBEDDINGS"] = "hash"  # esta prueba no mide búsqueda: evita cargar el modelo
os.environ["FAKE_A_KEY"] = "sk-test-aaaaaaaa1111"
os.environ["FAKE_B_KEY"] = "sk-test-bbbbbbbb2222"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORT = 8799
fake = FastAPI()
calls = {"rate": 0, "log": [], "anon_auth": []}
IDS = ["whisper-large-v3", "llama-3.1-8b-instant", "llama-3.3-70b-versatile", "openai/gpt-oss-120b", "qwen/qwen3-32b",
       "meta/llama-guard-4", "nvidia/nemotron-parse-2.0", "nvidia/nemotron-4-340b-reward", "mistralai/codestral-22b"]


@fake.get("/{who}/v1/models")
def models(who: str, request: Request):
    if who != "anon" and not request.headers.get("authorization", "").startswith("Bearer sk-test"):
        return JSONResponse({"error": {"message": "bad key"}}, 401)
    return {"object": "list", "data": [{"id": i, "object": "model", "created": 0, "owned_by": "x"} for i in IDS]}


@fake.post("/{who}/v1/chat/completions")
async def chat(who: str, request: Request):
    body = await request.json()
    model, prompt = body["model"], body["messages"][-1]["content"]
    calls["log"].append((who, model, sorted(k for k in body if k not in ("messages", "model"))))
    if who == "anon":
        calls["anon_auth"].append(request.headers.get("authorization"))
        if request.headers.get("authorization"):
            return JSONResponse({"message": "Forbidden: authentication failed."}, 403)
    if model == "missing":
        return JSONResponse({"error": {"message": "model not found"}}, 404)
    if model == "unavailable":
        return JSONResponse({"error": {"message": "Model 'unavailable' is currently unavailable.", "code": "model_unavailable"}}, 400)
    if model == "badkey400":  # así contesta Gemini con una key inválida
        return JSONResponse([{"error": {"code": 400, "message": "Please pass a valid API key", "status": "INVALID_ARGUMENT"}}], 400)
    if model == "paid-only":
        return JSONResponse({"error": {"message": "not allowed for this key"}}, 403)
    if model == "no-json" and "response_format" in body:
        return JSONResponse({"error": {"message": "response_format unsupported"}}, 400)
    if model == "rate":
        calls["rate"] += 1
        if calls["rate"] == 1:
            return JSONResponse({"error": {"message": "slow down"}}, 429)
    if model == "always-429":
        return JSONResponse({"error": {"message": "quota"}}, 429, headers={"retry-after": "45"})
    reasoning = ""
    if '"consenso"' in prompt:
        text = '```json\n{"consenso": 0.91, "acuerdos": ["Mover el backup"], "desacuerdos": []}\n```'
    elif '"tarjetas"' in prompt:
        text = json.dumps({"tarjetas": [{"pregunta": "¿A qué hora conviene el backup?", "respuesta": "A las 4 AM, después del reinicio.",
                                        "tipo": "decision", "etiquetas": ["backups"], "variantes": ["¿Cuándo hago el respaldo?"]}]}, ensure_ascii=False)
    elif model == "think":
        text = "<think>razonando en secreto…</think>Respuesta visible."
    elif model == "reason":
        reasoning, text = "El usuario pide algo corto.", "Respuesta tras razonar."
    else:
        text = f"Soy {who} con {model}. Propongo mover el backup a las 4 AM."

    async def gen():
        def chunk(delta):
            return "data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model,
                                          "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}) + "\n\n"
        for i in range(0, len(reasoning), 9):
            yield chunk({"role": "assistant", "reasoning": reasoning[i:i + 9]})
        for i in range(0, len(text), 7):
            yield chunk({"content": text[i:i + 7]})
            await asyncio.sleep(0.02)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
time.sleep(1.5)

from manager import api, config, db, debate, llm  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}"
FAKES = [
    {"id": "anon", "name": "Sin Key", "base_url": f"{BASE}/anon/v1", "keyless": {"rpm_model": 2}, "token": False,
     "prefer": ["gpt-oss-120b", "llama-3.3-70b", "qwen"], "rotate": 6},
    {"id": "fa", "name": "Falso A", "base_url": f"{BASE}/a/v1", "env": "FAKE_A_KEY", "prefer": ["llama-3.3-70b", "gpt-oss-120b", "qwen", "llama"]},
    {"id": "fb", "name": "Falso B", "base_url": f"{BASE}/b/v1", "env": "FAKE_B_KEY", "prefer": ["gpt-oss"],
     "extra": {"reasoning_effort": "low"}, "max_tokens": 4000},
    {"id": "demo", "name": "Demo", "kind": "mock"},
]
config.providers = lambda: FAKES
fails = []
ASK = [{"role": "user", "content": "hola"}]


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)


async def error_of(coro):
    try:
        await coro
    except llm.LLMError as e:
        return e
    return None


async def main():
    # Seguridad del servidor local
    check("pedido propio permitido", api.blocked({"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}) == "")
    check("otra página web bloqueada", api.blocked({"host": "127.0.0.1:8765", "origin": "https://sitio-ajeno.example"}) != "")
    check("DNS rebinding bloqueado", api.blocked({"host": "sitio-ajeno.example:8765"}) != "")

    # Elección de modelos
    check("sin activar, el proveedor sin key no cuenta", await llm.available() == ["fa", "fb"])
    config.set_enabled("anon", True)
    check("con key primero, sin key después", await llm.available() == ["fa", "fb", "anon"], str(await llm.available()))
    ms = await llm.list_models("fa")
    check("no elige modelos que no conversan", not any(x in m for m in ms for x in ("whisper", "guard", "parse", "reward")), str(ms))
    check("los de nicho quedan al final", ms[-1] == "mistralai/codestral-22b" and ms[0] == "llama-3.3-70b-versatile", str(ms))
    rot = [await llm.resolve("fa", "auto", i) for i in range(3)]
    check("reparte familias distintas", rot == ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "qwen/qwen3-32b"], str(rot))

    # Key inválida al estilo Gemini (400), antes de que esa key haya funcionado nunca
    e = await error_of(llm.chat("fb", "badkey400", ASK))
    check("400 de key inválida no se reintenta", e and "no es válida" in str(e) and not e.model_level and len(calls["log"]) == 1, str(e))

    # Streaming y variantes
    got = []
    r = await llm.chat("fa", "auto", ASK, on_delta=got.append)
    check("streaming real", r.text.startswith("Soy a con llama-3.3-70b-versatile") and len(got) > 3 and "".join(got) == r.text)
    r = await llm.chat("fb", "auto", ASK)
    check("parámetros extra y tope de tokens por proveedor", calls["log"][-1][2] == ["max_tokens", "reasoning_effort", "stream", "temperature"], str(calls["log"][-1]))
    got = []
    r = await llm.chat("fa", "think", ASK, on_delta=got.append)
    check("oculta <think>", r.text == "Respuesta visible." and "secreto" not in "".join(got))
    thinking = []
    r = await llm.chat("fa", "reason", ASK, on_think=lambda: thinking.append(1))
    check("avisa que el modelo razona", r.text == "Respuesta tras razonar." and thinking == [1])
    t = time.time()
    r = await llm.chat("fa", "rate", ASK)
    check("reintenta tras 429", bool(r.text) and calls["rate"] == 2 and time.time() - t >= 1.9)
    r = await llm.chat("fa", "no-json", ASK, json_mode=True)
    check("sin modo JSON, reintenta sin él", bool(r.text))

    # Modelos caídos
    e = await error_of(llm.chat("fa", "missing", ASK))
    check("404: modelo fuera de rotación", e and e.model_level and "no existe" in str(e))
    e = await error_of(llm.chat("fa", "unavailable", ASK))
    check("400 «unavailable»: fuera de rotación, sin reintentos inútiles", e and e.model_level and [c[1] for c in calls["log"]].count("unavailable") == 1)
    e = await error_of(llm.chat("fa", "paid-only", ASK))
    check("403 con una key que ya funcionó: es del modelo", e and e.model_level and "no está incluido en tu plan" in str(e), str(e))
    check("lista de salteados", {s["model"] for s in llm.skipped("fa")} == {"missing", "unavailable", "paid-only"}, str(llm.skipped("fa")))
    llm.mark_dead("fa", "llama-3.3-70b-versatile", 5, "prueba")
    check("la rotación esquiva lo caído", await llm.resolve("fa", "auto", 0) == "openai/gpt-oss-120b")
    llm._dead.clear()

    # Acceso sin key
    r = await llm.chat("anon", "auto", ASK)
    check("sin key: no manda Authorization", bool(r.text) and calls["anon_auth"] == [None], str(calls["anon_auth"]))
    await llm.chat("anon", "openai/gpt-oss-120b", ASK)
    gate = llm._gates(config.provider("anon"), "openai/gpt-oss-120b")[0]
    check("cupo por modelo agotado tras 2 pedidos", gate.wait_time() > 50)
    check("elige otro modelo con cupo", await llm.pick_free("anon") == "llama-3.3-70b-versatile", await llm.pick_free("anon"))
    res = await llm.test("fa")
    check("prueba de proveedor", res["ok"] and res["skipped"] == [])
    llm._windows.clear()

    # Discusión completa: reparto, respaldo, reasignación, juez y destilado
    calls["log"].clear()
    db.run("UPDATE agents SET provider='fa', model='always-429' WHERE name='Crítico'")
    run = debate.start("¿A qué hora conviene hacer el backup?", None, 2, None)
    seen = {"start": [], "err": [], "failed": [], "eval": None, "cards": None, "status": None}
    async for ev in run.stream():
        if ev["type"] == "message_start":
            seen["start"].append((ev["agent"], ev["round"], ev["provider"], ev["model"], ev["backup"]))
        elif ev["type"] == "message_error":
            seen["err"].append((ev["agent"], ev["round"], ev["error"]))
        elif ev["type"] == "message_failed":
            seen["failed"].append(ev["agent"])
        elif ev["type"] == "evaluation":
            seen["eval"] = (ev["consensus"], ev["by"], ev["next"])
        elif ev["type"] == "step_finished" and ev["step"] == "destilar":
            seen["cards"] = ev["cards"]
        elif ev["type"] in ("run_finished", "run_error"):
            seen["status"] = ev
    for s in seen["start"]:
        print("   ", s)
    check("termina bien", seen["status"]["type"] == "run_finished", str(seen["status"]))
    first = {a: p for a, rnd, p, m, b in seen["start"] if rnd == 1 and not b}
    check("reparte agentes entre proveedores", len({first.get("Analista"), first.get("Pragmático"), "fa"}) == 3, str(first))
    check("respaldo tras 429", any(a == "Crítico" and b for a, rnd, p, m, b in seen["start"]) and len(seen["err"]) == 1, str(seen["err"]))
    check("nadie se queda sin respuesta", seen["failed"] == [])
    check("juez con JSON entre ```", seen["eval"] == (0.91, "juez", "sintetizar"), str(seen["eval"]))
    check("destila tarjetas", bool(seen["cards"]) and seen["cards"][0]["question"].startswith("¿A qué hora"))


asyncio.run(main())
db._conn.close()
shutil.rmtree(tmp, ignore_errors=True)
print("\nFALLAS:", fails or "ninguna")
sys.stdout.flush()
os._exit(1 if fails else 0)
