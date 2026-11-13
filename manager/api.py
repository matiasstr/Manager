"""API REST + interfaz web. La documentación interactiva queda en /docs."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, db, debate, llm, store
from . import embeddings as emb

log = logging.getLogger("manager")
_warm = {"state": "cargando", "name": ""}


async def _warmup() -> None:
    try:
        t0 = time.time()
        await asyncio.to_thread(store.ensure_index)
        _warm.update(state="listo", name=emb.get().name)
        log.info("Memoria lista en %.1f s", time.time() - t0)
    except Exception as e:  # noqa: BLE001
        log.exception("No se pudo preparar el índice")
        _warm.update(state="error", name=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_warmup())
    yield
    task.cancel()


app = FastAPI(title="Manager", version=__version__, lifespan=lifespan,
              description="Mini cerebro multiagente: discusión entre LLM gratuitas, memoria con RAG y módulos consultables sin LLM.")


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


# ───────────────────────── Estado general ─────────────────────────

@app.get("/api/state")
async def state():
    providers = await llm.status()
    return {
        "version": __version__,
        "providers": providers,
        "demo": not any(p["ready"] and not p["demo"] for p in providers),
        "agents": db.q("SELECT * FROM agents ORDER BY is_moderator, id"),
        "modules": store.list_modules(),
        "stats": store.stats(),
        "embedder": _warm,
        "running": list(debate.RUNS),
    }


@app.get("/api/graph")
def graph():
    """El grafo real de LangGraph, en Mermaid."""
    return {"mermaid": debate.GRAPH.get_graph().draw_mermaid()}


# ───────────────────────── Proveedores ─────────────────────────

class KeyIn(BaseModel):
    key: str = ""


@app.put("/api/providers/{pid}/key")
async def set_key(pid: str, body: KeyIn):
    if not config.provider(pid):
        raise HTTPException(404, "Proveedor desconocido")
    config.set_key(pid, body.key or None)
    llm._models.pop(pid, None)
    return {"ok": True}


@app.get("/api/providers/{pid}/models")
async def models(pid: str):
    try:
        return {"models": await llm.list_models(pid)}
    except llm.LLMError as e:
        return {"models": [], "error": str(e)}


@app.post("/api/providers/{pid}/test")
async def test_provider(pid: str):
    try:
        return await llm.test(pid)
    except llm.LLMError as e:
        return {"ok": False, "error": str(e)}


# ───────────────────────── Agentes ─────────────────────────

class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    role: str = Field(min_length=1, max_length=1200)
    provider: str = "auto"
    model: str = "auto"
    color: str = "#5eb8a6"
    temperature: float = Field(0.7, ge=0, le=1.5)
    enabled: bool = True


@app.post("/api/agents")
def add_agent(a: AgentIn):
    aid = db.run("INSERT INTO agents(name,role,provider,model,color,temperature,enabled) VALUES(?,?,?,?,?,?,?)",
                 (a.name, a.role, a.provider, a.model or "auto", a.color, a.temperature, int(a.enabled)))
    return {"id": aid}


@app.put("/api/agents/{aid}")
def edit_agent(aid: int, a: AgentIn):
    db.run("UPDATE agents SET name=?, role=?, provider=?, model=?, color=?, temperature=?, enabled=? WHERE id=?",
           (a.name, a.role, a.provider, a.model or "auto", a.color, a.temperature, int(a.enabled), aid))
    return {"ok": True}


@app.delete("/api/agents/{aid}")
def del_agent(aid: int):
    if (db.one("SELECT is_moderator m FROM agents WHERE id=?", (aid,)) or {}).get("m"):
        raise HTTPException(400, "El moderador no se puede borrar")
    db.run("DELETE FROM agents WHERE id=?", (aid,))
    return {"ok": True}


# ───────────────────────── Contexto y memoria ─────────────────────────

class TextIn(BaseModel):
    text: str = ""


class DocIn(BaseModel):
    title: str = ""
    text: str = Field(min_length=1)


@app.get("/api/context")
def get_context():
    return {"text": db.meta_get("context")}


@app.put("/api/context")
def put_context(body: TextIn):
    db.meta_set("context", body.text)
    return {"ok": True}


@app.get("/api/docs-list")
def docs():
    return store.list_docs()


@app.post("/api/docs-list")
async def add_doc(d: DocIn):
    try:
        return await asyncio.to_thread(store.add_doc, d.title, d.text)
    except ValueError as e:
        raise _bad(e)


@app.delete("/api/docs-list/{doc_id}")
def del_doc(doc_id: int):
    store.delete_doc(doc_id)
    return {"ok": True}


@app.get("/api/search")
async def search(q: str, k: int = 6):
    return await asyncio.to_thread(store.search_memory, q, k)


# ───────────────────────── Módulos ─────────────────────────

class ModuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""


class CardIn(BaseModel):
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    kind: str = "hecho"
    tags: list[str] = []
    variants: list[str] = []


@app.get("/api/modules")
def modules():
    return store.list_modules()


@app.post("/api/modules")
def add_module(m: ModuleIn):
    return store.create_module(m.name, m.description)


@app.delete("/api/modules/{mid}")
def del_module(mid: int):
    store.delete_module(mid)
    return {"ok": True}


@app.get("/api/modules/{mid}/cards")
def cards(mid: int):
    return store.list_cards(mid)


@app.post("/api/modules/{mid}/cards")
async def add_card(mid: int, c: CardIn):
    try:
        return await asyncio.to_thread(store.add_card, mid, c.question, c.answer, c.kind, c.tags, c.variants, "manual")
    except ValueError as e:
        raise _bad(e)


@app.put("/api/cards/{cid}")
async def edit_card(cid: int, c: CardIn):
    await asyncio.to_thread(store.update_card, cid, c.question, c.answer, c.kind, c.tags, c.variants)
    return {"ok": True}


@app.delete("/api/cards/{cid}")
def del_card(cid: int):
    store.delete_card(cid)
    return {"ok": True}


def _filename(name: str, ext: str) -> str:
    return (re.sub(r"[^\w\-]+", "-", emb.fold(name)).strip("-") or "modulo") + ext


@app.get("/api/modules/{mid}/export.json")
def export_json(mid: int):
    try:
        data = store.export_module(mid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{_filename(data["name"], ".modulo.json")}"'})


@app.get("/api/modules/{mid}/export.html")
def export_html(mid: int):
    try:
        name, html = store.export_module_html(mid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return HTMLResponse(html, headers={"Content-Disposition": f'attachment; filename="{_filename(name, ".html")}"'})


@app.post("/api/modules/import")
async def import_module(data: dict):
    try:
        return await asyncio.to_thread(store.import_module, data)
    except ValueError as e:
        raise _bad(e)


# ───────────────────────── Consultar sin LLM ─────────────────────────

class AskIn(BaseModel):
    q: str = Field(min_length=1)
    module_ids: list[int] = []
    local_model: bool = False


@app.post("/api/ask")
async def ask(a: AskIn):
    """Responde desde los módulos. No llama a ninguna LLM salvo que se pida redactar con el modelo local."""
    res = await asyncio.to_thread(store.ask, a.q, a.module_ids or None)
    if a.local_model and res["answered"]:
        notes = "\n\n".join(f"- {c['question']}\n  {c['answer']}" for c in [res["card"], *res["related"][:2]])
        try:
            r = await llm.chat("ollama", "auto", [{"role": "user", "content":
                               f"Respondé la pregunta usando solo estas notas. Si no alcanzan, decilo.\n\n# Notas\n{notes}\n\n# Pregunta\n{a.q}"}],
                               temperature=0.2, max_tokens=600, purpose="answer")
            res["composed"] = {"text": r.text, "model": r.model, "ms": r.ms}
        except llm.LLMError as e:
            res["composed_error"] = str(e)
    return res


# ───────────────────────── Discusiones ─────────────────────────

class DiscussIn(BaseModel):
    problem: str = Field(min_length=1)
    agent_ids: list[int] = []
    max_rounds: int = Field(2, ge=1, le=4)
    module_id: int | None = None


class AcceptIn(BaseModel):
    module_id: int | None = None
    new_module: str = ""
    ids: list[int] | None = None


@app.get("/api/discussions")
def discussions():
    return db.q("SELECT id, problem, status, consensus, created_at, finished_at FROM discussions ORDER BY id DESC LIMIT 100")


@app.post("/api/discussions")
async def start_discussion(d: DiscussIn):
    try:
        run = debate.start(d.problem, d.agent_ids or None, d.max_rounds, d.module_id)
    except ValueError as e:
        raise _bad(e)
    return {"id": run.did}


@app.get("/api/discussions/{did}")
def discussion(did: int):
    d = db.one("SELECT * FROM discussions WHERE id=?", (did,))
    if not d:
        raise HTTPException(404, "Discusión inexistente")
    d["config"] = db.loads(d["config"], {})
    d["live"] = did in debate.RUNS
    d["proposals"] = [{**p, "tags": db.loads(p["tags"], []), "variants": db.loads(p["variants"], [])}
                      for p in db.q("SELECT * FROM proposals WHERE discussion_id=? ORDER BY id", (did,))]
    return d


@app.get("/api/discussions/{did}/events")
async def events(did: int, after: int = 0):
    """Server-Sent Events: en vivo si la discusión está corriendo, repetición desde la base si ya terminó."""
    if not db.one("SELECT id FROM discussions WHERE id=?", (did,)):
        raise HTTPException(404, "Discusión inexistente")

    async def gen():
        run = debate.RUNS.get(did)
        if run:
            async for ev in run.stream(after):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        else:
            for ev in debate.stored_events(did)[after:]:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        yield 'data: {"type":"end"}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/discussions/{did}/stop")
async def stop(did: int):
    run = debate.RUNS.get(did)
    if run and run.task:
        run.task.cancel()
    return {"ok": True}


@app.delete("/api/discussions/{did}")
def del_discussion(did: int):
    if did in debate.RUNS:
        raise HTTPException(400, "La discusión está en curso")
    db.run("DELETE FROM discussions WHERE id=?", (did,))
    return {"ok": True}


@app.post("/api/discussions/{did}/accept")
async def accept(did: int, a: AcceptIn):
    """Guarda en un módulo las tarjetas que destiló la discusión."""
    try:
        mid = a.module_id or (store.create_module(a.new_module)["id"] if a.new_module.strip() else None)
        if not mid:
            raise ValueError("Elegí un módulo o escribí el nombre de uno nuevo")
        saved = await asyncio.to_thread(debate.accept_cards, did, mid, a.ids)
    except ValueError as e:
        raise _bad(e)
    return {"module_id": mid, "saved": len(saved)}


# ───────────────────────── Interfaz ─────────────────────────

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
