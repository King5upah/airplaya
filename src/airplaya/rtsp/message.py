"""RTSP request parsing and response building.

The control channel is RTSP/1.0 in shape but the client also sends plain
HTTP/1.1 requests down the same socket, so this parser stays deliberately
lenient: read a request line, read headers, read exactly `Content-Length`
bytes of body, and let the handlers decide what is meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO

MAX_LINE = 8192
MAX_BODY = 8 * 1024 * 1024


class ProtocolError(Exception):
    """The peer sent something we cannot parse; the connection must close."""


@dataclass
class Request:
    method: str
    url: str
    protocol: str
    headers: dict[str, str]  # keys are lowercased
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def content_type(self) -> str:
        return self.header("content-type") or ""

    @property
    def is_binary_plist(self) -> bool:
        return "application/x-apple-binary-plist" in self.content_type

    @property
    def path(self) -> str:
        """The URL without its query string."""
        return self.url.split("?", 1)[0]


@dataclass
class Response:
    status: int = 200
    reason: str = "OK"
    protocol: str = "RTSP/1.0"
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    close_connection: bool = False

    def add_header(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def set_body(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.add_header("Content-Type", content_type)

    def serialize(self) -> bytes:
        lines = [f"{self.protocol} {self.status} {self.reason}"]
        headers = list(self.headers)
        # Clients hang waiting for a body length even when the body is empty.
        if not any(name.lower() == "content-length" for name, _ in headers):
            headers.append(("Content-Length", str(len(self.body))))
        lines += [f"{name}: {value}" for name, value in headers]
        head = "\r\n".join(lines) + "\r\n\r\n"
        return head.encode("utf-8", "replace") + self.body


def _read_line(stream: BinaryIO) -> bytes:
    line = stream.readline(MAX_LINE)
    if len(line) >= MAX_LINE and not line.endswith(b"\n"):
        raise ProtocolError("request line or header too long")
    return line


def read_request(stream: BinaryIO) -> Request | None:
    """Read one request. Returns `None` on a clean end of stream."""
    line = _read_line(stream)
    if not line:
        return None
    # Tolerate stray blank lines between requests.
    while line in (b"\r\n", b"\n"):
        line = _read_line(stream)
        if not line:
            return None

    parts = line.decode("utf-8", "replace").rstrip("\r\n").split(" ")
    if len(parts) != 3:
        raise ProtocolError(f"malformed request line: {line!r}")
    method, url, protocol = parts

    headers: dict[str, str] = {}
    while True:
        line = _read_line(stream)
        if not line:
            raise ProtocolError("connection closed inside headers")
        if line in (b"\r\n", b"\n"):
            break
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        name, _, value = text.partition(":")
        if not _:
            raise ProtocolError(f"malformed header: {text!r}")
        headers[name.strip().lower()] = value.strip()

    body = b""
    raw_length = headers.get("content-length")
    if raw_length:
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ProtocolError(f"bad Content-Length: {raw_length!r}") from exc
        if length < 0 or length > MAX_BODY:
            raise ProtocolError(f"refusing a {length}-byte body")
        body = stream.read(length) or b""
        if len(body) != length:
            raise ProtocolError(
                f"body truncated: wanted {length} bytes, got {len(body)}"
            )

    return Request(method.upper(), url, protocol, headers, body)
