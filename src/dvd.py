"""
dvd.py — DVD-Video access for the virtual files: libdvdread (+ libdvdcss for CSS,
loaded by libdvdread when present) through ctypes, and the bits of the IFO files
needed to play the main title with an exact seek.

  VIDEO_TS.IFO   titles -> title set (VTS) and title number inside it
  VTS_xx_0.IFO   the title's program chain (PGC): cells with duration and sector
                 range; audio streams and languages; video standard and aspect;
                 time map (TMAPT, one VOBU address every few seconds);
                 VOBU address map (ADMAP, every VOBU start)
  navigation pack  first sector of every VOBU (~0.5 s): cell id and the exact
                 time elapsed in the cell, and the PTS of its first frame

Seeking: the time map gets within a few seconds, then VOBU by VOBU (reading
their navigation packs, all close to each other) to the last one starting at
or before the requested time. Its time is exact, so the virtual file knows
precisely where playback restarts. FFmpeg's dvdvideo demuxer only seeks
approximately (seconds off, and it does not know where it landed).
"""
import bisect
import ctypes
import ctypes.util
import threading
from dataclasses import dataclass, field

SECTOR = 2048
DVD_READ_INFO_FILE, DVD_READ_TITLE_VOBS = 0, 3

# ISO 639-1 (DVD) -> ISO 639-2 (Blu-ray, file names)
LANG3 = {"en": "eng", "it": "ita", "fr": "fra", "de": "deu", "es": "spa", "pt": "por",
         "nl": "nld", "ru": "rus", "ja": "jpn", "zh": "zho", "ko": "kor", "pl": "pol",
         "cs": "ces", "hu": "hun", "sv": "swe", "da": "dan", "fi": "fin", "no": "nor",
         "el": "ell", "tr": "tur", "he": "heb", "ar": "ara", "hi": "hin", "hr": "hrv",
         "sr": "srp", "sl": "slv", "sk": "slk", "uk": "ukr", "ro": "ron", "bg": "bul"}
AUDIO_FORMATS = {0: "ac3", 2: "mp2", 3: "mp2", 4: "lpcm", 6: "dts"}


def _load() -> ctypes.CDLL:
    for name in (ctypes.util.find_library("dvdread"), "libdvdread.so.8", "libdvdread.so.4"):
        if name:
            try:
                return ctypes.CDLL(name)
            except OSError:
                pass
    raise OSError("libdvdread not found (Debian/Ubuntu: apt install libdvdread8 libdvdcss2)")


_lib = None


def _dvdread() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        _lib = _load()
        for n, res, args in (
                ("DVDOpen", ctypes.c_void_p, [ctypes.c_char_p]),
                ("DVDClose", None, [ctypes.c_void_p]),
                ("DVDOpenFile", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
                ("DVDCloseFile", None, [ctypes.c_void_p]),
                ("DVDFileSize", ctypes.c_ssize_t, [ctypes.c_void_p]),
                ("DVDReadBytes", ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]),
                ("DVDReadBlocks", ctypes.c_ssize_t,
                 [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p]),
                ("DVDUDFVolumeInfo", ctypes.c_int,
                 [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint])):
            getattr(_lib, n).restype = res
            getattr(_lib, n).argtypes = args
    return _lib


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


class VobFile:
    """The title VOBs of a VTS, as one sector-addressed file (decrypted)."""

    def __init__(self, handle, lock):
        self._h, self._lock = handle, lock

    def read(self, sector: int, count: int) -> bytes:
        buf = ctypes.create_string_buffer(count * SECTOR)
        with self._lock:
            n = _dvdread().DVDReadBlocks(self._h, sector, count, buf)
        return buf.raw[:max(0, n) * SECTOR]

    def close(self):
        if self._h:
            _dvdread().DVDCloseFile(self._h)
            self._h = None


class Dvd:
    """A DVD-Video disc, ISO image or folder with VIDEO_TS/."""

    def __init__(self, path: str):
        self._dvd = _dvdread().DVDOpen(path.encode())
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
            f = _dvdread().DVDOpenFile(self._dvd, vts, DVD_READ_INFO_FILE)
            if not f:
                raise OSError(f"cannot open the IFO of VTS {vts}")
            size = _dvdread().DVDFileSize(f) * SECTOR
            buf = ctypes.create_string_buffer(size)
            n = _dvdread().DVDReadBytes(f, buf, size)
            _dvdread().DVDCloseFile(f)
        return buf.raw[:max(0, n)]

    def title_vobs(self, vts: int) -> VobFile:
        with self._lock:
            h = _dvdread().DVDOpenFile(self._dvd, vts, DVD_READ_TITLE_VOBS)
        if not h:
            raise OSError(f"cannot open the VOBs of VTS {vts}")
        return VobFile(h, self._lock)

    def close(self):
        if self._dvd:
            _dvdread().DVDClose(self._dvd)
            self._dvd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
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
class DvdTitle:
    number: int         # title number on the disc (1-based)
    vts: int
    duration: float
    cells: list = field(default_factory=list)
    audio: list = field(default_factory=list)
    frame_size: str = "1024x576"   # square-pixel display size
    frame_rate: str = "25"
    tmap_unit: int = 0
    tmap: list = field(default_factory=list)     # VOBU sector every tmap_unit seconds
    vobus: list = field(default_factory=list)    # every VOBU start sector (ADMAP)

    def cell_index(self, vob_id: int, cell_id: int) -> int:
        for i, c in enumerate(self.cells):
            if c.vob_id == vob_id and c.cell_id == cell_id:
                return i
        return -1


def _titles(vmg: bytes) -> list[tuple[int, int, int]]:
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
                                LANG3.get(lang2, lang2), v[a + 5] in (3, 4)))

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

    tmapt = _u32(v, 0xD4) * SECTOR
    if tmapt and pgcn <= _u16(v, tmapt):
        m = tmapt + _u32(v, tmapt + 8 + (pgcn - 1) * 4)
        t.tmap_unit = v[m]
        t.tmap = [_u32(v, m + 4 + i * 4) & 0x7FFFFFFF for i in range(_u16(v, m + 2))]
    admap = _u32(v, 0xE4) * SECTOR
    t.vobus = [_u32(v, admap + 4 + i * 4) for i in range((_u32(v, admap) + 1 - 4) // 4)]
    return t


def main_title(dvd: Dvd) -> DvdTitle:
    """The longest title: the movie (the first title often is not)."""
    ifos: dict[int, bytes] = {}
    best = None
    for number, vts, ttn in _titles(dvd.ifo(0)):
        if vts not in ifos:
            ifos[vts] = dvd.ifo(vts)
        try:
            t = parse_title(ifos[vts], number, vts, ttn)
        except (IndexError, ValueError):
            continue
        if best is None or t.duration > best.duration:
            best = t
    if best is None:
        raise OSError("no title found on the DVD")
    return best


@dataclass
class Nav:
    time: float         # seconds from the start of the title
    pts: int            # 90 kHz PTS of the VOBU's first frame
    sector: int


def read_nav(vobs: VobFile, title: DvdTitle, sector: int):
    """The navigation pack at `sector`, or None if there is none (or it is not ours)."""
    s = vobs.read(sector, 1)
    if len(s) < SECTOR or s[0x26:0x2A] != b"\x00\x00\x01\xbf" or s[0x2C] != 0 \
            or s[0x400:0x404] != b"\x00\x00\x01\xbf" or s[0x406] != 1:
        return None
    dsi = 0x407
    i = title.cell_index(_u16(s, dsi + 0x18), s[dsi + 0x1B])
    if i < 0:
        return None
    return Nav(title.cells[i].start + _dvd_time(s, dsi + 0x1C), _u32(s, 0x2D + 0x0C), sector)


def seek(vobs: VobFile, title: DvdTitle, seconds: float) -> Nav:
    """The last VOBU starting at or before `seconds`, with its exact time."""
    seconds = max(0.0, seconds)
    ci = max(0, bisect.bisect_right([c.start for c in title.cells], seconds) - 1)
    cell = title.cells[ci]
    sector = cell.first
    if title.tmap_unit and title.tmap:                 # the time map gets close
        k = int(seconds // title.tmap_unit) - 1
        if k >= 0 and cell.first <= title.tmap[min(k, len(title.tmap) - 1)] <= cell.last:
            sector = title.tmap[min(k, len(title.tmap) - 1)]
    best = read_nav(vobs, title, sector) or read_nav(vobs, title, cell.first)
    if best is None:
        raise OSError(f"no navigation pack at sector {sector}")
    j = bisect.bisect_right(title.vobus, best.sector)  # then VOBU by VOBU
    while j < len(title.vobus) and title.vobus[j] <= cell.last:
        nav = read_nav(vobs, title, title.vobus[j])
        if nav is None or nav.time > seconds:
            break
        best, j = nav, j + 1
    return best
