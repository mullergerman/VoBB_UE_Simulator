"""Modelo de datos de la plataforma.

`Abonado` es el registro persistido (config del UE simulado). Los campos de
runtime (estado de registro, llamadas) NO se persisten: viven en memoria dentro
del PjsuaManager y se exponen vía la API/WebSocket.

`Profile` agrupa los parámetros de red/comportamiento compartidos (dominio,
P-CSCF, realm, registrar-uri, codecs, alerting, eco, expires). Un abonado puede
referenciar un perfil (profile_id) y heredar esos campos; solo define sus datos
propios (línea, IMPI, password). Si no referencia perfil, usa sus campos propios.
"""
from typing import Optional

from sqlmodel import Field, SQLModel

# Campos compartidos (los aporta el perfil cuando el abonado lo referencia).
SHARED_FIELDS = [
    "domain", "pcscf_addr", "pcscf_port", "transport", "auth_realm",
    "registrar_uri", "codec_pref", "alerting_delay_s", "echo_enabled",
    "reg_expires",
]


class AbonadoBase(SQLModel):
    enabled: bool = True
    display_name: str = ""
    line_number: str = Field(index=True)          # user part del AOR, p.ej. "1001"
    domain: str = "vobb.test"                     # home domain / realm
    pcscf_addr: str = "127.0.0.1"                 # dirección del P-CSCF / registrar
    pcscf_port: int = 5060
    transport: str = "udp"                        # udp | tcp
    auth_user: str = ""                           # IMPI / username Digest
    auth_password: str = ""                       # password EN PLANO (Digest MD5)
    auth_realm: str = ""                          # vacío => se usa `domain`
    # Registrar/Request-URI del REGISTER (= digest-uri). Vacío => sip:<domain>
    # (estándar 3GPP). Para replicar equipos que usan la IP del P-CSCF como
    # Request-URI, fijar p.ej. "sip:100.103.6.12".
    registrar_uri: str = ""
    codec_pref: str = "PCMU,PCMA"                 # prioridad de codecs (G.711)
    alerting_delay_s: int = 3                     # segundos de 180 antes de atender
    echo_enabled: bool = True                     # devolver audio en modo eco
    reg_expires: int = 600                        # expires del REGISTER
    # Perfil del que hereda los campos compartidos (None => usa los propios).
    profile_id: Optional[int] = Field(default=None, index=True)


class Abonado(AbonadoBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class AbonadoCreate(AbonadoBase):
    pass


class AbonadoUpdate(SQLModel):
    enabled: Optional[bool] = None
    display_name: Optional[str] = None
    line_number: Optional[str] = None
    domain: Optional[str] = None
    pcscf_addr: Optional[str] = None
    pcscf_port: Optional[int] = None
    transport: Optional[str] = None
    auth_user: Optional[str] = None
    auth_password: Optional[str] = None
    auth_realm: Optional[str] = None
    registrar_uri: Optional[str] = None
    codec_pref: Optional[str] = None
    alerting_delay_s: Optional[int] = None
    echo_enabled: Optional[bool] = None
    reg_expires: Optional[int] = None
    profile_id: Optional[int] = None


# --------------------------------------------------------------------------
# Perfiles: parámetros de red/comportamiento compartidos entre abonados.
# --------------------------------------------------------------------------
class ProfileBase(SQLModel):
    name: str = Field(index=True)
    domain: str = "vobb.test"
    pcscf_addr: str = "127.0.0.1"
    pcscf_port: int = 5060
    transport: str = "udp"
    auth_realm: str = ""
    registrar_uri: str = ""
    codec_pref: str = "PCMU,PCMA"
    alerting_delay_s: int = 3
    echo_enabled: bool = True
    reg_expires: int = 600


class Profile(ProfileBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class ProfileCreate(ProfileBase):
    pass


class ProfileUpdate(SQLModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    pcscf_addr: Optional[str] = None
    pcscf_port: Optional[int] = None
    transport: Optional[str] = None
    auth_realm: Optional[str] = None
    registrar_uri: Optional[str] = None
    codec_pref: Optional[str] = None
    alerting_delay_s: Optional[int] = None
    echo_enabled: Optional[bool] = None
    reg_expires: Optional[int] = None
