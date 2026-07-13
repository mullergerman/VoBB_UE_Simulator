"""API REST + WebSocket de la plataforma."""
import asyncio
import json
from typing import List

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import select

from .db import get_session, resolve_abonado
from .events import bus
from .models import (
    SHARED_FIELDS, Abonado, AbonadoCreate, AbonadoUpdate,
    Profile, ProfileCreate, ProfileUpdate,
)
from .pjsua_manager import manager

router = APIRouter()


def _apply(ab: Abonado, session) -> None:
    """Resuelve el perfil del abonado y (re)crea su cuenta en el motor SIP."""
    manager.update_account(resolve_abonado(ab, session))


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
        _apply(ab, s)
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
        _apply(ab, s)
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
        _apply(ab, s)
    manager.register(abonado_id, renew=True)
    return {"ok": True}


@router.post("/api/abonados/{abonado_id}/unregister")
def unregister_abonado(abonado_id: int):
    manager.register(abonado_id, renew=False)
    return {"ok": True}


# --------------------------------------------------------------------------
# Perfiles (parámetros compartidos entre abonados)
# --------------------------------------------------------------------------
@router.get("/api/profiles", response_model=List[Profile])
def list_profiles():
    with get_session() as s:
        return s.exec(select(Profile)).all()


@router.post("/api/profiles", response_model=Profile)
def create_profile(data: ProfileCreate):
    prof = Profile.model_validate(data)
    with get_session() as s:
        s.add(prof)
        s.commit()
        s.refresh(prof)
    return prof


@router.put("/api/profiles/{profile_id}", response_model=Profile)
def update_profile(profile_id: int, data: ProfileUpdate):
    with get_session() as s:
        prof = s.get(Profile, profile_id)
        if not prof:
            raise HTTPException(404, "Perfil no encontrado")
        for k, v in data.model_dump(exclude_unset=True).items():
            setattr(prof, k, v)
        s.add(prof)
        s.commit()
        s.refresh(prof)
        # Re-aplicar el perfil a todos sus abonados (re-registro con nueva config).
        abonados = s.exec(select(Abonado).where(Abonado.profile_id == profile_id)).all()
        for ab in abonados:
            _apply(ab, s)
    return prof


@router.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int):
    with get_session() as s:
        prof = s.get(Profile, profile_id)
        if not prof:
            raise HTTPException(404, "Perfil no encontrado")
        # Desvincular: copiar los campos compartidos del perfil a cada abonado
        # que lo usa (para que conserven su config) y ponerlos como "personalizado".
        abonados = s.exec(select(Abonado).where(Abonado.profile_id == profile_id)).all()
        for ab in abonados:
            for f in SHARED_FIELDS:
                setattr(ab, f, getattr(prof, f))
            ab.profile_id = None
            s.add(ab)
        s.delete(prof)
        s.commit()
    return {"ok": True, "detached": len(abonados)}


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
        # Snapshot inicial del estado del motor (registros + llamadas activas).
        await ws.send_text(json.dumps({"type": "status", **manager.status()}))
        # Replay del historial reciente de señalización SIP (para ver el flujo
        # aunque hayas abierto la web después de que ocurrió).
        for evt in list(bus.recent_sip):
            await ws.send_text(json.dumps({**evt, "replay": True}, default=str))
        while True:
            evt = await q.get()
            await ws.send_text(json.dumps(evt, default=str))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        bus.unsubscribe(q)
