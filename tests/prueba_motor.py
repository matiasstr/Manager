"""Prueba de punta a punta del motor con el proveedor demo y una base temporal:
memoria, módulos, consulta sin LLM, exportación y una discusión completa.

    python tests/prueba_motor.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time

tmp = tempfile.mkdtemp(prefix="manager_test_")
os.environ["MANAGER_DATA"] = tmp
for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "NVIDIA_API_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("MANAGER_EMBEDDINGS", None)  # esta prueba sí mide la búsqueda: necesita el modelo real

from manager import db, debate, llm, store  # noqa: E402
from manager import embeddings as emb  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)


async def main():
    t = time.time()
    e = emb.get()
    print("embedder:", e.name, e.dim, f"{time.time() - t:.1f}s")
    check("embedder real (no hash)", e.name.startswith("fastembed"))

    # Memoria
    store.add_doc("Política de vacaciones", "Cada empleado tiene 15 días hábiles de vacaciones por año.\n\n"
                  "Las vacaciones se piden con 30 días de anticipación al responsable del área.\n\n"
                  "Los días no usados se pueden pasar al año siguiente hasta un máximo de 5.")
    store.add_doc("Servidor de producción", "El servidor de producción corre en Ubuntu 24.04 y se reinicia los domingos a las 3 AM.\n\n"
                  "Los backups de la base de datos se hacen cada noche a las 2 AM y se guardan 14 días.")
    r = store.search_memory("¿cuántos días libres me corresponden?", 3)
    print("  memoria:", [(x["title"], x["cos"], x["confidence"]) for x in r])
    check("memoria: vacaciones primero", r and r[0]["title"] == "Política de vacaciones")
    r = store.search_memory("copias de seguridad", 3)
    print("  memoria:", [(x["title"], x["cos"], x["confidence"]) for x in r])
    check("memoria: backups primero", r and r[0]["title"] == "Servidor de producción")

    # Módulos
    m = store.create_module("Operaciones", "Cómo funciona la infraestructura")
    store.add_card(m["id"], "¿Cada cuánto se hacen los backups?", "Todas las noches a las 2 AM. Se conservan 14 días.",
                   "hecho", ["backups"], ["¿Cuándo se respalda la base de datos?"])
    store.add_card(m["id"], "¿Cómo pido vacaciones?", "Se piden al responsable del área con 30 días de anticipación.",
                   "procedimiento", ["rrhh"], ["¿Qué hago para tomarme días libres?"])
    store.add_card(m["id"], "¿Qué sistema operativo usa producción?", "Ubuntu 24.04.", "hecho", ["servidor"], [])
    d = store.add_card(m["id"], "¿cada cuánto se hacen los backups", "Cada noche a las 2 AM, con 14 días de retención.")
    check("dedup por pregunta", d["updated"] and len(store.list_cards(m["id"])) == 3)

    for q, expect in [
        ("cuando se respaldan los datos", "backups"),
        ("quiero tomarme unos días, qué tengo que hacer", "vacaciones"),
        ("que linux corre el server", "sistema operativo"),
        ("frecuencia de las copias de seguridad", "backups"),
    ]:
        a = store.ask(q)
        got = a["card"]["question"] if a["card"] else None
        print(f"  ask «{q}» → {got} conf={a['confidence']} cos={(a['card'] or a['related'][0])['cos'] if (a['card'] or a['related']) else None} {a['ms']}ms")
        check(f"ask: {expect}", a["answered"] and expect in got.lower())
    for q in ["¿cuál es la capital de Mongolia?", "receta de pan casero", "precio del dólar hoy"]:
        a = store.ask(q)
        top = (a["card"] or (a["related"][0] if a["related"] else {}))
        print(f"  ask «{q}» → answered={a['answered']} conf={a['confidence']} cos={top.get('cos')}")
        check(f"ask no responde: {q}", not a["answered"])

    # Exportar / importar
    data = store.export_module(m["id"])
    imp = store.import_module(data)
    check("export/import", imp["cards"] == 3 and imp["name"] == "Operaciones (2)", str(imp))
    name, html = store.export_module_html(m["id"])
    check("export html", "__TITULO__" not in html and "/*__DATOS__*/null" not in html and "Ubuntu 24.04" in html)
    store.delete_module(imp["id"])

    # Discusión completa (demo)
    check("sin proveedores reales", await llm.available() == [])
    db.meta_set("context", "Somos un equipo de 4 personas que mantiene un sistema interno.")
    run = debate.start("¿Conviene mover los backups a las 4 AM para no chocar con el reinicio del domingo?", None, 3, None)
    types = []
    deltas = 0
    async for ev in run.stream():
        if ev["type"] == "message_delta":
            deltas += 1
            continue
        types.append(ev["type"] + (":" + ev["step"] if "step" in ev else "") + (f"@{ev['round']}" if ev.get("round") else ""))
        if ev["type"] == "evaluation":
            print("  eval:", ev["round"], ev["consensus"], ev["semantic"], ev["by"], ev["next"], ev["matrix"])
        if ev["type"] == "step_finished" and ev["step"] == "recuperar":
            print("  recuperado:", [(i["kind"], i["title"][:40], i["confidence"]) for i in ev["items"]])
        if ev["type"] in ("run_error", "message_error"):
            print("  ERROR:", ev)
    print("  eventos:", " ".join(types))
    print("  deltas:", deltas)
    d = db.one("SELECT * FROM discussions WHERE id=?", (run.did,))
    check("discusión terminada", d["status"] == "terminada", d["status"] + " " + d["error"])
    check("síntesis guardada", "## Respuesta acordada" in d["synthesis"])
    props = db.q("SELECT * FROM proposals WHERE discussion_id=?", (run.did,))
    check("tarjetas destiladas", len(props) == 3)
    check("streaming", deltas > 50)
    check("orden de pasos", types[0] == "run_started" and types[-1] == "run_finished" and "step_started:criticar@2" in types)
    check("corte por consenso", "step_started:criticar@3" not in types)
    check("eventos persistidos", len(debate.stored_events(run.did)) == len(types))
    check("síntesis en memoria", any(x["kind"] == "síntesis" for x in store.list_docs()))
    saved = debate.accept_cards(run.did, m["id"], None)
    check("aceptar tarjetas", len(saved) == 3 and len(store.list_cards(m["id"])) == 6)
    a = store.ask("qué hay que definir antes de arrancar una etapa")
    check("consultar lo aprendido", a["answered"] and "métrica" in a["answer"], str(a["confidence"]))

    r = await debate.run_to_end("¿Qué base de datos conviene para un proyecto chico?", 1, m["id"])
    check("run_to_end + guardado automático", r["status"] == "terminada" and len(r["cards"]) == 3)

    print("\nGrafo:\n" + debate.GRAPH.get_graph().draw_mermaid())


asyncio.run(main())
db._conn.close()
shutil.rmtree(tmp, ignore_errors=True)
print("\nFALLAS:", fails or "ninguna")
sys.exit(1 if fails else 0)
