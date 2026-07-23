"""Mini-DSL para customizar headers SIP por procedimiento.

Cada abonado/perfil puede llevar un texto de reglas por procedimiento
(REGISTER / INVITE / SUBSCRIBE). Vacío => headers por defecto (comportamiento
histórico). Una regla por línea:

    Name: valor      set/override: reemplaza Name (o lo agrega si no existía)
    +Name: valor     add: agrega OTRA instancia de Name (no reemplaza)
    -Name            remove: quita todas las instancias de Name
    # comentario     las líneas en blanco y las que empiezan con # se ignoran

Limitación (PJSUA2): en REGISTER e INVITE solo se pueden tocar headers de
extensión; Via/From/To/Call-ID/CSeq/Contact/Max-Forwards los genera pjsip y no
pasan por acá. En el SUBSCRIBE, que es un builder propio, se puede tocar
cualquiera.
"""
from typing import List, Tuple

# (op, name, value) con op in {"set", "add", "del"}. name en su casing original;
# la comparación es case-insensitive (los headers SIP no distinguen mayúsculas).
Rule = Tuple[str, str, str]
Pair = Tuple[str, str]


def parse_rules(text: str) -> List[Rule]:
    rules: List[Rule] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            name = line[1:].split(":", 1)[0].strip()
            if name:
                rules.append(("del", name, ""))
            continue
        op = "set"
        if line.startswith("+"):
            op, line = "add", line[1:].strip()
        if ":" not in line:
            continue                      # sin ':' no es un header válido; se ignora
        name, value = line.split(":", 1)
        name, value = name.strip(), value.strip()
        if name:
            rules.append((op, name, value))
    return rules


def apply_to_pairs(base: List[Pair], rules: List[Rule]) -> List[Pair]:
    """Aplica las reglas sobre una lista de (name, value), preservando el orden
    de los headers base y agregando los nuevos al final."""
    out: List[Pair] = list(base)

    def idx_of(name: str) -> int:
        low = name.lower()
        for i, (n, _) in enumerate(out):
            if n.lower() == low:
                return i
        return -1

    for op, name, value in rules:
        low = name.lower()
        if op == "del":
            out[:] = [(n, v) for (n, v) in out if n.lower() != low]
        elif op == "add":
            out.append((name, value))
        else:  # set/override
            i = idx_of(name)
            if i >= 0:
                out[i] = (name, value)
                # quitar duplicados posteriores del mismo header
                out[:] = out[:i + 1] + [(n, v) for (n, v) in out[i + 1:]
                                        if n.lower() != low]
            else:
                out.append((name, value))
    return out


def apply_to_headers(base: List[Pair], text: str) -> List[Pair]:
    """Conveniencia: parsea `text` y lo aplica sobre `base`."""
    rules = parse_rules(text)
    return apply_to_pairs(base, rules) if rules else list(base)
