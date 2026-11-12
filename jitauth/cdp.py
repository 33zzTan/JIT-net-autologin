"""极简 Chrome DevTools Protocol 客户端（纯标准库 WebSocket）。

仅在 Selenium 不可用时作为兜底使用：连接浏览器调试端口，执行 JS。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.parse


class CDPError(Exception):
    pass


class CDPClient:
    """最小可用的 CDP over WebSocket 客户端（阻塞式）。"""

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._id = 0
        self._buf = b""

    # -- 连接 -------------------------------------------------------------- #
    def connect(self) -> None:
        parsed = urllib.parse.urlparse(self.ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        sock = socket.create_connection((host, port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(handshake.encode())

        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket 握手时连接被关闭")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status_line:
            raise CDPError(f"WebSocket 握手失败：{status_line}")
        self.sock = sock
        self._buf = rest

    # -- 帧读写 ------------------------------------------------------------ #
    def _recv_exact(self, count: int) -> bytes:
        while len(self._buf) < count:
            chunk = self.sock.recv(65536)  # type: ignore[union-attr]
            if not chunk:
                raise CDPError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def _send_frame(self, payload: bytes) -> None:
        header = bytearray()
        header.append(0x81)  # FIN + text
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack(">H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)  # type: ignore[union-attr]

    def _recv_frame(self) -> str:
        while True:
            first = self._recv_exact(2)
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            data = self._recv_exact(length) if length else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            if opcode == 0x8:  # close
                raise CDPError("浏览器关闭了调试连接")
            if opcode == 0x9:  # ping
                continue
            if opcode in (0x1, 0x2, 0x0):
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError:
                    return data.decode("utf-8", errors="replace")

    # -- 调用 -------------------------------------------------------------- #
    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        message = {"id": self._id, "method": method}
        if params:
            message["params"] = params
        self._send_frame(json.dumps(message).encode())

        while True:
            raw = self._recv_frame()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if payload.get("id") != self._id:
                continue  # 事件消息，忽略
            if "error" in payload:
                raise CDPError(f"{method} 失败：{payload['error']}")
            return payload.get("result", {})

    def evaluate(self, expression: str, timeout: float = 20.0) -> dict:
        return self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": int(timeout * 1000),
            },
        )

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self) -> "CDPClient":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
