"""Importa roles de ECC (github.com/affaan-m/ECC, licencia MIT) como agentes de debate de Manager.

Los agentes de ECC son archivos Markdown pensados para un asistente de código con herramientas.
De cada uno se toma solo lo que sirve en un debate: quién es y en qué se fija. Se descartan las
instrucciones de herramientas, los comandos y los bloques de código.

    git clone --depth 1 https://github.com/affaan-m/ECC.git
    python herramientas/importar_ecc.py ECC --listar
    python herramientas/importar_ecc.py ECC security-reviewer architect planner
    python herramientas/importar_ecc.py ECC security-reviewer --ver        (muestra el rol sin cargarlo)

Manager tiene que estar corriendo (por defecto en http://127.0.0.1:8765). Los agentes entran
apagados: se prenden por discusión desde la interfaz, o en Agentes → «participa por defecto».
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAX_ROLE = 1200  # límite de la API de Manager
MAX_NAME = 40
COLORS = ["#c2410c", "#0e7490", "#7c3aed", "#b45309", "#be185d", "#15803d", "#1d4ed8", "#a16207"]
SUFFIX = ("\n\nEn este debate no tenés herramientas ni código para ejecutar: aportá desde esa "
          "especialidad razonando sobre el problema planteado. Respondé en español.")
# Secciones que describen el foco del rol; el resto (comandos, flujos, formatos de salida) no se usa.
FOCUS = re.compile(r"responsibilit|your role|focus|principles|what (you|to) (look|check)|expertise|mission", re.I)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


def parse(path: Path) -> dict:
    text = path.read_text("utf-8", errors="replace")
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep and re.fullmatch(r"[\w-]+", k.strip()):
                meta[k.strip()] = v.strip().strip("\"'")
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    sections, title, buf = [], "", []
    for line in body.splitlines():
        h = re.match(r"^#{1,4}\s+(.*)", line)
        if h:
            sections.append((title, buf))
            title, buf = h.group(1).strip(), []
        else:
            buf.append(line)
    sections.append((title, buf))
    sections = [(t, b) for t, b in sections if "prompt defense" not in t.lower()]

    intro = ""
    for _, lines in sections:
        for para in re.split(r"\n\s*\n", "\n".join(lines)):
            para = " ".join(para.split())
            if re.match(r"you are\b", para, re.I):
                intro = para
                break
        if intro:
            break
    if not intro:  # sin presentación: se usa la descripción, sin la frase de cuándo invocarlo
        intro = re.split(r"\s+(?:Use|MUST BE USED|Invoke)\b", meta.get("description", ""))[0].strip()

    points = []
    for t, lines in sections:
        if not FOCUS.search(t):
            continue
        for line in lines:
            b = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)", line)
            if b:
                points.append(re.sub(r"[*_`]", "", b.group(1)).strip())
        if points:
            break

    slug = meta.get("name") or path.stem
    return {"slug": slug, "name": " ".join(w.capitalize() for w in slug.split("-"))[:MAX_NAME],
            "description": meta.get("description", ""), "intro": intro, "points": points}


def role(a: dict) -> str:
    out = a["intro"]
    room = MAX_ROLE - len(SUFFIX)
    if a["points"]:
        out += "\nFoco:"
        for p in a["points"]:
            if len(out) + len(p) + 3 > room:
                break
            out += f"\n- {p}"
    return out[:room].rstrip() + SUFFIX


def call(url: str, method: str = "GET", data: dict | None = None):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Importa roles de ECC como agentes de debate de Manager.")
    ap.add_argument("ecc", help="carpeta del clon de ECC (o directamente su carpeta agents/)")
    ap.add_argument("nombres", nargs="*", help="agentes a importar, por su nombre de archivo sin .md")
    ap.add_argument("--listar", action="store_true", help="muestra los agentes disponibles y sale")
    ap.add_argument("--ver", action="store_true", help="muestra el rol que se cargaría, sin cargarlo")
    ap.add_argument("--url", default="http://127.0.0.1:8765", help="dónde corre Manager")
    args = ap.parse_args()

    root = Path(args.ecc)
    folder = root / "agents" if (root / "agents").is_dir() else root
    files = {p.stem: p for p in sorted(folder.glob("*.md"))}
    if not files:
        print(f"No hay agentes de ECC en {folder}")
        return 1
    if args.listar or not args.nombres:
        for stem, p in files.items():
            print(f"{stem:28} {parse(p)['description'][:110]}")
        print(f"\n{len(files)} agentes. Pasá los nombres que quieras importar.")
        return 0

    missing = [n for n in args.nombres if n not in files]
    if missing:
        print("No existen en ECC:", ", ".join(missing))
        return 1
    chosen = [parse(files[n]) for n in args.nombres]
    if args.ver:
        for a in chosen:
            print(f"=== {a['name']} ({len(role(a))} caracteres)\n{role(a)}\n")
        return 0

    try:
        existing = {x["name"].lower() for x in call(args.url + "/api/state")["agents"]}
    except (urllib.error.URLError, OSError) as e:
        print(f"No pude hablar con Manager en {args.url}: {e}\nArrancalo con .\\iniciar.bat y probá de nuevo.")
        return 1
    added = 0
    for a in chosen:
        if a["name"].lower() in existing:
            print(f"ya estaba   {a['name']}")
            continue
        call(args.url + "/api/agents", "POST", {"name": a["name"], "role": role(a), "enabled": False,
                                                "color": COLORS[(len(existing) + added) % len(COLORS)]})
        added += 1
        print(f"importado   {a['name']}")
    print(f"\n{added} agentes nuevos. Entran apagados: prendelos al armar una discusión.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
