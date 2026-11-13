"""Embeddings locales. Nada sale de la máquina.

- fastembed (ONNX, sin PyTorch) con un modelo multilingüe chico: es el modo normal.
- hash de n-gramas: respaldo sin descargas, para cuando no hay internet la primera vez.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import unicodedata
import zlib

import numpy as np

from . import config

log = logging.getLogger("manager.embeddings")

# Modelos probados, en orden de preferencia: (nombre, prefijo consulta, prefijo pasaje, cos bajo, cos alto)
# Medido con 16 preguntas parafraseadas y 10 ajenas sobre 12 tarjetas en español:
#   jina-v2-base-es      15/16 aciertos, ajenas ≤ 0.43, relacionadas ≥ 0.47   (640 MB)
#   MiniLM multilingüe   12/16 aciertos, ajenas ≤ 0.48, relacionadas ≥ 0.42   (220 MB)
CANDIDATES = [
    ("jinaai/jina-embeddings-v2-base-es", "", "", 0.25, 0.62),
    ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "", "", 0.30, 0.70),
]


def fold(text: str) -> str:
    """Minúsculas y sin acentos."""
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


class HashEmbedder:
    """Vector disperso de palabras y trigramas de caracteres. Similitud léxica aproximada."""

    name = "hash-ngram-512"
    dim = 512
    calib = (0.12, 0.50)

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            words = re.findall(r"\w+", fold(text))
            feats: dict[str, float] = {}
            for w in words:
                feats[w] = feats.get(w, 0.0) + 1.0
                padded = f" {w} "
                for j in range(len(padded) - 2):
                    g = "#" + padded[j:j + 3]
                    feats[g] = feats.get(g, 0.0) + 0.4
            for f, v in feats.items():
                h = zlib.crc32(f.encode("utf-8"))
                out[i, h % self.dim] += (1.0 if (h >> 16) & 1 else -1.0) * (1.0 + np.log(v))
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(norms, 1e-9)


class FastEmbedder:
    def __init__(self, model: str, qp: str, pp: str, calib: tuple[float, float]):
        from fastembed import TextEmbedding

        config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=model, cache_dir=str(config.MODELS_DIR))
        self.name = f"fastembed:{model}"
        self._qp, self._pp, self.calib = qp, pp, calib
        self.dim = int(self.embed(["hola"]).shape[1])

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        prefix = self._qp if kind == "query" else self._pp
        vecs = np.array(list(self._model.embed([prefix + t[:2000] for t in texts])), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.maximum(norms, 1e-9)


_embedder = None
_init_lock = threading.Lock()


def get():
    global _embedder
    if _embedder is not None:
        return _embedder
    with _init_lock:
        if _embedder is not None:
            return _embedder
        if config.EMBEDDINGS != "hash":
            cands = CANDIDATES
            if config.EMBED_MODEL:
                cands = [c for c in CANDIDATES if c[0] == config.EMBED_MODEL] or [(config.EMBED_MODEL, "", "", 0.35, 0.75)]
            for model, qp, pp, lo, hi in cands:
                try:
                    _embedder = FastEmbedder(model, qp, pp, (lo, hi))
                    log.info("Embeddings locales: %s", model)
                    return _embedder
                except Exception as e:  # sin internet, modelo no soportado, etc.
                    log.warning("No se pudo cargar %s: %s", model, e)
        _embedder = HashEmbedder()
        log.warning("Usando embeddings de respaldo (hash). La búsqueda semántica será más pobre.")
        return _embedder


def embed(texts: list[str], kind: str = "passage") -> np.ndarray:
    if not texts:
        return np.zeros((0, get().dim), dtype=np.float32)
    return get().embed(texts, kind)


async def aembed(texts: list[str], kind: str = "passage") -> np.ndarray:
    return await asyncio.to_thread(embed, texts, kind)


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)
