"""Recorded discs (tests/recorded.py): a dump replays as the disc it was made
from, and every disc in tests/discs/ gives its expected files and keeps the
pipeline invariants of #75 on its own keyframes."""
import copy
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, override

import pytest

import dvd
import library
import sources
from bluray import Title
from builders import (
    DvdAudioAttr,
    DvdCell,
    DvdSubAttr,
    Item,
    Stream,
    clpi,
    mpls,
    nav_pack,
    vmg_ifo,
    vts_ifo,
)
from fakes import AUDIO, PGS, FakeBluray, FakeDvd, opener, pack
from library import TS_PACKET, Library, VirtualFile
from recorded import DISCS, EXPECTED, RecordedSource, recorded_discs
from sources import BlurayDiscSource, DvdSource, Source, dump_structure, open_disc
from test_library import QUALITY, assert_movie, fake_decode, fake_filler
from tree import ROOT_ID, Tree

# --- synthetic discs: the sources of tests/discs/SYNTHETIC_*.json ---------------

def eps(seconds: int) -> list[tuple[int, int]]:
    """A keyframe about every 5 s, at uneven times (45 kHz, low 8 bits cleared)."""
    return [((k * 225_000 + (k % 7) * 9_000 + (k % 3) * 1_311) & ~0xFF, k * 1000)
            for k in range(seconds // 5 + 1)]


class Synthetic3dMovie(FakeBluray):
    """FakeBluray in small: playlist 00070 the 3D movie in two clips, 00080 the
    same base view and a 2D bumper, longer: the movie on a 2D player only."""

    def __init__(self, three_d: bool = True) -> None:
        super().__init__(three_d=three_d, name="Synthetic 3D Movie - Blu-ray",
                         volume_id="SYNTHETIC_3D_MOVIE")

    @override
    def titles(self) -> list[Title]:
        return [Title("00080", 1300.0), Title("00070", 1200.0)]

    @override
    def read_file(self, path: str) -> bytes:
        name = path.rsplit("/", 1)[1][:5]
        if path.endswith(".mpls"):
            movie = [Item("00131", 45_000, 45_000 * 601, AUDIO, PGS),
                     Item("00133", 90_000, 90_000 + 45_000 * 599, AUDIO, PGS)]
            if name == "00070":
                return mpls(movie, dep_clips=["00132", "00134"])
            return mpls([*movie, Item("00200", 0, 45_000 * 100, AUDIO[:3], PGS)])
        dep = name in ("00132", "00134")
        return clpi(700_000, eps(1300), extents=list(range(0, 700_000, 50_000)),
                    pid=0x1012 if dep else 0x1011)


ITA_ENG = [Stream(0x1100, "ita", 0x81), Stream(0x1101, "eng", 0x81)]
ENG = [Stream(0x1100, "eng", 0x81)]


class SyntheticSeries(FakeBluray):
    """A 2D series: two episodes in ita+eng (00001, 00002), their eng-only copies
    (extras), a play-all with episode 2 first, and episode 1 with a warning clip
    in front of its credits (a variant, left out)."""

    def __init__(self) -> None:
        super().__init__(three_d=False, name="Synthetic Show Season 1",
                         volume_id="SYNTHETIC_SHOW_S1")
        self.playlists = {
            "00001": [Item("00011", 0, 45_000 * 1500, ITA_ENG, PGS[:2])],
            "00002": [Item("00012", 0, 45_000 * 1450, ITA_ENG, PGS[:2])],
            "00003": [Item("00021", 0, 45_000 * 1500, ENG)],
            "00004": [Item("00022", 0, 45_000 * 1450, ENG)],
            "00005": [Item("00012", 0, 45_000 * 1450, ITA_ENG, PGS[:2]),
                      Item("00011", 0, 45_000 * 1500, ITA_ENG, PGS[:2])],
            "00006": [Item("00011", 0, 45_000 * 1500, ITA_ENG, PGS[:2]),
                      Item("00030", 0, 45_000 * 10, ITA_ENG)]}

    @override
    def titles(self) -> list[Title]:
        return [Title(p, sum(i.out_time - i.in_time for i in items) / 45_000)
                for p, items in self.playlists.items()]

    @override
    def read_file(self, path: str) -> bytes:
        name = path.rsplit("/", 1)[1][:5]
        if path.endswith(".mpls"):
            return mpls(self.playlists[name])
        return clpi(300_000, eps(1500))


# VTS 1 and 2: the episodes; VTS 3: a trailer
DVD_CELLS = {1: [DvdCell(700.0, 0, 69_999, 1, 1), DvdCell(800.0, 70_000, 149_999, 1, 2)],
             2: [DvdCell(1440.0, 0, 143_999, 1, 1)],
             3: [DvdCell(90.0, 0, 8_999, 1, 1)]}
VOBU = 5000                                     # sectors: one VOBU every 50 s of cell
UNREADABLE = (1, 35_000)                        # (VTS, sector): a damaged navigation pack


class SyntheticVobs:
    def __init__(self, vts: int) -> None:
        self.vts = vts

    def read(self, sector: int, count: int) -> bytes:
        if count > 1:
            return pack() * count                  # CSS check: clear
        if (self.vts, sector) == UNREADABLE:
            return b""
        start = 0.0
        for c in DVD_CELLS[self.vts]:
            if c.first <= sector <= c.last:
                k = (sector - c.first) // VOBU
                t = k * 50 + (k % 5) * 0.36       # uneven, whole frames at 25 fps
                return nav_pack(c.vob_id, c.cell_id, t, 1_000_000 + round((start + t) * 90_000))
            start += c.duration
        return pack()

    def close(self) -> None:
        pass


class SyntheticDvd(FakeDvd):
    """A two-episode DVD, unlabelled unless given a label."""

    def __init__(self, label: str = "") -> None:
        super().__init__()
        self.label = label

    @override
    def ifo(self, vts: int) -> bytes:
        if vts == 0:
            return vmg_ifo([(1, 1), (2, 1), (3, 1)])
        cells = DVD_CELLS[vts]
        return vts_ifo(cells, [DvdAudioAttr(0, "it", 1, 0), DvdAudioAttr(0, "en", 1, 1)],
                       [DvdSubAttr("it", 1), DvdSubAttr("en", 1, wide=1)],
                       vobus=list(range(0, cells[-1].last, VOBU)))

    @override
    def volume_id(self) -> str:
        return self.label

    @override
    def title_vobs(self, vts: int) -> SyntheticVobs:  # type: ignore[override]
        return SyntheticVobs(vts)


Disc = FakeBluray | FakeDvd


def live(monkeypatch: pytest.MonkeyPatch, disc: Disc) -> None:
    """open_disc() and dump_structure() read `disc`."""
    if isinstance(disc, FakeBluray):
        monkeypatch.setattr(sources, "_open_disc", opener(disc))
        return

    def no_bluray(path: str) -> None:
        raise OSError("not a Blu-ray")

    monkeypatch.setattr(sources, "_open_disc", no_bluray)
    def open_dvd(path: str) -> FakeDvd:
        return disc

    monkeypatch.setattr(dvd, "Dvd", open_dvd)


# --- what is compared ----------------------------------------------------------

def seek_times(duration: float) -> list[float]:
    return [0.0, 0.5, 1.0, 1.7, *(duration * k / 37 for k in range(1, 37)), duration - 0.01]


def titles_of(s: Source) -> list[object]:
    """[title (playlist or DVD title number), name, duration]."""
    title = s.playlist if isinstance(s, BlurayDiscSource) else \
        s.title.number if isinstance(s, DvdSource) else None
    return [title, s.name, s.duration]


def summary(s: Source) -> dict[str, Any]:
    """What the program found: which title, its name, languages and keyframes."""
    return {"kind": type(s).__name__, "title": titles_of(s), "name": s.name, "category": s.category,
            "subfolder": s.subfolder, "duration": s.duration, "two_d": s.two_d,
            "fallback_name": s.fallback_name, "audio_langs": s.audio_langs,
            "audio_desc": s.audio_desc, "subs": [(t.stream, t.lang) for t in s.subs],
            "labels": [v.label for v in s.variants()],
            "keyframes": [s.keyframe_at_or_before(t) for t in seek_times(s.duration)]}


def tree_of(found: list[Source], log: Path) -> list[str]:
    """The files the library shows (one per audio language, per subtitle)."""
    tree = Tree()
    lib = Library(tree, "x264", str(log), "per-language", False)
    for s in found:
        lib.add(s)
    return sorted(tree.path_of(e.inode) for e in tree.nodes.values() if e.inode != ROOT_ID)


# --- synthetic disc -> dump -> replay -------------------------------------------

ROUNDTRIP: dict[str, Callable[[], Disc]] = {
    "bluray-3d": Synthetic3dMovie, "bluray-2d": lambda: Synthetic3dMovie(three_d=False),
    "bluray-series": SyntheticSeries, "dvd-labelled": lambda: SyntheticDvd("SYNTHETIC_DVD"),
    "dvd-unlabelled": SyntheticDvd}
# the committed fixtures are these discs' dumps, file named as the program names the disc
FIXTURES = ("bluray-3d", "bluray-series", "dvd-unlabelled")


@pytest.mark.parametrize("disc", ROUNDTRIP)
def test_replay_finds_what_the_disc_holds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                          disc: str) -> None:
    folder = tmp_path / "Somebody's Rips" / "disc"
    folder.mkdir(parents=True)
    live(monkeypatch, ROUNDTRIP[disc]())
    found = open_disc(str(folder))
    expected = [summary(s) for s in found]          # before the replay replaces the readers
    expected_tree = tree_of(found, tmp_path / "log")
    text = dump_structure(str(folder))

    replay_folder = tmp_path / "elsewhere"          # the name comes from the disc, not its path
    replay_folder.mkdir()
    recorded = RecordedSource(json.loads(text))
    replayed = recorded.sources(monkeypatch, replay_folder)
    assert [summary(s) for s in replayed] == expected
    assert tree_of(replayed, tmp_path / "log") == expected_tree
    if disc == "dvd-unlabelled":
        fingerprint = recorded.data["fingerprint"]
        assert {s.name for s in replayed} == {f"DVD-{fingerprint} - 1", f"DVD-{fingerprint} - 2"}
    if disc in FIXTURES:
        data = recorded.data
        stem = data["volume_id"] or f"DVD-{data['fingerprint']}"
        assert (DISCS / f"{stem}.json").read_text() == text, \
            "the fixture is not this disc's dump: write it again from dump_structure()"


def test_replay_reaches_no_disc_library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The replay stands on the recording alone: libbluray and libdvdread are not
    there, and a disc read through them would fail the test."""
    for path in recorded_discs():
        folder = tmp_path / path.stem
        folder.mkdir()
        found = RecordedSource.load(path).sources(monkeypatch, folder)
        assert found
        for s in found:
            s.keyframe_at_or_before(s.duration / 2)


# --- every recorded disc ------------------------------------------------------------

def test_every_recorded_disc_has_expected_results() -> None:
    discs = {p.stem for p in recorded_discs()}
    assert discs, "no recorded disc in tests/discs"
    assert discs == {p.stem for p in EXPECTED.glob("*.json")}


def expected_of(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((EXPECTED / path.name).read_text())
    return result


@pytest.mark.parametrize("path", recorded_discs(), ids=lambda p: p.stem)
def test_recorded_disc_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                             path: Path) -> None:
    """The titles the program takes from the disc, in order, and the files it
    shows for them: names, languages, extras."""
    found = RecordedSource.load(path).sources(monkeypatch, tmp_path)
    expected = expected_of(path)
    assert [titles_of(s) for s in found] == expected["titles"]
    assert tree_of(found, tmp_path / "log") == expected["tree"]


@pytest.fixture
def fake_pipelines(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[VirtualFile]]:
    """test_library.py's stand-in for FFmpeg and its small caches; every file
    stopped afterwards."""
    for name, value in {"AHEAD_MAX": 256 * 1024, "BEHIND_KEEP": 128 * 1024,
                        "JUMP_TOLERANCE": 256 * 1024, "EDGE_CACHE": 64 * 1024}.items():
        monkeypatch.setattr(library, name, value)
    monkeypatch.setattr(library, "decode_command", fake_decode)
    monkeypatch.setattr(VirtualFile, "filler_command", fake_filler)
    files: list[VirtualFile] = []
    yield files
    for vf in files:
        vf.stop()


POSITIONS = (0.013, 0.25, 0.5, 0.618, 0.9)      # share of the file


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash pipelines and os.killpg, POSIX only until #103")
@pytest.mark.parametrize("path", recorded_discs(), ids=lambda p: p.stem)
def test_recorded_disc_pipelines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                 fake_pipelines: list[VirtualFile], path: Path) -> None:
    """The invariants of test_generator_invariants_with_irregular_keyframes (#75),
    on the disc's own keyframes: the head is cached, a pipeline starts at a
    packet boundary at or before the read and covers it, the same read again
    starts nothing."""
    for source in RecordedSource.load(path).sources(monkeypatch, tmp_path):
        # one language: the fake pipeline writes at the bitrate of one audio track
        s = copy.copy(source.variants()[0])
        s.max_pipelines = len(POSITIONS) + 1      # no read is a late one for a left position
        vf = VirtualFile(10, s, "x264", QUALITY, str(tmp_path / "pipeline.log"))
        fake_pipelines.append(vf)
        edge = library.EDGE_CACHE
        assert edge <= vf.tail_start
        assert_movie(vf.read(0, edge), 0)
        assert vf.gens[0].base == 0 and vf.head is not None
        for p in POSITIONS:
            offset = int(p * vf.size)
            before = list(vf.gens)
            assert_movie(vf.read(offset, 4096), offset)
            started = [g for g in vf.gens if g not in before]
            assert len(started) <= 1
            for g in started:
                assert g.base <= offset and g.base % TS_PACKET == 0
                assert g.covers(offset)
            again = list(vf.gens)
            assert_movie(vf.read(offset, 4096), offset)
            assert vf.gens == again
        vf.stop()
