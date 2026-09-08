import socket
import ssl

HOST = "0.0.0.0"
PORT = 9443

context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_3
context.maximum_version = ssl.TLSVersion.TLSv1_3
context.verify_mode = ssl.CERT_REQUIRED
context.load_verify_locations(cafile="/certs/ca.crt")
context.load_cert_chain(certfile="/certs/server.crt", keyfile="/certs/server.key")

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((HOST, PORT))
    listener.listen(32)

    while True:
        raw, _ = listener.accept()
        try:
            with context.wrap_socket(raw, server_side=True) as tls_socket:
                data = tls_socket.recv(64)
                if data == b"ping":
                    tls_socket.sendall(b"ok")
        except (ssl.SSLError, OSError):
            try:
                raw.close()
            except OSError:
                pass
