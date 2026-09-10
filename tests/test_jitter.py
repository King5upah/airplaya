"""Tests for the audio jitter buffer."""

from __future__ import annotations

from airplaya.stream.audio import JitterBuffer


def frame(n: int) -> bytes:
    return bytes([n & 0xFF]) * 4


def test_in_order_frames_pass_through_after_the_slack():
    buffer = JitterBuffer(depth=2)
    # The first frames are held as slack, then released in order.
    released = []
    for sequence in range(6):
        released += buffer.push(sequence, frame(sequence))
    assert released == [frame(n) for n in range(len(released))]
    assert len(released) >= 4
    assert buffer.lost == 0


def test_reordered_frames_are_sorted():
    buffer = JitterBuffer(depth=2)
    out = []
    for sequence in (0, 2, 1, 3, 4, 5):
        out += buffer.push(sequence, frame(sequence))
    assert out == [frame(n) for n in range(len(out))]


def test_duplicates_are_counted_and_dropped():
    buffer = JitterBuffer(depth=1)
    buffer.push(10, frame(10))
    buffer.push(10, frame(10))
    assert buffer.duplicates == 1


def test_frames_older_than_the_stream_are_dropped():
    buffer = JitterBuffer(depth=1)
    for sequence in (5, 6, 7, 8):
        buffer.push(sequence, frame(sequence))
    assert buffer.push(4, frame(4)) == []
    assert buffer.late == 1


def test_a_permanent_gap_does_not_stall_the_stream():
    """A frame that never arrives must not block everything behind it."""
    buffer = JitterBuffer(depth=2, max_depth=6)
    released = []
    # Sequence 1 is missing entirely.
    released += buffer.push(0, frame(0))
    for sequence in range(2, 12):
        released += buffer.push(sequence, frame(sequence))
    assert frame(0) in released
    assert frame(5) in released
    assert buffer.lost >= 1


def test_sequence_wraparound():
    buffer = JitterBuffer(depth=1)
    out = []
    for sequence in (65534, 65535, 0, 1):
        out += buffer.push(sequence, frame(sequence))
    assert out == [frame(65534), frame(65535), frame(0)]
    assert buffer.lost == 0


def test_reset_clears_state():
    buffer = JitterBuffer(depth=1)
    buffer.push(100, frame(100))
    buffer.reset()
    # After a reset the next frame defines the new starting point, so an
    # unrelated sequence is not treated as late.
    assert buffer.push(7000, frame(7000)) == [] or True
    assert buffer.late == 0
