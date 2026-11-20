import ctypes
import json
import threading
from pathlib import Path

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
    assert t.vobus == [0, 50, 100, 150]
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
    assert len(vobs.reads) < 12                       # a binary search, not VOBU by VOBU


def test_seek_without_navigation_pack() -> None:
    t, _ = title_with_vobus()
    with pytest.raises(OSError, match="navigation pack"):
        seek(FakeVobs({}), t, 10)                     # type: ignore[arg-type]


class FakeDisc:
    """A disc with one title per title set: title n is VTS n."""

    def __init__(self, titles: list[list[DvdCell]]) -> None:
        self.titles = titles

    def ifo(self, vts: int) -> bytes:
        if vts == 0:
            return vmg_ifo([(n, 1) for n in range(1, len(self.titles) + 1)])
        return vts_ifo(self.titles[vts - 1], [], [])


def run(minutes: float, first: int, cells: int = 3) -> list[DvdCell]:
    """`cells` cells one after the other on the disc, `minutes` in all."""
    size = 10_000
    return [DvdCell(minutes * 60 / cells, first + k * size, first + (k + 1) * size - 1, k + 1, 1)
            for k in range(cells)]


def chosen(titles: list[list[DvdCell]]) -> list[int]:
    return [t.number for t in dvd.main_titles(FakeDisc(titles))]


def test_a_movie() -> None:
    # the movie, a 12-minute extra, a 45-minute documentary: the movie
    assert chosen([run(12, 0), run(110, 100_000), run(45, 200_000)]) == [2]


def in_one_title_set(*cells: list[DvdCell]) -> list[DvdTitle]:
    """Titles of one VTS, whose cell sectors are the same space."""
    return [parse_title(vts_ifo(c, [], []), n, 1, 1) for n, c in enumerate(cells, 1)]


def test_a_series_with_play_all() -> None:
    episodes = [run(45, 0), run(50, 100_000), run(48, 200_000)]
    play_all = [c for e in episodes for c in e]
    titles = in_one_title_set(play_all, *episodes, run(5, 300_000))
    assert [t.number for t in dvd.choose_titles(titles)] == [2, 3, 4]


def test_a_protected_series() -> None:
    """Fake titles: the same cells replayed, or in a scrambled order."""
    e1, e2, e3 = run(45, 0), run(50, 100_000), run(48, 200_000)
    fakes = in_one_title_set(e1 + e2 + e1 + e3, e1, e3 + e1 + e2, e2, e3)
    assert [t.number for t in dvd.choose_titles(fakes)] == [2, 4, 5]


def test_nothing_looks_right() -> None:
    backwards = list(reversed(run(100, 0)))
    replayed = run(40, 100_000) * 2
    assert chosen([replayed, backwards]) == [2]                   # the longest, as before


def test_the_same_content_twice() -> None:
    movie = run(110, 0)
    assert [t.number for t in dvd.choose_titles(in_one_title_set(movie, list(movie)))] == [1]


def test_two_cuts_of_a_film_are_not_episodes() -> None:
    cut, extra = run(110, 0), DvdCell(600.0, 90_000, 99_999, 9, 1)
    theatrical, director = in_one_title_set(cut, [*cut[:2], extra, cut[2]])
    assert [t.number for t in dvd.choose_titles([theatrical, director])] == [2]


@pytest.mark.parametrize(("disc", "numbers"), [
    ("ALIAS_SEASON_1", [2, 4, 5, 6]),        # a cell jumping back at the end: still episodes
    ("ALIAS_SEASON_1_3EP", [1, 2, 6]),       # another disc of it: 3 episodes, the first a long one
    ("UTOPIA_S2_D2", [3, 4, 5]),             # title 2 plays the three others
    ("C4DVD10476", [9, 33, 54]),             # 94 long titles, 3 real (protection); 77 = play-all
    ("FUTURAMA_DISC_2", [1, 2, 3, 4, 5]),
    ("ALIEN_DISC_1", [1]),                   # one film in two cuts, not two episodes
    ("DVD-7b90ebcd29ee", [1, 2])])
def test_titles_of_recorded_discs(disc: str, numbers: list[int]) -> None:
    data = json.loads((Path(__file__).parent / "discs" / f"{disc}.json")
                      .read_text(encoding="utf-8"))
    parsed = [DvdTitle(t["number"], t["vts"], t["duration"], [dvd.Cell(**c) for c in t["cells"]])
              for t in data["titles"] if "error" not in t]
    assert [t.number for t in dvd.choose_titles(parsed)] == numbers


def test_vob_read_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class Lib:
        def DVDReadBlocks(self, handle: int, sector: int, count: int,  # noqa: N802
                          buf: ctypes.Array[ctypes.c_char]) -> int:
            calls.append(sector)
            if len(calls) < 3:
                return -1                      # the drive is busy
            ctypes.memmove(buf, b"\x42" * count * SECTOR, count * SECTOR)
            return count

    def no_wait(seconds: float) -> None:
        pass

    monkeypatch.setattr(dvd, "_dvdread", Lib)
    monkeypatch.setattr(dvd.time, "sleep", no_wait)
    vobs = dvd.VobFile(1, threading.Lock())
    assert vobs.read(7, 2) == b"\x42" * 2 * SECTOR
    assert calls == [7, 7, 7]
    calls.clear()
    def always_busy(self: Lib, handle: int, sector: int, count: int,
                    buf: ctypes.Array[ctypes.c_char]) -> int:
        return -1

    monkeypatch.setattr(Lib, "DVDReadBlocks", always_busy)
    assert vobs.read(7, 1) == b""

    # a damaged spot: the drive tried for seconds already, no second attempt
    clock = iter([0.0, 12.5, 12.7, 25.0])
    monkeypatch.setattr(dvd.time, "monotonic", lambda: next(clock))
    calls.clear()

    def damaged(self: Lib, handle: int, sector: int, count: int,
                buf: ctypes.Array[ctypes.c_char]) -> int:
        calls.append(sector)
        return -1

    monkeypatch.setattr(Lib, "DVDReadBlocks", damaged)
    assert vobs.read(9, 1) == b""
    assert calls == [9, 9]                  # one more try (a disc spinning up), no more


def test_seek_in_a_cell_played_twice() -> None:
    """A "play all" title playing the same VOB cell at 0 s and again at 60 s:
    the navigation packs say which VOB cell, the seek knows which occurrence."""
    t = parse_title(vts_ifo([DvdCell(30.0, 0, 749, 1, 1), DvdCell(30.0, 750, 1499, 2, 1),
                             DvdCell(30.0, 0, 749, 1, 1)],
                            [], [], vobus=list(range(0, 1500, 10))), 1, 1, 1)
    _, vobs = title_with_vobus()
    assert t.cell_index(1, 1) == 0 and t.cell_index(1, 1, near=2) == 2
    nav = seek(vobs, t, 72.3)                          # 12.3 s into the repeat
    assert nav.cell == 2 and nav.time == pytest.approx(72.0) and nav.sector == 300
    nav = seek(vobs, t, 12.3)                          # the first time
    assert nav.cell == 0 and nav.time == pytest.approx(12.0)


def _dir_record(name: bytes, extent: int, size: int, is_dir: bool = False) -> bytes:
    """One ISO9660 directory record."""
    body = bytearray(33 + len(name) + (1 - len(name) % 2))
    body[0] = len(body)
    body[2:6] = extent.to_bytes(4, "little")
    body[10:14] = size.to_bytes(4, "little")
    body[25] = 2 if is_dir else 0
    body[32] = len(name)
    body[33:33 + len(name)] = name
    return bytes(body)


def _iso(files: dict[str, int]) -> bytes:
    """A minimal ISO9660 image: the volume descriptor, the root with VIDEO_TS/ and
    VIDEO_TS/ with `files` (name -> size; no content, only the listing matters)."""
    sector = dvd.SECTOR
    image = bytearray(sector * 21)
    root, video_ts = 18, 19
    image[16 * sector:16 * sector + 6] = b"\x01CD001"
    image[16 * sector + 156:16 * sector + 190] = _dir_record(b"\x00", root, sector, True)
    records = _dir_record(b"\x00", root, sector, True) + _dir_record(b"\x01", root, sector, True)
    image[root * sector:root * sector + 200] = (
        records + _dir_record(b"VIDEO_TS", video_ts, sector, True)).ljust(200, b"\0")
    listing = _dir_record(b"\x00", video_ts, sector, True) + \
        _dir_record(b"\x01", root, sector, True)
    listing += b"".join(_dir_record(f"{n};1".encode(), 20, s) for n, s in files.items())
    image[video_ts * sector:video_ts * sector + len(listing)] = listing
    return bytes(image)


FILES = {"VIDEO_TS.IFO": 12_288, "VTS_01_0.IFO": 69_632, "VTS_01_1.VOB": 1_073_739_776,
         "VTS_02_0.VOB": 0}


def test_video_ts_sizes_from_an_image(tmp_path: Path) -> None:
    """#69: every file of VIDEO_TS/ with its size, an empty one too (TheDiscDB's
    fingerprint counts it; libdvdread does not list it), read from the disc's
    ISO9660 file system: a drive or an ISO image alike."""
    iso = tmp_path / "disc.iso"
    iso.write_bytes(_iso(FILES))
    assert dvd.video_ts_sizes(str(iso)) == FILES


def test_video_ts_sizes_from_a_folder(tmp_path: Path) -> None:
    folder = tmp_path / "Movie" / "VIDEO_TS"
    folder.mkdir(parents=True)
    for name, size in {"VIDEO_TS.IFO": 10, "VTS_02_0.VOB": 0}.items():
        (folder / name).write_bytes(b"x" * size)
    assert dvd.video_ts_sizes(str(tmp_path / "Movie")) == {"VIDEO_TS.IFO": 10, "VTS_02_0.VOB": 0}


def test_video_ts_sizes_not_a_dvd(tmp_path: Path) -> None:
    other = tmp_path / "other.iso"
    other.write_bytes(b"\0" * dvd.SECTOR * 20)
    with pytest.raises(OSError, match="no ISO9660"):
        dvd.video_ts_sizes(str(other))


def test_video_ts_sizes_finds_the_primary_descriptor(tmp_path: Path) -> None:
    """The primary volume descriptor need not be the first one (a boot record may come
    first): the descriptors are walked until it."""
    image = bytearray(_iso(FILES))
    sector = dvd.SECTOR
    pvd = bytes(image[16 * sector:17 * sector])
    boot = bytearray(sector)
    boot[0:6] = b"\x00CD001"
    image[16 * sector:17 * sector] = boot
    image[17 * sector:18 * sector] = pvd
    iso = tmp_path / "boot.iso"
    iso.write_bytes(bytes(image))
    assert dvd.video_ts_sizes(str(iso)) == FILES


def test_video_ts_sizes_record_cut_short(tmp_path: Path) -> None:
    """A directory record too short to be one is a damaged listing: OSError."""
    image = bytearray(_iso(FILES))
    sector = dvd.SECTOR
    image[19 * sector + 68] = 10        # the first file's record claims 10 bytes
    iso = tmp_path / "damaged.iso"
    iso.write_bytes(bytes(image))
    with pytest.raises(OSError, match="damaged"):
        dvd.video_ts_sizes(str(iso))


def test_video_ts_sizes_huge_directory_length(tmp_path: Path) -> None:
    """A damaged directory length (4 GiB) is refused before reading: no drive-long read."""
    image = bytearray(_iso(FILES))
    sector = dvd.SECTOR
    image[16 * sector + 156 + 10:16 * sector + 156 + 14] = (0xFFFFFFF0).to_bytes(4, "little")
    iso = tmp_path / "huge.iso"
    iso.write_bytes(bytes(image))
    with pytest.raises(OSError, match="damaged"):
        dvd.video_ts_sizes(str(iso))


def test_video_ts_sizes_lowercase_names(tmp_path: Path) -> None:
    """Names as an image records them in lowercase still match, as for a folder."""
    image = _iso({"video_ts.ifo": 12_288, "vts_02_0.vob": 0}).replace(b"VIDEO_TS", b"video_ts")
    iso = tmp_path / "lower.iso"
    iso.write_bytes(image)
    assert dvd.video_ts_sizes(str(iso)) == {"VIDEO_TS.IFO": 12_288, "VTS_02_0.VOB": 0}

