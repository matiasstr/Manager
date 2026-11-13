"""Discusión entre agentes, orquestada como un grafo de LangGraph.

    recuperar → proponer (en paralelo) → evaluar ─┬→ criticar (en paralelo) → evaluar …
                                                  └→ sintetizar → destilar → guardar

Cada paso emite eventos tipados (inicio/fin de paso, texto en streaming por agente, evaluación de
consenso) que la interfaz consume por SSE para mostrar en vivo qué está haciendo el sistema.
"""
from __future__ import annotations

import asyncio
import json
import operator
import time
from typing import Annotated, TypedDict

import numpy as np
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import db, llm, store
from . import embeddings as emb

EARLY_STOP = 0.88  # consenso a partir del cual no hace falta otra ronda


# ───────────────────────── Eventos ─────────────────────────

class Run:
    """Bitácora de eventos de una discusión: en memoria para el streaming, en SQLite para la repetición."""

    def __init__(self, did: int):
        self.did = did
        self.events: list[dict] = []
        self.done = False
        self.task: asyncio.Task | None = None
        self.pool: list[str] = []
        self.started: set[str] = set()
        self._wake = asyncio.Event()
        self._seq = 0

    def emit(self, type: str, **data) -> None:
        ev = {"seq": len(self.events), "type": type, "t": round(time.time(), 3), **data}
        self.events.append(ev)
        if type != "message_delta":  # los fragmentos de texto no se guardan: el mensaje final ya trae todo
            db.run("INSERT INTO events(discussion_id,seq,type,data) VALUES(?,?,?,?)",
                   (self.did, self._seq, type, json.dumps(ev, ensure_ascii=False)))
            self._seq += 1
        self._wake.set()

    def finish(self) -> None:
        self.done = True
        self._wake.set()

    async def stream(self, after: int = 0):
        i = after
        while True:
            while i < len(self.events):
                yield self.events[i]
                i += 1
            if self.done:
                return
            self._wake.clear()
            await self._wake.wait()


RUNS: dict[int, Run] = {}


def stored_events(did: int) -> list[dict]:
    return [db.loads(r["data"], {}) for r in db.q("SELECT data FROM events WHERE discussion_id=? ORDER BY seq", (did,))]


# ───────────────────────── Prompts ─────────────────────────

SYSTEM = (
    "Sos {name}, integrante de un panel donde distintas IA discuten un problema. Tu rol: {role}\n"
    "Reglas: respondé en español, directo y concreto, en no más de 220 palabras. Usá el contexto y la memoria "
    "cuando apliquen y decí explícitamente cuando algo es una suposición tuya. No repitas el enunciado ni saludes."
)

PROPOSE = (
    "{base}\n\n# Problema\n{problem}\n\n"
    "Dá tu propuesta inicial: tu respuesta o solución, el razonamiento clave que la sostiene y las dudas o riesgos que ves."
)

CRITIQUE = (
    "{base}\n\n# Problema\n{problem}\n\n# Tu postura anterior\n{mine}\n\n# Posturas de los otros agentes\n{others}\n\n"
    "Ronda {round}. Criticá con argumentos lo que consideres incorrecto o débil en las otras posturas, reconocé lo que "
    "te convenció y actualizá tu postura. Si cambiás de opinión, decilo. Cerrá con una línea que empiece con "
    "«POSTURA FINAL:»."
)

JUDGE = (
    "Sos el moderador neutral de un panel. Evaluá cuánto coinciden las posturas de esta ronda.\n\n"
    "# Problema\n{problem}\n\n# Posturas\n{positions}\n\n"
    "Devolvé únicamente un objeto JSON con esta forma:\n"
    '{{"consenso": número entre 0 y 1, "acuerdos": ["…"], "desacuerdos": ["…"]}}\n'
    "«consenso» es 1 si coinciden en lo esencial y 0 si se contradicen. Máximo 4 ítems por lista, cada uno de una oración."
)

SYNTH = (
    "Sos {name}, moderador del panel. {role}\n\n{base}\n\n# Problema\n{problem}\n\n# Posturas finales\n{positions}\n\n"
    "{points}"
    "Escribí la síntesis final en Markdown con estas secciones, sin texto antes ni después:\n"
    "## Respuesta acordada\n## Puntos de acuerdo\n## Desacuerdos que persisten\n## Próximos pasos\n"
    "Si un desacuerdo no se resolvió, no lo escondas: explicá las dos posiciones. Máximo 350 palabras."
)

DISTILL = (
    "Convertí esta síntesis en tarjetas de conocimiento reutilizables. Más adelante se van a consultar con un "
    "buscador, SIN ninguna IA, así que cada tarjeta tiene que responder sola.\n\n"
    "# Problema original\n{problem}\n\n# Síntesis\n{synthesis}\n\n"
    "Devolvé únicamente un objeto JSON con esta forma:\n"
    '{{"tarjetas": [{{"pregunta": "…", "respuesta": "…", "tipo": "hecho|procedimiento|decision|definicion|advertencia", '
    '"etiquetas": ["…"], "variantes": ["otra forma de preguntar lo mismo", "…"]}}]}}\n'
    "Reglas: entre 3 y 7 tarjetas. La pregunta va como la escribiría una persona. La respuesta se entiende sin haber "
    "leído la discusión: no digas «el panel», «los agentes» ni «la síntesis». Incluí 2 variantes por tarjeta con "
    "otras palabras. La primera tarjeta responde directamente al problema original. No inventes datos que no estén en la síntesis."
)


def _base(context: str, memory: str) -> str:
    parts = []
    if context.strip():
        parts.append("# Contexto general\n" + context.strip()[:4000])
    if memory.strip():
        parts.append("# Conocimiento recuperado de la memoria\n" + memory.strip())
    return "\n\n".join(parts) or "# Contexto general\n(sin contexto cargado)"


def _clip(text: str, n: int = 1400) -> str:
    return text if len(text) <= n else text[:n] + " […]"


def _latest(turns: list[dict]) -> dict[int, dict]:
    """Última intervención válida de cada agente."""
    out: dict[int, dict] = {}
    for t in turns:
        if t.get("text"):
            out[t["agent_id"]] = t
    return out


# ───────────────────────── Llamadas a los modelos ─────────────────────────

def _attempts(run: Run, agent: dict) -> list[tuple[str, str, int]]:
    """Intento principal y respaldos: otro modelo del mismo proveedor, después otros proveedores."""
    plan = [(agent["pid"], agent["model"], agent["slot"])]
    if agent["model"] == "auto" and agent["slot"] != 0:
        plan.append((agent["pid"], "auto", 0))
    plan += [(pid, "auto", 0) for pid in run.pool if pid != agent["pid"]]
    return plan[:3]


async def _ask(run: Run, agent: dict, messages: list[dict], *, purpose: str, stage: str, rnd: int,
               problem: str = "", visible: bool = True, json_mode: bool = False, max_tokens: int = 1500) -> dict:
    mid = f"{stage}-{rnd}-{agent['id']}"
    turn = {"agent_id": agent["id"], "agent": agent["name"], "round": rnd, "stage": stage, "text": ""}
    plan = _attempts(run, agent)
    error = ""
    for n, (pid, model, slot) in enumerate(plan):
        try:
            model = await llm.resolve(pid, model, slot)
            if visible:
                run.emit("message_start", mid=mid, agent_id=agent["id"], agent=agent["name"], color=agent["color"],
                         stage=stage, round=rnd, provider=pid, model=model, backup=n > 0)
            r = await llm.chat(
                pid, model, messages, temperature=agent["temperature"], max_tokens=max_tokens, json_mode=json_mode,
                purpose=purpose, slot=slot, meta={"name": agent["name"], "round": rnd, "problem": problem},
                on_delta=(lambda d: run.emit("message_delta", mid=mid, delta=d)) if visible else None,
                on_reset=(lambda: run.emit("message_reset", mid=mid)) if visible else None,
            )
            if visible:
                run.emit("message_end", mid=mid, text=r.text, provider=r.provider, model=r.model, ms=r.ms)
            return {**turn, "text": r.text, "provider": r.provider, "model": r.model, "ms": r.ms}
        except llm.LLMError as e:
            error = str(e)
            run.emit("message_error", mid=mid, agent_id=agent["id"], agent=agent["name"], stage=stage, round=rnd,
                     error=error, retrying=n + 1 < len(plan), visible=visible)
    return {**turn, "error": error}


# ───────────────────────── Grafo ─────────────────────────

class State(TypedDict, total=False):
    did: int
    problem: str
    context: str
    memory: str
    agents: list[dict]
    moderator: dict
    module_id: int | None
    round: int
    max_rounds: int
    turns: Annotated[list[dict], operator.add]
    evals: Annotated[list[dict], operator.add]
    consensus: float
    synthesis: str
    cards: list[dict]


def _step(run: Run, step: str, rnd: int = 0) -> None:
    key = f"{step}-{rnd}"
    if key not in run.started:
        run.started.add(key)
        run.emit("step_started", step=step, round=rnd)


async def n_recuperar(s: State) -> dict:
    run = RUNS[s["did"]]
    _step(run, "recuperar")
    items = await asyncio.to_thread(store.search_all, s["problem"], 6)
    memory = "\n\n".join(f"- [{i['kind']}] {i['title']}: {_clip(i['text'], 600)}" for i in items)
    run.emit("step_finished", step="recuperar", round=0,
             items=[{**i, "text": _clip(i["text"], 320)} for i in items], embedder=emb.get().name)
    return {"memory": memory}


def _fan(stage: str):
    def route(s: State):
        payload = {k: s[k] for k in ("did", "problem", "context", "memory", "round")}
        return [Send(stage, {**payload, "agent": a, "turns": s.get("turns", [])}) for a in s["agents"]]
    return route


def _turn(stage: str):
    async def node(p: dict) -> dict:
        run, a, rnd = RUNS[p["did"]], p["agent"], p["round"]
        _step(run, stage, rnd)
        base = _base(p["context"], p["memory"])
        if stage == "proponer":
            user = PROPOSE.format(base=base, problem=p["problem"])
        else:
            last = _latest(p["turns"])
            mine = last.get(a["id"], {}).get("text") or "(todavía no diste una postura)"
            others = "\n\n".join(f"## {t['agent']}\n{_clip(t['text'])}" for aid, t in last.items() if aid != a["id"])
            user = CRITIQUE.format(base=base, problem=p["problem"], mine=_clip(mine), others=others or "(sin otras posturas)", round=rnd)
        system = SYSTEM.format(name=a["name"], role=a["role"])
        turn = await _ask(run, a, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                          purpose="propose" if stage == "proponer" else "critique", stage=stage, rnd=rnd, problem=p["problem"])
        return {"turns": [turn]}
    return node


async def n_evaluar(s: State) -> dict:
    run, rnd = RUNS[s["did"]], s["round"]
    stage = "proponer" if rnd == 1 else "criticar"
    run.emit("step_finished", step=stage, round=rnd)
    _step(run, "evaluar", rnd)

    now = [t for t in s["turns"] if t["round"] == rnd and t.get("text")]
    if not now and rnd == 1:
        errors = "; ".join(sorted({t.get("error", "") for t in s["turns"] if t.get("error")}))
        raise RuntimeError("Ningún agente pudo responder. " + errors)
    current = list(_latest(s["turns"]).values())

    # 1) Similitud semántica local entre posturas (gratis, sin LLM).
    names = [t["agent"] for t in current]
    matrix = [[1.0]]
    semantic = 1.0
    if len(current) > 1:
        vecs = await emb.aembed([t["text"] for t in current])
        sim = vecs @ vecs.T
        matrix = [[round(float(x), 3) for x in row] for row in sim]
        lo, hi = emb.get().calib
        off = sim[~np.eye(len(current), dtype=bool)].mean()
        semantic = float(np.clip((off - lo) / (1 - lo), 0, 1))

    # 2) Juez LLM: mide acuerdo real y lista los puntos.
    consensus, agreements, disagreements, by = semantic, [], [], "embeddings"
    if len(current) > 1:
        positions = "\n\n".join(f"## {t['agent']}\n{_clip(t['text'])}" for t in current)
        r = await _ask(run, s["moderator"], [{"role": "user", "content": JUDGE.format(problem=s["problem"], positions=positions)}],
                       purpose="judge", stage="evaluar", rnd=rnd, problem=s["problem"], visible=False, json_mode=True, max_tokens=700)
        data = llm.parse_json(r["text"]) if r.get("text") else None
        if isinstance(data, dict) and isinstance(data.get("consenso"), (int, float)):
            consensus = float(np.clip(data["consenso"], 0, 1))
            agreements = [str(x) for x in (data.get("acuerdos") or [])][:4]
            disagreements = [str(x) for x in (data.get("desacuerdos") or [])][:4]
            by = "juez"

    more = rnd < s["max_rounds"] and consensus < EARLY_STOP and bool(now)
    ev = {"round": rnd, "consensus": round(consensus, 3), "semantic": round(semantic, 3), "by": by,
          "agreements": agreements, "disagreements": disagreements, "names": names, "matrix": matrix,
          "next": "criticar" if more else "sintetizar"}
    run.emit("evaluation", **ev)
    run.emit("step_finished", step="evaluar", round=rnd, consensus=ev["consensus"])
    return {"round": rnd + 1 if more else s["max_rounds"] + 1, "consensus": consensus, "evals": [ev]}


def _after_eval(s: State):
    if s["round"] > s["max_rounds"]:
        return "sintetizar"
    return _fan("criticar")(s)


async def n_sintetizar(s: State) -> dict:
    run, mod = RUNS[s["did"]], s["moderator"]
    _step(run, "sintetizar")
    positions = "\n\n".join(f"## {t['agent']}\n{_clip(t['text'], 1800)}" for t in _latest(s["turns"]).values())
    last = s["evals"][-1] if s.get("evals") else {}
    points = ""
    if last.get("agreements") or last.get("disagreements"):
        points = ("# Evaluación de la última ronda\nAcuerdos: " + "; ".join(last.get("agreements", [])) +
                  "\nDesacuerdos: " + ("; ".join(last.get("disagreements", [])) or "ninguno") + "\n\n")
    prompt = SYNTH.format(name=mod["name"], role=mod["role"], base=_base(s["context"], s["memory"]),
                          problem=s["problem"], positions=positions, points=points)
    r = await _ask(run, mod, [{"role": "user", "content": prompt}], purpose="synth", stage="sintetizar", rnd=0,
                   problem=s["problem"], max_tokens=2000)
    if not r.get("text"):
        raise RuntimeError("El moderador no pudo escribir la síntesis. " + r.get("error", ""))
    db.run("UPDATE discussions SET synthesis=?, consensus=? WHERE id=?", (r["text"], s.get("consensus"), s["did"]))
    run.emit("step_finished", step="sintetizar", round=0)
    return {"synthesis": r["text"]}


async def n_destilar(s: State) -> dict:
    run = RUNS[s["did"]]
    _step(run, "destilar")
    prompt = DISTILL.format(problem=s["problem"], synthesis=s["synthesis"])
    r = await _ask(run, s["moderator"], [{"role": "user", "content": prompt}], purpose="distill", stage="destilar", rnd=0,
                   problem=s["problem"], visible=False, json_mode=True, max_tokens=2500)
    data = llm.parse_json(r["text"]) if r.get("text") else None
    raw = data.get("tarjetas") if isinstance(data, dict) else data if isinstance(data, list) else []
    cards = []
    for c in raw or []:
        if not isinstance(c, dict) or not c.get("pregunta") or not c.get("respuesta"):
            continue
        card = {"question": str(c["pregunta"]).strip(), "answer": str(c["respuesta"]).strip(),
                "kind": str(c.get("tipo") or "hecho").split("|")[0].strip().lower(),
                "tags": [str(x) for x in (c.get("etiquetas") or [])][:6],
                "variants": [str(x) for x in (c.get("variantes") or [])][:4]}
        card["id"] = db.run("INSERT INTO proposals(discussion_id,question,answer,kind,tags,variants) VALUES(?,?,?,?,?,?)",
                            (s["did"], card["question"], card["answer"], card["kind"],
                             json.dumps(card["tags"], ensure_ascii=False), json.dumps(card["variants"], ensure_ascii=False)))
        cards.append(card)
    run.emit("step_finished", step="destilar", round=0, cards=cards,
             warning="" if cards else "No se pudieron extraer tarjetas. " + r.get("error", ""))
    return {"cards": cards}


async def n_guardar(s: State) -> dict:
    run = RUNS[s["did"]]
    _step(run, "guardar")
    title = "Síntesis: " + s["problem"].strip().replace("\n", " ")[:90]
    doc = await asyncio.to_thread(store.add_doc, title, f"Problema: {s['problem']}\n\n{s['synthesis']}", "síntesis")
    saved = 0
    if s.get("module_id") and s.get("cards"):
        saved = len(await asyncio.to_thread(accept_cards, s["did"], s["module_id"], None))
    run.emit("step_finished", step="guardar", round=0, doc=doc, saved=saved, module_id=s.get("module_id"))
    return {}


def build():
    g = StateGraph(State)
    g.add_node("recuperar", n_recuperar)
    g.add_node("proponer", _turn("proponer"))
    g.add_node("criticar", _turn("criticar"))
    g.add_node("evaluar", n_evaluar)
    g.add_node("sintetizar", n_sintetizar)
    g.add_node("destilar", n_destilar)
    g.add_node("guardar", n_guardar)
    g.add_edge(START, "recuperar")
    g.add_conditional_edges("recuperar", _fan("proponer"), ["proponer"])
    g.add_edge("proponer", "evaluar")
    g.add_edge("criticar", "evaluar")
    g.add_conditional_edges("evaluar", _after_eval, ["criticar", "sintetizar"])
    g.add_edge("sintetizar", "destilar")
    g.add_edge("destilar", "guardar")
    g.add_edge("guardar", END)
    return g.compile()


GRAPH = build()


# ───────────────────────── Ejecución ─────────────────────────

def accept_cards(did: int, module_id: int, ids: list[int] | None) -> list[int]:
    """Pasa tarjetas propuestas por una discusión a un módulo."""
    rows = db.q("SELECT * FROM proposals WHERE discussion_id=? AND card_id IS NULL", (did,))
    out = []
    for r in rows:
        if ids is not None and r["id"] not in ids:
            continue
        res = store.add_card(module_id, r["question"], r["answer"], r["kind"], db.loads(r["tags"], []),
                             db.loads(r["variants"], []), source=f"discusión #{did}")
        db.run("UPDATE proposals SET card_id=? WHERE id=?", (res["id"], r["id"]))
        out.append(res["id"])
    return out


async def _assign(agents: list[dict], moderator: dict) -> tuple[list[str], bool]:
    """Reparte agentes entre los proveedores disponibles para que discutan modelos distintos."""
    real = await llm.available()
    pool = real or ["demo"]
    load: dict[str, int] = {p: 0 for p in pool}

    def take(a: dict, pid: str) -> None:
        a["pid"], a["slot"] = pid, load.get(pid, 0)  # el slot elige un modelo distinto dentro del proveedor
        load[pid] = a["slot"] + 1

    for a in agents:  # primero los que tienen proveedor fijo…
        if a["provider"] != "auto":
            take(a, a["provider"])
    for a in agents:  # …después los automáticos, siempre al proveedor menos usado
        if a["provider"] == "auto":
            take(a, min(pool, key=lambda p: load[p]))
    moderator["pid"] = moderator["provider"] if moderator["provider"] != "auto" else pool[0]
    moderator["slot"] = 0
    return pool, not real


async def _execute(run: Run, state: State) -> None:
    did, t0 = run.did, time.time()
    try:
        run.pool, demo = await _assign(state["agents"], state["moderator"])
        pub = lambda a: {k: a[k] for k in ("id", "name", "role", "color", "pid")}  # noqa: E731
        run.emit("run_started", id=did, problem=state["problem"], max_rounds=state["max_rounds"], demo=demo,
                 agents=[pub(a) for a in state["agents"]], moderator=pub(state["moderator"]))
        final = await GRAPH.ainvoke(state, {"recursion_limit": 80})
        db.run("UPDATE discussions SET status='terminada', finished_at=?, consensus=? WHERE id=?",
               (time.time(), final.get("consensus"), did))
        run.emit("run_finished", consensus=round(final.get("consensus", 0.0), 3), seconds=round(time.time() - t0, 1))
    except asyncio.CancelledError:
        db.run("UPDATE discussions SET status='cancelada', finished_at=? WHERE id=?", (time.time(), did))
        run.emit("run_error", error="Discusión detenida.", cancelled=True)
    except Exception as e:  # noqa: BLE001
        msg = str(e) or e.__class__.__name__
        db.run("UPDATE discussions SET status='error', error=?, finished_at=? WHERE id=?", (msg, time.time(), did))
        run.emit("run_error", error=msg)
    finally:
        run.finish()
        RUNS.pop(did, None)


def start(problem: str, agent_ids: list[int] | None = None, max_rounds: int = 2, module_id: int | None = None) -> Run:
    problem = problem.strip()
    if not problem:
        raise ValueError("Escribí el problema a discutir")
    agents = db.q("SELECT * FROM agents WHERE is_moderator=0 AND enabled=1 ORDER BY id")
    if agent_ids:
        agents = db.q(f"SELECT * FROM agents WHERE is_moderator=0 AND id IN ({','.join('?' * len(agent_ids))}) ORDER BY id", agent_ids)
    if not agents:
        raise ValueError("No hay agentes activos para discutir")
    moderator = db.one("SELECT * FROM agents WHERE is_moderator=1 ORDER BY id LIMIT 1") or {
        "id": 0, "name": "Moderador", "role": "Sintetizás con neutralidad.", "provider": "auto", "model": "auto",
        "color": "#5eb8a6", "temperature": 0.3}
    max_rounds = max(1, min(int(max_rounds), 4))
    config = {"agents": [a["id"] for a in agents], "max_rounds": max_rounds, "module_id": module_id}
    did = db.run("INSERT INTO discussions(problem,config,created_at) VALUES(?,?,?)",
                 (problem, json.dumps(config), time.time()))
    run = Run(did)
    RUNS[did] = run
    state: State = {"did": did, "problem": problem, "context": db.meta_get("context"), "memory": "",
                    "agents": agents, "moderator": moderator, "module_id": module_id,
                    "round": 1, "max_rounds": max_rounds, "turns": [], "evals": []}
    run.task = asyncio.create_task(_execute(run, state))
    return run


async def run_to_end(problem: str, max_rounds: int = 2, module_id: int | None = None) -> dict:
    """Para integraciones (MCP): corre una discusión completa y devuelve el resultado."""
    run = start(problem, None, max_rounds, module_id)
    await run.task
    d = db.one("SELECT id, status, synthesis, consensus, error FROM discussions WHERE id=?", (run.did,))
    d["cards"] = db.q("SELECT question, answer, kind FROM proposals WHERE discussion_id=?", (run.did,))
    return d
