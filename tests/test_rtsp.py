"""Tests for the control-channel message layer."""

from __future__ import annotations

import io

import pytest

from airplaya.rtsp.message import ProtocolError, Response, read_request


def parse(raw: bytes):
    return read_request(io.BytesIO(raw))


def test_parses_a_request_with_a_body():
    request = parse(
        b"POST /fp-setup RTSP/1.0\r\n"
        b"CSeq: 4\r\n"
        b"Content-Length: 5\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"\r\n"
        b"hello"
    )
    assert request is not None
    assert (request.method, request.url, request.protocol) == (
        "POST",
        "/fp-setup",
        "RTSP/1.0",
    )
    assert request.header("cseq") == "4"
    assert request.body == b"hello"


def test_header_lookup_is_case_insensitive():
    request = parse(b"OPTIONS * RTSP/1.0\r\nCSEQ: 1\r\n\r\n")
    assert request is not None
    assert request.header("CSeq") == "1"


def test_path_drops_the_query_string():
    request = parse(b"GET /info?txtAirPlay RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    assert request is not None
    assert request.path == "/info"


def test_end_of_stream_returns_none():
    assert parse(b"") is None


def test_truncated_body_is_an_error():
    """A short read must fail loudly rather than hand a handler half a plist."""
    with pytest.raises(ProtocolError):
        parse(b"SETUP rtsp://x RTSP/1.0\r\nContent-Length: 10\r\n\r\nshort")


def test_malformed_request_line_is_an_error():
    with pytest.raises(ProtocolError):
        parse(b"NONSENSE\r\n\r\n")


def test_response_always_carries_a_content_length():
    serialized = Response().serialize()
    assert b"Content-Length: 0" in serialized


def test_response_body_and_content_type():
    response = Response()
    response.add_header("CSeq", "7")
    response.set_body(b"\x01\x02", "application/octet-stream")
    serialized = response.serialize()
    assert serialized.startswith(b"RTSP/1.0 200 OK\r\n")
    assert b"CSeq: 7\r\n" in serialized
    assert b"Content-Type: application/octet-stream\r\n" in serialized
    assert b"Content-Length: 2\r\n" in serialized
    assert serialized.endswith(b"\r\n\r\n\x01\x02")
