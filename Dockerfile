# ---------------------------------------------------------------------------
# VoBB UE Simulator — imagen con PJSUA2 compilado desde fuente.
#
# Multi-arch: compila pjproject nativo para la arquitectura del host que hace
# el build (arm64 en macOS, amd64 en la VM Linux). No usar `platform:` en el
# compose para que cada host construya en su arch nativa (build-on-target).
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm

ARG PJPROJECT_VERSION=2.15.1

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# --- toolchain + dependencias de PJSIP (audio/SSL) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl ca-certificates \
        swig \
        python3-dev \
        libssl-dev \
        libasound2-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# --- compilar pjproject + bindings pjsua2 de Python ---
RUN cd /tmp \
    && curl -fsSL -o pjproject.tgz \
        https://github.com/pjsip/pjproject/archive/refs/tags/${PJPROJECT_VERSION}.tar.gz \
    && tar xzf pjproject.tgz \
    && cd pjproject-${PJPROJECT_VERSION} \
    # Subir límites por defecto de PJSUA: 8 cuentas es muy poco para simular
    # muchos abonados. config_site.h aplica a toda la compilación.
    && printf '%s\n' \
        '#define PJSUA_MAX_ACC 512' \
        '#define PJSUA_MAX_CALLS 512' \
        '#define PJMEDIA_CONF_MAX_PORTS 1200' \
        > pjlib/include/pj/config_site.h \
    && export CFLAGS="-fPIC -O2 -DPJ_HAS_IPV6=1" \
    && ./configure --enable-shared --disable-video --disable-sound \
    && make dep && make \
    && make install \
    && ldconfig \
    && cd pjsip-apps/src/swig \
    && make python \
    && cd python \
    && python3 -m pip install . \
    && cd /tmp && rm -rf pjproject-${PJPROJECT_VERSION} pjproject.tgz

# --- smoke test: si el binding no importa, el build falla acá ---
RUN python3 -c "import pjsua2; print('pjsua2 OK')"

WORKDIR /srv
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8080 \
    DB_PATH=/srv/data/vobb.db

EXPOSE 8080 5060/udp
VOLUME ["/srv/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
