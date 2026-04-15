#!/usr/bin/env bash

set -euo pipefail

CERT_DIR=".certs"
CERT_FILE="${CERT_DIR}/localhost.pem"
KEY_FILE="${CERT_DIR}/localhost-key.pem"

mkdir -p "${CERT_DIR}"

if command -v mkcert >/dev/null 2>&1; then
  echo "mkcert detected. Generating trusted localhost certificate..."
  mkcert -install
  mkcert -key-file "${KEY_FILE}" -cert-file "${CERT_FILE}" localhost 127.0.0.1 ::1
  echo "Certificate written to ${CERT_FILE}"
  echo "Key written to ${KEY_FILE}"
  exit 0
fi

echo "mkcert not found. Falling back to self-signed certificate via openssl..."
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout "${KEY_FILE}" \
  -out "${CERT_FILE}" \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1"

echo "Self-signed certificate written to ${CERT_FILE}"
echo "Key written to ${KEY_FILE}"
echo "Tip: install mkcert for trusted local certs without browser warnings."
