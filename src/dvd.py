"""
dvd.py — DVD-Video access for the virtual files: libdvdread (+ libdvdcss for CSS,
loaded by libdvdread when present) through ctypes, and the bits of the IFO files
needed to play the main title with an exact seek.

  VIDEO_TS.IFO   titles -> title set (VTS) and title number inside it
  VTS_xx_0.IFO   the title's program chain (PGC): cells with duration and sector
                 range; audio streams and languages; video standard and aspect;
                 VOBU address map (ADMAP, every VOBU start)
  navigation pack  first sector of every VOBU (~0.5 s): cell id and the exact
                 time elapsed in the cell, and the PTS of its first frame

Seeking: a binary search over the VOBUs of the cell, reading their navigation
packs, finds the last one starting at or before the requested time. Its time
is exact, so the virtual file knows precisely where playback restarts. "Play
all" titles can play the same cells more than once: a navigation pack is
matched to the occurrence being read. FFmpeg's dvdvideo demuxer only seeks
approximately (seconds off, and it does not know where it landed).
"""
import bisect
import ctypes
import ctypes.util
import functools
import itertools
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from langs import lang as to_lang

SECTOR = 2048
READ_ATTEMPTS = 4
SLOW_FAILURE = 2.0                  # seconds
DVD_READ_INFO_FILE, DVD_READ_TITLE_VOBS = 0, 3

AUDIO_FORMATS = {0: "ac3", 2: "mp2", 3: "mp2", 4: "lpcm", 6: "dts"}


def _load() -> ctypes.CDLL:
    for name in (ctypes.util.find_library("dvdread"), "libdvdread.so.8", "libdvdread.so.4"):
        if name:
            try:
                return ctypes.CDLL(name)
            except OSError:
                pass
    raise OSError("libdvdread not found (Debian/Ubuntu: apt install libdvdread8 libdvdcss2)")


@functools.cache
def _dvdread() -> ctypes.CDLL:
    lib = _load()
    for n, res, args in (
            ("DVDOpen", ctypes.c_void_p, [ctypes.c_char_p]),
            ("DVDClose", None, [ctypes.c_void_p]),
            ("DVDOpenFile", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
            ("DVDCloseFile", None, [ctypes.c_void_p]),
            ("DVDFileSize", ctypes.c_ssize_t, [ctypes.c_void_p]),
            ("DVDReadBytes", ctypes.c_ssize_t,
             [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]),
            ("DVDReadBlocks", ctypes.c_ssize_t,
             [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p]),
            ("DVDUDFVolumeInfo", ctypes.c_int,
             [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint])):
        getattr(lib, n).restype = res
        getattr(lib, n).argtypes = args
    return lib


def _u16(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 2], "big")


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "big")


def _bcd(x: int) -> int:
    return (x >> 4) * 10 + (x & 15)


def _dvd_time(b: bytes, o: int) -> float:
    """BCD hh:mm:ss:ff, the frame rate in the top bits of the last byte."""
    rate = {1: 25.0, 3: 30000 / 1001}.get(b[o + 3] >> 6, 25.0)
    return _bcd(b[o]) * 3600 + _bcd(b[o + 1]) * 60 + _bcd(b[o + 2]) + _bcd(b[o + 3] & 0x3F) / rate


def scrambled(sectors: bytes) -> bool:
    """True if a pack in these sectors is still CSS-scrambled: the PES scrambling
    bits right after the pack header, where libdvdcss itself looks. Navigation
    packs are never scrambled, so one scrambled pack says the title is."""
    for i in range(0, len(sectors) - SECTOR + 1, SECTOR):
        pack = sectors[i:i + SECTOR]
        if pack[:4] == b"\x00\x00\x01\xba" and pack[0x14] & 0x30:
            return True
    return False


def has_dvdcss() -> bool:
    """libdvdread decrypts CSS through libdvdcss, when it finds it."""
    return bool(ctypes.util.find_library("dvdcss")) or \
        any(Path(d, "libdvdcss.so.2").exists()
            for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/usr/lib"))


class SectorReader(Protocol):
    """Reads sectors of the title VOBs (VobFile)."""

    def read(self, sector: int, count: int) -> bytes: ...


class VobFile:
    """The title VOBs of a VTS, as one sector-addressed file (decrypted)."""

    def __init__(self, handle: int, lock: threading.Lock) -> None:
        self._h: int | None = handle
        self._lock = lock

    def read(self, sector: int, count: int) -> bytes:
        """The sectors; a read the drive fails at once (busy, seeking elsewhere for
        another reader) is tried again a few times, a slow failure once, before
        giving up (b'')."""
        buf = ctypes.create_string_buffer(count * SECTOR)
        n = 0
        slow_failures = 0
        for attempt in range(READ_ATTEMPTS):
            if attempt:
                time.sleep(0.2 * attempt)
            started = time.monotonic()
            with self._lock:
                n = _dvdread().DVDReadBlocks(self._h, sector, count, buf)
            if n > 0:
                break
            # a failure after seconds: the disc spinning up again after a pause
            # (worth one more try), or a damaged spot the drive already tried
            # hard to read (more tries would only multiply the wait)
            if time.monotonic() - started > SLOW_FAILURE:
                slow_failures += 1
                if slow_failures > 1:
                    break
        return buf.raw[:max(0, n) * SECTOR]

    def close(self) -> None:
        if self._h:
            _dvdread().DVDCloseFile(self._h)
            self._h = None


class Dvd:
    """A DVD-Video disc, ISO image or folder with VIDEO_TS/."""

    def __init__(self, path: str) -> None:
        # only the title key we need, when a title's VOBs are opened, instead of
        # all of them at every open (each costs a seek on the disc; libdvdcss
        # keeps them cached in ~/.dvdcss anyway)
        os.environ.setdefault("DVDREAD_NOKEYS", "1")
        self._dvd: int | None = _dvdread().DVDOpen(path.encode())
        if not self._dvd:
            raise OSError(f"cannot open DVD: {path}")
        self._lock = threading.Lock()

    def volume_id(self) -> str:
        buf = ctypes.create_string_buffer(33)
        with self._lock:
            ok = _dvdread().DVDUDFVolumeInfo(self._dvd, buf, 33, None, 0) == 0
        return buf.value.decode("ascii", "replace").strip() if ok else ""

    def ifo(self, vts: int) -> bytes:
        with self._lock:
            f: int | None = _dvdread().DVDOpenFile(self._dvd, vts, DVD_READ_INFO_FILE)
            if not f:
                raise OSError(f"cannot open the IFO of VTS {vts}")
            size: int = _dvdread().DVDFileSize(f) * SECTOR
            buf = ctypes.create_string_buffer(size)
            n: int = _dvdread().DVDReadBytes(f, buf, size)
            _dvdread().DVDCloseFile(f)
        return buf.raw[:max(0, n)]

    def title_vobs(self, vts: int) -> VobFile:
        with self._lock:
            h: int | None = _dvdread().DVDOpenFile(self._dvd, vts, DVD_READ_TITLE_VOBS)
        if not h:
            raise OSError(f"cannot open the VOBs of VTS {vts}")
        return VobFile(h, self._lock)

    def close(self) -> None:
        if self._dvd:
            _dvdread().DVDClose(self._dvd)
            self._dvd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()


@dataclass
class Cell:
    start: float        # seconds from the start of the title
    duration: float
    first: int          # first VOBU start sector
    last: int           # last sector of the cell
    vob_id: int
    cell_id: int


@dataclass
class DvdAudio:
    stream: int         # substream / stream id in the program stream (0x80 = first AC-3...)
    codec: str
    lang: str           # ISO 639-2, e.g. "ita"
    commentary: bool


@dataclass
class DvdSub:
    stream: int         # subpicture stream id (0x20 + n)
    lang: str           # ISO 639-2
    kind: str           # "normal", "commentary", "forced", "other"


SUB_KINDS = {0: "normal", 1: "normal", 2: "normal", 3: "other", 5: "normal", 6: "normal",
             7: "other", 9: "forced", 13: "commentary", 14: "commentary", 15: "commentary"}


def _rgb(y: int, cr: int, cb: int) -> str:
    def clip(x: float) -> int:
        return max(0, min(255, round(x)))

    return (f"{clip(y + 1.402 * (cr - 128)):02x}"
            f"{clip(y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)):02x}"
            f"{clip(y + 1.772 * (cb - 128)):02x}")


@dataclass
class DvdTitle:
    number: int         # title number on the disc (1-based)
    vts: int
    duration: float
    cells: list[Cell] = field(default_factory=list[Cell])
    audio: list[DvdAudio] = field(default_factory=list[DvdAudio])
    subs: list[DvdSub] = field(default_factory=list[DvdSub])
    palette: list[str] = field(default_factory=list[str])  # 16 "rrggbb" subpicture colours
    palette_raw: bytes = b""                     # the same 16 entries as stored in the PGC
    frame_size: str = "1024x576"   # square-pixel display size
    frame_rate: str = "25"
    vobus: list[int] = field(default_factory=list[int])   # every VOBU start sector (ADMAP)

    def cell_index(self, vob_id: int, cell_id: int, near: int | None = None) -> int:
        """The title's cell playing that VOB cell. "Play all" titles can play the
        same cell twice: then the one at index `near` (where we are) if it is one
        of them, else the first."""
        found = [i for i, c in enumerate(self.cells) if (c.vob_id, c.cell_id) == (vob_id, cell_id)]
        if near in found:
            return near
        return found[0] if found else -1


def titles(vmg: bytes) -> list[tuple[int, int, int]]:
    """(title number, VTS, title number inside the VTS) of every title."""
    tt = _u32(vmg, 0xC4) * SECTOR
    return [(i + 1, vmg[tt + 8 + i * 12 + 6], vmg[tt + 8 + i * 12 + 7])
            for i in range(_u16(vmg, tt))]


def parse_title(vts_ifo: bytes, number: int, vts: int, ttn: int) -> DvdTitle:
    v = vts_ifo
    ptt = _u32(v, 0xC8) * SECTOR
    chapters = ptt + _u32(v, ptt + 8 + (ttn - 1) * 4)
    pgcn = _u16(v, chapters)                          # PGC of the title's first chapter
    pgci = _u32(v, 0xCC) * SECTOR
    pgc = pgci + _u32(v, pgci + 8 + (pgcn - 1) * 8 + 4)
    t = DvdTitle(number, vts, _dvd_time(v, pgc + 4))

    # video attributes: PAL/NTSC and 4:3 / 16:9 -> square-pixel display size
    video = _u16(v, 0x200)
    pal, wide = (video >> 12) & 3 == 1, (video >> 10) & 3 == 3
    height = 576 if pal else 480
    t.frame_size = f"{(height * 16 // 9 if wide else height * 4 // 3) // 2 * 2}x{height}"
    t.frame_rate = "25" if pal else "30000/1001"

    # audio: attributes in the VTS header, availability and stream number in the PGC
    for i in range(min(8, _u16(v, 0x202))):
        a = 0x204 + i * 8
        control = _u16(v, pgc + 0x0C + i * 2)
        if not control & 0x8000:
            continue
        fmt, n = v[a] >> 5, (control >> 8) & 7
        stream = {0: 0x80, 6: 0x88, 4: 0xA0}.get(fmt, 0x1C0) + n
        lang2 = v[a + 2:a + 4].decode("ascii", "replace").strip("\0 ").lower() or "und"
        t.audio.append(DvdAudio(stream, AUDIO_FORMATS.get(fmt, f"fmt{fmt}"),
                                to_lang(lang2), v[a + 5] in (3, 4)))

    # subpictures: attributes in the VTS header, stream numbers in the PGC (the
    # one for the display format: widescreen on 16:9 titles); palette in the PGC
    for i in range(min(32, _u16(v, 0x254))):
        a = 0x256 + i * 6
        control = _u32(v, pgc + 0x1C + i * 4)
        if not control >> 31:
            continue
        n = (control >> 16) & 31 if wide else (control >> 24) & 31
        lang2 = v[a + 2:a + 4].decode("ascii", "replace").strip("\0 ").lower() or "und"
        t.subs.append(DvdSub(0x20 + n, to_lang(lang2), SUB_KINDS.get(v[a + 5], "other")))
    t.palette = [_rgb(v[pgc + 0xA4 + i * 4 + 1], v[pgc + 0xA4 + i * 4 + 2],
                      v[pgc + 0xA4 + i * 4 + 3]) for i in range(16)]
    t.palette_raw = bytes(v[pgc + 0xA4:pgc + 0xA4 + 64])

    # cells, skipping the non-first angles of angle blocks
    playback = pgc + _u16(v, pgc + 0xE8)
    position = pgc + _u16(v, pgc + 0xEA)
    start = 0.0
    for c in range(v[pgc + 3]):
        e = playback + c * 24
        block_mode, block_type = v[e] >> 6, (v[e] >> 4) & 3
        if block_type == 1 and block_mode in (2, 3):   # angle block, not its first cell
            continue
        dur = _dvd_time(v, e + 4)
        t.cells.append(Cell(start, dur, _u32(v, e + 8), _u32(v, e + 20),
                            _u16(v, position + c * 4), v[position + c * 4 + 3]))
        start += dur

    admap = _u32(v, 0xE4) * SECTOR
    t.vobus = [_u32(v, admap + 4 + i * 4) for i in range((_u32(v, admap) + 1 - 4) // 4)]
    return t


class IfoReader(Protocol):
    """Reads the IFO files of a disc (Dvd): 0 = VIDEO_TS.IFO, n = VTS_n_0.IFO."""

    def ifo(self, vts: int) -> bytes: ...


EPISODE_MIN = 20 * 60              # seconds: shorter titles are extras
EPISODE_RATIO = 0.5                # titles at least this share of the longest: episodes


def _forward(title: DvdTitle) -> bool:
    """The title plays the disc from start to end, each cell once. Copy
    protections of the 2000s-2010s (UK series discs...) add dozens of fake titles
    that replay the same cells in a scrambled order."""
    firsts = [c.first for c in title.cells]
    return all(b > a for a, b in itertools.pairwise(firsts))


def _cell_set(title: DvdTitle) -> set[tuple[int, int]]:
    return {(c.first, c.last) for c in title.cells}


def main_titles(dvd: IfoReader) -> list[DvdTitle]:
    """What to offer from the disc: the movie, or the episodes of a series.

    Titles that jump back on the disc or replay cells are left out (fake titles
    of copy protections), so are the short ones (extras) and "play all" titles
    made of others. If several of the rest are of similar length they are the
    episodes, in title order; otherwise the longest is the movie. A disc where
    nothing passes those tests gets its longest title, as before."""
    ifos: dict[int, bytes] = {}
    parsed: list[DvdTitle] = []
    for number, vts, ttn in titles(dvd.ifo(0)):
        if vts not in ifos:
            ifos[vts] = dvd.ifo(vts)
        try:
            parsed.append(parse_title(ifos[vts], number, vts, ttn))
        except (IndexError, ValueError):
            continue
    if not parsed:
        raise OSError("no title found on the DVD")
    candidates: dict[frozenset[tuple[int, int]], DvdTitle] = {}
    for t in parsed:                           # the same content twice: the first title
        if t.duration >= EPISODE_MIN and _forward(t):
            candidates.setdefault(frozenset(_cell_set(t)), t)
    units = list(candidates.values())
    units = [t for t in units                  # a "play all" contains other titles
             if sum(1 for o in units if o is not t and _cell_set(o) <= _cell_set(t)) < 2]
    if not units:
        return [max(parsed, key=lambda t: t.duration)]
    longest = max(units, key=lambda t: t.duration)
    episodes = [t for t in units if t.duration >= EPISODE_RATIO * longest.duration]
    return sorted(episodes, key=lambda t: t.number) if len(episodes) > 1 else [longest]


@dataclass
class Nav:
    time: float         # seconds from the start of the title
    pts: int            # 90 kHz PTS of the VOBU's first frame
    sector: int
    cell: int           # index of the title's cell it belongs to


def read_nav(vobs: SectorReader, title: DvdTitle, sector: int,
             cell: int | None = None) -> Nav | None:
    """The navigation pack at `sector`, or None if there is none (or it is not ours).
    `cell`: the title's cell being read, for cells the title plays more than once."""
    s = vobs.read(sector, 1)
    if len(s) < SECTOR or s[0x26:0x2A] != b"\x00\x00\x01\xbf" or s[0x2C] != 0 \
            or s[0x400:0x404] != b"\x00\x00\x01\xbf" or s[0x406] != 1:
        return None
    dsi = 0x407
    i = title.cell_index(_u16(s, dsi + 0x18), s[dsi + 0x1B], cell)
    if i < 0:
        return None
    return Nav(title.cells[i].start + _dvd_time(s, dsi + 0x1C), _u32(s, 0x2D + 0x0C), sector, i)


def seek(vobs: SectorReader, title: DvdTitle, seconds: float) -> Nav:
    """The last VOBU starting at or before `seconds`, with its exact time.

    A binary search over the VOBUs of the cell (from the VOBU address map),
    reading their navigation packs: about ten reads, and never a VOBU after the
    time asked for. The disc's time map is not used: on some discs it does not
    match the title's timeline."""
    seconds = max(0.0, seconds)
    ci = max(0, bisect.bisect_right([c.start for c in title.cells], seconds) - 1)
    cell = title.cells[ci]
    lo = bisect.bisect_left(title.vobus, cell.first)
    best = None
    while best is None and lo < len(title.vobus) and title.vobus[lo] <= cell.last:
        # the cell's first VOBU; if it cannot be read (a damaged spot), the next
        best = read_nav(vobs, title, title.vobus[lo], ci)
        lo += 1
    if best is None:
        raise OSError(f"no navigation pack in the cell at sector {cell.first}")
    hi = bisect.bisect_right(title.vobus, cell.last) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        nav = read_nav(vobs, title, title.vobus[mid], ci)
        if nav is not None and nav.time <= seconds:
            best, lo = nav, mid + 1
        else:                                  # later, or unreadable: look earlier
            hi = mid - 1
    return best
