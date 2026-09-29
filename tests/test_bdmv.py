from types import TracebackType
from typing import Self

import pytest

import bdmv
from bdmv import (
    AACS_UNIT_PACKETS,
    SOURCE_PACKET,
    AudioStream,
    Clip,
    EmulatedSsif,
    PlayItem,
    SubStream,
    m2ts_seek,
    open_ssif,
    parse_clpi,
    parse_mpls,
    ssif_seek,
)
from builders import Item, Stream, clpi, mpls

UNIT = AACS_UNIT_PACKETS * SOURCE_PACKET


def test_parse_mpls_items_audio_subs() -> None:
    items = parse_mpls(mpls([
        Item("00131", 900_000, 4_500_000,
             audio=[Stream(0x1100, "eng", 0x86), Stream(0x1101, "fre", 0x81),
                    Stream(0x1102, "ger", 0x99)],
             pgs=[Stream(0x1200, "ita", 0x90), Stream(0x1201, "eng", 0x91)]),
        Item("00133", 45_000, 90_000),
    ]))
    assert [(i.clip, i.in_time, i.out_time) for i in items] == [
        ("00131", 900_000, 4_500_000), ("00133", 45_000, 90_000)]
    assert items[0].duration == 80.0
    assert items[0].audio == [AudioStream(0x1100, "dts-hd ma", "eng"),
                              AudioStream(0x1101, "ac3", "fra"),       # B code -> T code
                              AudioStream(0x1102, "0x99", "deu")]      # unknown codec
    # only presentation graphics (0x90); 0x91 are menus
    assert items[0].subs == [SubStream(0x1200, "ita")]
    assert items[1].audio == [] and items[1].subs == []
    assert items[0].dep_clip == ""


@pytest.mark.parametrize("subpath_type", [8, 9])
def test_parse_mpls_dependent_view(subpath_type: int) -> None:
    data = mpls([Item("00131", 0, 45_000), Item("00133", 0, 45_000)],
                dep_clips=["00132", "00134"], subpath_type=subpath_type)
    assert [i.dep_clip for i in parse_mpls(data)] == ["00132", "00134"]


def test_parse_mpls_other_subpath_ignored() -> None:
    data = mpls([Item("00131", 0, 45_000)], dep_clips=["00132"], subpath_type=3)
    assert parse_mpls(data)[0].dep_clip == ""


def test_parse_mpls_rejects_other_files() -> None:
    with pytest.raises(ValueError, match="MPLS"):
        parse_mpls(b"HDMV0200" + b"\x00" * 100)


EPS = [(0, 0), (256 * 100, 50), (1 << 19, 0x20010), ((1 << 19) + 512, 0x20100), (3 << 20, 0x50000)]


def test_parse_clpi_ep_map_and_extents() -> None:
    clip = parse_clpi(clpi(1234, EPS, extents=[0, 100, 300], other_pid=0x1100))
    assert clip.num_packets == 1234
    assert list(zip(clip.ep_pts, clip.ep_spn, strict=True)) == EPS
    assert clip.extent_start == [0, 100, 300]


def test_parse_clpi_dependent_pid_and_no_extents() -> None:
    clip = parse_clpi(clpi(10, EPS[:2], pid=0x1012), pid=0x1012)
    assert list(zip(clip.ep_pts, clip.ep_spn, strict=True)) == EPS[:2]
    assert clip.extent_start == []
    # asking for another PID finds no entry point
    assert parse_clpi(clpi(10, EPS[:2], pid=0x1012)).ep_pts == []


def test_parse_clpi_rejects_other_files() -> None:
    with pytest.raises(ValueError, match="CLPI"):
        parse_clpi(b"MPLS0200" + b"\x00" * 100)


def _item(in_time: int = 45_000) -> PlayItem:
    return PlayItem("00001", in_time, in_time + 45_000 * 600, "00002")


def test_m2ts_seek() -> None:
    item = _item()
    clip = Clip(10_000, [45_000, 45_000 + 90_000, 45_000 + 180_000], [0, 1000, 2500], [])
    sp = m2ts_seek(item, clip, 2.5)                     # between the 2nd and 3rd keyframe
    assert sp.time == 2.0
    assert sp.pts90 == 2 * (45_000 + 90_000)
    assert sp.ssif_offset == 1000 // AACS_UNIT_PACKETS * UNIT          # AACS-unit aligned
    assert sp.ssif_offset % 6144 == 0
    assert m2ts_seek(item, clip, 0).ssif_offset == 0
    # before the first keyframe: the first one (its time may be before the in time)
    early = Clip(10_000, [44_000, 90_000], [0, 64], [])
    assert m2ts_seek(item, early, 0).time == pytest.approx(-1000 / 45_000)


def test_ssif_seek_starts_at_the_dependent_extent() -> None:
    item = _item(0)
    base = Clip(1000, [0, 45_000, 90_000], [0, 300, 700], [0, 250, 600])
    dep = Clip(800, [0, 45_000, 90_000], [0, 240, 500], [0, 200, 480])
    sp = ssif_seek(item, base, dep, 1.5)      # keyframe at 1 s, base packet 300 in extent 1
    assert sp.time == 1.0
    # .ssif order D0 B0 D1 B1 ...: D1 starts after D0 and B0 = 250 + 200 packets
    assert sp.ssif_offset == (250 + 200) // AACS_UNIT_PACKETS * UNIT
    assert ssif_seek(item, base, dep, 5).ssif_offset == (600 + 480) // AACS_UNIT_PACKETS * UNIT


class FakeFile:
    """A decrypted .m2ts read in 6144-byte units, like bluray.DiscFile."""

    def __init__(self, data: bytes) -> None:
        self.data, self.pos, self.closed = data, 0, False

    def seek(self, offset: int) -> None:
        assert offset % UNIT == 0
        self.pos = offset

    def read_unit(self) -> bytes:
        chunk = self.data[self.pos:self.pos + UNIT]
        self.pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()


class FakeDisc:
    def __init__(self, files: dict[str, bytes], ssif: bool = False) -> None:
        self.files, self.ssif, self.opened = files, ssif, list[FakeFile]()

    def open(self, path: str) -> FakeFile:
        if path.endswith(".ssif") and not self.ssif:
            raise OSError("ssif is not yet supported")
        f = FakeFile(self.files[path])
        self.opened.append(f)
        return f


def _numbered(tag: int, count: int) -> bytes:
    """`count` source packets, each holding (tag, its index)."""
    return b"".join(bytes([tag]) + i.to_bytes(4, "big") + b"\x00" * (SOURCE_PACKET - 5)
                    for i in range(count))


def _packets(data: bytes) -> list[tuple[int, int]]:
    return [(data[o], int.from_bytes(data[o + 1:o + 5], "big"))
            for o in range(0, len(data), SOURCE_PACKET)]


def test_emulated_ssif_interleaves_extents() -> None:
    # extents that do not fall on AACS unit boundaries, and more than one unit long
    base = Clip(100, [], [], [0, 45, 70])
    dep = Clip(80, [], [], [0, 37, 50])
    disc = FakeDisc({"BDMV/STREAM/00001.m2ts": _numbered(1, 100),
                     "BDMV/STREAM/00002.m2ts": _numbered(2, 80)})
    item = _item(0)
    with open_ssif(disc, item, base, dep) as ssif:
        assert isinstance(ssif, EmulatedSsif)
        out = b""
        while unit := ssif.read_unit():
            assert len(unit) % SOURCE_PACKET == 0 and len(unit) <= UNIT
            out += unit
        expected = ([(2, i) for i in range(37)] + [(1, i) for i in range(45)]
                    + [(2, i) for i in range(37, 50)] + [(1, i) for i in range(45, 70)]
                    + [(2, i) for i in range(50, 80)] + [(1, i) for i in range(70, 100)])
        assert _packets(out) == expected

        # a seek lands anywhere in the interleaved stream
        ssif.seek(90 * SOURCE_PACKET)
        assert _packets(ssif.read_unit())[0] == expected[90]
    assert all(f.closed for f in disc.opened)


def test_open_ssif_prefers_the_real_file() -> None:
    disc = FakeDisc({"BDMV/STREAM/SSIF/00001.ssif": b"x" * UNIT}, ssif=True)
    f = open_ssif(disc, _item(0), Clip(0, [], [], []), Clip(0, [], [], []))
    assert isinstance(f, FakeFile)


def test_audio_codecs_table() -> None:
    assert bdmv.AUDIO_CODECS[0x83] == "truehd"
