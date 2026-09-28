import io

from builders import nal, pes, source_packets, ts_packets
from ssif_demux import (BASE_PID, DEP_PID, SsifDemuxer, _drop_bd_delimiter, _pes_payload,
                        _strip_nals, _timestamp, demux)
from builders import ts_field


def test_timestamp_roundtrip() -> None:
    for ts in (0, 1, 90_000, (1 << 33) - 1, 0x1_2345_6789):
        assert _timestamp(ts_field(ts)) == ts


def test_pes_payload() -> None:
    assert _pes_payload(pes(b"abc", 1000, 900)) == (900, 1000, b"abc")
    assert _pes_payload(pes(b"abc", 1000)) == (1000, 1000, b"abc")     # no DTS: PTS
    assert _pes_payload(pes(b"abc", None)) is None                      # no timestamp
    assert _pes_payload(b"\x00\x00\x02\xe0" + b"\x00" * 20) is None
    assert _pes_payload(b"\x00\x00") is None


def test_strip_nals() -> None:
    au = nal(9) + nal(1, b"\x55" * 5) + nal(12, b"\xff\xff") + nal(10, b"") + nal(1, b"\x66")
    assert _strip_nals(au, {10, 11, 12}) == nal(9) + nal(1, b"\x55" * 5) + nal(1, b"\x66")
    plain = nal(1, b"\x77")
    assert _strip_nals(plain, {10, 11, 12}) is plain                    # fast path


def test_drop_bd_delimiter() -> None:
    rest = nal(15, b"\x01\x02") + nal(20, b"\x03")
    assert _drop_bd_delimiter(nal(24, b"\x00\x81") + rest) == rest      # keeps the 4-byte start
    assert _drop_bd_delimiter(nal(24, b"\x81", long_start=False) + nal(20, b"\x03", False)) \
        == nal(20, b"\x03", False)
    assert _drop_bd_delimiter(rest) == rest
    assert _drop_bd_delimiter(nal(24, b"\x81")) == b""
    assert _drop_bd_delimiter(b"") == b""


def frame(n: int, dts: int, base_cc: int, dep_cc: int) -> tuple[list[bytes], list[bytes]]:
    """TS packets of both views of frame n (payloads big enough to span packets)."""
    base = nal(9, b"\x10") + nal(5 if n == 0 else 1, bytes([n]) * 300)
    dep = nal(24, b"\x81") + nal(20, bytes([0x80 | n]) * 200)
    return (ts_packets(BASE_PID, pes(base, dts + 3000, dts), base_cc),
            ts_packets(DEP_PID, pes(dep, dts + 3000, dts, stream_id=0xE0), dep_cc))


def expected_au(n: int) -> bytes:
    return (nal(9, b"\x10") + nal(5 if n == 0 else 1, bytes([n]) * 300)
            + nal(20, bytes([0x80 | n]) * 200))


def stream(n_frames: int, start_dts: int = 90_000, extent: int = 3) -> bytes:
    """Frames in .ssif order: extents of `extent` frames, dependent before base."""
    packets: list[bytes] = []
    for first in range(0, n_frames, extent):
        base_pk: list[bytes] = []
        dep_pk: list[bytes] = []
        for n in range(first, min(first + extent, n_frames)):
            b, d = frame(n, start_dts + n * 3754, 0, 0)
            base_pk += b
            dep_pk += d
        packets += dep_pk + base_pk
    packets.append(ts_packets(0x1100, pes(b"audio", 1))[0])      # other PIDs are ignored
    return source_packets(packets)


def test_pairs_views_in_base_order() -> None:
    d = SsifDemuxer()
    data = stream(7)
    out = []
    for i in range(0, len(data), 1000):                 # chunks cut mid-packet
        out += list(d.feed(data[i:i + 1000]))
    out += list(d.flush())
    assert out == [expected_au(n) for n in range(7)]
    assert (d.frames, d.dropped) == (7, 0)


def test_start_pts_drops_frames_before_the_keyframe() -> None:
    dts = [90_000 + n * 3754 for n in range(6)]
    d = SsifDemuxer(start_pts=dts[3] + 3000 + 100)      # EP_map precision: low bits ignored
    out = list(d.feed(stream(6))) + list(d.flush())
    # frame 3's PTS matches once the low 9 bits are dropped
    assert out and out[0] == expected_au(3)


def test_base_frame_without_dependent_view_is_dropped_eventually() -> None:
    packets: list[bytes] = []
    for n in range(300):
        b, _ = frame(n % 200, n * 3754, 0, 0)
        packets += b
    d = SsifDemuxer()
    out = list(d.feed(source_packets(packets))) + list(d.flush())
    assert out == []
    assert d.dropped == 300


def test_demux_file_to_file() -> None:
    dst = io.BytesIO()
    d = demux(io.BytesIO(stream(4)), dst, chunk=777)
    assert dst.getvalue() == b"".join(expected_au(n) for n in range(4))
    assert d.frames == 4
