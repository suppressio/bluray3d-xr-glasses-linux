import logging
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import bluray as bluray_module
import dvd
import sources
from bdmv import AudioStream
from bluray import DiscInfo, Title
from builders import Item, Stream, clpi, mpls
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
    assert "Rick and Morty Staffel1 - Extra 1: no ita audio track, using eng" in caplog.text
    assert "Rick and Morty Staffel1 - Extra 2: no ita audio track, using eng" in caplog.text
    # #27: the season from the disc name ("Staffel1"), episodes in play-all order;
    # #39: the eng-only titles are the disc's extras, numbered apart in Extra/, and
    # keep today's naming (no episode number applies to them)
    assert [(s.name, s.subfolder) for s in found] == [
        ("S01E01 - Rick and Morty", ""), ("S01E02 - Rick and Morty", ""),
        ("Rick and Morty Staffel1 - Extra 1", "Extra"),
        ("Rick and Morty Staffel1 - Extra 2", "Extra")]
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


def test_bluray_all_on_3d_series_without_play_all(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#67: the 3D movie rule needs a play-all; without one, 3D episodes stay episodes."""
    iso = tmp_path / "Series.iso"
    iso.write_text("")
    disc = MultiEpisodeBluray({"00001": [_ep("00010")], "00002": [_ep("00020")]},
                              dep_clips={"00001": ["00011"], "00002": ["00021"]})

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
    # no season recognised: today's naming
    ("Firefly - Blu-ray", "FIREFLY", ["Firefly - 1", "Firefly - 2"]),
    # a season split over discs: the first episode number is not known, no S/E
    ("Rick and Morty Staffel 1 Disc 2 - Blu-ray", "RICK_AND_MORTY_S1_D2",
     ["Rick and Morty Staffel 1 Disc 2 - 1", "Rick and Morty Staffel 1 Disc 2 - 2"]),
    # the season only in the label: the show keeps the disc's own name
    ("Rick and Morty", "RICK_AND_MORTY_S1", ["S01E01 - Rick and Morty", "S01E02 - Rick and Morty"]),
    # "Disc 2" only in the label still marks a split season
    ("Show Season 1", "SHOW_S1_DISC_2", ["Show Season 1 - 1", "Show Season 1 - 2"]),
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

    def missing(path: str) -> bool:
        return False

    monkeypatch.setattr(sources.ctypes.util, "find_library", nothing)
    monkeypatch.setattr(sources.os.path, "exists", missing)
    assert sources._decrypt_backends() == [{}]
    monkeypatch.setattr(sources.ctypes.util, "find_library", mmbd)
    assert sources._decrypt_backends()[1] == {"LIBAACS_PATH": "libmmbd",
                                              "LIBBDPLUS_PATH": "libmmbd"}


@pytest.mark.parametrize(("protection", "env", "expected"), [
    ("", {}, "not encrypted"),
    ("AACS", {}, "AACS, decrypted with libaacs"),
    ("AACS + BD+", {"LIBAACS_PATH": "libmmbd", "LIBBDPLUS_PATH": "libmmbd"},
     "AACS + BD+, decrypted with MakeMKV (libmmbd)"),
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
