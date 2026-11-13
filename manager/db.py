"""SQLite: un solo archivo con todo el cerebro (texto, vectores e índice BM25)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from .config import DB_PATH

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None, timeout=10)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA foreign_keys=ON")
_lock = threading.RLock()

FTS = 'tokenize="unicode61 remove_diacritics 2"'

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'auto', model TEXT NOT NULL DEFAULT 'auto',
  color TEXT NOT NULL DEFAULT '#5eb8a6', temperature REAL NOT NULL DEFAULT 0.7,
  enabled INTEGER NOT NULL DEFAULT 1, is_moderator INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS docs(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'nota',
  chars INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
  text TEXT NOT NULL, vec BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, {FTS});

CREATE TABLE IF NOT EXISTS modules(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cards(
  id INTEGER PRIMARY KEY, module_id INTEGER NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
  question TEXT NOT NULL, answer TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'hecho',
  tags TEXT NOT NULL DEFAULT '[]', variants TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, vec BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(question, answer, extra, {FTS});

CREATE TABLE IF NOT EXISTS discussions(
  id INTEGER PRIMARY KEY, problem TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'en curso',
  config TEXT NOT NULL DEFAULT '{{}}', synthesis TEXT NOT NULL DEFAULT '', consensus REAL,
  error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, finished_at REAL);
CREATE TABLE IF NOT EXISTS events(
  discussion_id INTEGER NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY(discussion_id, seq));
CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY, discussion_id INTEGER NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
  question TEXT NOT NULL, answer TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'hecho',
  tags TEXT NOT NULL DEFAULT '[]', variants TEXT NOT NULL DEFAULT '[]',
  card_id INTEGER);
"""

DEFAULT_AGENTS = [
    ("Analista", "Descomponés el problema y razonás paso a paso con rigor. Buscás la respuesta más correcta "
     "y sostenés cada afirmación con un argumento o un dato del contexto.", "#4f9cf5", 0.5, 1, 0),
    ("Crítico", "Sos el abogado del diablo. Buscás fallas, supuestos ocultos, casos borde y riesgos en cada "
     "propuesta, incluida la tuya. No aceptás una afirmación sin justificación.", "#f0705a", 0.6, 1, 0),
    ("Pragmático", "Priorizás lo simple y aplicable. Evaluás costo, esfuerzo y pasos concretos, y descartás "
     "lo que no se puede ejecutar en la práctica.", "#e2a93b", 0.7, 1, 0),
    ("Creativo", "Proponés alternativas no obvias y reformulás el problema cuando ayuda. Conectás ideas de "
     "otros dominios, pero marcás qué es especulación.", "#a98bf0", 0.9, 0, 0),
    ("Moderador", "Sintetizás la discusión con neutralidad. Separás lo acordado de lo que sigue en disputa "
     "y no agregás opiniones propias.", "#5eb8a6", 0.3, 1, 1),
]


def q(sql: str, params: tuple | list = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple | list = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def run(sql: str, params: tuple | list = ()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        return cur.lastrowid or 0


def meta_get(key: str, default: str = "") -> str:
    r = one("SELECT value FROM meta WHERE key=?", (key,))
    return r["value"] if r else default


def meta_set(key: str, value: str) -> None:
    run("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def init() -> None:
    with _lock:
        _conn.executescript(SCHEMA)
    if not one("SELECT id FROM agents LIMIT 1"):
        for name, role, color, temp, enabled, mod in DEFAULT_AGENTS:
            run("INSERT INTO agents(name,role,color,temperature,enabled,is_moderator) VALUES(?,?,?,?,?,?)",
                (name, role, color, temp, enabled, mod))
    # Discusiones que quedaron a medias si el servidor se cerró.
    run("UPDATE discussions SET status='interrumpida', finished_at=? WHERE status='en curso'", (time.time(),))


def loads(s: str, default):
    try:
        return json.loads(s)
    except Exception:
        return default


init()
