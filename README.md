# VoBB UE Simulator

Plataforma de prueba para dar de alta **dispositivos VoBB (Voice over BackBone) simulados**
y vincularlos a un **core IMS**. Cada abonado simulado se registra a un **P-CSCF** vía SIP
con autenticación **Digest MD5** (password en plano), y al recibir una llamada deja sonar el
**alerting** unos segundos, atiende y **devuelve el audio en modo eco**.

Incluye una **interfaz web de gestión** para dar de alta abonados, originar/cortar llamadas,
y visualizar en tiempo real la **señalización SIP** y las **estadísticas RTP** de cada abonado.

## Usuarios, login y permisos

La plataforma tiene **control de acceso**. Al abrir la web se pide login. Arranca con un
usuario administrador por defecto:

- **Usuario:** `admin` · **Contraseña:** `admin` (⚠️ cambiala en el primer acceso, o definí
  `ADMIN_USER` / `ADMIN_PASSWORD` por entorno antes del primer arranque).

El **administrador** puede crear usuarios, asignarles permisos y numeración:

- **Permisos** (para usuarios no admin): `Gestionar abonados`, `Controlar llamadas`,
  `Gestionar perfiles`. El admin tiene todo + gestión de usuarios.
- **Numeración por usuario**: se asignan números o rangos (ej. `1000-1099`, `+541148519500`).
  Un usuario solo ve/gestiona los abonados cuya línea cae en su numeración. La señalización
  SIP y las estadísticas RTP por WebSocket también se filtran por numeración.
- **Abonados compartidos**: si la misma línea entra en la numeración de varios usuarios,
  el abonado queda compartido entre ellos.

Autenticación por token firmado (HMAC, stateless) enviado como `Authorization: Bearer`; el
secreto se genera y persiste en el volumen de datos (`data/secret.key`).

## Perfiles

Un **Perfil** agrupa los parámetros de red/comportamiento compartidos (dominio, P-CSCF,
realm, registrar/Request-URI, codecs, alerting, eco, expires). Cada abonado puede
referenciar un perfil y **heredar** esos campos, definiendo solo sus datos propios
(línea/IMPU, IMPI, password). Así se dan de alta muchos usuarios con la misma
parametrización, y al **editar el perfil** se re-aplica (re-registra) a todos sus abonados.
Un abonado sin perfil ("personalizado") usa sus campos propios. La plataforma arranca con
un perfil por defecto (`Local (Kamailio)`) y 4 abonados asociados.

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
| `ADMIN_USER` | `admin` | usuario admin creado en el primer arranque |
| `ADMIN_PASSWORD` | `admin` | contraseña del admin inicial (cambiar) |
| `BIND_ADDR` | — | IP local para **todo** (SIP, RTP, relay, reg-event). Vacío: se autodetecta por la ruta al P-CSCF |
| `BIND_IFACE` | — | alternativa a `BIND_ADDR`: nombre de interfaz (`ens192`) |
| `MEDIA_PUBLIC_ADDR` | — | IP a anunciar en el SDP si hay NAT 1:1 delante |
| `SIP_RELAY` | `1` | relay SIP que unifica el flow de origen en `:5060`. `0` lo desactiva |
| `REG_EVENT_SUBSCRIBE` | `1` en `ims` | master switch del reg-event; el on/off fino es por abonado/perfil |
| `REG_EVENT_MIN_PERIOD` | `30` | piso (s) del refresh del SUBSCRIBE (evita tormenta si el Expires negociado es chico) |
| `REGISTER_STAGGER_MS` | `200` | espaciado (ms) entre cada REGISTER en el registro en masa |
| `CALL_HISTORY_MAX` | `2000` | tope de registros del histórico de llamadas (retención) |

### Arranque sin ráfaga de registros

Al levantar, las cuentas se **crean pero no se registran** (`registerOnAdd=False`): así se evita
la ráfaga de REGISTER simultáneos que satura el SBC/P-CSCF. El registro se dispara **a mano**
desde el **Dashboard** → *Registrar todos*, que emite los REGISTER **escalonados**
(`REGISTER_STAGGER_MS` entre cada uno). También están *Desregistrar todos* y *Colgar todas*, y
el botón por línea de siempre en la vista Abonados.

### Vistas

- **Dashboard** (principal): stat tiles (abonados, registrados, sin registrar, activas, ASR,
  motor), control general (registrar/desregistrar/colgar en masa), estado de red (admin) y
  resumen de llamadas.
- **Abonados:** control de llamada (originar), llamadas activas y lista de abonados.
- **Llamadas:** estadísticas (total, atendidas, fallidas, ASR, ACD, activas) e **histórico
  persistente** (tabla `CallRecord` en SQLite, sobrevive reinicios) con filtros MO/MT y
  atendida/fallida, RTP por llamada, y *Limpiar histórico* (admin).
- **Monitor, Perfiles, Usuarios:** como antes.

### Una sola interfaz de salida

En un host con varias interfaces, si los sockets quedan en `0.0.0.0` PJSIP publica en
`Via`/`Contact` y en el `c=` del SDP la IP de la **ruta por defecto**, que puede no ser la
interfaz por la que realmente sale el SIP: el resultado típico es SIP por una interfaz y RTP
esperado por otra, y un P-CSCF que descarta lo que no coincide con el flow del REGISTER.
Por eso la app resuelve **una** IP local (`BIND_ADDR` > `BIND_IFACE` > ruta hacia el P-CSCF) y
la usa para bindear y anunciar todo. Se ve en el log de arranque:

```
[net] IP local unificada: 10.20.30.40 (ruta hacia 10.20.30.1)
[sip] transporte :5070 bound=10.20.30.40 public=10.20.30.40:5060
[relay] EXT ('10.20.30.40', 5060)  INT ('10.20.30.40', 5062)
```

### Relay SIP

El P-CSCF ata el flow del abonado a la dupla (IP, puerto) de origen del REGISTER. Como PJSUA2
no puede emitir el SUBSCRIBE reg-event por su propio transporte, el relay (`app/sip_relay.py`)
se queda con `:5060` y pjsua sale a través suyo: REGISTER, INVITE, ACK/BYE y el SUBSCRIBE
comparten un único flow. El relay se anuncia con su propio `Via` y `Record-Route` para que ni
las respuestas ni los requests in-dialog de pjsua se escapen por su puerto interno (`:5070`).

## Perfiles y abonados

La config se organiza en dos niveles:

- **Perfil** (vista *Perfiles* → editor dedicado): concentra **toda** la config de red
  (dominio, P-CSCF addr/port/transporte, realm, registrar-URI), comportamiento (codecs G.711,
  alerting, eco, reg-expires), **reg-event** (on/off + periodo) y los **mensajes SIP por
  procedimiento** (headers de REGISTER/INVITE/SUBSCRIBE, editor de reglas con vista previa).
- **Abonado** (modal): solo **identidad** — display name, línea (IMPU), **número corto (MT)**,
  usuario/password Digest, habilitado — y **a qué perfil pertenece**. No edita parámetros de
  red: los hereda del perfil.

Detalles:

- **Número corto (MT):** al originar, la sugerencia de destino usa el número corto del abonado
  si está definido (p.ej. `line_number=+541112341234`, `short_number=12341234`); si no, la línea.
- **reg-event:** on/off por perfil y periodo (Expires del SUBSCRIBE, base del refresh). El
  refresh ocurre al 90% del Expires negociado, con piso `REG_EVENT_MIN_PERIOD` (30s por defecto)
  para que un Expires chico no genere tormenta de SUBSCRIBE.

### Headers SIP por procedimiento (mini-DSL)

Cada **perfil** lleva reglas de headers por procedimiento (REGISTER / INVITE / SUBSCRIBE),
editables en el editor de perfil como filas estructuradas (operación · header · valor) con
vista previa del mensaje resultante. Por detrás se serializan a un mini-DSL; vacío = headers por
defecto (comportamiento histórico). Una regla por línea:

```
Name: valor      # reemplaza el header Name (o lo agrega si no existía)
+Name: valor     # agrega OTRA instancia de Name (no reemplaza)
-Name            # quita el header Name
# comentario     # las líneas en blanco y las que empiezan con # se ignoran
```

Ejemplo (REGISTER de un UE LTE): `P-Access-Network-Info: 3GPP-E-UTRAN;utran-cell-id-3gpp=...`.
Límite de PJSUA2: en REGISTER/INVITE solo se tocan headers de extensión (Via/From/To/Call-ID/
CSeq/Contact/Expires los genera pjsip). En el SUBSCRIBE, que es un builder propio
(`app/reg_subscribe.py`), se puede overridear cualquiera (incluido Expires, User-Agent, Event).
Implementación en `app/sip_headers.py`.
