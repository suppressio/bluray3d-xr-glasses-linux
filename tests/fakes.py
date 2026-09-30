"""Stand-ins for a Blu-ray disc, a DVD and ffprobe, built on the synthetic files."""
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from bluray import DiscInfo, Title
from builders import DvdAudioAttr, DvdCell, DvdSubAttr, Item, Stream, clpi, mpls, vmg_ifo, vts_ifo

# --- Blu-ray ---------------------------------------------------------------

AUDIO = [Stream(0x1100, "eng", 0x83), Stream(0x1101, "eng", 0x86), Stream(0x1102, "ita", 0x82),
         Stream(0x1103, "ger", 0x81)]
PGS = [Stream(0x1200, "ita", 0x90), Stream(0x1201, "eng", 0x90), Stream(0x1202, "ita", 0x90)]
# a keyframe every 2 s; the EP_map keeps no low 8 bits of the time
EPS = [((k * 90_000) & ~0xFF, k * 1000) for k in range(3000)]


@dataclass
class FakeBluray:
    """Playlist 00070: a 3D movie in two clips; 00080: a 2D version, longer."""
    three_d: bool = True
    name: str = "Tron: Legacy - Blu-ray"
    volume_id: str = "TRON_LEGACY_3D"
    decrypted: bool = True
    protection: str = "AACS"
    closed: bool = False

    def info(self) -> DiscInfo:
        return DiscInfo(self.name, self.volume_id, self.three_d, self.decrypted, 0, self.protection)

    def titles(self) -> list[Title]:
        return [Title("00080", 7300.0), Title("00070", 7200.0)]

    def read_file(self, path: str) -> bytes:
        files = {
            "BDMV/PLAYLIST/00070.mpls": mpls(
                [Item("00131", 45_000, 45_000 * 3601, AUDIO, PGS),
                 Item("00133", 90_000, 90_000 + 45_000 * 3599, AUDIO, PGS)],
                dep_clips=["00132", "00134"]),
            "BDMV/PLAYLIST/00080.mpls": mpls([Item("00200", 0, 45_000 * 7300, AUDIO[:3], PGS)]),
        }
        if path in files:
            return files[path]
        clip = path.rsplit("/", 1)[1][:5]
        dep = clip in ("00132", "00134")
        return clpi(3_000_000, EPS, extents=list(range(0, 3_000_000, 50_000)),
                    pid=0x1012 if dep else 0x1011)

    def close(self) -> None:
        self.closed = True


def opener(disc: FakeBluray, env: dict[str, str] | None = None
           ) -> Callable[[str], tuple[FakeBluray, DiscInfo, dict[str, str]]]:
    """A stand-in for sources._open_disc that opens `disc` with the backend `env`."""
    def open_disc(path: str) -> tuple[FakeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), env or {}
    return open_disc


# --- DVD ---------------------------------------------------------------------

class FakeDvd:
    """Title 1 is a short trailer (VTS 1), title 2 the movie (VTS 2)."""

    def __init__(self, path: str = "") -> None:
        self.path, self.closed = path, False
        self.movie = vts_ifo(
            [DvdCell(3000.0, 0, 99_999, 1, 1), DvdCell(3000.0, 100_000, 199_999, 2, 1)],
            [DvdAudioAttr(0, "it", 1, 0), DvdAudioAttr(0, "en", 1, 1), DvdAudioAttr(0, "en", 3, 2)],
            [DvdSubAttr("it", 1, wide=0), DvdSubAttr("it", 9, wide=1), DvdSubAttr("en", 1, wide=2)],
            palette=[(235, 128, 128)] * 16, pgcn=2, vobus=[0, 100_000])

    def ifo(self, vts: int) -> bytes:
        if vts == 0:
            return vmg_ifo([(1, 1), (2, 1)])
        if vts == 1:
            return vts_ifo([DvdCell(90.0, 0, 999)], [DvdAudioAttr(0, "en")], [])
        return self.movie

    def volume_id(self) -> str:
        return "BACK_TO_THE_FUTURE"

    def title_vobs(self, vts: int) -> object:
        return object()

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()


# --- MKV (ffprobe output) ------------------------------------------------------

def mkv_probe(*args: str) -> dict[str, Any]:
    if "-read_intervals" in args:                       # keyframe lookup
        return {"packets": [{"pts_time": "N/A", "flags": "K_"},
                            {"pts_time": "41.708", "flags": "__"},
                            {"pts_time": "42.125", "flags": "K_"}]}
    return {"format": {"duration": "7512.3"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {"index": 1, "codec_type": "audio", "codec_name": "dts",
                 "tags": {"language": "ita"}},
                {"index": 2, "codec_type": "audio", "codec_name": "ac3",
                 "tags": {"language": "ita"}},
                {"index": 3, "codec_type": "audio", "codec_name": "truehd",
                 "tags": {"language": "eng"}},
                {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
                 "tags": {"language": "ita"}},
                {"index": 5, "codec_type": "subtitle", "codec_name": "subrip",
                 "tags": {"language": "eng"}},
                {"index": 6, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
                 "tags": {"language": "fre"}},
            ]}
