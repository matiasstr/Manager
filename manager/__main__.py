"""python -m manager          → interfaz web y API en http://127.0.0.1:8765
python -m manager --mcp       → servidor MCP por stdio, para conectarlo a otros agentes
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import webbrowser


def main() -> None:
    ap = argparse.ArgumentParser(prog="manager", description="Mini cerebro multiagente")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8765))
    ap.add_argument("--data", help="carpeta de datos (por defecto ./data)")
    ap.add_argument("--no-browser", action="store_true", help="no abrir el navegador al iniciar")
    ap.add_argument("--mcp", action="store_true", help="correr como servidor MCP (stdio)")
    args = ap.parse_args()
    if args.data:
        os.environ["MANAGER_DATA"] = args.data

    if args.mcp:
        from .mcp_server import server
        server.run(transport="stdio")
        return

    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    url = f"http://{args.host}:{args.port}"
    for stream in (sys.stdout, sys.stderr):  # la consola de Windows no siempre es UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    print(f"\n  Manager: {url}\n  API:     {url}/docs\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run("manager.api:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
