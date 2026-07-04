from __future__ import annotations

import json
import socket
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

    try:
        payload = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ProtocolError("JSON payload must be an object")

    return payload


def connect_socket(path: Path, timeout: float = 5.0) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(path))
    return sock

