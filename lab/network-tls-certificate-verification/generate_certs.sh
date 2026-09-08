#!/bin/sh
set -eu

mkdir -p /certs

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /certs/ca.key \
  -out /certs/ca.crt \
  -days 1 \
  -subj "/CN=Atlerror Lab CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1

openssl req -newkey rsa:2048 -nodes \
  -keyout /certs/trusted.key \
  -out /certs/trusted.csr \
  -subj "/CN=trusted.atlerror.test" >/dev/null 2>&1
cat > /tmp/trusted.ext <<'EOF'
subjectAltName=DNS:trusted.atlerror.test
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
authorityKeyIdentifier=keyid,issuer
subjectKeyIdentifier=hash
EOF
openssl x509 -req \
  -in /certs/trusted.csr \
  -CA /certs/ca.crt \
  -CAkey /certs/ca.key \
  -CAcreateserial \
  -out /certs/trusted.crt \
  -days 1 \
  -sha256 \
  -extfile /tmp/trusted.ext >/dev/null 2>&1

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /certs/untrusted.key \
  -out /certs/untrusted.crt \
  -days 1 \
  -subj "/CN=untrusted.atlerror.test" \
  -addext "subjectAltName=DNS:untrusted.atlerror.test" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth" >/dev/null 2>&1

rm -f /certs/trusted.csr /certs/ca.srl /tmp/trusted.ext
