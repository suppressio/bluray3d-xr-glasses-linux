"""A disc recorded by --dump-structure (tests/discs/*.json), replayed as the
program's own sources: no drive, libbluray, libdvdread, decoder or FFmpeg.

Only the readers are replaced. parse_mpls/parse_clpi (Blu-ray) and dvd.titles/
parse_title/parse_nav (DVD) answer from the recording, so what runs on top of
them is the program's own: open_disc() chooses the titles, names them and seeks
in them exactly as on the disc. Expected results live apart, in
tests/discs/expected/: nothing here knows what the choice should be."""
import builtins
import json
from pathlib import Path
from typing import Any, Self

import pytest

import bluray
import dvd
import sources
from bdmv import AudioStream, Clip, PlayItem, SubStream
from bluray import DiscInfo, Title
from sources import Source, open_disc

DISCS = Path(__file__).parent / "discs"
EXPECTED = DISCS / "expected"


def recorded_discs() -> list[Path]:
    return sorted(DISCS.glob("*.json"))


def _fail(name: str) -> Exception:
    """The error recorded for an unreadable file, of the same built-in type
    where there is one: the program skips some (#63) and not others."""
    kind = getattr(builtins, name, None)
    return kind(f"recorded: {name}") if isinstance(kind, type) and \
        issubclass(kind, Exception) else ValueError(f"recorded: {name}")


def _not_here(*args: object) -> None:
    raise AssertionError("a recorded disc must not reach libbluray or libdvdread")


class _Disc:
    """bluray.Disc / dvd.Dvd: files are named by their path, parsed below."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def titles(self) -> list[Title]:
        return [Title(t["playlist"], t["duration"]) for t in self.data["titles"]]

    def read_file(self, path: str) -> bytes:
        return path.encode()

    def ifo(self, vts: int) -> bytes:
        return str(vts).encode()

    def volume_id(self) -> str:
        return self.data["volume_id"]

    def title_vobs(self, vts: int) -> "_Vobs":
        title_set = self.data["title_sets"].get(str(vts))
        if title_set is None or title_set["navs"] is None:
            raise OSError(f"recorded: cannot open the VOBs of VTS {vts}")
        return _Vobs(vts)

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class _Vobs:
    def __init__(self, vts: int) -> None:
        self.vts = vts

    def read(self, sector: int, count: int) -> bytes:
        # a navigation pack is read one sector at a time; the CSS check reads more
        # and finds no pack: the disc was recorded decrypted
        return f"{self.vts}:{sector}".encode() if count == 1 else b""

    def close(self) -> None:
        pass


class RecordedSource:
    """One recorded disc; sources() is open_disc() on it."""

    def __init__(self, data: dict[str, Any]) -> None:
        if data.get("format") != sources.STRUCTURE_FORMAT:
            raise ValueError(f"recorded format {data.get('format')}, "
                             f"--dump-structure writes {sources.STRUCTURE_FORMAT}")
        self.data = data

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def sources(self, monkeypatch: pytest.MonkeyPatch, folder: Path,
                audio_langs: list[str] | None = None) -> list[Source]:
        """What the program finds on the disc. `folder` only has to exist: the
        program looks at it for its time stamp and side files."""
        monkeypatch.setattr(bluray, "_libbluray", _not_here)
        monkeypatch.setattr(dvd, "_dvdread", _not_here)
        if self.data["kind"] == "bluray":
            self._bluray(monkeypatch)
        else:
            self._dvd(monkeypatch)
        return open_disc(str(folder), audio_langs)

    def _bluray(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = self.data
        playlists = {t["playlist"]: t for t in d["titles"]}
        info = DiscInfo(d["name"], d["volume_id"], d["has_3d"], True, 0)

        def parse_mpls(data: bytes) -> list[PlayItem]:
            title = playlists[Path(data.decode()).stem]
            if "error" in title:
                raise _fail(title["error"])
            return [PlayItem(i["clip"], i["in_time"], i["out_time"], i["dep_clip"],
                             [AudioStream(**a) for a in i["audio"]],
                             [SubStream(**s) for s in i["subs"]]) for i in title["items"]]

        def parse_clpi(data: bytes, pid: int = 0x1011) -> Clip:
            c = d["clips"][Path(data.decode()).stem]
            if "error" in c:
                raise _fail(c["error"])
            assert c["video_pid"] == pid, "the EP_map of another view was recorded"
            return Clip(c["num_packets"], c["ep_pts"], c["ep_spn"], c["extent_start"])

        def open_bluray(path: str) -> tuple[_Disc, DiscInfo, dict[str, str]]:
            return _Disc(d), info, {}

        monkeypatch.setattr(sources, "_open_disc", open_bluray)
        monkeypatch.setattr(sources, "parse_mpls", parse_mpls)
        monkeypatch.setattr(sources, "parse_clpi", parse_clpi)

    def _dvd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = self.data
        by_number = {t["number"]: t for t in d["titles"]}
        navs: dict[bytes, dvd.NavPack | None] = {}
        for vts, title_set in d["title_sets"].items():
            n: dict[str, list[Any]] = title_set["navs"] or {}
            for i, sector in enumerate(title_set["vobus"] if n else []):
                navs[f"{vts}:{sector}".encode()] = None if n["vob_id"][i] is None else \
                    dvd.NavPack(n["vob_id"][i], n["cell_id"][i], n["cell_time"][i], n["pts"][i])

        def no_bluray(path: str) -> None:
            raise OSError("recorded: not a Blu-ray")

        def open_dvd(path: str) -> _Disc:
            return _Disc(d)

        def titles(vmg: bytes) -> list[tuple[int, int, int]]:
            return [(t["number"], t["vts"], t["ttn"]) for t in d["titles"]]

        def parse_title(ifo: bytes, number: int, vts: int, ttn: int) -> dvd.DvdTitle:
            t = by_number[number]
            if "error" in t:
                raise _fail(t["error"])
            return dvd.DvdTitle(number, vts, t["duration"],
                                [dvd.Cell(**c) for c in t["cells"]],
                                [dvd.DvdAudio(**a) for a in t["audio"]],
                                [dvd.DvdSub(**s) for s in t["subs"]],
                                frame_size=t["frame_size"], frame_rate=t["frame_rate"],
                                vobus=d["title_sets"][str(vts)]["vobus"])

        monkeypatch.setattr(sources, "_open_disc", no_bluray)
        monkeypatch.setattr(dvd, "Dvd", open_dvd)
        monkeypatch.setattr(dvd, "titles", titles)
        monkeypatch.setattr(dvd, "parse_title", parse_title)
        monkeypatch.setattr(dvd, "parse_nav", navs.get)
