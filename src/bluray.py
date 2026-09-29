"""
bluray.py — minimal ctypes binding to libbluray: open a disc, ISO or BDMV folder
and read its files, decrypted on the fly when a decryption backend works.

libbluray picks the AACS/BD+ backend when the disc is opened:
  - libaacs (+ ~/.config/aacs/KEYDB.cfg)    default
  - libmmbd (MakeMKV)                        LIBAACS_PATH=libmmbd LIBBDPLUS_PATH=libmmbd
  - nothing, for unencrypted ISO/BDMV folders
"""
import ctypes
import ctypes.util
import functools
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    c_char,
    c_char_p,
    c_int,
    c_int32,
    c_int64,
    c_uint8,
    c_uint32,
    c_uint64,
    c_void_p,
)
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from ctypes import _Pointer  # pyright: ignore[reportPrivateUsage]  # only in typeshed's ctypes

AACS_UNIT = 6144   # the decrypting reader returns exactly one aligned unit per call


def _load() -> ctypes.CDLL:
    names = [ctypes.util.find_library("bluray"), "libbluray.so.4", "libbluray.so.2"]
    for name in filter(None, names):
        try:
            return ctypes.CDLL(name)
        except OSError:
            pass
    raise OSError("libbluray not found (Debian/Ubuntu: apt install libbluray2 or libbluray4)")


class _BDFile(Structure):
    pass


_BDFile._fields_ = [
    ("internal", c_void_p),
    ("close", CFUNCTYPE(None, POINTER(_BDFile))),
    ("seek", CFUNCTYPE(c_int64, POINTER(_BDFile), c_int64, c_int32)),
    ("tell", CFUNCTYPE(c_int64, POINTER(_BDFile))),
    ("eof", CFUNCTYPE(c_int, POINTER(_BDFile))),
    ("read", CFUNCTYPE(c_int64, POINTER(_BDFile), c_void_p, c_int64)),
    ("write", CFUNCTYPE(c_int64, POINTER(_BDFile), c_void_p, c_int64)),
]


class _TitleInfo(Structure):
    _fields_ = [("idx", c_uint32), ("playlist", c_uint32), ("duration", c_uint64),
                ("clip_count", c_uint32), ("angle_count", c_uint8),
                ("chapter_count", c_uint32), ("mark_count", c_uint32),
                ("clips", c_void_p), ("chapters", c_void_p), ("marks", c_void_p),
                ("mvc_base_view_r_flag", c_uint8), ("sdr_conversion_notification_flag", c_uint8)]


class _DiscInfo(Structure):          # BLURAY_DISC_INFO, up to the BD+ fields
    _fields_ = [("bluray_detected", c_uint8), ("disc_name", c_char_p),
                ("udf_volume_id", c_char_p), ("disc_id", c_uint8 * 20),
                ("no_menu_support", c_uint8), ("first_play_supported", c_uint8),
                ("top_menu_supported", c_uint8), ("num_titles", c_uint32),
                ("titles", c_void_p), ("first_play", c_void_p), ("top_menu", c_void_p),
                ("num_hdmv_titles", c_uint32), ("num_bdj_titles", c_uint32),
                ("num_unsupported_titles", c_uint32), ("bdj_detected", c_uint8),
                ("bdj_supported", c_uint8), ("libjvm_detected", c_uint8),
                ("bdj_handled", c_uint8), ("bdj_org_id", c_char * 9),
                ("bdj_disc_id", c_char * 33), ("video_format", c_uint8),
                ("frame_rate", c_uint8), ("content_exist_3D", c_uint8),
                ("initial_output_mode_preference", c_uint8), ("provider_data", c_uint8 * 32),
                ("aacs_detected", c_uint8), ("libaacs_detected", c_uint8),
                ("aacs_handled", c_uint8), ("aacs_error_code", c_int), ("aacs_mkbv", c_int),
                ("bdplus_detected", c_uint8), ("libbdplus_detected", c_uint8),
                ("bdplus_handled", c_uint8)]


@dataclass
class DiscInfo:
    name: str             # disc name from its metadata ('' if none)
    volume_id: str        # UDF volume label, e.g. TRON_LEGACY_3D
    has_3d: bool
    decrypted: bool       # False: AACS/BD+ present and not handled by any backend
    aacs_error: int


@dataclass
class Title:
    playlist: str         # e.g. "00070"
    duration: float       # seconds


@functools.cache
def _libbluray() -> ctypes.CDLL:
    """libbluray, loaded on first use (a DVD-only setup may not have it)."""
    lib = _load()
    lib.bd_open.restype = c_void_p
    lib.bd_open.argtypes = [c_char_p, c_char_p]
    lib.bd_close.argtypes = [c_void_p]
    lib.bd_open_file_dec.restype = POINTER(_BDFile)
    lib.bd_open_file_dec.argtypes = [c_void_p, c_char_p]
    lib.bd_get_disc_info.restype = POINTER(_DiscInfo)
    lib.bd_get_disc_info.argtypes = [c_void_p]
    lib.bd_get_titles.restype = c_uint32
    lib.bd_get_titles.argtypes = [c_void_p, c_uint8, c_uint32]
    lib.bd_get_title_info.restype = POINTER(_TitleInfo)
    lib.bd_get_title_info.argtypes = [c_void_p, c_uint32, c_uint32]
    lib.bd_free_title_info.argtypes = [POINTER(_TitleInfo)]
    return lib


TITLES_RELEVANT = 0x03


class DiscFile:
    """A file on the disc, read-only, decrypted if it is an encrypted stream."""

    def __init__(self, handle: "_Pointer[_BDFile]", path: str) -> None:
        self._h: _Pointer[_BDFile] | None = handle
        self.path = path
        self._buf = ctypes.create_string_buffer(AACS_UNIT)

    def seek(self, offset: int) -> None:
        """Seek to a multiple of 6144 bytes (streams can only be decrypted per unit)."""
        if offset % AACS_UNIT:
            raise ValueError("offset must be a multiple of 6144")
        h = self._handle()
        if h.contents.seek(h, offset, 0) < 0:
            raise OSError(f"seek failed in {self.path}")

    def read_unit(self) -> bytes:
        """Next 6144-byte unit ('' at end of file)."""
        h = self._handle()
        n: int = h.contents.read(h, self._buf, AACS_UNIT)
        return self._buf.raw[:n] if n > 0 else b""

    def read_all(self) -> bytes:
        out = bytearray()
        while unit := self.read_unit():
            out += unit
        return bytes(out)

    def _handle(self) -> "_Pointer[_BDFile]":
        if self._h is None:
            raise ValueError(f"{self.path} is closed")
        return self._h

    def close(self) -> None:
        if self._h:
            self._h.contents.close(self._h)
            self._h = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()


class Disc:
    def __init__(self, path: str) -> None:
        self._bd: int | None = _libbluray().bd_open(path.encode(), None)
        if not self._bd:
            raise OSError(f"cannot open Blu-ray: {path}")

    def open(self, path: str) -> DiscFile:
        h = _libbluray().bd_open_file_dec(self._bd, path.encode())
        if not h:
            raise OSError(f"cannot open {path} on the disc")
        return DiscFile(h, path)

    def info(self) -> DiscInfo:
        d = _libbluray().bd_get_disc_info(self._bd).contents
        encrypted = d.aacs_detected or d.bdplus_detected
        handled = (not d.aacs_detected or d.aacs_handled) and \
                  (not d.bdplus_detected or d.bdplus_handled)
        return DiscInfo(name=(d.disc_name or b"").decode("utf-8", "replace"),
                        volume_id=(d.udf_volume_id or b"").decode("utf-8", "replace"),
                        has_3d=bool(d.content_exist_3D),
                        decrypted=bool(not encrypted or handled),
                        aacs_error=d.aacs_error_code)

    def titles(self, min_seconds: int = 600) -> list[Title]:
        """Playlists at least min_seconds long, duplicates removed."""
        out: list[Title] = []
        for i in range(_libbluray().bd_get_titles(self._bd, TITLES_RELEVANT, min_seconds)):
            ti = _libbluray().bd_get_title_info(self._bd, i, 0)
            if ti:
                out.append(Title(f"{ti.contents.playlist:05d}", ti.contents.duration / 90000))
                _libbluray().bd_free_title_info(ti)
        return out

    def read_file(self, path: str) -> bytes:
        with self.open(path) as f:
            return f.read_all()

    def close(self) -> None:
        if self._bd:
            _libbluray().bd_close(self._bd)
            self._bd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()
