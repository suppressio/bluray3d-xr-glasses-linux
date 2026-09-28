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
from ctypes import CFUNCTYPE, POINTER, Structure, c_char_p, c_int, c_int32, c_int64, c_void_p

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

_lib = _load()
_lib.bd_open.restype = c_void_p
_lib.bd_open.argtypes = [c_char_p, c_char_p]
_lib.bd_close.argtypes = [c_void_p]
_lib.bd_open_file_dec.restype = POINTER(_BDFile)
_lib.bd_open_file_dec.argtypes = [c_void_p, c_char_p]


class DiscFile:
    """A file on the disc, read-only, decrypted if it is an encrypted stream."""

    def __init__(self, handle: POINTER(_BDFile), path: str):
        self._h = handle
        self.path = path
        self._buf = ctypes.create_string_buffer(AACS_UNIT)

    def seek(self, offset: int) -> None:
        """Seek to a multiple of 6144 bytes (streams can only be decrypted per unit)."""
        if offset % AACS_UNIT:
            raise ValueError("offset must be a multiple of 6144")
        if self._h.contents.seek(self._h, offset, 0) < 0:
            raise OSError(f"seek failed in {self.path}")

    def read_unit(self) -> bytes:
        """Next 6144-byte unit ('' at end of file)."""
        n = self._h.contents.read(self._h, self._buf, AACS_UNIT)
        return self._buf.raw[:n] if n > 0 else b""

    def read_all(self) -> bytes:
        out = bytearray()
        while unit := self.read_unit():
            out += unit
        return bytes(out)

    def close(self) -> None:
        if self._h:
            self._h.contents.close(self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Disc:
    def __init__(self, path: str):
        self._bd = _lib.bd_open(path.encode(), None)
        if not self._bd:
            raise OSError(f"cannot open Blu-ray: {path}")

    def open(self, path: str) -> DiscFile:
        h = _lib.bd_open_file_dec(self._bd, path.encode())
        if not h:
            raise OSError(f"cannot open {path} on the disc")
        return DiscFile(h, path)

    def read_file(self, path: str) -> bytes:
        with self.open(path) as f:
            return f.read_all()

    def close(self) -> None:
        if self._bd:
            _lib.bd_close(self._bd)
            self._bd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
