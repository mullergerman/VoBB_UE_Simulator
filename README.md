# VoBB UE Simulator

Plataforma de prueba para dar de alta **dispositivos VoBB (Voice over BackBone) simulados**
y vincularlos a un **core IMS**. Cada abonado simulado se registra a un **P-CSCF** vía SIP
con autenticación **Digest MD5** (password en plano), y al recibir una llamada deja sonar el
**alerting** unos segundos, atiende y **devuelve el audio en modo eco**.

Incluye una **interfaz web de gestión** para dar de alta abonados, originar/cortar llamadas,
y visualizar en tiempo real la **señalización SIP** y las **estadísticas RTP** de cada abonado.

## Arquitectura

- **Backend** Python (FastAPI) con un único `pjsua2.Endpoint` que aloja **un `Account` por abonado**.
- **Motor SIP/media**: PJSIP / PJSUA2 (compilado desde fuente en la imagen).
- **Web**: SPA estática (REST + WebSocket) servida por el backend.
- **Registrar local**: Kamailio embebido para el modo UE-a-UE sin core IMS.
- **Persistencia**: SQLite (config de abonados).

```
Web (SPA) ──REST/WS──► FastAPI ──► PjsuaManager ──► pjsua2.Endpoint
                                                     ├─ Account 1001
                                                     ├─ Account 1002 ...
   MODO local ─► Kamailio (registrar+proxy)   MODO ims ─► P-CSCF real
```

## Modo LOCAL — test UE-a-UE en este host (macOS o Linux)

Levanta el backend + web + registrar Kamailio embebido. Los 4 abonados por defecto
(`1001`–`1004`) se registran contra el registrar local y pueden llamarse entre sí.
El media (RTP) es interno al contenedor, así que funciona en Docker Desktop de macOS
sin host-networking.

```bash
docker compose --profile local up --build
```

Luego abrir **http://localhost:8080**:

1. Verificar que los 4 abonados aparecen **registrados** (punto verde).
2. En *Control de llamadas*: origen `1001` → destino `1002` → **Llamar**.
3. Observar en *Detalle en vivo* la señalización SIP (`INVITE / 100 / 180` — alerting
   ~3 s — luego `200 OK`) y las estadísticas RTP crecer.
4. La llamada queda en eco (el origen escucha su propio audio). **Colgar** desde la web.

> El registrar local **no desafía Digest** (acepta el REGISTER directamente); sólo enruta
> por número. El flujo Digest real (`401` → `Authorization` → `200`) se ejercita en modo IMS.

## Modo IMS — vinculación con P-CSCF real (VM Linux x86_64)

Migración transparente: **copiar este repo a la VM** y construir ahí (build-on-target,
sin emulación ni cambios de código).

```bash
# en la VM Linux (amd64):
docker compose --profile ims up --build
```

Usa `network_mode: host` para que SIP/RTP salgan reales hacia el P-CSCF. Luego, desde
la web (http://<vm-ip>:8080), **editar cada abonado** y configurar:

- **P-CSCF addr / port / transporte** del core IMS real,
- **Dominio** (home domain / realm),
- **Auth user (IMPI)** y **password** Digest en plano,
- número de línea, codecs, delay de alerting, eco.

Al guardar/registrar, verificar en la consola SIP el intercambio Digest
(`REGISTER` → `401 Unauthorized` → `REGISTER` con `Authorization` → `200 OK`).

## Portabilidad cross-arch

La imagen **compila pjproject desde fuente**, por lo que es arquitectura-agnóstica: en macOS
(arm64) y en la VM (amd64) se compila nativo al hacer `docker compose build`. No se fija
`platform:` en el compose para evitar emulación. Para distribuir una imagen ya construida:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/vobb-ue-sim:latest --push .
```

## Desarrollo sin Docker (sólo web / CRUD)

Para iterar la interfaz sin compilar pjsua2, se puede correr el backend con el motor SIP
deshabilitado (la web y el CRUD funcionan; no hay registro ni llamadas):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SIP_DISABLED=1 .venv/bin/uvicorn app.main:app --port 8080
```

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `MODE` | `local` | `local` (registrar embebido) o `ims` (P-CSCF real) |
| `HTTP_PORT` | `8080` | puerto de la interfaz web |
| `SIP_PORT` | `5060` | puerto SIP del endpoint |
| `SIP_TRANSPORT` | `udp` | `udp` o `tcp` |
| `RTP_PORT_START` | `4000` | inicio del rango de puertos RTP |
| `PJSUA_LOG_LEVEL` | `4` | nivel de log PJSIP (≥4 vuelca mensajes SIP) |
| `LOCAL_REGISTRAR` | `127.0.0.1` | P-CSCF del seed inicial (modo local: `registrar`) |
| `LOCAL_DOMAIN` | `vobb.test` | dominio del seed inicial |
| `SIP_DISABLED` | — | `1` para arrancar sin motor SIP (sólo web) |

## Modelo de abonado

Cada abonado configura: número de línea, dominio, P-CSCF (addr/port/transporte), usuario y
password Digest (plano), realm, codecs (G.711 PCMU/PCMA), delay de alerting, eco on/off y
expires del registro.
