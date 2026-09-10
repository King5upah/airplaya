"""A small control channel, so a front end can drive the receiver while it runs.

Newline-delimited JSON over loopback TCP. One object per line in, one reply per
line out:

```
{"command": "record", "path": "C:/.../clip.mp4"}   -> {"ok": true, "path": "..."}
{"command": "stop_record"}                          -> {"ok": true, "clip": {...}}
{"command": "status"}                                -> {"ok": true, "recording": false, ...}
```

Bound to 127.0.0.1 only. This accepts commands with no authentication, so it
must never be reachable from the network; the port is ephemeral and printed in
the log for the front end that spawned the process to pick up.
"""

from __future__ import annotations

import json
import socketserver
import threading
from typing import Any, Callable

from airplaya.log import get_logger

log = get_logger(__name__)

CommandHandler = Callable[[dict[str, Any]], dict[str, Any]]


class ControlServer:
    """Serves control commands on a loopback port."""

    def __init__(self, handler: CommandHandler) -> None:
        self._handler = handler
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        """Bind an ephemeral loopback port and serve. Returns the port."""
        if self._server is not None:
            return self._server.server_address[1]

        self._server = _Server(("127.0.0.1", 0), self._handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="control", daemon=True
        )
        self._thread.start()
        port = self._server.server_address[1]
        # The front end reads this line to find the port.
        log.info("control channel listening on port %d", port)
        return port

    def stop(self) -> None:
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: CommandHandler) -> None:
        self.command_handler = handler
        super().__init__(address, _Connection)

    def handle_error(self, request, client_address) -> None:
        log.exception("error while serving a control command")


class _Connection(socketserver.StreamRequestHandler):
    timeout = 3600

    def handle(self) -> None:
        log.debug("control client connected")
        try:
            for raw in self.rfile:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                self.wfile.write(self._respond(line).encode("utf-8") + b"\n")
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass
        finally:
            log.debug("control client disconnected")

    def _respond(self, line: str) -> str:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("expected an object")
        except ValueError as exc:
            return json.dumps({"ok": False, "error": f"bad command: {exc}"})

        try:
            result = self.server.command_handler(message)
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the receiver
            log.warning("control command %r failed: %s", message.get("command"), exc)
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, **result})
