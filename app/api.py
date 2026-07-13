"""API REST + WebSocket de la plataforma."""
import asyncio
import json
from typing import List

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import select

from .db import get_session
from .events import bus
from .models import Abonado, AbonadoCreate, AbonadoUpdate
from .pjsua_manager import manager

router = APIRouter()


# --------------------------------------------------------------------------
# Abonados (CRUD)
# --------------------------------------------------------------------------
@router.get("/api/abonados", response_model=List[Abonado])
def list_abonados():
    with get_session() as s:
        return s.exec(select(Abonado)).all()


@router.post("/api/abonados", response_model=Abonado)
def create_abonado(data: AbonadoCreate):
    ab = Abonado.model_validate(data)
    with get_session() as s:
        s.add(ab)
        s.commit()
        s.refresh(ab)
    manager.add_account(ab)
    return ab


@router.put("/api/abonados/{abonado_id}", response_model=Abonado)
def update_abonado(abonado_id: int, data: AbonadoUpdate):
    with get_session() as s:
        ab = s.get(Abonado, abonado_id)
        if not ab:
            raise HTTPException(404, "Abonado no encontrado")
        for k, v in data.model_dump(exclude_unset=True).items():
            setattr(ab, k, v)
        s.add(ab)
        s.commit()
        s.refresh(ab)
    manager.update_account(ab)
    return ab


@router.delete("/api/abonados/{abonado_id}")
def delete_abonado(abonado_id: int):
    with get_session() as s:
        ab = s.get(Abonado, abonado_id)
        if not ab:
            raise HTTPException(404, "Abonado no encontrado")
        s.delete(ab)
        s.commit()
    manager.remove_account(abonado_id)
    return {"ok": True}


@router.post("/api/abonados/{abonado_id}/register")
def register_abonado(abonado_id: int):
    with get_session() as s:
        ab = s.get(Abonado, abonado_id)
        if not ab:
            raise HTTPException(404, "Abonado no encontrado")
    manager.add_account(ab)
    manager.register(abonado_id, renew=True)
    return {"ok": True}


@router.post("/api/abonados/{abonado_id}/unregister")
def unregister_abonado(abonado_id: int):
    manager.register(abonado_id, renew=False)
    return {"ok": True}


# --------------------------------------------------------------------------
# Llamadas
# --------------------------------------------------------------------------
@router.post("/api/calls")
def make_call(payload: dict):
    from_id = payload.get("from_id")
    to_number = payload.get("to_number")
    if from_id is None or not to_number:
        raise HTTPException(400, "Se requieren from_id y to_number")
    try:
        call_id = manager.originate(int(from_id), str(to_number))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "call_id": call_id}


@router.delete("/api/calls/{call_id}")
def end_call(call_id: str):
    manager.hangup(call_id)
    return {"ok": True}


@router.post("/api/calls/hangup_all")
def hangup_all():
    manager.hangup_all()
    return {"ok": True}


# --------------------------------------------------------------------------
# Estado del motor
# --------------------------------------------------------------------------
@router.get("/api/status")
def status():
    return manager.status()


# --------------------------------------------------------------------------
# WebSocket de eventos en tiempo real
# --------------------------------------------------------------------------
@router.websocket("/ws")
async def ws_events(ws: WebSocket):
    await ws.accept()
    q = bus.subscribe()
    try:
        # Snapshot inicial del estado del motor.
        await ws.send_text(json.dumps({"type": "status", **manager.status()}))
        while True:
            evt = await q.get()
            await ws.send_text(json.dumps(evt, default=str))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        bus.unsubscribe(q)
