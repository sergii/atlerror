import socket
import ssl

HOST = "0.0.0.0"
PORT = 9443
TRUSTED_NAME = "trusted.atlerror.test"
UNTRUSTED_NAME = "untrusted.atlerror.test"


def server_context(certfile, keyfile):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


trusted_context = server_context("/certs/trusted.crt", "/certs/trusted.key")
untrusted_context = server_context("/certs/untrusted.crt", "/certs/untrusted.key")


def choose_context(ssl_socket, server_name, _initial_context):
    if server_name == UNTRUSTED_NAME:
        ssl_socket.context = untrusted_context
    else:
        ssl_socket.context = trusted_context


trusted_context.set_servername_callback(choose_context)

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((HOST, PORT))
    listener.listen(32)

    while True:
        connection, _ = listener.accept()
        try:
            with trusted_context.wrap_socket(connection, server_side=True) as tls_connection:
                tls_connection.settimeout(1.0)
                try:
                    tls_connection.recv(64)
                except (TimeoutError, socket.timeout):
                    pass
                tls_connection.sendall(b"ok")
        except (ssl.SSLError, ConnectionError, OSError):
            try:
                connection.close()
            except OSError:
                pass
