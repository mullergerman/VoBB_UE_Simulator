"""API REST + WebSocket de la plataforma (con autenticación y permisos)."""
import json
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import select, func

from .auth import (
    CurrentUser, get_current_user, get_user_numbers, hash_password,
    line_in_ranges, make_token, parse_token, require_admin, require_perm,
    verify_password,
)
from .db import get_session, resolve_abonado
from .events import bus
from .models import (
    SHARED_FIELDS, Abonado, AbonadoCreate, AbonadoUpdate, CallRecord,
    LoginRequest, Profile, ProfileCreate, ProfileUpdate,
    User, UserCreate, UserNumber, UserUpdate,
)
from .pjsua_manager import manager

router = APIRouter()

_SIP_USER = re.compile(r"sip:([^@;>\s]+)@", re.I)


def _apply(ab: Abonado, session) -> None:
    """Resuelve el perfil del abonado y (re)crea su cuenta en el motor SIP."""
    manager.update_account(resolve_abonado(ab, session))


def _ranges(user: CurrentUser, s):
    return get_user_numbers(user.id, s)


def _can_line(user: CurrentUser, line: str, s) -> bool:
    return user.is_admin or line_in_ranges(line, _ranges(user, s))


def _get_abonado_checked(abonado_id: int, user: CurrentUser, s) -> Abonado:
    ab = s.get(Abonado, abonado_id)
    if not ab:
        raise HTTPException(404, "Abonado no encontrado")
    if not _can_line(user, ab.line_number, s):
        raise HTTPException(403, "El abonado no está en tu numeración asignada")
    return ab


# ==========================================================================
# Autenticación
# ==========================================================================
def _user_public(u: User, s) -> dict:
    nums = get_user_numbers(u.id, s)
    try:
        perms = json.loads(u.permissions or "[]")
    except Exception:
        perms = []
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "is_admin": u.is_admin, "enabled": u.enabled, "permissions": perms,
        "numbers": [{"start": n.start, "end": n.end} for n in nums],
    }


@router.post("/api/auth/login")
def login(data: LoginRequest):
    with get_session() as s:
        u = s.exec(select(User).where(User.username == data.username)).first()
        if not u or not u.enabled or not verify_password(data.password, u.password_salt, u.password_hash):
            raise HTTPException(401, "Usuario o contraseña inválidos")
        return {"token": make_token(u.id), "user": _user_public(u, s)}


@router.get("/api/auth/me")
def me(user: CurrentUser = Depends(get_current_user)):
    with get_session() as s:
        u = s.get(User, user.id)
        return _user_public(u, s)


# ==========================================================================
# Usuarios (solo admin)
# ==========================================================================
def _set_user_numbers(user_id: int, numbers, s) -> None:
    for old in s.exec(select(UserNumber).where(UserNumber.user_id == user_id)).all():
        s.delete(old)
    for n in numbers or []:
        start = (n.start or "").strip()
        if not start:
            continue
        s.add(UserNumber(user_id=user_id, start=start, end=(n.end or "").strip()))


@router.get("/api/users")
def list_users(admin: CurrentUser = Depends(require_admin)):
    with get_session() as s:
        return [_user_public(u, s) for u in s.exec(select(User)).all()]


@router.post("/api/users")
def create_user(data: UserCreate, admin: CurrentUser = Depends(require_admin)):
    with get_session() as s:
        if s.exec(select(User).where(User.username == data.username)).first():
            raise HTTPException(409, "Ya existe un usuario con ese nombre")
        salt, h = hash_password(data.password)
        u = User(
            username=data.username, display_name=data.display_name,
            password_salt=salt, password_hash=h, is_admin=data.is_admin,
            enabled=data.enabled, permissions=json.dumps(data.permissions or []),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        _set_user_numbers(u.id, data.numbers, s)
        s.commit()
        return _user_public(u, s)


@router.put("/api/users/{user_id}")
def update_user(user_id: int, data: UserUpdate, admin: CurrentUser = Depends(require_admin)):
    with get_session() as s:
        u = s.get(User, user_id)
        if not u:
            raise HTTPException(404, "Usuario no encontrado")
        d = data.model_dump(exclude_unset=True)
        # No permitir quitar el rol admin ni deshabilitar al último administrador
        # (evita quedar sin acceso administrativo / sin botones de gestión).
        demote = (d.get("is_admin") is False) or (d.get("enabled") is False)
        if u.is_admin and demote:
            n_admins = len(s.exec(select(User).where(User.is_admin == True, User.enabled == True)).all())  # noqa: E712
            if n_admins <= 1:
                raise HTTPException(409, "No se puede quitar el rol/deshabilitar al último administrador")
        if "username" in d and d["username"]:
            u.username = d["username"]
        if "display_name" in d:
            u.display_name = d["display_name"]
        if "is_admin" in d and d["is_admin"] is not None:
            u.is_admin = d["is_admin"]
        if "enabled" in d and d["enabled"] is not None:
            u.enabled = d["enabled"]
        if "permissions" in d and d["permissions"] is not None:
            u.permissions = json.dumps(d["permissions"])
        if d.get("password"):
            u.password_salt, u.password_hash = hash_password(d["password"])
        s.add(u)
        s.commit()
        if "numbers" in d and d["numbers"] is not None:
            _set_user_numbers(user_id, data.numbers, s)
            s.commit()
        s.refresh(u)
        return _user_public(u, s)


@router.delete("/api/users/{user_id}")
def delete_user(user_id: int, admin: CurrentUser = Depends(require_admin)):
    with get_session() as s:
        u = s.get(User, user_id)
        if not u:
            raise HTTPException(404, "Usuario no encontrado")
        if u.is_admin and len(s.exec(select(User).where(User.is_admin == True)).all()) <= 1:  # noqa: E712
            raise HTTPException(409, "No se puede borrar el último administrador")
        for n in s.exec(select(UserNumber).where(UserNumber.user_id == user_id)).all():
            s.delete(n)
        s.delete(u)
        s.commit()
    return {"ok": True}


# ==========================================================================
# Abonados (CRUD) — filtrados por la numeración del usuario
# ==========================================================================
@router.get("/api/abonados")
def list_abonados(user: CurrentUser = Depends(get_current_user)):
    with get_session() as s:
        abonados = s.exec(select(Abonado)).all()
        if user.is_admin:
            return abonados
        ranges = _ranges(user, s)
        return [a for a in abonados if line_in_ranges(a.line_number, ranges)]


@router.post("/api/abonados", response_model=Abonado)
def create_abonado(data: AbonadoCreate, user: CurrentUser = Depends(require_perm("edit_abonados"))):
    ab = Abonado.model_validate(data)
    with get_session() as s:
        if not _can_line(user, ab.line_number, s):
            raise HTTPException(403, "La línea no está en tu numeración asignada")
        s.add(ab)
        s.commit()
        s.refresh(ab)
        _apply(ab, s)
    return ab


@router.put("/api/abonados/{abonado_id}", response_model=Abonado)
def update_abonado(abonado_id: int, data: AbonadoUpdate, user: CurrentUser = Depends(require_perm("edit_abonados"))):
    with get_session() as s:
        ab = _get_abonado_checked(abonado_id, user, s)
        upd = data.model_dump(exclude_unset=True)
        # Si cambia la línea, la nueva también debe estar en la numeración.
        if "line_number" in upd and not _can_line(user, upd["line_number"], s):
            raise HTTPException(403, "La nueva línea no está en tu numeración asignada")
        for k, v in upd.items():
            setattr(ab, k, v)
        s.add(ab)
        s.commit()
        s.refresh(ab)
        _apply(ab, s)
    return ab


@router.delete("/api/abonados/{abonado_id}")
def delete_abonado(abonado_id: int, user: CurrentUser = Depends(require_perm("edit_abonados"))):
    with get_session() as s:
        ab = _get_abonado_checked(abonado_id, user, s)
        s.delete(ab)
        s.commit()
    manager.remove_account(abonado_id)
    return {"ok": True}


@router.post("/api/abonados/{abonado_id}/register")
def register_abonado(abonado_id: int, user: CurrentUser = Depends(get_current_user)):
    with get_session() as s:
        ab = _get_abonado_checked(abonado_id, user, s)
        if not (user.has_perm("edit_abonados") or user.has_perm("control_calls")):
            raise HTTPException(403, "Sin permiso para registrar")
        _apply(ab, s)
    manager.register(abonado_id, renew=True)
    return {"ok": True}


@router.post("/api/abonados/{abonado_id}/unregister")
def unregister_abonado(abonado_id: int, user: CurrentUser = Depends(get_current_user)):
    with get_session() as s:
        _get_abonado_checked(abonado_id, user, s)
        if not (user.has_perm("edit_abonados") or user.has_perm("control_calls")):
            raise HTTPException(403, "Sin permiso")
    manager.register(abonado_id, renew=False)
    return {"ok": True}


# ==========================================================================
# Perfiles
# ==========================================================================
@router.get("/api/profiles", response_model=List[Profile])
def list_profiles(user: CurrentUser = Depends(get_current_user)):
    with get_session() as s:
        return s.exec(select(Profile)).all()


@router.post("/api/profiles", response_model=Profile)
def create_profile(data: ProfileCreate, user: CurrentUser = Depends(require_perm("manage_profiles"))):
    prof = Profile.model_validate(data)
    with get_session() as s:
        s.add(prof)
        s.commit()
        s.refresh(prof)
    return prof


@router.put("/api/profiles/{profile_id}", response_model=Profile)
def update_profile(profile_id: int, data: ProfileUpdate, user: CurrentUser = Depends(require_perm("manage_profiles"))):
    with get_session() as s:
        prof = s.get(Profile, profile_id)
        if not prof:
            raise HTTPException(404, "Perfil no encontrado")
        for k, v in data.model_dump(exclude_unset=True).items():
            setattr(prof, k, v)
        s.add(prof)
        s.commit()
        s.refresh(prof)
        for ab in s.exec(select(Abonado).where(Abonado.profile_id == profile_id)).all():
            _apply(ab, s)
    return prof


@router.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int, user: CurrentUser = Depends(require_perm("manage_profiles"))):
    with get_session() as s:
        prof = s.get(Profile, profile_id)
        if not prof:
            raise HTTPException(404, "Perfil no encontrado")
        abonados = s.exec(select(Abonado).where(Abonado.profile_id == profile_id)).all()
        for ab in abonados:
            for f in SHARED_FIELDS:
                setattr(ab, f, getattr(prof, f))
            ab.profile_id = None
            s.add(ab)
        s.delete(prof)
        s.commit()
    return {"ok": True, "detached": len(abonados)}


# ==========================================================================
# Llamadas
# ==========================================================================
@router.post("/api/calls")
def make_call(payload: dict, user: CurrentUser = Depends(require_perm("control_calls"))):
    from_id = payload.get("from_id")
    to_number = payload.get("to_number")
    if from_id is None or not to_number:
        raise HTTPException(400, "Se requieren from_id y to_number")
    with get_session() as s:
        _get_abonado_checked(int(from_id), user, s)
    try:
        call_id = manager.originate(int(from_id), str(to_number))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "call_id": call_id}


@router.delete("/api/calls/{call_id}")
def end_call(call_id: str, user: CurrentUser = Depends(require_perm("control_calls"))):
    manager.hangup(call_id)
    return {"ok": True}


@router.post("/api/calls/hangup_all")
def hangup_all(user: CurrentUser = Depends(require_perm("control_calls"))):
    manager.hangup_all()
    return {"ok": True}


# ==========================================================================
# Registro en masa (escalonado) — control general del sistema
# ==========================================================================
def _allowed_abonado_ids(user: CurrentUser, s):
    """None para admin (todas); si no, el set de ids cuya línea cae en la
    numeración del usuario."""
    if user.is_admin:
        return None
    ranges = _ranges(user, s)
    return {ab.id for ab in s.exec(select(Abonado)).all()
            if line_in_ranges(ab.line_number, ranges)}


@router.post("/api/registrations/register_all")
def register_all(user: CurrentUser = Depends(require_perm("control_calls"))):
    with get_session() as s:
        ids = _allowed_abonado_ids(user, s)
    n = manager.register_all(ids=ids)
    return {"ok": True, "count": n}


@router.post("/api/registrations/unregister_all")
def unregister_all(user: CurrentUser = Depends(require_perm("control_calls"))):
    with get_session() as s:
        ids = _allowed_abonado_ids(user, s)
    n = manager.unregister_all(ids=ids)
    return {"ok": True, "count": n}


# ==========================================================================
# Histórico y estadísticas de llamadas
# ==========================================================================
def _history_query(user: CurrentUser, s, direction=None, result=None):
    stmt = select(CallRecord).order_by(CallRecord.id.desc())
    if direction in ("MO", "MT"):
        stmt = stmt.where(CallRecord.direction == direction)
    if result == "answered":
        stmt = stmt.where(CallRecord.answered == True)   # noqa: E712
    elif result == "failed":
        stmt = stmt.where(CallRecord.answered == False)  # noqa: E712
    rows = s.exec(stmt).all()
    if not user.is_admin:
        ranges = _ranges(user, s)
        rows = [r for r in rows if line_in_ranges(r.local_line, ranges)]
    return rows


@router.get("/api/call-history")
def call_history(limit: int = 200, direction: Optional[str] = None,
                 result: Optional[str] = None,
                 user: CurrentUser = Depends(get_current_user)):
    with get_session() as s:
        rows = _history_query(user, s, direction, result)
    limit = max(1, min(int(limit or 200), 2000))
    return [r.model_dump() for r in rows[:limit]]


@router.get("/api/call-stats")
def call_stats(user: CurrentUser = Depends(get_current_user)):
    with get_session() as s:
        rows = _history_query(user, s)
    total = len(rows)
    answered = sum(1 for r in rows if r.answered)
    failed = total - answered
    durs = [r.duration_s for r in rows if r.answered]
    acd = round(sum(durs) / len(durs), 1) if durs else 0
    asr = round(100.0 * answered / total, 1) if total else 0
    by_code = {}
    for r in rows:
        by_code[r.last_code] = by_code.get(r.last_code, 0) + 1
    active = len(manager.status().get("calls") or [])
    return {"total": total, "answered": answered, "failed": failed,
            "asr": asr, "acd": acd, "active": active, "by_code": by_code}


@router.delete("/api/call-history")
def clear_history(user: CurrentUser = Depends(require_admin)):
    with get_session() as s:
        for r in s.exec(select(CallRecord)).all():
            s.delete(r)
        s.commit()
    return {"ok": True}


# ==========================================================================
# Estado del motor
# ==========================================================================
@router.get("/api/status")
def status(user: CurrentUser = Depends(get_current_user)):
    st = manager.status()
    if not user.is_admin:
        with get_session() as s:
            ranges = _ranges(user, s)
        st = _filter_status(st, ranges)
    return st


# ==========================================================================
# WebSocket de eventos en tiempo real (autenticado + filtrado por numeración)
# ==========================================================================
def _sip_lines(raw: str):
    return _SIP_USER.findall(raw or "")


def _event_allowed(evt: dict, ranges) -> bool:
    """Filtra un evento para un usuario no-admin según su numeración."""
    t = evt.get("type")
    if t == "log":
        return False                       # logs del motor: solo admin
    if t == "sip":
        return any(line_in_ranges(u, ranges) for u in _sip_lines(evt.get("raw", "")))
    if t == "call_record":
        return line_in_ranges(evt.get("local_line", ""), ranges)
    line = evt.get("line")
    if line is not None:
        return line_in_ranges(line, ranges)
    return True


def _filter_status(st: dict, ranges) -> dict:
    st = dict(st)
    st.pop("net", None)                    # detalle de infraestructura: solo admin
    regs = st.get("registrations") or {}
    st["registrations"] = {k: v for k, v in regs.items() if line_in_ranges(v.get("line", ""), ranges)}
    st["calls"] = [c for c in (st.get("calls") or []) if line_in_ranges(c.get("line", ""), ranges)]
    return st


@router.websocket("/ws")
async def ws_events(ws: WebSocket, token: Optional[str] = None):
    uid = parse_token(token) if token else None
    if uid is None:
        await ws.close(code=1008)
        return
    with get_session() as s:
        u = s.get(User, uid)
        if not u or not u.enabled:
            await ws.close(code=1008)
            return
        is_admin = u.is_admin
        ranges = [] if is_admin else get_user_numbers(uid, s)

    await ws.accept()
    q = bus.subscribe()
    try:
        st = manager.status()
        if not is_admin:
            st = _filter_status(st, ranges)
        await ws.send_text(json.dumps({"type": "status", **st}))
        for evt in list(bus.recent_sip):
            if is_admin or _event_allowed(evt, ranges):
                await ws.send_text(json.dumps({**evt, "replay": True}, default=str))
        while True:
            evt = await q.get()
            if is_admin or _event_allowed(evt, ranges):
                await ws.send_text(json.dumps(evt, default=str))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        bus.unsubscribe(q)
