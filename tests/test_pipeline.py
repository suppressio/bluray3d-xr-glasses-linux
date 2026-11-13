import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import dvd
import pipeline
import sources
from fakes import FakeBluray, FakeDvd, mkv_probe, opener
from pipeline import QUALITIES, decode_command, encoder_args, pick_encoder, quality_for
from sources import BlurayDiscSource, DvdSource, MkvSource

VAAPI_SUFFIX = pipeline.FILTER_SUFFIX["vaapi"]


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
    assert encoder_args("vaapi", 8_000_000, "/dev/dri/renderD128") == (
        "-vaapi_device /dev/dri/renderD128 -c:v h264_vaapi -rc_mode CBR -b:v 8000k "
        "-maxrate 8000k -bufsize 4000k")


def test_filter_suffix() -> None:
    assert pipeline.filter_suffix("nvenc") == pipeline.filter_suffix("x264") == ""
    assert pipeline.filter_suffix("vaapi") == "format=nv12,hwupload"
    assert pipeline.with_filter_suffix("[0:v]null[v]", "") == "[0:v]null[v]"
    assert pipeline.with_filter_suffix("[0:v]null[v]", "format=nv12,hwupload") == \
        "[0:v]null,format=nv12,hwupload[v]"


def _runner(codec_result: dict[str, bool]) -> object:
    """A fake subprocess.run: succeeds for the -c:v codec named in codec_result
    (True), fails for the others, independent of the real test host."""
    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        codec = cmd[cmd.index("-c:v") + 1]
        return subprocess.CompletedProcess(cmd, 0 if codec_result.get(codec) else 1)
    return run


def _fake_glob(devices: list[str]) -> object:
    """A fake glob.glob: the /dev/dri/renderD* devices found, independent of
    the real test host."""
    def glob(pattern: str) -> list[str]:
        return list(devices)
    return glob


def test_pick_encoder_auto_nvenc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline.subprocess, "run", _runner({"h264_nvenc": True}))
    assert pick_encoder() == ("nvenc", "")


def test_pick_encoder_auto_vaapi(monkeypatch: pytest.MonkeyPatch) -> None:
    # the NVIDIA card's render node exists but does not encode (RICK, SPN#156);
    # the next one does
    monkeypatch.setattr(pipeline.glob, "glob",
                        _fake_glob(["/dev/dri/renderD128", "/dev/dri/renderD129"]))
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        ok = "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "h264_vaapi" \
            and "/dev/dri/renderD129" in cmd
        return subprocess.CompletedProcess(cmd, 0 if ok else 1)

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    assert pick_encoder("auto") == ("vaapi", "/dev/dri/renderD129")
    vaapi_calls = [c for c in calls if "h264_vaapi" in c]
    assert [c[c.index("-vaapi_device") + 1] for c in vaapi_calls] == \
        ["/dev/dri/renderD128", "/dev/dri/renderD129"]
    assert all("-vf" in c and "format=nv12,hwupload" in c for c in vaapi_calls)
    # the probe encodes with the real configuration (CBR), not the encoder's
    # defaults: a device that only does CQP must not be picked (SPN#158 review)
    assert all("-rc_mode" in c and c[c.index("-rc_mode") + 1] == "CBR" for c in vaapi_calls)


def test_probe_uses_the_real_encoder_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    pick_encoder("nvenc")
    assert "-rc" in calls[-1] and calls[-1][calls[-1].index("-rc") + 1] == "cbr"
    pick_encoder("x264")
    assert "nal-hrd=cbr" in calls[-1]


def test_vaapi_compose_does_not_need_a_render_group_in_the_image() -> None:
    # the image has no "render" group: group_add by name would stop the
    # container before pick_encoder() could fall back (SPN#158 review)
    text = (Path(__file__).parent.parent / "docker-compose.vaapi.yml").read_text()
    entries = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("- ")]
    assert "- render" not in entries
    assert any("RENDER_GID" in e for e in entries)


def test_pick_encoder_auto_falls_back_to_x264(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline.glob, "glob", _fake_glob([]))
    monkeypatch.setattr(pipeline.subprocess, "run", _runner({}))
    assert pick_encoder("auto") == ("x264", "")


def test_pick_encoder_forced_nvenc_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline.subprocess, "run", _runner({}))
    with pytest.raises(SystemExit):
        pick_encoder("nvenc")


def test_pick_encoder_forced_vaapi_no_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline.glob, "glob", _fake_glob([]))
    monkeypatch.setattr(pipeline.subprocess, "run", _runner({}))
    with pytest.raises(SystemExit):
        pick_encoder("vaapi")


def test_pick_encoder_forced_vaapi_device_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # a device exists (e.g. the NVIDIA card's render node) but does not encode
    monkeypatch.setattr(pipeline.glob, "glob", _fake_glob(["/dev/dri/renderD128"]))
    monkeypatch.setattr(pipeline.subprocess, "run", _runner({}))
    with pytest.raises(SystemExit):
        pick_encoder("vaapi")


def test_pick_encoder_forced_vaapi_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline.glob, "glob", _fake_glob(["/dev/dri/renderD128"]))
    monkeypatch.setattr(pipeline.subprocess, "run", _runner({"h264_vaapi": True}))
    assert pick_encoder("vaapi") == ("vaapi", "/dev/dri/renderD128")


def test_pick_encoder_forced_x264(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline.subprocess, "run", _runner({"libx264": True}))
    assert pick_encoder("x264") == ("x264", "")
    monkeypatch.setattr(pipeline.subprocess, "run", _runner({}))
    with pytest.raises(SystemExit):
        pick_encoder("x264")


def test_pick_encoder_unknown() -> None:
    with pytest.raises(ValueError, match="quicksync"):
        pick_encoder("quicksync")


def three_d(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BlurayDiscSource:
    (tmp_path / "t.iso").write_text("")
    monkeypatch.setattr(sources, "_open_disc", opener(FakeBluray()))
    return BlurayDiscSource(str(tmp_path / "t.iso"), ["ita"])


@pytest.mark.skipif(sys.platform == "win32",
                    reason="3D audio uses os.mkfifo, POSIX only until #103")
def test_decode_3d(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = three_d(monkeypatch, tmp_path)
    assert quality_for(s, "light") == QUALITIES[("3d", "light")]
    cmd = decode_command(s, 12.0, "OUT")
    parts = [p.strip() for p in cmd.split("|")]
    assert "disc_reader.py" in parts[0]
    assert parts[1] == "edge264mvc_test - -O"
    assert parts[2] == (f"ffmpeg -nostdin -v warning -i -  {s.audio_input(12.0)} -map 0:v "
                        f"{s.audio_map()} OUT")


@pytest.mark.skipif(sys.platform == "win32",
                    reason="3D audio uses os.mkfifo, POSIX only until #103")
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


@pytest.mark.skipif(sys.platform == "win32",
                    reason="3D audio uses os.mkfifo, POSIX only until #103")
def test_decode_3d_filter_suffix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The 3D path without subtitles has no filter_complex at all today (plain
    -map 0:v): a non-empty filter_suffix (VAAPI) must still get one, with the
    suffix appearing exactly once."""
    s = three_d(monkeypatch, tmp_path)
    ffmpeg = decode_command(s, 12.0, "OUT", VAAPI_SUFFIX).split("|")[2]
    assert ffmpeg.count(VAAPI_SUFFIX) == 1
    assert "-map 0:v" not in ffmpeg
    graph = shlex.split(ffmpeg)[shlex.split(ffmpeg).index("-filter_complex") + 1]
    assert graph == f"[0:v]{VAAPI_SUFFIX}[v]"

    s.sub, s.sub_depth = s.subs[0], 10
    ffmpeg = decode_command(s, 12.0, "OUT", VAAPI_SUFFIX).split("|")[2]
    assert ffmpeg.count(VAAPI_SUFFIX) == 1
    graph = shlex.split(ffmpeg)[shlex.split(ffmpeg).index("-filter_complex") + 1]
    assert graph == ("[1:i:0x1200]split[sa][sb];[0:v][sa]overlay=x=10:y=0:eof_action=pass[l];"
                     f"[l][sb]overlay=x=1910:y=0:eof_action=pass,{VAAPI_SUFFIX}[v]")


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


def test_decode_2d_bluray_filter_suffix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "t.iso").write_text("")
    monkeypatch.setattr(sources, "_open_disc", opener(FakeBluray(three_d=False)))
    s = BlurayDiscSource(str(tmp_path / "t.iso"), ["ita"])
    ffmpeg = decode_command(s, 5.0, "OUT", VAAPI_SUFFIX).split("|")[1]
    assert ffmpeg.count(VAAPI_SUFFIX) == 1
    assert shlex.split(ffmpeg)[shlex.split(ffmpeg).index("-filter_complex") + 1] == \
        f"[0:i:0x1011]null,null,{VAAPI_SUFFIX}[v]"
    s.sub = s.subs[1]
    ffmpeg = decode_command(s, 5.0, "OUT", VAAPI_SUFFIX).split("|")[1]
    assert ffmpeg.count(VAAPI_SUFFIX) == 1
    assert shlex.split(ffmpeg)[shlex.split(ffmpeg).index("-filter_complex") + 1] == \
        f"[0:i:0x1011]null[pre];[pre][0:i:0x1201]overlay=eof_action=pass[ov];[ov]null,{VAAPI_SUFFIX}[v]"


def test_decode_dvd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "BTTF").mkdir()
    monkeypatch.setattr(dvd, "Dvd", FakeDvd)
    s = DvdSource(str(tmp_path / "BTTF"), ["ita"])
    s.sub = s.subs[0]
    ffmpeg = decode_command(s, 5.0, "OUT").split("|")[1]
    assert "-ifo_palette" in ffmpeg and "-f mpeg " in ffmpeg
    # DVD titles often open with seconds without audio: ffmpeg looks further for it
    assert "-analyzeduration 8000000 -probesize 20000000 -i -" in ffmpeg
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
