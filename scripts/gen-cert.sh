#!/usr/bin/env bash
# Самоподписанный сертификат для локальных экспериментов.
# В проде используют Let's Encrypt / внутренний CA, а не это.
# Папку можно поменять: CERT_DIR=/certs bash scripts/gen-cert.sh
set -euo pipefail
DIR="${CERT_DIR:-certs}"
mkdir -p "$DIR"
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$DIR/server.key" -out "$DIR/server.crt" -days 365 \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,DNS:lb,IP:127.0.0.1"
echo "OK: $DIR/server.crt, $DIR/server.key"
