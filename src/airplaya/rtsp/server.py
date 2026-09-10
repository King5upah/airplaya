"""The control-channel server.

One thread per connection, one `Session` per connection. Requests are routed by
`(method, path)`; the CSeq header is echoed back, which the client uses to match
responses.

A handler that raises does not kill the receiver: the client gets a 500 and the
connection stays up unless the failure was in parsing the stream itself, which
is unrecoverable and closes it.
"""

from __future__ import annotations

import socketserver
import sys
import threading
from typing import Callable

from airplaya.crypto.fairplay import FairPlayError
from airplaya.crypto.pairing import PairingError
from airplaya.crypto.playfair import PlayfairUnavailable
from airplaya.log import get_logger
from airplaya.rtsp import handlers
from airplaya.rtsp.message import ProtocolError, Request, Response, read_request
from airplaya.session import Session

log = get_logger(__name__)

Handler = Callable[[Session, Request, Response], None]


class RtspServer(socketserver.ThreadingTCPServer):
    # See `bind_exclusive`: on Windows, address reuse lets a stale receiver
    # keep serving while a new one silently binds the same port.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def __init__(self, address: tuple[str, int], receiver) -> None:
        self.receiver = receiver
        super().__init__(address, RtspConnection)

    def handle_error(self, request, client_address) -> None:
        log.exception("unhandled error while serving %s", client_address)


class RtspConnection(socketserver.StreamRequestHandler):
    # Without this, a client that stops talking pins a thread forever.
    timeout = 300

    def setup(self) -> None:
        super().setup()
        self.session = Session(
            identity=self.server.receiver.identity,
            client_address=self.client_address[0],
        )
        threading.current_thread().name = f"rtsp-{self.client_address[0]}"

    def handle(self) -> None:
        log.info("control connection from %s", self.client_address[0])
        try:
            while True:
                try:
                    request = read_request(self.rfile)
                except ProtocolError as exc:
                    log.error("closing connection: %s", exc)
                    return
                if request is None:
                    return

                response = self._dispatch(request)
                self.wfile.write(response.serialize())
                self.wfile.flush()
                if response.close_connection:
                    return
        except (ConnectionResetError, BrokenPipeError, TimeoutError) as exc:
            log.info("control connection ended: %s", type(exc).__name__)
        finally:
            log.info("control connection from %s closed", self.client_address[0])

    def _dispatch(self, request: Request) -> Response:
        response = Response(protocol=request.protocol or "RTSP/1.0")

        # RECORD is the one method whose response the client matches without a
        # CSeq echo; every other request expects it back.
        cseq = request.header("cseq")
        if cseq is not None and request.method != "RECORD":
            response.add_header("CSeq", cseq)
        response.add_header("Server", "AirTunes/220.68")

        handler = self._resolve(request)
        if handler is None:
            log.warning("no handler for %s %s", request.method, request.url)
            response.status, response.reason = 404, "Not Found"
            return response

        log.debug("%s %s", request.method, request.url)
        try:
            handler(request, response)
        except PlayfairUnavailable as exc:
            # Without the native library nothing downstream can work, so make
            # the reason obvious rather than letting decryption fail silently.
            log.error("FairPlay key decryption unavailable: %s", exc)
            response.status, response.reason = 500, "Internal Server Error"
            response.close_connection = True
        except (PairingError, FairPlayError) as exc:
            log.error("handshake failed on %s %s: %s", request.method, request.url, exc)
            response.status, response.reason = 400, "Bad Request"
            response.close_connection = True
        except Exception:
            log.exception("handler for %s %s failed", request.method, request.url)
            response.status, response.reason = 500, "Internal Server Error"
        return response

    def _resolve(self, request: Request) -> Callable[[Request, Response], None] | None:
        session = self.session
        receiver = self.server.receiver
        method, path = request.method, request.path

        if method == "GET" and path == "/info":
            return lambda req, res: handlers.handle_info(
                session, req, res, receiver, receiver.config, receiver.hw_addr
            )
        if method == "POST":
            post_routes: dict[str, Handler] = {
                "/pair-setup": handlers.handle_pair_setup,
                "/pair-verify": handlers.handle_pair_verify,
                "/fp-setup": handlers.handle_fp_setup,
                "/feedback": handlers.handle_feedback,
                "/audioMode": handlers.handle_feedback,
            }
            handler = post_routes.get(path)
            if handler is not None:
                return lambda req, res: handler(session, req, res)
        if method == "SETUP":
            return lambda req, res: handlers.handle_setup(session, req, res, receiver)
        if method == "TEARDOWN":
            return lambda req, res: handlers.handle_teardown(session, req, res, receiver)

        simple: dict[str, Handler] = {
            "OPTIONS": handlers.handle_options,
            "GET_PARAMETER": handlers.handle_get_parameter,
            "SET_PARAMETER": handlers.handle_set_parameter,
            "RECORD": handlers.handle_record,
            # FLUSH only matters for buffered audio, which we do not play.
            "FLUSH": handlers.handle_feedback,
        }
        handler = simple.get(method)
        if handler is not None:
            return lambda req, res: handler(session, req, res)
        return None
