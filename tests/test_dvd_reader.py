from builders import SECTOR, pack_header, ps_pack
from dvd_reader import PackFilter, _shift, _timestamp, empty_subpicture_pack

ANCHOR = 900_000
AC3_1, AC3_2, SUB = 0x80, 0x81, 0x21


def pts_of(pack: bytes | bytearray) -> int:
    return _timestamp(pack, 14 + 9)


def test_shift() -> None:
    pack = bytearray(ps_pack(0xE0, 1000))
    _shift(pack, 14, 2000)
    assert pts_of(pack) == 3000
    _shift(pack, 14, -4000)
    assert pts_of(pack) == (1 << 33) - 1000


def test_empty_subpicture_pack() -> None:
    p = empty_subpicture_pack(pack_header(), SUB, 123_456)
    assert len(p) == SECTOR
    assert p[14:18] == b"\x00\x00\x01\xbd"
    assert pts_of(p) == 123_456
    assert p[14 + 9 + 5] == SUB                                  # substream after the header
    assert PackFilter(set(), ANCHOR, SUB).keep(p) == p


def test_video_always_audio_from_the_anchor_on() -> None:
    f = PackFilter({AC3_1}, ANCHOR)
    video = ps_pack(0xE0, ANCHOR - 3000)
    assert f.keep(video) == video
    assert f.keep(ps_pack(0xBD, ANCHOR - 1, substream=AC3_1)) is None     # before the frame
    assert f.keep(ps_pack(0xBD, None, substream=AC3_1)) is None           # no PTS yet
    first = ps_pack(0xBD, ANCHOR + 10, substream=AC3_1)
    assert f.keep(first) == first
    # once started, every packet of the track, with or without PTS
    assert f.keep(ps_pack(0xBD, None, substream=AC3_1)) is not None
    assert f.keep(ps_pack(0xBD, ANCHOR + 5000, substream=AC3_2)) is None  # not chosen
    assert f.keep(ps_pack(0xBE, None)) is None                            # padding
    assert f.keep(b"\x00" * SECTOR) is None                               # not a pack


def test_mpeg_audio_and_subpictures() -> None:
    f = PackFilter({0x1C0}, ANCHOR, SUB)
    mpa = ps_pack(0xC0, ANCHOR)
    assert f.keep(mpa) == mpa
    sub = ps_pack(0xBD, ANCHOR - 90_000, substream=SUB)
    assert f.keep(sub) == sub                    # subpictures are not held back
    assert f.keep(ps_pack(0xBD, ANCHOR, substream=0x22)) is None


def test_delta_moves_timestamps() -> None:
    f = PackFilter({AC3_1}, ANCHOR)
    f.delta = -7_000_000
    out = f.keep(ps_pack(0xBD, ANCHOR + 7_000_000 + 100, substream=AC3_1))
    assert out is not None and pts_of(out) == ANCHOR + 100
    video = f.keep(ps_pack(0xE0, ANCHOR + 7_000_000))
    assert video is not None and pts_of(video) == ANCHOR


def test_system_header_is_skipped() -> None:
    system_header = b"\x00\x00\x01\xbb" + (6).to_bytes(2, "big") + b"\x00" * 6
    pack = ps_pack(0xE0, ANCHOR)
    with_header = pack[:14] + system_header + pack[14:-len(system_header)]
    assert PackFilter(set(), ANCHOR).keep(with_header) == with_header
