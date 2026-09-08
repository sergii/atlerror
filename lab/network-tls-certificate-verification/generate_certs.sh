#!/bin/sh
set -eu

mkdir -p /certs

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /certs/ca.key \
  -out /certs/ca.crt \
  -days 1 \
  -subj "/CN=Atlerror Lab CA" >/dev/null 2>&1

openssl req -newkey rsa:2048 -nodes \
  -keyout /certs/trusted.key \
  -out /certs/trusted.csr \
  -subj "/CN=trusted.atlerror.test" >/dev/null 2>&1
printf 'subjectAltName=DNS:trusted.atlerror.test\n' > /tmp/trusted.ext
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
  -addext "subjectAltName=DNS:untrusted.atlerror.test" >/dev/null 2>&1

rm -f /certs/trusted.csr /certs/ca.srl /tmp/trusted.ext
