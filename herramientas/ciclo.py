"""Ciclo de caza humano-en-el-medio para Manager.

La máquina planifica, VOS probás con tus herramientas, cargás los resultados, y la máquina
analiza eso y planifica el próximo paso. Se repite. Manager NO ejecuta nada contra el target:
el nodo de ejecución sos vos, con tu autorización y dentro del alcance del programa.

  python herramientas/ciclo.py plan                 # genera el próximo paso a paso (lee contexto + resultados)
  python herramientas/ciclo.py resultado "..."      # registra lo que probaste y qué pasó
  python herramientas/ciclo.py estado               # muestra los resultados cargados y el último plan
  python herramientas/ciclo.py reset                # vacía el log del ciclo (no toca la memoria de Manager)

El contexto del programa (alcance, reglas) se toma del contexto general de Manager
(Agentes -> Contexto). Manager tiene que estar corriendo (por defecto http://127.0.0.1:8765).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from manager import config  # noqa: E402  (para la ubicación de los datos locales)

LOG = config.DATA_DIR / "ciclo.json"
# Grupo que planifica/analiza. Si alguno no existe, se usa lo que haya.
EQUIPO = ["Planner", "Security Reviewer", "Pragmático", "Crítico"]
RONDAS = 3

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


def call(url: str, method: str = "GET", data: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load() -> dict:
    if LOG.exists():
        try:
            return json.loads(LOG.read_text("utf-8"))
        except Exception:
            pass
    return {"resultados": [], "planes": []}


def save(state: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def ahora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def cmd_resultado(args, base: str) -> int:
    texto = args.texto.strip()
    if not texto:
        print("Escribí qué probaste y qué pasó, entre comillas.")
        return 1
    state = load()
    state["resultados"].append({"t": ahora(), "texto": texto})
    save(state)
    # También a la memoria de Manager, para que la recuperación por RAG lo encuentre.
    try:
        call(base + "/api/docs-list", "POST",
             {"title": f"Resultado de caza — {ahora()}", "text": texto})
    except (urllib.error.URLError, OSError) as e:
        print(f"(guardado local OK, pero no pude escribir en la memoria de Manager: {e})")
    print(f"Resultado #{len(state['resultados'])} registrado. Corré 'plan' para el próximo paso.")
    return 0


def cmd_estado(args, base: str) -> int:
    state = load()
    print(f"== Ciclo ({len(state['resultados'])} resultados, {len(state['planes'])} planes)\n")
    if state["resultados"]:
        print("Resultados cargados:")
        for i, r in enumerate(state["resultados"], 1):
            print(f"  {i}. [{r['t']}] {r['texto']}")
    else:
        print("Todavía no cargaste resultados.")
    if state["planes"]:
        ultimo = state["planes"][-1]
        print(f"\nÚltimo plan ({ultimo['t']}, consenso {ultimo.get('consenso')}):\n")
        print(ultimo["sintesis"])
    return 0


def cmd_reset(args, base: str) -> int:
    save({"resultados": [], "planes": []})
    print("Log del ciclo vaciado. La memoria de Manager no se toca.")
    return 0


def _problema(state: dict) -> str:
    if state["resultados"]:
        res = "\n".join(f"- [{r['t']}] {r['texto']}" for r in state["resultados"])
        return ("Ciclo de caza del programa cargado en el contexto. Resultados de las pruebas que ya hice "
                f"(las ejecuté yo, no vos):\n{res}\n\nAnalizá estos resultados: qué confirman, qué descartan, "
                "qué puede ser falso positivo y qué hallazgo se insinúa. Después dame el PRÓXIMO paso a paso "
                "concreto: qué probar ahora, en qué activo/endpoint, qué buscar exactamente y cómo validarlo "
                "dentro del alcance y las reglas. Si algo ya quedó confirmado, decí cómo documentarlo para el reporte.")
    return ("Ciclo de caza del programa cargado en el contexto. Todavía no hay resultados de pruebas. "
            "Dame el PRIMER paso a paso concreto para arrancar dentro del alcance y las reglas: preparación "
            "del entorno, mapeo de superficie y las primeras pruebas priorizadas por lo que el programa paga. "
            "Pasos en orden, con qué buscar en cada uno.")


def cmd_plan(args, base: str) -> int:
    state = load()
    try:
        agentes = call(base + "/api/state")["agents"]
    except (urllib.error.URLError, OSError) as e:
        print(f"No pude hablar con Manager en {base}: {e}\nArrancalo con .\\iniciar.bat y probá de nuevo.")
        return 1
    porNombre = {a["name"]: a["id"] for a in agentes}
    ids = [porNombre[n] for n in EQUIPO if n in porNombre]
    if len(ids) < 2:
        ids = [a["id"] for a in agentes if not a["is_moderator"] and a["enabled"]][:4]
    if len(ids) < 2:
        print("No hay suficientes agentes. Creá al menos dos en Agentes.")
        return 1

    vuelta = len(state["planes"]) + 1
    print(f"== Vuelta {vuelta}: planificando con {len(ids)} agentes sobre {len(state['resultados'])} resultados...")
    did = call(base + "/api/discussions", "POST",
               {"problem": _problema(state), "agent_ids": ids, "max_rounds": RONDAS})["id"]
    print(f"   discusión #{did} en curso (podés verla en vivo en {base})")

    t0 = time.time()
    d = {}
    while time.time() - t0 < 600:
        time.sleep(5)
        try:
            d = call(base + f"/api/discussions/{did}")
        except (urllib.error.URLError, OSError):
            continue
        if not d.get("live"):
            break
        print(f"   ... {int(time.time() - t0)}s", end="\r")
    print(" " * 40, end="\r")

    sintesis = (d.get("synthesis") or "").strip()
    if not sintesis:
        print(f"La discusión terminó sin síntesis (estado: {d.get('status')}). {d.get('error') or ''}")
        return 1
    state["planes"].append({"t": ahora(), "did": did, "consenso": d.get("consensus"), "sintesis": sintesis})
    save(state)
    print(f"\n{'='*70}\nPASO A PASO — vuelta {vuelta} (consenso {d.get('consensus')})\n{'='*70}\n")
    print(sintesis)
    print(f"\n{'-'*70}\nProbá estos pasos con tus herramientas y cargá lo que pase:")
    print('  python herramientas/ciclo.py resultado "qué probaste y qué devolvió"')
    print("Después corré 'plan' de nuevo para la próxima vuelta.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Ciclo de caza humano-en-el-medio para Manager.")
    ap.add_argument("comando", choices=["plan", "resultado", "estado", "reset"])
    ap.add_argument("texto", nargs="?", default="", help="para 'resultado': qué probaste y qué pasó")
    ap.add_argument("--url", default="http://127.0.0.1:8765", help="dónde corre Manager")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    return {"plan": cmd_plan, "resultado": cmd_resultado, "estado": cmd_estado, "reset": cmd_reset}[args.comando](args, base)


if __name__ == "__main__":
    sys.exit(main())
