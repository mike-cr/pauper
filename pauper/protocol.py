from __future__ import annotations

import errno
import json
import select
import socket
import time
from pathlib import Path
from typing import Any


class ProtocolError(RuntimeError):
    pass


def send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    sock.sendall(data)


def recv_json_line(file_obj) -> dict[str, Any]:
    line = file_obj.readline()
    if not line:
        raise ProtocolError("connection closed")

    return parse_json_line(line)


def recv_json_line_socket(sock: socket.socket, timeout: float) -> tuple[dict[str, Any], bytes]:
    line, rest = recv_until_newline(sock, timeout)
    return parse_json_line(line), rest


def parse_json_line(line: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ProtocolError("JSON payload must be an object")

    return payload


def recv_until_newline(sock: socket.socket, timeout: float) -> tuple[bytes, bytes]:
    deadline = time.monotonic() + timeout
    chunks = []
    while True:
        data = recv_available(sock, deadline, 4096)
        chunks.append(data)
        if b"\n" in data:
            joined = b"".join(chunks)
            line, _separator, rest = joined.partition(b"\n")
            return line + b"\n", rest


def recv_exactly(sock: socket.socket, size: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks = []
    remaining = size
    while remaining > 0:
        data = recv_available(sock, deadline, remaining)
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def recv_available(sock: socket.socket, deadline: float, size: int) -> bytes:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("pauperd did not respond before the timeout")

        readable, _writable, _errors = select.select([sock], [], [], remaining)
        if not readable:
            raise TimeoutError("pauperd did not respond before the timeout")

        try:
            data = sock.recv(size)
        except BlockingIOError as exc:
            if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                continue
            raise

        if not data:
            raise ProtocolError("connection closed")
        return data


def connect_socket(path: Path, timeout: float = 5.0) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(path))
    return sock
