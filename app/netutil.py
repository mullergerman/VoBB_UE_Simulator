"""Selección de la interfaz local por la que debe salir TODO el tráfico.

Contexto: la VM que hospeda el simulador tiene varias interfaces virtuales. Si
los sockets se bindean a 0.0.0.0, PJSIP publica en Via/Contact —y en el `c=` del
SDP— la IP que devuelve `pj_gethostip()`, que es la de la **ruta por defecto**.
Esa no tiene por qué ser la interfaz por la que realmente sale el SIP hacia el
P-CSCF. Síntomas típicos de esa discordancia:

  - el SIP sale por una interfaz pero el RTP se anuncia (y se espera) en otra;
  - el P-CSCF descarta requests cuyo origen no coincide con el flow del REGISTER
    (anti-spoofing del GM), p.ej. el SUBSCRIBE reg-event.

Solución: resolver UNA sola IP local y usarla para todo —transporte SIP de
pjsua, sockets RTP, relay SIP y subscriber reg-event— tanto para bindear como
para publicar en Via/Contact/SDP.

Prioridad de resolución:
  1. BIND_ADDR   -> IP explícita (control total del operador).
  2. BIND_IFACE  -> nombre de interfaz (p.ej. "ens192"); se lee su IPv4.
  3. autodetección: IP de origen que el kernel elige para llegar al P-CSCF.
  4. None        -> 0.0.0.0 (comportamiento histórico).
"""
import socket
import struct
from typing import Optional

from . import config
from .events import bus

_bind_ip: Optional[str] = None


def _iface_ip(name: str) -> Optional[str]:
    """IPv4 asignada a una interfaz por nombre (Linux, SIOCGIFADDR)."""
    try:
        import fcntl  # sólo Linux
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(
                s.fileno(), 0x8915, struct.pack("256s", name[:15].encode())
            )
            return socket.inet_ntoa(packed[20:24])
        finally:
            s.close()
    except Exception:
        return None


def local_addrs():
    """[(interfaz, ip)] de las IPv4 locales (Linux, SIOCGIFCONF). Sólo para
    diagnóstico: si falla, devuelve lista vacía."""
    out = []
    try:
        import array
        import fcntl  # sólo Linux
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            size = 64 * 40                      # hasta 64 interfaces
            buf = array.array("B", b"\0" * size)
            n = struct.unpack("iL", fcntl.ioctl(
                s.fileno(), 0x8912,             # SIOCGIFCONF
                struct.pack("iL", size, buf.buffer_info()[0])))[0]
            raw = buf.tobytes()
            for i in range(0, n, 40):           # sizeof(struct ifreq) en x86_64
                name = raw[i:i + 16].split(b"\0", 1)[0].decode(errors="replace")
                out.append((name, socket.inet_ntoa(raw[i + 20:i + 24])))
        finally:
            s.close()
    except Exception:
        pass
    return out


def is_bindable(ip: str) -> bool:
    """True si `ip` está asignada a alguna interfaz de este host. Es la prueba
    que importa: si no lo está, todo bind con esa IP falla (EADDRNOTAVAIL)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            return True
        finally:
            s.close()
    except Exception:
        return False


def source_ip_for(dst_addr: str, port: int = 5060) -> Optional[str]:
    """IP local que el kernel usaría para alcanzar `dst_addr` (truco del connect
    UDP: no envía nada, sólo consulta la tabla de ruteo)."""
    if not dst_addr:
        return None
    try:
        p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            p.connect((dst_addr, int(port)))
            ip = p.getsockname()[0]
        finally:
            p.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return None


def resolve(pcscf_hint: Optional[str] = None, pcscf_port: int = 5060) -> Optional[str]:
    """Fija (una vez) la IP local de trabajo y la devuelve."""
    global _bind_ip
    addrs = local_addrs()
    print(f"[net] BIND_ADDR={config.BIND_ADDR or '-'} "
          f"BIND_IFACE={config.BIND_IFACE or '-'} | interfaces locales: "
          + (", ".join(f"{n}={a}" for n, a in addrs) or "(no enumeradas)"),
          flush=True)

    ip = None
    origin = ""
    if config.BIND_ADDR:
        ip, origin = config.BIND_ADDR.split(":")[0].strip(), "BIND_ADDR"
    elif config.BIND_IFACE:
        ip = _iface_ip(config.BIND_IFACE)
        origin = f"BIND_IFACE={config.BIND_IFACE}"
        if not ip:
            print(f"[net] ERROR: la interfaz {config.BIND_IFACE} no existe o no "
                  f"tiene IPv4; se ignora BIND_IFACE", flush=True)

    # Una IP que no está asignada a ninguna interfaz hace fallar TODO bind
    # (EADDRNOTAVAIL) y la app terminaría degradando a 0.0.0.0 en silencio,
    # que es justo el problema que se quería evitar. Se avisa fuerte.
    if ip and not is_bindable(ip):
        print(f"[net] ERROR: {ip} ({origin}) no está asignada a ninguna interfaz "
              f"de este host; se ignora y se autodetecta por ruta.", flush=True)
        try:
            bus.emit("log", level="warn",
                     msg=f"{origin}={ip} no es una IP local; se ignora")
        except Exception:
            pass
        ip, origin = None, ""

    if not ip and pcscf_hint:
        ip = source_ip_for(pcscf_hint, pcscf_port)
        origin = f"ruta hacia {pcscf_hint}"
    _bind_ip = ip or None
    if _bind_ip:
        print(f"[net] IP local unificada: {_bind_ip} ({origin})", flush=True)
    else:
        print("[net] sin IP local fija: sockets en 0.0.0.0 (puede haber "
              "discordancia SIP/RTP en hosts multi-interfaz)", flush=True)
    return _bind_ip


def bind_ip() -> Optional[str]:
    """IP local resuelta, o None si no hay ninguna fijada."""
    return _bind_ip


def bind_host() -> str:
    """Dirección para `socket.bind()`: la IP resuelta o 0.0.0.0."""
    return _bind_ip or "0.0.0.0"


def local_ip_for(dst_addr: str) -> str:
    """IP a publicar en Via/Contact hacia `dst_addr`."""
    return _bind_ip or source_ip_for(dst_addr) or "127.0.0.1"
