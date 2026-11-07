import itertools
import json
import logging
import os
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, override

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import bluray as bluray_module
import dvd
import osplatform
import sources
from bdmv import AudioStream, Clip, PlayItem, m2ts_seek, ssif_seek
from bluray import DiscInfo, Title
from builders import (
    DvdAudioAttr,
    DvdCell,
    DvdSubAttr,
    Item,
    Stream,
    clpi,
    mpls,
    nav_pack,
    vts_ifo,
)
from fakes import EPS, FakeBluray, FakeDvd, mkv_probe, opener, pack
from sources import (
    BlurayDiscSource,
    DvdSource,
    MkvSource,
    SubTrack,
    _first_per_lang,
    _parse_season,
    _pick_audio,
    _safe_name,
    discover,
    find_sidecars,
    is_bluray,
    is_dvd,
    open_disc,
)


@pytest.fixture(autouse=True)
def private_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FIFOs and IFO copies go in the test's own temporary folder."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    sources._run_dir.cache_clear()


@pytest.mark.parametrize(("name", "expected"), [
    ("Tron: Legacy - Blu-ray", "Tron - Legacy"),
    ("Ready Player One \u2013 Blu-ray 3D\u2122", "Ready Player One"),    # en dash, trademark
    ('A/B "C" <D>?', "A - B - C - D"),
    ("  Spaces   everywhere . ", "Spaces everywhere"),
    ("Blu-ray", "Blu-ray"),
    ("", "Blu-ray"),
])
def test_safe_name(name: str, expected: str) -> None:
    assert _safe_name(name) == expected


@pytest.mark.parametrize(("name", "expected"), [
    ("Rick_and_Morty_Staffel1", (1, "Rick and Morty")),
    ("Rick and Morty - Staffel 01", (1, "Rick and Morty")),
    ("RICK_AND_MORTY_S1", (1, "RICK AND MORTY")),
    ("Westworld Season 2", (2, "Westworld")),
    ("Il Trono Di Spade Stagione 3", (3, "Il Trono Di Spade")),
    ("Dix Pour Cent Saison 1", (1, "Dix Pour Cent")),
    ("Serie_Temporada_2", (2, "Serie")),
    ("Show Season 1 Disc 2", None),           # split over discs: unreliable
    ("Show Vol. 2 Season 1", None),
    ("Show Stagione 1 Disco 2", None),
    ("Dix Pour Cent Saison 1 Disque 2", None),
    ("Firefly", None),                         # no season recognised
])
def test_parse_season(name: str, expected: tuple[int, str] | None) -> None:
    assert _parse_season(name) == expected


@pytest.mark.parametrize(("names", "expected"), [
    (("Show", "SHOW_S1"), (1, "Show")),                     # season from the label
    (("Show Season 2", "SHOW_S1"), (2, "Show")),            # the disc's name wins
    (("Show Season 1", "SHOW_S1_DISC_2"), None),            # part marker in the label
    (("", "SHOW_S1"), (1, "SHOW")),                         # no name: the label alone
])
def test_parse_season_name_and_label(names: tuple[str, str],
                                     expected: tuple[int, str] | None) -> None:
    assert _parse_season(*names) == expected


A = [AudioStream(1, "truehd", "eng"), AudioStream(2, "dts-hd ma", "eng"),
     AudioStream(3, "ac3", "ita"), AudioStream(4, "dts", "ita"), AudioStream(5, "ac3", "deu")]


def test_pick_audio() -> None:
    assert [a.pid for a in _pick_audio(A, None)] == [2, 4, 5]        # every language, best codec
    assert [a.pid for a in _pick_audio(A, ["ita", "eng"])] == [4, 2]  # requested order
    assert [a.pid for a in _pick_audio(A, ["jpn"])] == [1]            # none found: the first
    assert _pick_audio([], ["ita"]) == []


def test_pick_audio_logs_fallback(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="bd3d_fs")
    _pick_audio(A, ["jpn"], log_as="Episode 1")
    assert "Episode 1: no jpn audio track, using eng" in caplog.text
    caplog.clear()
    _pick_audio(A, ["ita"], log_as="Episode 1")   # found: nothing logged
    assert not caplog.text
    _pick_audio(A, ["jpn"])                        # no log_as: nothing logged
    assert not caplog.text


def test_first_per_lang() -> None:
    tracks = [SubTrack(1, "ita"), SubTrack(2, "eng"), SubTrack(3, "ita")]
    assert _first_per_lang(tracks) == [SubTrack(1, "ita"), SubTrack(2, "eng")]


def test_find_sidecars(tmp_path: Path) -> None:
    for name in ("Movie.mkv", "Movie.srt", "Movie.ita.srt", "Movie.eng.SUP", "Movie.txt",
                 "Movie2.srt", "Other.srt"):
        (tmp_path / name).write_text("x")
    assert find_sidecars(str(tmp_path / "Movie.mkv")) == [
        (".eng.SUP", str(tmp_path / "Movie.eng.SUP")),
        (".ita.srt", str(tmp_path / "Movie.ita.srt")),
        (".srt", str(tmp_path / "Movie.srt"))]


def test_is_bluray_is_dvd(tmp_path: Path) -> None:
    (tmp_path / "bd" / "BDMV").mkdir(parents=True)
    (tmp_path / "bd" / "BDMV" / "index.bdmv").write_text("")
    (tmp_path / "dvd" / "VIDEO_TS").mkdir(parents=True)
    (tmp_path / "dvd" / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("")
    (tmp_path / "x.ISO").write_text("")
    assert is_bluray(tmp_path / "bd") and not is_dvd(tmp_path / "bd")
    assert is_dvd(tmp_path / "dvd") and not is_bluray(tmp_path / "dvd")
    assert is_bluray(tmp_path / "x.ISO")
    assert not is_bluray(tmp_path / "missing")


# --- Blu-ray -----------------------------------------------------------------

def bluray(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, disc: FakeBluray,
           langs: list[str] | None = None, env: dict[str, str] | None = None) -> BlurayDiscSource:
    iso = tmp_path / "Tron.iso"
    iso.write_text("")
    (tmp_path / "Tron.ita.srt").write_text("1")
    monkeypatch.setattr(sources, "_open_disc", opener(disc, env))
    return BlurayDiscSource(str(iso), langs)


def test_bluray_3d(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    disc = FakeBluray()
    s = bluray(monkeypatch, tmp_path, disc)
    assert disc.closed
    assert not s.two_d and s.playlist == "00070"       # the longest title with MVC in every clip
    assert (s.category, s.name_suffix, s.quality_key) == ("Blu-ray 3D", " - 3D SBS", "3d")
    assert s.name == "Tron - Legacy"
    assert s.duration == pytest.approx(3600.0 + 3599.0)
    assert s.starts == [0.0, 3600.0]
    assert (s.audio_pids, s.audio_langs) == ([0x1101, 0x1102, 0x1103], ["eng", "ita", "deu"])
    assert s.audio_desc == "0x1101 eng dts-hd ma, 0x1102 ita dts, 0x1103 deu ac3"
    assert [(t.stream, t.lang) for t in s.subs] == [(0x1200, "ita"), (0x1201, "eng")]
    assert s.sidecars == [(".ita.srt", str(tmp_path / "Tron.ita.srt"))]

    v = s.variants()
    assert [(x.label, x.audio_pids, x.audio_desc) for x in v] == [
        ("ENG", [0x1101], "0x1101 eng dts-hd ma"), ("ITA", [0x1102], "0x1102 ita dts"),
        ("DEU", [0x1103], "0x1103 deu ac3")]
    assert s.audio_map() == ("-map 1:i:0x1101 -metadata:s:a:0 language=eng "
                             "-map 1:i:0x1102 -metadata:s:a:1 language=ita "
                             "-map 1:i:0x1103 -metadata:s:a:2 language=deu")


def test_bluray_keyframes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = bluray(monkeypatch, tmp_path, FakeBluray())
    assert s.keyframe_at_or_before(0) == 0.0
    assert s.keyframe_at_or_before(-3) == 0.0
    ms = 0.006                                   # EP_map precision (256 / 45 kHz)
    # clip 1 has its in time at 1 s: keyframes at -1, 1, 3 ... s of the title
    assert s.keyframe_at_or_before(101.5) == pytest.approx(101.0, abs=ms)
    # clip 2 starts at 3600 s with in time 2 s: keyframes at 3600, 3602 ...
    assert s.keyframe_at_or_before(3605.0) == pytest.approx(3604.0, abs=ms)
    # the truncated EP_map time falls a few ms before the in time: never before the clip
    assert s.keyframe_at_or_before(3600.5) == 3600.0


def test_bluray_keyframe_within_its_clip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # a keyframe before its clip's start sends the decoder to a time no clip holds
    # (371d80a); one after the requested time leaves a gap the pipeline never covers
    s = bluray(monkeypatch, tmp_path, FakeBluray())
    ends = [*s.starts[1:], s.duration]

    @settings(max_examples=200, deadline=None)
    @given(clip=st.integers(0, len(s.starts) - 1), into=st.floats(0.0, 1.0, exclude_max=True))
    def within_clip(clip: int, into: float) -> None:
        seconds = s.starts[clip] + into * (ends[clip] - s.starts[clip])
        assert s.starts[clip] <= s.keyframe_at_or_before(seconds) <= seconds

    within_clip()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="3D audio uses os.mkfifo, POSIX only until #103")
def test_bluray_3d_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = bluray(monkeypatch, tmp_path, FakeBluray(), ["ita"],
               env={"LIBAACS_PATH": "libmmbd", "LIBBDPLUS_PATH": "libmmbd"})
    s.sub = s.subs[1]
    cmd = shlex.split(s.video_command(100.0))
    assert cmd[:2] == ["LIBAACS_PATH=libmmbd", "LIBBDPLUS_PATH=libmmbd"]
    assert cmd[3].endswith("disc_reader.py")
    assert cmd[4:] == [str(tmp_path / "Tron.iso"), "--playlist", "00070", "--start", "100.002",
                       "--audio-pid", "0x1102", "--audio-pid", "0x1201", "--audio-out", cmd[-1]]
    fifo = cmd[-1]
    assert Path(fifo).is_fifo()
    assert s.audio_input(100.0) == \
        f"-f mpegts -analyzeduration 1000000 -probesize 5000000 -i {fifo}"
    # every pipeline start gets a new FIFO, the previous one goes
    fifo2 = shlex.split(s.video_command(0.0))[-1]
    assert fifo2 != fifo and not os.path.exists(fifo) and Path(fifo2).is_fifo()


def test_bluray_3d_audio_fifo_is_posix_only(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    # Windows has no os.mkfifo: a clear error, not an AttributeError (#103)
    s = bluray(monkeypatch, tmp_path, FakeBluray(), ["ita"])
    monkeypatch.setattr(sources.sys, "platform", "win32")
    with pytest.raises(NotImplementedError, match=r"os\.mkfifo"):
        s.video_command(100.0)


def test_bluray_2d(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    disc = FakeBluray(three_d=False, name="", volume_id="READY_PLAYER_ONE")
    s = bluray(monkeypatch, tmp_path, disc,
               ["ita", "eng"])
    assert s.two_d and s.playlist == "00080"
    assert (s.category, s.name_suffix, s.frame_size, s.quality_key) == \
        ("Blu-ray", "", "1920x1080", "2d")
    assert s.name == "Ready Player One"
    assert s.audio_langs == ["ita", "eng"]
    cmd = s.video_command(10.0)
    assert cmd.endswith("--audio-pid 0x1102 --audio-pid 0x1101 --mode 2d")
    assert "--audio-out" not in cmd


@pytest.mark.parametrize("three_d", [False, True])
def test_bluray_longest_playlist_malformed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                           caplog: pytest.LogCaptureFixture,
                                           three_d: bool) -> None:
    """#65: the longest playlist (00080) is malformed: the movie is the next readable
    title, 00070 (in 3D when the disc has it), instead of the disc failing."""
    read_file = FakeBluray.read_file
    broken = {"BDMV/PLAYLIST/00080.mpls"}

    def read(self: FakeBluray, path: str) -> bytes:
        return b"not MPLS" if path in broken else read_file(self, path)

    monkeypatch.setattr(FakeBluray, "read_file", read)
    caplog.set_level(logging.WARNING, logger="bd3d_fs")
    disc = FakeBluray(three_d=three_d)
    s = bluray(monkeypatch, tmp_path, disc)
    assert (s.playlist, s.two_d) == ("00070", not three_d)
    assert f"{tmp_path / 'Tron.iso'}: playlist 00080 skipped: not an MPLS file" in caplog.text
    assert disc.closed

    broken.add("BDMV/PLAYLIST/00070.mpls")              # nothing readable left
    with pytest.raises(OSError, match="no readable title"):
        bluray(monkeypatch, tmp_path, disc)


def test_bluray_no_title(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    disc = FakeBluray()
    def no_titles(self: FakeBluray) -> list[Title]:
        return []

    monkeypatch.setattr(FakeBluray, "titles", no_titles)
    with pytest.raises(OSError, match="no title"):
        bluray(monkeypatch, tmp_path, disc)
    assert disc.closed


@dataclass
class MultiEpisodeBluray:
    """A disc whose titles are given directly as {playlist: items}, for all_on();
    3D when `dep_clips` gives the MVC dependent view clips of some playlists."""
    playlists: dict[str, list[Item]]
    name: str = "Rick and Morty Staffel1 - Blu-ray"
    volume_id: str = "RICK_AND_MORTY_S1"
    closed: bool = False
    dep_clips: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def info(self) -> DiscInfo:
        return DiscInfo(self.name, self.volume_id, bool(self.dep_clips), True, 0, "AACS")

    def titles(self) -> list[Title]:
        return [Title(pl, sum((it.out_time - it.in_time) / 45_000 for it in items))
                for pl, items in self.playlists.items()]

    def read_file(self, path: str) -> bytes:
        name = path.rsplit("/", 1)[1][:5]
        if path.endswith(".mpls"):
            return mpls(self.playlists[name], self.dep_clips.get(name))
        dep = any(name in clips for clips in self.dep_clips.values())
        return clpi(3_000_000, EPS, extents=list(range(0, 3_000_000, 50_000)),
                    pid=0x1012 if dep else 0x1011)

    def close(self) -> None:
        self.closed = True


def test_bluray_all_on_dedup_and_audio_fallback(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Reproduces #25, shaped like the real disc (Rick_Morty_S1, MPLS dump in PR #34):
    episodes with ita+eng, a variant playlist per episode with a near-empty clip appended
    (same episode, must not be listed twice), and further titles with their own clips and
    no Italian track (must still show up, with the audio fallback made visible)."""
    iso = tmp_path / "Rick.iso"
    iso.write_text("")

    def ep(clip: str, audio: list[Stream]) -> Item:
        return Item(clip, 0, 45_000 * 1200, audio=audio)

    def card(clip: str) -> Item:
        return Item(clip, 0, 45_000 * 5)

    ita_and_eng = [Stream(0x1100, "eng", 0x81), Stream(0x1101, "ita", 0x81)]
    eng_only = [Stream(0x1100, "eng", 0x81)]
    disc = MultiEpisodeBluray({
        "00001": [ep("00010", ita_and_eng)],
        "00002": [ep("00020", ita_and_eng)],
        # the same two episodes with a short clip appended (01004... on the real disc)
        "00013": [ep("00010", ita_and_eng), card("00099")],
        "00014": [ep("00020", ita_and_eng), card("00099")],
        # a separate pair of titles, really without an Italian track
        "00021": [ep("00030", eng_only)],
        "00022": [ep("00040", eng_only)],
    })
    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    caplog.set_level(logging.WARNING, logger="bd3d_fs")

    found = [s for s in BlurayDiscSource.all_on(str(iso), ["ita"])
             if isinstance(s, BlurayDiscSource)]

    assert [s.playlist for s in found] == ["00001", "00002", "00021", "00022"]
    assert [s.audio_langs for s in found] == [["ita"], ["ita"], ["eng"], ["eng"]]
    assert caplog.text.count("no ita audio track, using eng") == 2
    assert "Rick and Morty - Extra 1: no ita audio track, using eng" in caplog.text
    assert "Rick and Morty - Extra 2: no ita audio track, using eng" in caplog.text
    # #27: the season from the disc name ("Staffel1"), episodes in play-all order;
    # #39: the eng-only titles are the disc's extras, numbered apart in Extra/;
    # #81: named after the show like the episodes, no episode number applies to them
    assert [(s.name, s.subfolder) for s in found] == [
        ("S01E01 - Rick and Morty", ""), ("S01E02 - Rick and Morty", ""),
        ("Rick and Morty - Extra 1", "Extra"),
        ("Rick and Morty - Extra 2", "Extra")]
    # the same split whatever language is asked for (#39: eng would number them 3, 4)
    for langs in (["eng"], None):
        again = BlurayDiscSource.all_on(str(iso), langs)
        assert [s.subfolder for s in again] == ["", "", "Extra", "Extra"]


def _ep(clip: str) -> Item:
    return Item(clip, 0, 45_000 * 1200, audio=[Stream(0x1100, "eng", 0x81)])


def _card(clip: str) -> Item:
    return Item(clip, 0, 45_000 * 5)


def test_bluray_all_on_single_title(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    iso = tmp_path / "Movie.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({"00001": [_ep("00010")]})

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = BlurayDiscSource.all_on(str(iso))
    assert len(found) == 1
    assert isinstance(found[0], BlurayDiscSource)
    assert (found[0].playlist, found[0].name, found[0].subfolder) == \
        ("00001", "Rick and Morty Staffel1", "")
    assert disc.closed


@pytest.mark.parametrize(("duration", "count"), [(200, 1), (300, 1), (301, 4)])
def test_bluray_all_on_short_titles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                    duration: int, count: int) -> None:
    iso = tmp_path / "Movie.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({
        f"{i:05}": [Item(f"{i * 10:05}", 0, 45_000 * duration,
                         audio=[Stream(0x1100, "eng", 0x81)])]
        for i in range(1, 5)
    })

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = BlurayDiscSource.all_on(str(iso))
    assert len(found) == count
    assert [s.name for s in found] == (["Rick and Morty Staffel1"] if count == 1 else
                                      [f"S01E{i:02} - Rick and Morty" for i in range(1, 5)])


@pytest.mark.parametrize(("longest", "count"), [(1500, 1), (1490, 2)])
def test_bluray_all_on_duration_ratio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                      longest: int, count: int) -> None:
    iso = tmp_path / "Movie.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({
        "00001": [Item("00010", 0, 45_000 * 1000, audio=[Stream(0x1100, "eng", 0x81)])],
        "00002": [Item("00020", 0, 45_000 * longest, audio=[Stream(0x1100, "eng", 0x81)])],
    })

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]
    assert len(found) == count
    assert [(s.playlist, s.name) for s in found] == (
        [("00002", "Rick and Morty Staffel1")] if count == 1 else
        [("00001", "S01E01 - Rick and Morty"), ("00002", "S01E02 - Rick and Morty")])


def test_bluray_all_on_variants_of_one_episode(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    iso = tmp_path / "Movie.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({
        "00001": [_ep("00010")],
        "00002": [_ep("00010"), _card("00099")],
        "00003": [_ep("00010"), _card("00098")],
    })

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = BlurayDiscSource.all_on(str(iso))
    assert len(found) == 1
    assert isinstance(found[0], BlurayDiscSource)
    # Movie selection uses the longest original playlist, rather than the dedup winner.
    assert (found[0].playlist, found[0].name) == ("00002", "Rick and Morty Staffel1")


@pytest.mark.parametrize("duration", [1200, 200])
def test_bluray_all_on_malformed_mpls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                      caplog: pytest.LogCaptureFixture, duration: int) -> None:
    """#63: a malformed playlist is skipped (logged), long or short, and the disc loads
    with the other titles; the short one no longer turns a series into a movie."""
    iso = tmp_path / "Movie.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({
        "00001": [_ep("00010")], "00002": [_ep("00020")],
        "00003": [Item("00030", 0, 45_000 * duration)],
    })
    read_file = MultiEpisodeBluray.read_file
    malformed_reads = 0

    def read(self: MultiEpisodeBluray, path: str) -> bytes:
        nonlocal malformed_reads
        if path == "BDMV/PLAYLIST/00003.mpls":
            malformed_reads += 1
            return b"not MPLS"
        return read_file(self, path)

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(MultiEpisodeBluray, "read_file", read)
    monkeypatch.setattr(sources, "_open_disc", open_disc)
    caplog.set_level(logging.WARNING, logger="bd3d_fs")
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]
    assert [(s.playlist, s.name) for s in found] == [
        ("00001", "S01E01 - Rick and Morty"), ("00002", "S01E02 - Rick and Morty")]
    assert f"{iso}: playlist 00003 skipped: not an MPLS file" in caplog.text
    assert malformed_reads == 1
    assert disc.closed


def test_bluray_all_on_only_play_all_playlists(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    iso = tmp_path / "Series.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({
        "00001": [_ep("00010"), _ep("00030"), _ep("00020")],
        "00002": [_ep("00020"), _ep("00010"), _ep("00030")],
        "00003": [_ep("00030"), _ep("00020"), _ep("00010")],
    })

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]
    # All titles are candidates again, ordered by the lowest-numbered play-all.
    assert [(s.playlist, s.name, s.duration) for s in found] == [
        ("00001", "S01E01 - Rick and Morty", 3600),
        ("00003", "S01E02 - Rick and Morty", 3600),
        ("00002", "S01E03 - Rick and Morty", 3600),
    ]
    assert disc.closed


def test_bluray_all_on_no_play_all_keeps_playlist_order(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#17: with no play-all playlist on the disc, episodes keep today's order:
    sorted by playlist number."""
    iso = tmp_path / "Series.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({"00001": [_ep("00020")], "00002": [_ep("00010")]})

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]
    assert [s.playlist for s in found] == ["00001", "00002"]


def test_bluray_all_on_uses_play_all_order(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#17: a play-all playlist (PlayItems visiting each episode's opening clip in
    sequence) gives the authoritative episode order, even when playlist numbers run
    the other way; the play-all itself is not exposed as an episode."""
    iso = tmp_path / "Series.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({
        "00001": [_ep("00020")],              # playlist number says "episode 1"...
        "00002": [_ep("00010")],              # ... but its clip is episode 2's
        "00003": [_ep("00010"), _ep("00020")],  # play-all: clip 00010, then clip 00020
    })

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]

    assert [s.playlist for s in found] == ["00002", "00001"]
    assert [s.name for s in found] == \
        ["S01E01 - Rick and Morty", "S01E02 - Rick and Morty"]


def test_bluray_all_on_play_all_with_lowest_number(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#17: a play-all with the lowest playlist number shares its opening clip with an
    episode; it must not win the deduplication in that episode's place (its double
    length would then fail the similar-duration test and collapse the disc into one
    title)."""
    iso = tmp_path / "Series.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({
        "00000": [_ep("00010"), _ep("00020")],  # play-all: clip 00010, then clip 00020
        "00001": [_ep("00020")],
        "00002": [_ep("00010")],
    })

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]

    assert [s.playlist for s in found] == ["00002", "00001"]


def test_bluray_all_on_play_all_order_with_variants(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#17: the play-all order and the alternate-audio/subtitle dedup compose: variant
    playlists (same opening clip, a short clip appended) are still deduplicated, and the
    surviving episodes are still reordered by the play-all."""
    iso = tmp_path / "Series.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({
        "00001": [_ep("00020")],
        "00002": [_ep("00010")],
        "00011": [_ep("00020"), _card("00099")],   # variant of the "00020" episode
        "00012": [_ep("00010"), _card("00099")],   # variant of the "00010" episode
        "00020": [_ep("00010"), _ep("00020")],      # play-all: clip 00010, then clip 00020
    })

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]

    assert [s.playlist for s in found] == ["00002", "00001"]
    assert [s.name for s in found] == \
        ["S01E01 - Rick and Morty", "S01E02 - Rick and Morty"]


def _movie(clip: str, minutes: int, langs: tuple[str, ...] = ("eng",)) -> Item:
    return Item(clip, 0, 45_000 * 60 * minutes,
                audio=[Stream(0x1100 + n, lang, 0x81) for n, lang in enumerate(langs)])


@pytest.mark.parametrize(("playlists", "dep_clips", "expected"), [
    # #67, Tron: Legacy 3D: the movie (00070) and its two halves, each one in 3D
    ({"00070": [_movie("00131", 65), _movie("00133", 60)],
      "00082": [_movie("00131", 65)], "00083": [_movie("00133", 60)]},
     {"00070": ["00132", "00134"], "00082": ["00132"], "00083": ["00134"]},
     [("00070", 7500)]),
    # the same, plus a longer 3D title that repeats a half: not the movie (review of #67)
    ({"00070": [_movie("00131", 65), _movie("00133", 60)],
      "00082": [_movie("00131", 65)], "00083": [_movie("00133", 60)],
      "00090": [_movie("00131", 65), _movie("00133", 60), _movie("00131", 65)]},
     {"00070": ["00132", "00134"], "00082": ["00132"], "00083": ["00134"],
      "00090": ["00132", "00134", "00132"]},
     [("00070", 7500)]),
    # Star Trek Into Darkness 3D: one relevant title, a short 2D trailer
    ({"00800": [_movie("00800", 132)], "00801": [_movie("00900", 2)]},
     {"00800": ["00801"]}, [("00800", 7920)]),
    # Mad Max: Fury Road 3D: two playlists of the same clip, other audio languages
    ({"00098": [_movie("00098", 120, ("eng", "jpn"))],
      "00103": [_movie("00098", 120, ("eng", "ita", "spa"))]},
     {"00098": ["00109"], "00103": ["00109"]}, [("00098", 7200)]),
])
def test_bluray_all_on_3d_movie(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                playlists: dict[str, list[Item]],
                                dep_clips: dict[str, list[str]],
                                expected: list[tuple[str, int]]) -> None:
    """#67: a 3D disc whose 3D play-all covers every part is one movie, in 3D."""
    iso = tmp_path / "Movie.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray(playlists, name="Movie 3D", volume_id="MOVIE_3D",
                              dep_clips=dep_clips)

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]
    assert [(s.playlist, s.duration) for s in found] == expected
    assert [(s.two_d, s.name, s.subfolder) for s in found] == [(False, "Movie 3D", "")]


@pytest.mark.parametrize("extra", [
    {},
    # a 3D montage of 5 minutes from each episode: not a play-all of the whole parts
    {"00003": [Item("00010", 0, 45_000 * 300), Item("00020", 0, 45_000 * 300)]},
])
def test_bluray_all_on_3d_series_without_play_all(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: dict[str, list[Item]]) -> None:
    """#67: the 3D movie rule needs a play-all of the whole parts; without one, 3D
    episodes stay episodes."""
    iso = tmp_path / "Series.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({"00001": [_ep("00010")], "00002": [_ep("00020")]} | extra,
                              dep_clips={"00001": ["00011"], "00002": ["00021"],
                                         "00003": ["00011", "00021"]})

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]
    assert [(s.playlist, s.two_d) for s in found] == [("00001", False), ("00002", False)]


def _two_episode_disc(name: str, label: str) -> MultiEpisodeBluray:
    def ep(clip: str) -> Item:
        return Item(clip, 0, 45_000 * 1200, audio=[Stream(0x1100, "eng", 0x81)])

    return MultiEpisodeBluray({"00001": [ep("00010")], "00002": [ep("00020")]},
                              name=name, volume_id=label)


@pytest.mark.parametrize(("name", "label", "expected"), [
    # #27: a recognised, reliable season -> S<season>E<episode> first in the name
    ("Rick_and_Morty_Staffel1 - Blu-ray", "RICK_AND_MORTY_S1",
     ["S01E01 - Rick and Morty", "S01E02 - Rick and Morty"]),
    # no season, no disc marker recognised (#80): episode number first
    ("Firefly - Blu-ray", "FIREFLY", ["E01 - Firefly", "E02 - Firefly"]),
    # a season split over discs: the first episode number is not known, no S/E;
    # the disc marker shows in parentheses instead of reading as part of the show (#80)
    ("Rick and Morty Staffel 1 Disc 2 - Blu-ray", "RICK_AND_MORTY_S1_D2",
     ["E01 - Rick and Morty Staffel 1 (Disc 2)", "E02 - Rick and Morty Staffel 1 (Disc 2)"]),
    # the season only in the label: the show keeps the disc's own name
    ("Rick and Morty", "RICK_AND_MORTY_S1", ["S01E01 - Rick and Morty", "S01E02 - Rick and Morty"]),
    # "Disc 2" only in the label still marks a split season; the marker still shows
    ("Show Season 1", "SHOW_S1_DISC_2",
     ["E01 - Show Season 1 (Disc 2)", "E02 - Show Season 1 (Disc 2)"]),
])
def test_bluray_episode_naming(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                               name: str, label: str, expected: list[str]) -> None:
    iso = tmp_path / "disc.iso"
    iso.write_text("")
    disc = _two_episode_disc(name, label)

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]
    assert [s.name for s in found] == expected


def test_bluray_all_on_uniform_lang_label(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#80: "Il Prigioniero" Disco 1, Italian edition: two episodes with only an Italian
    track, one with Italian + English. The language prefix must be the same for every
    episode of the disc, not just the one that happens to carry two languages; the
    fallback name also shows the disc marker instead of baking "Disco 1" into the show
    name with an episode number that looks like a continuation of it."""
    iso = tmp_path / "Prigioniero.iso"
    iso.write_text("")

    def ep(clip: str, audio: list[Stream]) -> Item:
        return Item(clip, 0, 45_000 * 1200, audio=audio)

    ita_only = [Stream(0x1100, "ita", 0x81)]
    ita_and_eng = [Stream(0x1100, "ita", 0x81), Stream(0x1101, "eng", 0x81)]
    disc = MultiEpisodeBluray({
        "00001": [ep("00010", ita_only)],
        "00002": [ep("00020", ita_only)],
        "00003": [ep("00030", ita_and_eng)],
    }, name="Il Prigioniero Disco 1 - Blu-ray", volume_id="IL_PRIGIONIERO_D1")

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]
    assert [s.name for s in found] == [
        "E01 - Il Prigioniero (Disco 1)", "E02 - Il Prigioniero (Disco 1)",
        "E03 - Il Prigioniero (Disco 1)"]
    assert [[v.label for v in s.variants()] for s in found] == \
        [["ITA"], ["ITA"], ["ITA", "ENG"]]


def test_bluray_uniform_lang_label_episode_without_audio(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#80: an episode with no selected audio track (none in the chosen languages)
    has no language to put in front of its name: it keeps the plain name instead
    of failing on audio_langs[0] when another episode forces the prefix."""
    iso = tmp_path / "disc.iso"
    iso.write_text("")

    def ep(clip: str, audio: list[Stream]) -> Item:
        return Item(clip, 0, 45_000 * 1200, audio=audio)

    disc = MultiEpisodeBluray({
        "00001": [ep("00010", [])],
        "00002": [ep("00020", [Stream(0x1100, "ita", 0x81), Stream(0x1101, "eng", 0x81)])],
    }, name="Firefly - Blu-ray", volume_id="FIREFLY")

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = [s for s in BlurayDiscSource.all_on(str(iso)) if isinstance(s, BlurayDiscSource)]
    assert [[v.label for v in s.variants()] for s in found] == [[""], ["ITA", "ENG"]]


@pytest.mark.parametrize(("name", "expected"), [
    ("Il Prigioniero Disco 1", ("Disco 1", "Il Prigioniero")),
    ("Rick and Morty Staffel 1 Disc 2", ("Disc 2", "Rick and Morty Staffel 1")),
    ("Show Vol. 2", ("Vol 2", "Show")),
    ("Firefly", None),
])
def test_parse_disc_part(name: str, expected: tuple[str, str] | None) -> None:
    assert sources._parse_disc_part(name) == expected


@pytest.mark.parametrize(("name", "raw_names", "expected"), [
    # #81: a recognised season names the extra after the show, not the raw disc name
    ("Rick_and_Morty_Staffel1", ["Rick_and_Morty_Staffel1", "RICK AND MORTY STAFFEL1"],
     ("Rick and Morty - Extra 1", "Extra")),
    # a disc-part marker still gives the cleaned show name (no marker for extras)
    ("Il_Prigioniero_Disco_1", ["Il_Prigioniero_Disco_1", "IL PRIGIONIERO DISCO 1"],
     ("Il Prigioniero - Extra 2", "Extra")),
    # neither recognised: underscores in the fallback name become spaces
    ("Firefly_Bonus", ["Firefly_Bonus", "FIREFLY BONUS"], ("Firefly Bonus - Extra 3", "Extra")),
])
def test_episode_name_extra(name: str, raw_names: list[str],
                            expected: tuple[str, str]) -> None:
    episode = int(expected[0].rsplit(" ", 1)[1])
    assert sources._episode_name(name, raw_names, episode, True) == expected


NO_EXTRAS: list[str] = []


@pytest.mark.parametrize(("langs", "expected"), [
    # second copy of each episode with fewer dubs, plus a bonus: split
    (["ie", "ie", "ie", "e", "e", "e"], (["ie"] * 3, ["e"] * 3)),
    # second copy plus bonuses (Rick and Morty S1: 11 + 12)
    (["ie"] * 11 + ["e"] * 12, (["ie"] * 11, ["e"] * 12)),
    # the last episodes were never dubbed: all stay episodes
    (["ji"] * 7 + ["j"] * 3, (["ji"] * 7 + ["j"] * 3, NO_EXTRAS)),
    # two episodes carry one more dub: they are not "the episodes"
    (["eis"] * 2 + ["ei"] * 7, (["eis"] * 2 + ["ei"] * 7, NO_EXTRAS)),
    # a single episode missing a dub stays an episode
    (["ie", "ie", "ie", "e"], (["ie", "ie", "ie", "e"], NO_EXTRAS)),
    # no group holds every other group's languages: no split
    (["ie", "ie", "ef", "ef"], (["ie", "ie", "ef", "ef"], NO_EXTRAS)),
    # all the same: no split
    (["ie", "ie"], (["ie", "ie"], NO_EXTRAS)),
])
def test_split_extras(langs: list[str], expected: tuple[list[str], list[str]]) -> None:
    episodes, extras = sources._split_extras(langs, frozenset)
    assert (episodes, extras) == expected


def test_bluray_partial_injection_closes_own_disc(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    """A disc passed without info/env is not used: the source opens its own and must close
    it, leaving the passed one alone."""
    own, passed = FakeBluray(), FakeBluray()
    iso = tmp_path / "Tron.iso"
    iso.write_text("")
    monkeypatch.setattr(sources, "_open_disc", opener(own))
    BlurayDiscSource(str(iso), _disc=passed)  # type: ignore[arg-type]
    assert own.closed and not passed.closed


def test_bluray_all_on_opens_disc_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#24: a multi-episode disc must open once (one bd_open + AACS handshake), not
    once per episode: all_on() keeps its disc open and hands it down to every episode."""
    iso = tmp_path / "Rick.iso"
    iso.write_text("")

    def ep(clip: str) -> Item:
        return Item(clip, 0, 45_000 * 1200, audio=[Stream(0x1100, "eng", 0x81)])

    disc = MultiEpisodeBluray({
        "00001": [ep("00010")], "00002": [ep("00020")], "00003": [ep("00030")],
    })
    opens = 0

    def open_disc(path: str) -> tuple[MultiEpisodeBluray, DiscInfo, dict[str, str]]:
        nonlocal opens
        opens += 1
        return disc, disc.info(), {}

    monkeypatch.setattr(sources, "_open_disc", open_disc)
    found = BlurayDiscSource.all_on(str(iso))

    assert len(found) == 3
    assert opens == 1
    assert disc.closed


def test_decrypt_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    def nothing(name: str) -> str | None:
        return None

    def mmbd(name: str) -> str | None:
        return "libmmbd.so.0"

    def missing(self: Path) -> bool:
        return False

    monkeypatch.setattr(osplatform.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(osplatform.Path, "exists", missing)
    assert sources._decrypt_backends() == [{}]
    monkeypatch.setattr(osplatform.ctypes.util, "find_library", mmbd)
    assert sources._decrypt_backends()[1] == {"LIBAACS_PATH": "libmmbd",
                                              "LIBBDPLUS_PATH": "libmmbd"}


@pytest.mark.parametrize(("protection", "env", "expected"), [
    ("", {}, "not encrypted"),
    ("AACS", {}, "AACS, decrypted with libaacs"),
    ("AACS + BD+", {"LIBAACS_PATH": "libmmbd", "LIBBDPLUS_PATH": "libmmbd"},
     "AACS + BD+, decrypted with MakeMKV (libmmbd)"),
    ("AACS + BD+", {}, "AACS + BD+, decrypted with libaacs + libbdplus"),
])
def test_decrypted_with(protection: str, env: dict[str, str], expected: str) -> None:
    info = FakeBluray(protection=protection).info()
    assert sources._decrypted_with(info, env) == expected


def test_open_disc_tries_every_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    class Disc(FakeBluray):
        def __init__(self, path: str) -> None:
            super().__init__(decrypted=os.environ.get("LIBAACS_PATH") == "libmmbd")
            seen.append(os.environ.get("LIBAACS_PATH"))

    monkeypatch.setattr(bluray_module, "Disc", Disc)
    monkeypatch.setattr(sources, "_decrypt_backends",
                        lambda: [{}, {"LIBAACS_PATH": "libmmbd", "LIBBDPLUS_PATH": "libmmbd"}])
    monkeypatch.delenv("LIBAACS_PATH", raising=False)
    _, info, env = sources._open_disc("/dev/sr0")
    assert seen == [None, "libmmbd"] and info.decrypted and env["LIBAACS_PATH"] == "libmmbd"
    assert "LIBAACS_PATH" not in os.environ                  # restored

    def libaacs_only() -> list[dict[str, str]]:
        return [{}]

    monkeypatch.setattr(sources, "_decrypt_backends", libaacs_only)
    with pytest.raises(OSError, match="cannot decrypt"):
        sources._open_disc("/dev/sr0")


@pytest.mark.parametrize(("bdplus_by", "opens"), [
    ("libbdplus", True),     # libaacs + libbdplus handle BD+: seeks stay clean
    ("libmmbd", False),      # libmmbd handles BD+ but breaks it after a seek
    ("none", False),
])
def test_open_disc_bdplus_only_through_libbdplus(
        monkeypatch: pytest.MonkeyPatch, bdplus_by: str, opens: bool) -> None:
    class Disc(FakeBluray):
        def __init__(self, path: str) -> None:
            mmbd = os.environ.get("LIBBDPLUS_PATH") == "libmmbd"
            handled = {"libbdplus": not mmbd, "libmmbd": mmbd, "none": False}[bdplus_by]
            super().__init__(decrypted=handled, protection="AACS + BD+")

    monkeypatch.delenv("LIBBDPLUS_PATH", raising=False)
    monkeypatch.setattr(bluray_module, "Disc", Disc)
    def table_found(path: str) -> bool:
        return True

    monkeypatch.setattr(bluray_module, "bdplus_table_found", table_found)
    monkeypatch.setattr(sources, "_decrypt_backends",
                        lambda: [{}, {"LIBAACS_PATH": "libmmbd", "LIBBDPLUS_PATH": "libmmbd"}])
    if opens:
        _, _, env = sources._open_disc("/dev/sr0")
        assert env == {}
    else:
        with pytest.raises(sources.UnsupportedDiscError,
                           match="BD\\+ disc: not decrypted") as err:
            sources._open_disc("/dev/sr0")
        assert "KEYDB" not in str(err.value)
    assert "LIBBDPLUS_PATH" not in os.environ                # restored


def test_open_disc_refuses_bdplus_without_a_conversion_table(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # libbdplus reports BD+ handled even when it has no cached table and its own
    # run of the disc's BD+ program fails: the picture would break up (#87)
    opened: list[FakeBluray] = []

    class Disc(FakeBluray):
        def __init__(self, path: str) -> None:
            super().__init__(protection="AACS + BD+")
            opened.append(self)

    monkeypatch.setattr(bluray_module, "Disc", Disc)
    def no_table(path: str) -> bool:
        return False

    def libaacs_only() -> list[dict[str, str]]:
        return [{}]

    monkeypatch.setattr(bluray_module, "bdplus_table_found", no_table)
    monkeypatch.setattr(sources, "_decrypt_backends", libaacs_only)
    with pytest.raises(sources.UnsupportedDiscError, match="BD\\+ disc: no conversion table"):
        sources._open_disc("/dev/sr0")
    assert len(opened) == 1 and opened[0].closed


@pytest.mark.parametrize("backends", [[{}], [{}, {"LIBAACS_PATH": "libmmbd",
                                                   "LIBBDPLUS_PATH": "libmmbd"}]])
def test_open_disc_refuses_bdplus_from_an_exported_libmmbd(
        monkeypatch: pytest.MonkeyPatch, backends: list[dict[str, str]]) -> None:
    # LIBBDPLUS_PATH=libmmbd exported by the user: the libaacs backend gets it too
    class Disc(FakeBluray):
        def __init__(self, path: str) -> None:
            super().__init__(decrypted=os.environ.get("LIBBDPLUS_PATH") == "libmmbd",
                             protection="AACS + BD+")

    monkeypatch.setattr(bluray_module, "Disc", Disc)
    monkeypatch.setattr(sources, "_decrypt_backends", lambda: backends)
    monkeypatch.setenv("LIBBDPLUS_PATH", "libmmbd")
    with pytest.raises(sources.UnsupportedDiscError, match="BD\\+ disc: not decrypted"):
        sources._open_disc("/dev/sr0")
    assert os.environ["LIBBDPLUS_PATH"] == "libmmbd"         # restored


@pytest.mark.parametrize(("aacs_error", "expected", "unexpected"), [
    # AACS errors -1 (AACS files unreadable), -6 (drive/MMC open failed) and
    # -7 (drive/MMC command failed): drive or media problems, not a missing key
    # -> no KEYDB/MakeMKV hint
    (-1, "cannot read the disc", "KEYDB"),
    (-6, "cannot read the disc", "KEYDB"),
    (-7, "cannot read the disc", "KEYDB"),
    # any other AACS error: an actual key/decryption failure -> keep the hint
    (-3, r"cannot decrypt .*KEYDB\.cfg.*MakeMKV", "cannot read the disc"),
])
def test_open_disc_distinguishes_read_errors_from_missing_keys(
        monkeypatch: pytest.MonkeyPatch, aacs_error: int, expected: str, unexpected: str) -> None:
    class Disc(FakeBluray):
        def __init__(self, path: str) -> None:
            super().__init__(decrypted=False, aacs_error=aacs_error)

    def one_backend() -> list[dict[str, str]]:
        return [{}]

    monkeypatch.setattr(bluray_module, "Disc", Disc)
    monkeypatch.setattr(sources, "_decrypt_backends", one_backend)
    with pytest.raises(OSError, match=expected) as excinfo:
        sources._open_disc("/dev/sr0")
    assert unexpected not in str(excinfo.value)
    assert str(excinfo.value).count("/dev/sr0") == 0           # the caller adds the path once


# --- DVD -----------------------------------------------------------------------

def dvd_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
               langs: list[str] | None = None) -> DvdSource:
    folder = tmp_path / "BTTF"
    folder.mkdir()
    monkeypatch.setattr(dvd, "Dvd", FakeDvd)
    return DvdSource(str(folder), langs)


def test_dvd_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = dvd_source(monkeypatch, tmp_path)
    assert s.title.number == 2
    assert (s.name, s.category, s.two_d, s.quality_key) == \
        ("Back To The Future", "DVD", True, "dvd")
    assert s.duration == pytest.approx(6000.0)
    assert (s.frame_size, s.frame_rate) == ("1024x576", "25")
    assert s.pre_filter == "bwdif=deint=interlaced"
    assert s.post_filter == "scale=1024:576,setsar=1"
    # commentary left out; one normal subtitle track per language
    assert (s.audio_pids, s.audio_langs) == ([0x80, 0x81], ["ita", "eng"])
    assert [(t.stream, t.lang) for t in s.subs] == [(0x20, "ita"), (0x22, "eng")]
    assert [v.label for v in s.variants()] == ["ITA", "ENG"]
    assert s.audio_map() == ("-map 0:i:0x80 -metadata:s:a:0 language=ita "
                             "-map 0:i:0x81 -metadata:s:a:1 language=eng")
    assert s.audio_input(0) == ""


def test_dvd_css_without_libdvdcss(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(FakeDvd, "scrambled", True)
    with pytest.raises(OSError, match=r"encrypted \(CSS\) and libdvdcss is missing"):
        dvd_source(monkeypatch, tmp_path)


@pytest.mark.parametrize(("dvdcss", "expected"), [
    (True, "DVD, read through libdvdcss (for CSS)"), (False, "DVD not encrypted")])
def test_dvd_protection_logged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                               caplog: pytest.LogCaptureFixture, dvdcss: bool,
                               expected: str) -> None:
    monkeypatch.setattr(dvd, "has_dvdcss", lambda: dvdcss)
    caplog.set_level(logging.INFO, logger="bd3d_fs")
    dvd_source(monkeypatch, tmp_path)
    assert expected in caplog.text


def test_scrambled() -> None:
    assert not dvd.scrambled(pack() * 4)
    assert dvd.scrambled(pack() + pack(0b01))
    assert not dvd.scrambled(b"\xff" * 2048)            # not a pack at all


def test_temp_files_share_one_folder_removed_at_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    at_exit: list[tuple[object, ...]] = []
    monkeypatch.setattr(sources.atexit, "register", lambda *args: at_exit.append(args))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    first, second = sources._temp_file("audio.ts"), sources._temp_file("audio.ts")
    folder = Path(first).parent
    assert first != second and Path(second).parent == folder
    assert at_exit == [(shutil.rmtree, str(folder), True)]


def test_dvd_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = dvd_source(monkeypatch, tmp_path, ["eng"])
    s.sub = s.subs[1]
    cmd = shlex.split(s.video_command(61.5))
    assert cmd[1].endswith("dvd_reader.py")
    assert cmd[2:] == [str(tmp_path / "BTTF"), "--title", "2", "--start", "61.502",
                       "--audio", "0x81", "--sub", "0x22"]
    args = shlex.split(s.sub_decoder_args())
    assert args[0] == "-ifo_palette" and "-canvas_size" not in args
    ifo = Path(args[1]).read_bytes()
    # the main title's palette goes into the first PGC, where ffmpeg reads it
    pgci = int.from_bytes(ifo[0xCC:0xD0], "big") * 2048
    first = pgci + int.from_bytes(ifo[pgci + 12:pgci + 16], "big")
    assert ifo[first + 0xA4:first + 0xA4 + 64] == s.title.palette_raw
    s.forced_only = True
    assert s.sub_decoder_args().endswith(" -forced_subs_only 1")


def test_open_disc_falls_back_to_dvd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def no_bluray(path: str) -> None:
        raise OSError("not a Blu-ray")

    monkeypatch.setattr(sources, "_open_disc", no_bluray)
    monkeypatch.setattr(dvd, "Dvd", FakeDvd)
    (tmp_path / "BTTF").mkdir()
    found = open_disc(str(tmp_path / "BTTF"))
    assert len(found) == 1 and isinstance(found[0], DvdSource)

    def no_dvd(path: str) -> None:
        raise OSError("not a DVD")

    monkeypatch.setattr(dvd, "Dvd", no_dvd)
    with pytest.raises(OSError, match="not a Blu-ray"):
        open_disc(str(tmp_path / "BTTF"))


# --- --dump-structure ----------------------------------------------------------

# keyframes at irregular times (an open GOP here, a scene cut there), as on real
# discs: the dump must keep them exact, not rounded to a regular grid
IRREGULAR_EPS = [((k * 90_000 + (k % 7) * 9_000 + (k % 3) * 1_311) & ~0xFF, k * 1000)
                 for k in range(3000)]
# what must never reach a dump: the source's path, the decryption backend, the
# payload of the streams, the commands that read them
PRIVATE_DIR = "Private Rips of Someone"
STREAM_PAYLOAD = b"PROTECTED-AV-PAYLOAD"


class RecordingBluray(FakeBluray):
    """FakeBluray with irregular keyframes, recording every file read; its
    streams hold a marker the dump must never contain."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    @override
    def read_file(self, path: str) -> bytes:
        self.reads.append(path)
        if path.startswith("BDMV/STREAM/"):
            return STREAM_PAYLOAD
        if path.startswith("BDMV/CLIPINF/"):
            dep = path.rsplit("/", 1)[1][:5] in ("00132", "00134")
            return clpi(3_000_000, IRREGULAR_EPS, extents=list(range(0, 3_000_000, 50_000)),
                        pid=0x1012 if dep else 0x1011)
        return super().read_file(path)


def dumped_bluray(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, disc: FakeBluray
                  ) -> tuple[str, str]:
    """(the folder the disc is read from, its dump), decrypted by libmmbd."""
    folder = tmp_path / PRIVATE_DIR
    folder.mkdir()
    monkeypatch.setattr(sources, "_open_disc", opener(
        disc, {"LIBAACS_PATH": "libmmbd", "LIBBDPLUS_PATH": "libmmbd"}))
    return str(folder), sources.dump_structure(str(folder))


def test_dump_structure_bluray(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    disc = RecordingBluray()
    _, text = dumped_bluray(monkeypatch, tmp_path, disc)
    data = json.loads(text)
    assert disc.closed
    assert {k: data[k] for k in ("format", "kind", "name", "volume_id", "has_3d")} == {
        "format": 1, "kind": "bluray", "name": "Tron: Legacy - Blu-ray",
        "volume_id": "TRON_LEGACY_3D", "has_3d": True}
    # every title in libbluray's order, before any choice (00080 is not the movie)
    assert [(t["playlist"], t["duration"]) for t in data["titles"]] == \
        [("00080", 7300.0), ("00070", 7200.0)]
    first = data["titles"][1]["items"][0]
    assert (first["clip"], first["dep_clip"], first["in_time"], first["out_time"]) == \
        ("00131", "00132", 45_000, 45_000 * 3601)
    assert [(a["pid"], a["codec"], a["lang"]) for a in first["audio"]] == [
        (0x1100, "truehd", "eng"), (0x1101, "dts-hd ma", "eng"), (0x1102, "dts", "ita"),
        (0x1103, "ac3", "deu")]
    assert [(s["stream"], s["lang"]) for s in first["subs"]] == \
        [(0x1200, "ita"), (0x1201, "eng"), (0x1202, "ita")]
    assert data["titles"][0]["items"][0]["dep_clip"] == ""
    # each clip once, the MVC ones with the dependent view's EP_map
    assert {c: data["clips"][c]["video_pid"] for c in data["clips"]} == {
        "00200": 0x1011, "00131": 0x1011, "00132": 0x1012, "00133": 0x1011, "00134": 0x1012}
    clip = data["clips"]["00131"]
    assert clip["ep_pts"] == [p for p, _ in IRREGULAR_EPS]
    assert clip["ep_spn"] == [s for _, s in IRREGULAR_EPS]
    assert clip["extent_start"] == list(range(0, 3_000_000, 50_000))
    assert clip["num_packets"] == 3_000_000
    # the navigation files only: never a stream
    assert all(r.startswith(("BDMV/PLAYLIST/", "BDMV/CLIPINF/")) for r in disc.reads)
    # a long EP_map is one line of the file, a title's fields one line each
    assert len(text.splitlines()) < 300


@pytest.mark.parametrize("three_d", [True, False])
def test_dump_structure_bluray_replays_keyframes(monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path, three_d: bool) -> None:
    """The dump holds what keyframe_at_or_before() needs: seeks computed from it
    alone land on the same keyframes as the source on the disc, 3D and 2D."""
    disc = RecordingBluray()
    disc.three_d = three_d
    folder, text = dumped_bluray(monkeypatch, tmp_path, disc)
    data = json.loads(text)
    s = BlurayDiscSource(folder)
    title = next(t for t in data["titles"] if t["playlist"] == s.playlist)
    items = [PlayItem(i["clip"], i["in_time"], i["out_time"], i["dep_clip"])
             for i in title["items"]]

    def clip(c: str) -> Clip:
        d = data["clips"][c]
        return Clip(d["num_packets"], d["ep_pts"], d["ep_spn"], d["extent_start"])

    starts = list(itertools.accumulate([0.0] + [it.duration for it in items[:-1]]))
    for t in (0.0, 1.0, 101.5, 1234.567, 3599.99, 3600.5, 3605.0, 7000.0):
        i = max(n for n, start in enumerate(starts) if start <= t)
        item, rel = items[i], t - starts[i]
        sp = m2ts_seek(item, clip(item.clip), rel) if s.two_d else \
            ssif_seek(item, clip(item.clip), clip(item.dep_clip), rel)
        assert starts[i] + max(0.0, sp.time) == s.keyframe_at_or_before(t)


def test_dump_structure_bluray_keeps_no_key_path_or_payload(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder, text = dumped_bluray(monkeypatch, tmp_path, RecordingBluray())
    for leak in (folder, PRIVATE_DIR, str(tmp_path), "libmmbd", "LIBAACS", "LIBBDPLUS", "AACS",
                 "decrypt", STREAM_PAYLOAD.decode(), "disc_reader", "python", "--playlist",
                 "m2ts", "ssif"):
        assert leak not in text, leak


def test_dump_structure_bluray_records_unreadable_files(monkeypatch: pytest.MonkeyPatch,
                                                        tmp_path: Path) -> None:
    """A broken playlist or clip is recorded as such, as the program skips it."""
    disc = RecordingBluray()
    normal = disc.read_file

    def read(path: str) -> bytes:
        if path in ("BDMV/PLAYLIST/00080.mpls", "BDMV/CLIPINF/00134.clpi"):
            raise OSError(f"cannot open {path} on the disc")
        return normal(path)

    monkeypatch.setattr(disc, "read_file", read)
    data = json.loads(dumped_bluray(monkeypatch, tmp_path, disc)[1])
    assert data["titles"][0] == {"playlist": "00080", "duration": 7300.0, "error": "OSError"}
    assert data["clips"]["00134"] == {"video_pid": 0x1012, "error": "OSError"}
    assert "00200" not in data["clips"]
    assert "ep_pts" in data["clips"]["00133"]


class NavVobs:
    """Title VOBs whose VOBU starts are navigation packs; records what is read
    and fills every other sector with stream payload."""

    def __init__(self, navs: dict[int, bytes]) -> None:
        self.navs, self.reads = navs, list[tuple[int, int]]()

    def read(self, sector: int, count: int) -> bytes:
        self.reads.append((sector, count))
        if sector in self.navs and count == 1:
            return self.navs[sector]
        if sector == 50_000:                         # a damaged spot
            return b""
        return b"".join(self.navs.get(s, STREAM_PAYLOAD.ljust(2048, b"\0"))
                        for s in range(sector, sector + count))

    def close(self) -> None:
        pass


# VTS 2 (the movie): VOBUs at 0, 50 000 (unreadable), 100 000 and 150 000;
# irregular times within each VOB cell, its PTS restarting at the second VOB
MOVIE_NAVS = {0: nav_pack(1, 1, 0.0, 90_000), 100_000: nav_pack(2, 1, 0.48, 7_000_000),
              150_000: nav_pack(2, 1, 1500.04, 7_000_000 + 135_003_600)}


class NavDvd(FakeDvd):
    vobs: ClassVar[dict[int, NavVobs]] = {}

    def __init__(self, path: str = "") -> None:
        super().__init__(path)
        self.movie = vts_ifo(
            [DvdCell(3000.0, 0, 99_999, 1, 1), DvdCell(3000.0, 100_000, 199_999, 2, 1)],
            [DvdAudioAttr(0, "it", 1, 0), DvdAudioAttr(0, "en", 3, 1)],
            [DvdSubAttr("it", 1, wide=0), DvdSubAttr("en", 9, wide=1)],
            palette=[(235, 128, 128)] * 16, pgcn=2, vobus=[0, 50_000, 100_000, 150_000])

    @override
    def title_vobs(self, vts: int) -> "NavVobs":  # type: ignore[override]
        return NavDvd.vobs.setdefault(vts, NavVobs(MOVIE_NAVS if vts == 2 else {}))


def dumped_dvd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """(the folder the DVD is read from, its dump), after Blu-ray failed on it."""
    def no_bluray(path: str) -> None:
        raise OSError("cannot open Blu-ray")

    folder = tmp_path / PRIVATE_DIR
    folder.mkdir()
    monkeypatch.setattr(sources, "_open_disc", no_bluray)
    monkeypatch.setattr(dvd, "Dvd", NavDvd)
    monkeypatch.setattr(NavDvd, "vobs", {})
    return str(folder), sources.dump_structure(str(folder))


def test_dump_structure_dvd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = json.loads(dumped_dvd(monkeypatch, tmp_path)[1])
    assert (data["format"], data["kind"], data["volume_id"]) == (1, "dvd", "BACK_TO_THE_FUTURE")
    # both titles, the trailer too: main_titles() has not chosen yet
    assert [(t["number"], t["vts"], t["ttn"], t["duration"]) for t in data["titles"]] == \
        [(1, 1, 1, 90.0), (2, 2, 1, 6000.0)]
    movie = data["titles"][1]
    assert (movie["frame_size"], movie["frame_rate"]) == ("1024x576", "25")
    assert movie["cells"] == [
        {"start": 0.0, "duration": 3000.0, "first": 0, "last": 99_999, "vob_id": 1, "cell_id": 1},
        {"start": 3000.0, "duration": 3000.0, "first": 100_000, "last": 199_999, "vob_id": 2,
         "cell_id": 1}]
    assert movie["audio"] == [
        {"stream": 0x80, "codec": "ac3", "lang": "ita", "commentary": False},
        {"stream": 0x81, "codec": "ac3", "lang": "eng", "commentary": True}]
    assert movie["subs"] == [{"stream": 0x20, "lang": "ita", "kind": "normal"},
                             {"stream": 0x21, "lang": "eng", "kind": "forced"}]
    sets = data["title_sets"]
    assert sets["1"] == {"vobus": [], "navs": {"vob_id": [], "cell_id": [], "cell_time": [],
                                               "pts": []}}
    assert sets["2"]["vobus"] == [0, 50_000, 100_000, 150_000]
    navs = sets["2"]["navs"]
    assert navs["vob_id"] == [1, None, 2, 2]
    assert navs["cell_id"] == [1, None, 1, 1]
    assert navs["pts"] == [90_000, None, 7_000_000, 7_000_000 + 135_003_600]
    # the times exactly as the navigation pack gives them
    assert navs["cell_time"] == [0.0, None, 0.48, dvd.parse_nav(MOVIE_NAVS[150_000]).cell_time]  # type: ignore[union-attr]
    # one sector at each VOBU start, nothing else
    assert sorted(NavDvd.vobs[2].reads) == [(0, 1), (50_000, 1), (100_000, 1), (150_000, 1)]


def test_dump_structure_dvd_replays_keyframes(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    """The dump holds what DvdSource's seek needs: a title rebuilt from it, with
    navigation packs rebuilt from it, seeks to the same VOBUs as the disc."""
    folder, text = dumped_dvd(monkeypatch, tmp_path)
    data = json.loads(text)
    movie = data["titles"][1]
    title = dvd.DvdTitle(movie["number"], movie["vts"], movie["duration"],
                         cells=[dvd.Cell(**c) for c in movie["cells"]],
                         vobus=data["title_sets"]["2"]["vobus"])
    n = data["title_sets"]["2"]["navs"]
    rebuilt = NavVobs({s: nav_pack(v, c, t, p) for s, v, c, t, p in zip(
        title.vobus, n["vob_id"], n["cell_id"], n["cell_time"], n["pts"], strict=True)
        if v is not None})
    s = DvdSource(folder)
    assert s.title.number == title.number
    for t in (0.0, 10.0, 2999.0, 3000.2, 3000.5, 4600.0, 5999.0):
        assert dvd.seek(rebuilt, title, t) == dvd.seek(NavDvd.vobs[2], s.title, t)
        assert dvd.seek(rebuilt, title, t).time == s.keyframe_at_or_before(t)


def test_dump_structure_dvd_keeps_no_path_payload_or_palette(monkeypatch: pytest.MonkeyPatch,
                                                             tmp_path: Path) -> None:
    folder, text = dumped_dvd(monkeypatch, tmp_path)
    for leak in (folder, PRIVATE_DIR, str(tmp_path), STREAM_PAYLOAD.decode(), "palette",
                 "dvd_reader", "python", "--title", "ifo"):
        assert leak not in text, leak


def test_dump_structure_neither_bluray_nor_dvd(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_bluray(path: str) -> None:
        raise OSError("cannot open Blu-ray: x")

    def no_dvd(path: str) -> None:
        raise OSError("cannot open DVD: x")

    monkeypatch.setattr(sources, "_open_disc", no_bluray)
    monkeypatch.setattr(dvd, "Dvd", no_dvd)
    with pytest.raises(OSError, match="cannot open Blu-ray"):
        sources.dump_structure("x")


# --- MKV ---------------------------------------------------------------------

def mkv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        langs: list[str] | None = None) -> MkvSource:
    path = tmp_path / "Tron 3D.mkv"
    path.write_text("")
    monkeypatch.setattr(sources, "ffprobe_json", mkv_probe)
    return MkvSource(str(path), langs)


def test_mkv_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = mkv(monkeypatch, tmp_path)
    assert (s.name, s.duration) == ("Tron 3D", 7512.3)
    assert (s.audio_indexes, s.audio_langs) == ([1, 3], ["ita", "eng"])
    assert s.audio_desc == "#1 ita dts, #3 eng truehd"
    assert [(t.stream, t.lang) for t in s.subs] == [(4, "ita"), (6, "fra")]
    assert [(v.label, v.audio_indexes, v.audio_desc) for v in s.variants()] == [
        ("ITA", [1], "#1 ita dts"), ("ENG", [3], "#3 eng truehd")]
    assert s.audio_map() == "-map 1:1 -map 1:3"
    s.sub = s.subs[0]
    assert s.sub_ref(1) == "1:4"
    assert mkv(monkeypatch, tmp_path, ["fra", "eng"]).audio_indexes == [3]
    assert mkv(monkeypatch, tmp_path, ["jpn"]).audio_indexes == [1]


def test_mkv_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = mkv(monkeypatch, tmp_path)
    quoted = shlex.quote(s.path)
    assert s.video_command(0) == (f"ffmpeg -nostdin -v error -i {quoted} -map 0:v:0 -c copy "
                                  "-bsf:v h264_mp4toannexb -f h264 -")
    # K + 0.2 lands on keyframe K (ffmpeg moves DTS seeks back by 3/23 s)
    assert "-ss 42.325 -i" in s.video_command(42.125)
    assert s.audio_input(42.125) == f"-ss 42.125 -i {quoted}"
    assert s.keyframe_at_or_before(42.5) == 42.125
    assert s.keyframe_at_or_before(0) == 0.0


def test_discover(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "3D.mkv").write_text("")
    (tmp_path / "a" / "2D.mkv").write_text("")
    (tmp_path / "a" / "notes.txt").write_text("")
    monkeypatch.setattr(sources, "ffprobe_json", mkv_probe)
    def has_mvc(path: str) -> bool:
        return path.endswith("3D.mkv")

    monkeypatch.setattr(MkvSource, "has_mvc", staticmethod(has_mvc))
    found = discover([str(tmp_path / "a")])
    assert [s.name for s in found] == ["3D"]
    with pytest.raises(SystemExit):
        discover([str(tmp_path / "missing")])


@pytest.mark.parametrize("prefixed", [False, True])
def test_discover_logs_the_failed_candidate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                            caplog: pytest.LogCaptureFixture,
                                            prefixed: bool) -> None:
    iso = tmp_path / "broken.iso"
    iso.write_text("")

    def fails(path: str, audio_langs: list[str] | None = None) -> list[sources.Source]:
        # pathless (AACS) and path-bearing ("no title found", CSS) errors alike
        prefix = f"{path}: " if prefixed else ""
        raise OSError(prefix + "cannot read the disc (AACS error -1): drive or media problem")

    monkeypatch.setattr(sources, "open_disc", fails)
    with caplog.at_level(logging.INFO, logger=sources.log.name):
        assert discover([str(tmp_path)]) == []
    assert f"{iso}: skipped: cannot read the disc" in caplog.text   # the path, exactly once
    assert caplog.text.count(str(iso)) == 1
