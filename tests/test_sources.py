import os
import shlex
import tempfile
from pathlib import Path

import pytest

import bluray as bluray_module
import dvd
import sources
from bdmv import AudioStream
from bluray import Title
from fakes import FakeBluray, FakeDvd, mkv_probe, opener
from sources import (
    BlurayDiscSource,
    DvdSource,
    MkvSource,
    SubTrack,
    _first_per_lang,
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


A = [AudioStream(1, "truehd", "eng"), AudioStream(2, "dts-hd ma", "eng"),
     AudioStream(3, "ac3", "ita"), AudioStream(4, "dts", "ita"), AudioStream(5, "ac3", "deu")]


def test_pick_audio() -> None:
    assert [a.pid for a in _pick_audio(A, None)] == [2, 4, 5]        # every language, best codec
    assert [a.pid for a in _pick_audio(A, ["ita", "eng"])] == [4, 2]  # requested order
    assert [a.pid for a in _pick_audio(A, ["jpn"])] == [1]            # none found: the first
    assert _pick_audio([], ["ita"]) == []


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


def test_bluray_no_title(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    disc = FakeBluray()
    def no_titles(self: FakeBluray) -> list[Title]:
        return []

    monkeypatch.setattr(FakeBluray, "titles", no_titles)
    with pytest.raises(OSError, match="no title"):
        bluray(monkeypatch, tmp_path, disc)
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
