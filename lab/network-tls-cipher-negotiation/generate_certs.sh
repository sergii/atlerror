#!/bin/sh
set -eu

mkdir -p /certs

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /certs/ca.key \
  -out /certs/ca.crt \
  -days 1 \
  -subj "/CN=Atlerror N3.5 Lab CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1

openssl req -newkey rsa:2048 -nodes \
  -keyout /certs/server.key \
  -out /certs/server.csr \
  -subj "/CN=secure.atlerror.test" >/dev/null 2>&1

cat > /tmp/server.ext <<'EOF'
subjectAltName=DNS:secure.atlerror.test
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
authorityKeyIdentifier=keyid,issuer
subjectKeyIdentifier=hash
EOF

openssl x509 -req \
  -in /certs/server.csr \
  -CA /certs/ca.crt \
  -CAkey /certs/ca.key \
  -CAcreateserial \
  -out /certs/server.crt \
  -days 1 \
  -sha256 \
  -extfile /tmp/server.ext >/dev/null 2>&1

rm -f /certs/server.csr /certs/ca.srl /tmp/server.ext
