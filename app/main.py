"""Punto de entrada: wiring de FastAPI + PJSUA2 + estáticos."""
import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlmodel import select

from . import config
from .api import router
from .db import get_session, init_db, resolve_abonado
from .events import bus
from .models import Abonado
from .pjsua_manager import manager

app = FastAPI(title="VoBB UE Simulator")
app.include_router(router)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """Control de caché de los estáticos para que tras un deploy el navegador
    tome siempre la última versión:
    - index.html (`/`): `no-store` => nunca se cachea; siempre referencia el
      `?v=N` actual de app.js/styles.css. Rompe cualquier caché vieja.
    - .js/.css: `no-cache` => revalidan con ETag (304 si no cambió)."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        resp.headers["Cache-Control"] = "no-store"
    elif path.endswith((".js", ".css")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
async def _startup():
    init_db()
    bus.attach_loop(asyncio.get_event_loop())
    # Arranca el motor SIP en un thread (libStart puede bloquear brevemente).
    await asyncio.get_event_loop().run_in_executor(None, manager.start)
    # Da de alta las cuentas de los abonados habilitados (resolviendo su perfil).
    with get_session() as s:
        abonados = [resolve_abonado(ab, s) for ab in s.exec(select(Abonado)).all()]
    for ab in abonados:
        if ab.enabled:
            await asyncio.get_event_loop().run_in_executor(None, manager.add_account, ab)
    # Las cuentas se crean pero NO se registran (evita la ráfaga que satura el
    # SBC). El registro se dispara a mano/escalonado desde el Dashboard.
    n = len([a for a in abonados if a.enabled])
    if n:
        print(f"[startup] {n} cuentas creadas SIN registrar; usá 'Registrar todos' "
              f"en el Dashboard para registrarlas escalonadas.", flush=True)
        bus.emit("log", level="info",
                 msg=f"{n} cuentas listas (sin registrar). Registrá desde el Dashboard.")


@app.on_event("shutdown")
async def _shutdown():
    manager.stop()


# Estáticos al final para no pisar las rutas /api y /ws.
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
