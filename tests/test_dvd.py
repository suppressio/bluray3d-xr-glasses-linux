import pytest

import dvd
from builders import SECTOR, DvdAudioAttr, DvdCell, DvdSubAttr, dvd_time, nav_pack, vmg_ifo, vts_ifo
from dvd import DvdTitle, _bcd, _dvd_time, _rgb, parse_title, read_nav, seek, titles


def test_bcd_and_time() -> None:
    assert _bcd(0x59) == 59
    assert _dvd_time(dvd_time(3723.4), 0) == pytest.approx(3723.4)
    assert _dvd_time(bytes([0x01, 0x02, 0x03, (3 << 6) | 0x15]), 0) == pytest.approx(
        3723 + 15 / (30000 / 1001))


def test_rgb() -> None:
    assert _rgb(16, 128, 128) == "101010"
    assert _rgb(235, 128, 128) == "ebebeb"
    assert _rgb(0, 255, 0) == "b20000"                       # clipped to 0..255


def test_titles() -> None:
    assert titles(vmg_ifo([(1, 1), (2, 1), (2, 2)])) == [(1, 1, 1), (2, 2, 1), (3, 2, 2)]


CELLS = [DvdCell(600.0, 0, 9999, 1, 1), DvdCell(10.0, 10000, 10099, 1, 2, angle=(1, 1)),
         DvdCell(10.0, 10100, 10199, 1, 3, angle=(2, 1)),   # other angle of the block: skipped
         DvdCell(1200.0, 10200, 29999, 2, 1)]


def test_parse_title() -> None:
    ifo = vts_ifo(CELLS,
                  [DvdAudioAttr(0, "it", 1, 0), DvdAudioAttr(6, "en", 1, 1),
                   DvdAudioAttr(0, "en", 3, 2), DvdAudioAttr(2, "", 1, 3)],
                  [DvdSubAttr("it", 1, wide=2, four3=0), DvdSubAttr("fr", 9, wide=3, four3=1)],
                  tmap_unit=4, tmap=[100, 200, 300], vobus=[0, 50, 100, 150],
                  palette=[(235, 128, 128)] + [(16, 128, 128)] * 15, pgcn=2)
    t = parse_title(ifo, number=3, vts=2, ttn=1)
    assert (t.number, t.vts) == (3, 2)
    assert t.duration == pytest.approx(1820.0)
    assert (t.frame_size, t.frame_rate) == ("1024x576", "25")
    assert [(a.stream, a.codec, a.lang, a.commentary) for a in t.audio] == [
        (0x80, "ac3", "ita", False), (0x89, "dts", "eng", False),
        (0x82, "ac3", "eng", True), (0x1C3, "mp2", "und", False)]
    # 16:9 title: the widescreen stream numbers
    assert [(s.stream, s.lang, s.kind) for s in t.subs] == [(0x22, "ita", "normal"),
                                                            (0x23, "fra", "forced")]
    assert t.palette[0] == "ebebeb" and t.palette[1] == "101010"
    assert len(t.palette_raw) == 64
    assert [(c.start, c.duration, c.first, c.last, c.vob_id, c.cell_id) for c in t.cells] == [
        (0.0, 600.0, 0, 9999, 1, 1), (600.0, 10.0, 10000, 10099, 1, 2),
        (610.0, 1200.0, 10200, 29999, 2, 1)]
    assert (t.tmap_unit, t.tmap, t.vobus) == (4, [100, 200, 300], [0, 50, 100, 150])
    assert t.cell_index(2, 1) == 2 and t.cell_index(9, 9) == -1


@pytest.mark.parametrize(("pal", "wide", "size", "rate"), [
    (True, False, "768x576", "25"), (False, True, "852x480", "30000/1001"),
    (False, False, "640x480", "30000/1001")])
def test_frame_size(pal: bool, wide: bool, size: str, rate: str) -> None:
    t = parse_title(vts_ifo(CELLS[:1], [], [DvdSubAttr("en", 1, wide=4, four3=5)],
                            pal=pal, wide=wide), 1, 1, 1)
    assert (t.frame_size, t.frame_rate) == (size, rate)
    assert t.subs[0].stream == (0x24 if wide else 0x25)


class FakeVobs:
    """Title VOBs where some sectors are navigation packs."""

    def __init__(self, navs: dict[int, bytes]) -> None:
        self.navs, self.reads = navs, list[int]()

    def read(self, sector: int, count: int) -> bytes:
        self.reads.append(sector)
        return b"".join(self.navs.get(s, b"\x00" * SECTOR) for s in range(sector, sector + count))


def title_with_vobus(step: float = 0.4) -> tuple[DvdTitle, FakeVobs]:
    """Two cells, a VOBU every `step` seconds, 10 sectors each; cell 2 restarts
    its PTS (as some discs do at a new VOB)."""
    t = parse_title(vts_ifo([DvdCell(30.0, 0, 749, 1, 1), DvdCell(30.0, 750, 1499, 2, 1)],
                            [], [], tmap_unit=4, tmap=[100 * k for k in range(1, 15)],
                            vobus=list(range(0, 1500, 10))), 1, 1, 1)
    navs: dict[int, bytes] = {}
    for s in range(0, 1500, 10):
        cell = 0 if s < 750 else 1
        cell_time = (s - 750 * cell) / 10 * step
        pts = 90_000 + round(cell_time * 90_000) + (0 if cell == 0 else 7_000_000)
        navs[s] = nav_pack(cell + 1, 1, cell_time, pts)
    return t, FakeVobs(navs)


def test_read_nav() -> None:
    t, vobs = title_with_vobus()
    nav = read_nav(vobs, t, 770)                      # type: ignore[arg-type]
    assert nav is not None
    assert (nav.time, nav.pts, nav.sector) == (30.8, 90_000 + 72_000 + 7_000_000, 770)
    assert read_nav(vobs, t, 771) is None             # type: ignore[arg-type]
    bad = bytearray(vobs.navs[0])
    bad[0x406] = 0
    vobs.navs[5] = bytes(bad)
    assert read_nav(vobs, t, 5) is None               # type: ignore[arg-type]


@pytest.mark.parametrize(("seconds", "time"), [(0, 0.0), (12.3, 12.0), (29.9, 29.6),
                                                (30.0, 30.0), (45.26, 45.2), (-5, 0.0)])
def test_seek(seconds: float, time: float) -> None:
    t, vobs = title_with_vobus()
    nav = seek(vobs, t, seconds)                      # type: ignore[arg-type]
    assert nav.time == pytest.approx(time)
    assert nav.sector == round(time * 25)
    assert len(vobs.reads) < 20                       # the time map gets close first


def test_seek_without_navigation_pack() -> None:
    t, _ = title_with_vobus()
    with pytest.raises(OSError, match="navigation pack"):
        seek(FakeVobs({}), t, 10)                     # type: ignore[arg-type]


def test_main_title_is_the_longest() -> None:
    class FakeDvd:
        def ifo(self, vts: int) -> bytes:
            if vts == 0:
                return vmg_ifo([(1, 1), (2, 1)])
            return vts_ifo([DvdCell(60.0 * vts * vts, 0, 99)], [], [])

    t = dvd.main_title(FakeDvd())                     # type: ignore[arg-type]
    assert (t.number, t.vts, t.duration) == (2, 2, 240.0)
