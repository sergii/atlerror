#!/bin/sh
set -eu

mkdir -p /certs

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /certs/ca.key \
  -out /certs/ca.crt \
  -days 1 \
  -subj "/CN=Atlerror N3.6 Lab CA" \
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

openssl req -newkey rsa:2048 -nodes \
  -keyout /certs/client.key \
  -out /certs/client.csr \
  -subj "/CN=atlerror-client" >/dev/null 2>&1

cat > /tmp/client.ext <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
authorityKeyIdentifier=keyid,issuer
subjectKeyIdentifier=hash
EOF

openssl x509 -req \
  -in /certs/client.csr \
  -CA /certs/ca.crt \
  -CAkey /certs/ca.key \
  -CAcreateserial \
  -out /certs/client.crt \
  -days 1 \
  -sha256 \
  -extfile /tmp/client.ext >/dev/null 2>&1

# A second, independent CA signs an otherwise valid clientAuth certificate.
# The server does not trust this CA; N3.6-L2 uses it to isolate client-certificate trust-anchor mismatch.
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /certs/rogue-ca.key \
  -out /certs/rogue-ca.crt \
  -days 1 \
  -subj "/CN=Atlerror N3.6 Rogue CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1

openssl req -newkey rsa:2048 -nodes \
  -keyout /certs/wrong-ca-client.key \
  -out /certs/wrong-ca-client.csr \
  -subj "/CN=atlerror-client" >/dev/null 2>&1

openssl x509 -req \
  -in /certs/wrong-ca-client.csr \
  -CA /certs/rogue-ca.crt \
  -CAkey /certs/rogue-ca.key \
  -CAcreateserial \
  -out /certs/wrong-ca-client.crt \
  -days 1 \
  -sha256 \
  -extfile /tmp/client.ext >/dev/null 2>&1

rm -f \
  /certs/server.csr \
  /certs/client.csr \
  /certs/wrong-ca-client.csr \
  /certs/ca.srl \
  /certs/rogue-ca.srl \
  /tmp/server.ext \
  /tmp/client.ext
