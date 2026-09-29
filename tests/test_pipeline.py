import shlex
import subprocess
import tempfile
from pathlib import Path

import pytest

import dvd
import pipeline
import sources
from fakes import FakeBluray, FakeDvd, mkv_probe, opener
from pipeline import QUALITIES, decode_command, encoder_args, pick_encoder, quality_for
from sources import BlurayDiscSource, DvdSource, MkvSource


@pytest.fixture(autouse=True)
def private_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def test_qualities() -> None:
    for (kind, level), q in QUALITIES.items():
        assert q.name == level and kind in ("3d", "2d", "dvd")
        assert q.video < q.muxrate                       # room for audio and the muxer
        assert q.bytes_per_sec == q.muxrate // 8
    assert QUALITIES[("3d", "normal")].muxrate == 24_000_000


def test_encoder_args() -> None:
    assert encoder_args("x264", 20_000_000) == (
        "-c:v libx264 -preset veryfast -b:v 20000k -maxrate 20000k -bufsize 10000k "
        "-x264-params nal-hrd=cbr")
    assert "-rc cbr -b:v 8000k -maxrate 8000k -bufsize 4000k" in encoder_args("nvenc", 8_000_000)


def test_pick_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pick_encoder("x264") == "x264"
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    assert pick_encoder() == "x264"
    assert "h264_nvenc" in calls[0]
    def works(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pipeline.subprocess, "run", works)
    assert pick_encoder("auto") == "nvenc"


def three_d(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BlurayDiscSource:
    (tmp_path / "t.iso").write_text("")
    monkeypatch.setattr(sources, "_open_disc", opener(FakeBluray()))
    return BlurayDiscSource(str(tmp_path / "t.iso"), ["ita"])


def test_decode_3d(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = three_d(monkeypatch, tmp_path)
    assert quality_for(s, "light") == QUALITIES[("3d", "light")]
    cmd = decode_command(s, 12.0, "OUT")
    parts = [p.strip() for p in cmd.split("|")]
    assert "disc_reader.py" in parts[0]
    assert parts[1] == "edge264_test - -Ok"
    assert parts[2] == (f"ffmpeg -nostdin -v warning -i -  {s.audio_input(12.0)} -map 0:v "
                        f"{s.audio_map()} OUT")


def test_decode_3d_subtitles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = three_d(monkeypatch, tmp_path)
    s.sub, s.sub_depth = s.subs[0], 10
    ffmpeg = decode_command(s, 12.0, "OUT").split("|")[2]
    # the subtitle comes with the audio (input 1), on a 1920x1080 canvas, drawn in
    # both eyes, each copy moved inward
    assert "-canvas_size 1920x1080" in ffmpeg
    assert ffmpeg.index("-canvas_size") < ffmpeg.index(s.audio_input(12.0))
    graph = shlex.split(ffmpeg)[shlex.split(ffmpeg).index("-filter_complex") + 1]
    assert graph == ("[1:i:0x1200]split[sa][sb];[0:v][sa]overlay=x=10:y=0:eof_action=pass[l];"
                     "[l][sb]overlay=x=1910:y=0:eof_action=pass[v]")
    s.forced_only = True
    assert "-canvas_size 1920x1080 -forced_subs_only 1" in decode_command(s, 0, "OUT")


def test_decode_2d_bluray(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "t.iso").write_text("")
    monkeypatch.setattr(sources, "_open_disc", opener(FakeBluray(three_d=False)))
    s = BlurayDiscSource(str(tmp_path / "t.iso"), ["ita"])
    cmd = decode_command(s, 5.0, "OUT")
    assert "edge264" not in cmd
    ffmpeg = cmd.split("|")[1]
    assert "-f mpegts -analyzeduration 2000000 -probesize 10000000 -i -" in ffmpeg
    assert shlex.split(ffmpeg)[shlex.split(ffmpeg).index("-filter_complex") + 1] == \
        "[0:i:0x1011]null,null[v]"
    s.sub = s.subs[1]
    ffmpeg = decode_command(s, 5.0, "OUT").split("|")[1]
    assert shlex.split(ffmpeg)[shlex.split(ffmpeg).index("-filter_complex") + 1] == \
        "[0:i:0x1011]null[pre];[pre][0:i:0x1201]overlay=eof_action=pass[ov];[ov]null[v]"
    assert "-canvas_size 1920x1080" in ffmpeg
    assert ffmpeg.rstrip().endswith("-map 0:i:0x1102 -metadata:s:a:0 language=ita OUT")


def test_decode_dvd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "BTTF").mkdir()
    monkeypatch.setattr(dvd, "Dvd", FakeDvd)
    s = DvdSource(str(tmp_path / "BTTF"), ["ita"])
    s.sub = s.subs[0]
    ffmpeg = decode_command(s, 5.0, "OUT").split("|")[1]
    assert "-ifo_palette" in ffmpeg and "-f mpeg " in ffmpeg
    assert shlex.split(ffmpeg)[shlex.split(ffmpeg).index("-filter_complex") + 1] == (
        "[0:i:0x1e0]bwdif=deint=interlaced[pre];[pre][0:i:0x20]overlay=eof_action=pass[ov];"
        "[ov]scale=1024:576,setsar=1[v]")


def test_decode_mkv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "m.mkv").write_text("")
    monkeypatch.setattr(sources, "ffprobe_json", mkv_probe)
    s = MkvSource(str(tmp_path / "m.mkv"))
    s.sub = s.subs[0]
    ffmpeg = decode_command(s, 42.125, "OUT").split("|")[2]
    assert "[1:4]split[sa][sb]" in ffmpeg and "-ss 42.125 -i" in ffmpeg
