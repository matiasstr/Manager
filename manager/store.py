"""Memoria y módulos de conocimiento.

Búsqueda híbrida: vectores locales (coseno) + BM25 (SQLite FTS5), fusionados con Reciprocal Rank Fusion.
Nada de este archivo llama a una LLM: consultar un módulo es recuperación pura.
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import threading
import time

import numpy as np

from . import db
from . import embeddings as emb

STOP = set(
    "de la el en y a los las del un una unos unas que es por con para se su al lo como mas o pero sus le ya si no "
    "me mi te tu este esta esto ese esa eso hay muy tambien fue ser son cual cuales cuando donde quien quienes "
    "the of and to in is are for on with what how why which do does can".split()
)

_version = {"chunks": 0, "cards": 0}
_cache: dict[str, tuple[int, np.ndarray, np.ndarray, np.ndarray]] = {}
_ready = False
_ready_lock = threading.Lock()


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", emb.fold(text)) if len(t) > 1 and t not in STOP]


def _fts_query(text: str) -> str | None:
    toks = list(dict.fromkeys(tokens(text)))[:24]
    if not toks:
        return None
    return " OR ".join(f'"{t}"*' if len(t) >= 5 else f'"{t}"' for t in toks)


# ───────────────────────── Índice ─────────────────────────

def _card_text(question: str, answer: str, variants: list[str]) -> str:
    return "\n".join([question, *variants, answer[:600]])


# Cada tarjeta y cada fragmento guardan VARIOS vectores y se puntúan por el más cercano.
# Un solo vector para "pregunta + variantes + respuesta" diluye el significado y falla con paráfrasis.

def _card_vecs(question: str, answer: str, variants: list[str]) -> np.ndarray:
    """Una fila por forma de preguntar, más una para la respuesta."""
    return emb.embed([question, *variants, answer[:600]])


def _chunk_vecs(title: str, text: str) -> np.ndarray:
    """El fragmento entero y cada oración por separado: se busca en chico y se devuelve el fragmento."""
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 20][:16]
    return emb.embed([f"{title}\n{text}", *sents])


def ensure_index() -> None:
    """Si cambió el modelo de embeddings (o faltan vectores), recalcula todo."""
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        name = emb.get().name
        stale = db.meta_get("embedder") != name
        where = "" if stale else " WHERE vec IS NULL"
        for r in db.q("SELECT c.id, c.text, d.title FROM chunks c JOIN docs d ON d.id=c.doc_id" + where.replace("vec", "c.vec")):
            db.run("UPDATE chunks SET vec=? WHERE id=?", (emb.to_blob(_chunk_vecs(r["title"], r["text"])), r["id"]))
        for r in db.q("SELECT id, question, answer, variants FROM cards" + where):
            vecs = _card_vecs(r["question"], r["answer"], db.loads(r["variants"], []))
            db.run("UPDATE cards SET vec=? WHERE id=?", (emb.to_blob(vecs), r["id"]))
        db.meta_set("embedder", name)
        _version["chunks"] += 1
        _version["cards"] += 1
        _ready = True


def _vectors(table: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(id por fila, matriz de vectores, grupo por fila). Un mismo id ocupa varias filas."""
    ver = _version[table]
    hit = _cache.get(table)
    if hit and hit[0] == ver:
        return hit[1], hit[2], hit[3]
    group = "doc_id" if table == "chunks" else "module_id"
    dim = emb.get().dim
    ids: list[int] = []
    groups: list[int] = []
    mats: list[np.ndarray] = []
    for r in db.q(f"SELECT id, {group} AS g, vec FROM {table} WHERE vec IS NOT NULL"):
        if not r["vec"] or len(r["vec"]) % (dim * 4):
            continue
        m = emb.from_blob(r["vec"]).reshape(-1, dim)
        mats.append(m)
        ids += [r["id"]] * len(m)
        groups += [r["g"]] * len(m)
    mat = np.vstack(mats) if mats else np.zeros((0, dim), dtype=np.float32)
    out = (np.array(ids, dtype=np.int64), mat, np.array(groups, dtype=np.int64))
    _cache[table] = (ver, *out)
    return out


def hybrid(table: str, query: str, k: int = 5, groups: list[int] | None = None) -> list[dict]:
    """Devuelve [{id, rrf, cos, lex}] ordenado por relevancia fusionada."""
    ensure_index()
    ids, mat, grp = _vectors(table)
    if len(ids) == 0 or not query.strip():
        return []
    qv = emb.embed([query], "query")[0]
    rows = mat @ qv
    if groups:
        sel = np.isin(grp, groups)
        ids, rows = ids[sel], rows[sel]
        if len(ids) == 0:
            return []
    uids, inv = np.unique(ids, return_inverse=True)
    best = np.full(len(uids), -1.0, dtype=np.float32)
    np.maximum.at(best, inv, rows)  # mejor fila de cada tarjeta o fragmento
    order = np.argsort(-best)[: k * 4]
    rrf: dict[int, float] = {int(uids[i]): 1.0 / (61 + rank) for rank, i in enumerate(order)}
    cosmap = {int(u): float(b) for u, b in zip(uids, best)}

    lex: set[int] = set()
    fq = _fts_query(query)
    if fq:
        weights = ", 3.0, 1.0, 2.0" if table == "cards" else ""
        try:
            rows = db.q(f"SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ? "
                        f"ORDER BY bm25({table}_fts{weights}) LIMIT ?", (fq, k * 8))
        except sqlite3.OperationalError:
            rows = []
        rank = 0
        for r in rows:
            rid = r["rowid"]
            if rid in cosmap:
                rank += 1
                rrf[rid] = rrf.get(rid, 0.0) + 1.0 / (60 + rank)
                lex.add(rid)
    top = sorted(rrf, key=lambda i: rrf[i], reverse=True)[:k]
    return [{"id": i, "rrf": rrf[i], "cos": cosmap[i], "lex": i in lex} for i in top]


def confidence(query: str, cos: float, text: str) -> float:
    """0..1. Combina cercanía semántica calibrada por modelo y cobertura de palabras de la consulta."""
    lo, hi = emb.get().calib
    c_vec = float(np.clip((cos - lo) / (hi - lo), 0, 1))
    qt = {t[:6] for t in tokens(query)}
    tt = {t[:6] for t in tokens(text)}
    cov = len(qt & tt) / len(qt) if qt else 0.0
    return round(min(1.0, 0.75 * c_vec + 0.25 * cov), 3)


# ───────────────────────── Memoria (documentos) ─────────────────────────

def chunk_text(text: str, size: int = 900) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if p.strip()]
    pieces: list[str] = []
    for p in paras:
        if len(p) <= size:
            pieces.append(p)
            continue
        cur = ""
        for s in re.split(r"(?<=[.!?])\s+|\n", p):
            if len(cur) + len(s) + 1 > size and cur:
                pieces.append(cur.strip())
                cur = ""
            while len(s) > size:
                pieces.append(s[:size])
                s = s[size:]
            cur += s + " "
        if cur.strip():
            pieces.append(cur.strip())
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def add_doc(title: str, text: str, kind: str = "nota") -> dict:
    ensure_index()
    title = title.strip() or text.strip()[:60]
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("El texto está vacío")
    vecs = [_chunk_vecs(title, c) for c in chunks]
    doc_id = db.run("INSERT INTO docs(title,kind,chars,created_at) VALUES(?,?,?,?)", (title, kind, len(text), time.time()))
    for c, v in zip(chunks, vecs):
        cid = db.run("INSERT INTO chunks(doc_id,text,vec) VALUES(?,?,?)", (doc_id, c, emb.to_blob(v)))
        db.run("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (cid, f"{title}\n{c}"))
    _version["chunks"] += 1
    return {"id": doc_id, "title": title, "chunks": len(chunks)}


def delete_doc(doc_id: int) -> None:
    for r in db.q("SELECT id FROM chunks WHERE doc_id=?", (doc_id,)):
        db.run("DELETE FROM chunks_fts WHERE rowid=?", (r["id"],))
    db.run("DELETE FROM docs WHERE id=?", (doc_id,))
    _version["chunks"] += 1


def list_docs() -> list[dict]:
    return db.q("SELECT d.id, d.title, d.kind, d.chars, d.created_at, COUNT(c.id) AS chunks "
                "FROM docs d LEFT JOIN chunks c ON c.doc_id=d.id GROUP BY d.id ORDER BY d.created_at DESC")


def search_memory(query: str, k: int = 5) -> list[dict]:
    hits = hybrid("chunks", query, k)
    out = []
    for h in hits:
        r = db.one("SELECT c.id, c.text, c.doc_id, d.title, d.kind FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE c.id=?", (h["id"],))
        if r:
            out.append({**r, "cos": round(h["cos"], 3), "confidence": confidence(query, h["cos"], f"{r['title']}\n{r['text']}")})
    return sorted(out, key=lambda r: r["confidence"], reverse=True)


# ───────────────────────── Módulos y tarjetas ─────────────────────────

def _card(r: dict) -> dict:
    r = dict(r)
    r.pop("vec", None)
    r["tags"] = db.loads(r.get("tags", "[]"), [])
    r["variants"] = db.loads(r.get("variants", "[]"), [])
    return r


def list_modules() -> list[dict]:
    return db.q("SELECT m.id, m.name, m.description, m.created_at, COUNT(c.id) AS cards "
                "FROM modules m LEFT JOIN cards c ON c.module_id=m.id GROUP BY m.id ORDER BY m.name COLLATE NOCASE")


def create_module(name: str, description: str = "") -> dict:
    name = name.strip()
    if not name:
        raise ValueError("El módulo necesita un nombre")
    base, n = name, 2
    while db.one("SELECT id FROM modules WHERE name=? COLLATE NOCASE", (name,)):
        name = f"{base} ({n})"
        n += 1
    mid = db.run("INSERT INTO modules(name,description,created_at) VALUES(?,?,?)", (name, description.strip(), time.time()))
    return {"id": mid, "name": name}


def get_or_create_module(name: str) -> int:
    r = db.one("SELECT id FROM modules WHERE name=? COLLATE NOCASE", (name.strip(),))
    return r["id"] if r else create_module(name)["id"]


def delete_module(module_id: int) -> None:
    for r in db.q("SELECT id FROM cards WHERE module_id=?", (module_id,)):
        db.run("DELETE FROM cards_fts WHERE rowid=?", (r["id"],))
    db.run("DELETE FROM modules WHERE id=?", (module_id,))
    _version["cards"] += 1


def list_cards(module_id: int) -> list[dict]:
    return [_card(r) for r in db.q("SELECT * FROM cards WHERE module_id=? ORDER BY id DESC", (module_id,))]


def _index_card(cid: int, question: str, answer: str, tags: list[str], variants: list[str]) -> None:
    db.run("DELETE FROM cards_fts WHERE rowid=?", (cid,))
    db.run("INSERT INTO cards_fts(rowid,question,answer,extra) VALUES(?,?,?,?)",
           (cid, question, answer, " ".join(variants + tags)))


def add_card(module_id: int, question: str, answer: str, kind: str = "hecho", tags: list[str] | None = None,
             variants: list[str] | None = None, source: str = "", vec: np.ndarray | None = None) -> dict:
    ensure_index()
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        raise ValueError("La tarjeta necesita pregunta y respuesta")
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    variants = [str(v).strip() for v in (variants or []) if str(v).strip()]
    if vec is None:
        vec = _card_vecs(question, answer, variants)

    # Duplicados: misma pregunta, o casi idéntica semánticamente → se actualiza en vez de sumar otra.
    dup = None
    for r in db.q("SELECT id, question FROM cards WHERE module_id=?", (module_id,)):
        if emb.fold(r["question"]).strip(" ¿?.") == emb.fold(question).strip(" ¿?."):
            dup = r["id"]
            break
    if dup is None and emb.get().name.startswith("fastembed"):
        ids, mat, grp = _vectors("cards")
        idx = np.where(grp == module_id)[0]
        if len(idx):
            cos = mat[idx] @ vec[0]  # la pregunta nueva contra todo lo que ya hay en el módulo
            best = int(np.argmax(cos))
            if cos[best] >= 0.95:
                dup = int(ids[idx][best])
    args = (question, answer, kind, json.dumps(tags, ensure_ascii=False), json.dumps(variants, ensure_ascii=False), source, emb.to_blob(vec))
    if dup:
        db.run("UPDATE cards SET question=?, answer=?, kind=?, tags=?, variants=?, source=?, vec=? WHERE id=?", (*args, dup))
        cid = dup
    else:
        cid = db.run("INSERT INTO cards(question,answer,kind,tags,variants,source,vec,module_id,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (*args, module_id, time.time()))
    _index_card(cid, question, answer, tags, variants)
    _version["cards"] += 1
    return {"id": cid, "updated": bool(dup)}


def update_card(card_id: int, question: str, answer: str, kind: str, tags: list[str], variants: list[str]) -> None:
    ensure_index()
    vec = _card_vecs(question, answer, variants)
    db.run("UPDATE cards SET question=?, answer=?, kind=?, tags=?, variants=?, vec=? WHERE id=?",
           (question.strip(), answer.strip(), kind, json.dumps(tags, ensure_ascii=False),
            json.dumps(variants, ensure_ascii=False), emb.to_blob(vec), card_id))
    _index_card(card_id, question, answer, tags, variants)
    _version["cards"] += 1


def delete_card(card_id: int) -> None:
    db.run("DELETE FROM cards_fts WHERE rowid=?", (card_id,))
    db.run("DELETE FROM cards WHERE id=?", (card_id,))
    _version["cards"] += 1


def search_cards(query: str, k: int = 5, module_ids: list[int] | None = None) -> list[dict]:
    out = []
    for h in hybrid("cards", query, k, module_ids):
        r = db.one("SELECT c.*, m.name AS module FROM cards c JOIN modules m ON m.id=c.module_id WHERE c.id=?", (h["id"],))
        if r:
            c = _card(r)
            text = _card_text(c["question"], c["answer"], c["variants"] + c["tags"])
            out.append({**c, "cos": round(h["cos"], 3), "lex": h["lex"], "confidence": confidence(query, h["cos"], text)})
    return sorted(out, key=lambda c: c["confidence"], reverse=True)


HIGH, MID = 0.62, 0.42  # confianza alta: respuesta directa; media: coincidencia parcial, conviene verificar


def ask(query: str, module_ids: list[int] | None = None, k: int = 5) -> dict:
    """Responde desde los módulos, sin LLM. Si no hay nada suficientemente cercano, lo dice."""
    t0 = time.perf_counter()
    cards = search_cards(query, k, module_ids)
    best = cards[0] if cards else None
    conf = best["confidence"] if best else 0.0
    level = "alta" if conf >= HIGH else "media" if conf >= MID else "ninguna"
    answered = level != "ninguna"
    return {
        "answered": answered,
        "level": level,
        "confidence": conf,
        "answer": best["answer"] if answered else None,
        "card": best if answered else None,
        "related": cards[1:] if answered else cards,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
        "embedder": emb.get().name,
    }


def search_all(query: str, k: int = 6) -> list[dict]:
    """Contexto para una discusión: lo más relevante de la memoria y de los módulos."""
    items = []
    for r in search_memory(query, k):
        items.append({"kind": "memoria", "title": r["title"], "text": r["text"], "confidence": r["confidence"]})
    for c in search_cards(query, k):
        items.append({"kind": "módulo", "title": f"{c['module']}: {c['question']}", "text": c["answer"], "confidence": c["confidence"]})
    items = [i for i in items if i["confidence"] >= 0.35]
    return sorted(items, key=lambda i: i["confidence"], reverse=True)[:k]


def stats() -> dict:
    return {
        "docs": db.one("SELECT COUNT(*) n FROM docs")["n"],
        "chunks": db.one("SELECT COUNT(*) n FROM chunks")["n"],
        "modules": db.one("SELECT COUNT(*) n FROM modules")["n"],
        "cards": db.one("SELECT COUNT(*) n FROM cards")["n"],
        "discussions": db.one("SELECT COUNT(*) n FROM discussions")["n"],
    }


# ───────────────────────── Exportar / importar ─────────────────────────

FORMAT = "manager-modulo/1"


def export_module(module_id: int) -> dict:
    m = db.one("SELECT * FROM modules WHERE id=?", (module_id,))
    if not m:
        raise ValueError("Módulo inexistente")
    cards = []
    for r in db.q("SELECT * FROM cards WHERE module_id=? ORDER BY id", (module_id,)):
        c = _card(r)
        cards.append({"question": c["question"], "answer": c["answer"], "kind": c["kind"], "tags": c["tags"],
                      "variants": c["variants"], "source": c["source"],
                      "vec": base64.b64encode(r["vec"]).decode() if r["vec"] else None})
    return {"format": FORMAT, "name": m["name"], "description": m["description"],
            "embedder": emb.get().name, "exported_at": time.time(), "cards": cards}


def import_module(data: dict) -> dict:
    if data.get("format") != FORMAT or not isinstance(data.get("cards"), list):
        raise ValueError("El archivo no es un módulo de Manager")
    ensure_index()
    mod = create_module(str(data.get("name") or "Módulo importado"), str(data.get("description") or ""))
    same = data.get("embedder") == emb.get().name
    dim = emb.get().dim
    n = 0
    for c in data["cards"]:
        vec = None
        if same and c.get("vec"):
            raw = base64.b64decode(c["vec"])
            if raw and len(raw) % (dim * 4) == 0:
                vec = emb.from_blob(raw).reshape(-1, dim)
        try:
            add_card(mod["id"], str(c.get("question", "")), str(c.get("answer", "")), str(c.get("kind") or "hecho"),
                     c.get("tags") or [], c.get("variants") or [], str(c.get("source") or ""), vec)
            n += 1
        except ValueError:
            continue
    return {**mod, "cards": n}


def export_module_html(module_id: int) -> tuple[str, str]:
    """Una sola página .html con el módulo adentro y un buscador BM25: se abre con doble clic, sin servidor."""
    from .config import ROOT

    data = export_module(module_id)
    for c in data["cards"]:
        c.pop("vec", None)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    tpl = (ROOT / "manager" / "plantilla_modulo.html").read_text("utf-8")
    title = data["name"].replace("<", "‹").replace(">", "›")
    return data["name"], tpl.replace("__TITULO__", title).replace("/*__DATOS__*/null", payload)
