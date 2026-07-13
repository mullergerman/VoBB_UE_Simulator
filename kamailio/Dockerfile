# Registrar/proxy Kamailio para el modo local. Se construye desde Debian, que
# publica el paquete kamailio para amd64 y arm64 -> imagen multi-arch nativa.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        kamailio \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 5060/udp
CMD ["kamailio", "-DD", "-E", "-f", "/etc/kamailio/kamailio.cfg"]
