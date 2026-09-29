"""The stand-in pictures (loading animation, black tail): which part of the
picture goes where, for 2D and 3D movies and 2D or side-by-side animations."""
import shlex
from pathlib import Path
from typing import Any

import pytest

import bd3d_fs
from bd3d_fs import Bd3dFS, Library, VirtualFile, is_side_by_side
from pipeline import Quality
from test_bd3d_fs import FakeSource, no_invalidate


def graph_of(cmd: str) -> str:
    args = shlex.split(cmd)
    return args[args.index("-filter_complex") + 1]


def movie(tmp_path: Path, two_d: bool) -> VirtualFile:
    source = FakeSource(str(tmp_path / "disc"))
    if two_d:
        source.two_d, source.frame_size = True, "1920x1080"
    return VirtualFile(1, source, "x264", Quality("normal", 1, 8_000_000), str(tmp_path / "log"))


@pytest.mark.parametrize(("two_d", "sbs", "expected"), [
    (False, False, ("scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:-1:-1,"
                    "setsar=1,split[a][b];[a][b]hstack[v]")),                # in both eyes
    (False, True, ("scale=3840:1080:force_original_aspect_ratio=decrease,pad=3840:1080:-1:-1,"
                   "setsar=1[v]")),                                          # as it is
    (True, False, ("scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:-1:-1,"
                   "setsar=1[v]")),
    (True, True, "crop=iw/2:ih:0:0,fps=24000/1001,scale=1920:1080:"),        # the left eye
])
def test_filler_picture(tmp_path: Path, two_d: bool, sbs: bool, expected: str) -> None:
    vf = movie(tmp_path, two_d)
    graph = graph_of(vf.filler_command("-i loader.mp4", 12.0, sbs=sbs))
    assert graph.startswith("[0:v]")
    assert expected in graph


def test_is_side_by_side(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes = {"sbs.mp4": (3840, 1080), "flat.mp4": (1920, 1080)}

    def probe(*args: str) -> dict[str, Any]:
        if args[-1] not in sizes:
            raise OSError("no such file")
        w, h = sizes[args[-1]]
        return {"streams": [{"width": w, "height": h}]}

    monkeypatch.setattr(bd3d_fs, "ffprobe_json", probe)
    is_side_by_side.cache_clear()
    assert is_side_by_side("sbs.mp4")
    assert not is_side_by_side("flat.mp4")
    assert not is_side_by_side("missing.mp4")
    is_side_by_side.cache_clear()


def test_library_picks_the_3d_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bd3d_fs.pyfuse3, "invalidate_entry_async", no_invalidate)
    fs = Bd3dFS()
    lib = Library(fs, "x264", str(tmp_path / "log"), "single", False, [], "flat.mp4", "sbs.mp4")
    three_d = FakeSource(str(tmp_path / "a"))
    two_d = FakeSource(str(tmp_path / "b"))
    two_d.two_d, two_d.category = True, "Blu-ray"
    lib.add(three_d)
    lib.add(two_d)
    loaders = {e.source.two_d: e.loader for e in fs.files() if isinstance(e, VirtualFile)}
    assert loaders == {False: "sbs.mp4", True: "flat.mp4"}
    fs2 = Bd3dFS()
    Library(fs2, "x264", str(tmp_path / "log"), "single", False, [], "flat.mp4").add(three_d)
    assert [e.loader for e in fs2.files() if isinstance(e, VirtualFile)] == ["flat.mp4"]


def test_the_default_loader_is_side_by_side() -> None:
    """Shipped as one side-by-side video: 3D movies show it in 3D, 2D ones its
    left eye (probed with the real ffprobe). The simple one is 2D."""
    default = bd3d_fs.loader_video(bd3d_fs.DEFAULT_LOADER)
    simple = bd3d_fs.loader_video("simple")
    assert default is not None and simple is not None
    is_side_by_side.cache_clear()
    assert is_side_by_side(default)
    assert not is_side_by_side(simple)
    is_side_by_side.cache_clear()


def test_loader_choice(tmp_path: Path) -> None:
    assert bd3d_fs.loader_video("none") is None
    assert bd3d_fs.loader_video("simple") == str(bd3d_fs.LOADERS / "simple.mp4")
    own = tmp_path / "spinner.mp4"
    own.write_bytes(b"")
    assert bd3d_fs.loader_video(str(own)) == str(own)
    with pytest.raises(SystemExit, match="retrowave, simple"):
        bd3d_fs.loader_video("vaporwave")
