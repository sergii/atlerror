import socket
import struct

HOST = "0.0.0.0"
PORT = 5353
KNOWN_NAME = "known.atlerror.test."
KNOWN_IP = "10.20.30.40"


def decode_qname(payload, offset=12):
    labels = []
    while True:
        length = payload[offset]
        offset += 1
        if length == 0:
            break
        labels.append(payload[offset : offset + length].decode("ascii"))
        offset += length
    return ".".join(labels) + ".", offset


def response_for(query):
    txid, flags, qdcount, _, _, _ = struct.unpack("!HHHHHH", query[:12])
    if qdcount != 1:
        return b""

    qname, offset = decode_qname(query)
    question_end = offset + 4
    question = query[12:question_end]
    recursive_desired = flags & 0x0100
    base_flags = 0x8000 | 0x0400 | recursive_desired

    if qname.lower() == KNOWN_NAME:
        header = struct.pack("!HHHHHH", txid, base_flags, 1, 1, 0, 0)
        answer = (
            b"\xc0\x0c"
            + struct.pack("!HHIH", 1, 1, 60, 4)
            + socket.inet_aton(KNOWN_IP)
        )
        return header + question + answer

    header = struct.pack("!HHHHHH", txid, base_flags | 0x0003, 1, 0, 0, 0)
    return header + question


with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
    server.bind((HOST, PORT))
    while True:
        payload, address = server.recvfrom(2048)
        if payload == b"health":
            server.sendto(b"ok", address)
            continue
        try:
            response = response_for(payload)
        except Exception:
            response = b""
        if response:
            server.sendto(response, address)
