"""Servidor MCP: expone el cerebro como herramientas para cualquier agente compatible
(Claude Desktop, Claude Code, Cursor, etc.). Usa la misma base que la interfaz web.
"""
from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

from . import db, debate, store

server = MCPServer(
    "manager",
    instructions=(
        "Mini cerebro multiagente. Usá `consultar` primero: responde desde módulos de conocimiento sin gastar LLM. "
        "Si no alcanza, `buscar_memoria` trae notas y síntesis previas, y `discutir` abre un debate entre varias LLM."
    ),
)


def _module_ids(modulo: str) -> list[int] | None:
    if not modulo.strip():
        return None
    r = db.one("SELECT id FROM modules WHERE name=? COLLATE NOCASE", (modulo.strip(),))
    if not r:
        raise ValueError(f"No existe el módulo «{modulo}». Módulos: " + ", ".join(m["name"] for m in store.list_modules()))
    return [r["id"]]


@server.tool()
async def consultar(pregunta: str, modulo: str = "") -> dict:
    """Responde una pregunta desde los módulos de conocimiento, sin usar ninguna LLM.

    Devuelve la respuesta guardada más cercana con su confianza (0 a 1), o `answered: false` si no hay nada parecido.
    `modulo` limita la búsqueda a un módulo por nombre; vacío busca en todos.
    """
    res = await asyncio.to_thread(store.ask, pregunta, _module_ids(modulo))
    slim = lambda c: {k: c[k] for k in ("question", "answer", "module", "confidence")}  # noqa: E731
    return {"answered": res["answered"], "level": res["level"], "confidence": res["confidence"], "answer": res["answer"],
            "source": slim(res["card"]) if res["card"] else None, "related": [slim(c) for c in res["related"][:3]]}


@server.tool()
async def buscar_memoria(consulta: str, cantidad: int = 5) -> list[dict]:
    """Busca en la memoria (notas, documentos y síntesis de discusiones anteriores) con búsqueda híbrida."""
    rows = await asyncio.to_thread(store.search_memory, consulta, max(1, min(cantidad, 10)))
    return [{"title": r["title"], "text": r["text"], "confidence": r["confidence"]} for r in rows]


@server.tool()
def listar_modulos() -> list[dict]:
    """Lista los módulos de conocimiento con la cantidad de tarjetas de cada uno."""
    return [{"name": m["name"], "description": m["description"], "cards": m["cards"]} for m in store.list_modules()]


@server.tool()
async def guardar_conocimiento(modulo: str, pregunta: str, respuesta: str, tipo: str = "hecho") -> dict:
    """Guarda una tarjeta (pregunta y respuesta autocontenida) en un módulo. Crea el módulo si no existe."""
    mid = store.get_or_create_module(modulo)
    return await asyncio.to_thread(store.add_card, mid, pregunta, respuesta, tipo, [], [], "mcp")


@server.tool()
async def agregar_nota(titulo: str, texto: str) -> dict:
    """Agrega información nueva a la memoria general para que las próximas discusiones la tengan en cuenta."""
    return await asyncio.to_thread(store.add_doc, titulo, texto)


@server.tool()
async def discutir(problema: str, rondas: int = 2, modulo: str = "") -> dict:
    """Abre una discusión entre varias LLM sobre un problema y devuelve la síntesis final.

    Tarda de 20 segundos a un par de minutos. Si se indica `modulo`, las tarjetas destiladas se guardan ahí.
    """
    mid = store.get_or_create_module(modulo) if modulo.strip() else None
    return await debate.run_to_end(problema, rondas, mid)
