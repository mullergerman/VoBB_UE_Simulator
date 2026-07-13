"""Autenticación y autorización.

- Password: PBKDF2-HMAC-SHA256 con salt (stdlib, sin dependencias externas).
- Token: firmado con HMAC y secreto persistido en el volumen de datos (JWT-like,
  stateless; sobrevive reinicios y no requiere tabla de sesiones).
- Dependencias FastAPI: get_current_user / require_admin / require_perm.
- Rangos: line_in_ranges() decide si una línea pertenece a un usuario.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import Depends, Header, HTTPException, Query
from sqlmodel import select

from . import config
from .db import get_session
from .models import User, UserNumber

TOKEN_TTL = 7 * 24 * 3600            # 7 días
_SECRET_PATH = os.path.join(os.path.dirname(config.DB_PATH), "secret.key")
_secret_cache: Optional[bytes] = None


def _secret() -> bytes:
    global _secret_cache
    if _secret_cache is None:
        if os.path.exists(_SECRET_PATH):
            with open(_SECRET_PATH, "rb") as f:
                _secret_cache = f.read()
        else:
            _secret_cache = secrets.token_bytes(32)
            os.makedirs(os.path.dirname(_SECRET_PATH), exist_ok=True)
            with open(_SECRET_PATH, "wb") as f:
                f.write(_secret_cache)
    return _secret_cache


# ---- passwords ----
def hash_password(password: str, salt: Optional[str] = None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    return salt, h


def verify_password(password: str, salt: str, expected: str) -> bool:
    try:
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    except Exception:
        return False
    return hmac.compare_digest(h, expected)


# ---- tokens ----
def make_token(user_id: int) -> str:
    exp = int(time.time()) + TOKEN_TTL
    msg = f"{user_id}.{exp}"
    sig = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}.{sig}".encode()).decode()


def parse_token(token: str) -> Optional[int]:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user_id, exp, sig = raw.rsplit(".", 2)
        good = hmac.new(_secret(), f"{user_id}.{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        if int(exp) < time.time():
            return None
        return int(user_id)
    except Exception:
        return None


# ---- usuario actual (objeto liviano, desacoplado de la sesión ORM) ----
@dataclass
class CurrentUser:
    id: int
    username: str
    display_name: str
    is_admin: bool
    enabled: bool
    permissions: List[str] = field(default_factory=list)

    def has_perm(self, perm: str) -> bool:
        return self.is_admin or perm in self.permissions


def _extract_token(authorization: Optional[str], token_q: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return token_q


def get_current_user(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
) -> CurrentUser:
    tok = _extract_token(authorization, token)
    uid = parse_token(tok) if tok else None
    if uid is None:
        raise HTTPException(401, "No autenticado")
    with get_session() as s:
        u = s.get(User, uid)
        if not u or not u.enabled:
            raise HTTPException(401, "Usuario inválido o deshabilitado")
        try:
            perms = json.loads(u.permissions or "[]")
        except Exception:
            perms = []
        return CurrentUser(
            id=u.id, username=u.username, display_name=u.display_name,
            is_admin=u.is_admin, enabled=u.enabled, permissions=perms,
        )


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(403, "Requiere administrador")
    return user


def require_perm(perm: str):
    def dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_perm(perm):
            raise HTTPException(403, f"Sin permiso: {perm}")
        return user
    return dep


# ---- rangos de numeración ----
def _num(s: str) -> Optional[int]:
    s = (s or "").strip().lstrip("+")
    return int(s) if s.isdigit() else None


def get_user_numbers(user_id: int, session) -> List[UserNumber]:
    return session.exec(select(UserNumber).where(UserNumber.user_id == user_id)).all()


def line_in_ranges(line: str, ranges: List[UserNumber]) -> bool:
    ln = _num(line)
    for r in ranges:
        start = r.start.strip()
        end = (r.end or r.start).strip()
        a, b = _num(start), _num(end)
        if ln is not None and a is not None and b is not None:
            if a <= ln <= b:
                return True
        elif (line or "").strip() == start:   # comparación textual exacta
            return True
    return False


def user_can_access_line(user: CurrentUser, line: str, session) -> bool:
    if user.is_admin:
        return True
    return line_in_ranges(line, get_user_numbers(user.id, session))
