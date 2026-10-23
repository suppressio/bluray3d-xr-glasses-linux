#!/usr/bin/env python3
"""
bd3d_fs.py — the program: arguments, logging, the drive watcher, and wiring
library.py (the virtual files, decoded on the fly from the disc) to a front
end (fuse_fs.py for Samba, http_server.py/dlna.py for DLNA). Every Blu-ray
(disc in the drive, ISO, BDMV folder, 3D MKV rip) shows up as a file that does
not exist on disk, e.g. "Blu-ray 3D/ITA - <movie> - 3D SBS.ts" (Full-SBS
3840x1080) or "Blu-ray/ITA - <movie>.ts". When a player reads a piece of it,
that piece is decoded on the fly from the source.

Share the mount point over SMB (--share samba, the default) and a 3D player
on the LAN (e.g. the VITURE Neckband 3D Player) sees it as a regular SBS
video; --share dlna announces the same files over DLNA/UPnP instead (no FUSE
mount, no Samba), for players that browse a media server (dlna.py).

Usage:
    python3 bd3d_fs.py [--mount /srv/bd3d] ~/Videos/3D [more MKVs or folders...]
Unmount: Ctrl+C (or fusermount3 -u /srv/bd3d)

Requires: python3-trio, edge264_test, ffmpeg, ffprobe; python3-pyfuse3 and fuse3
for --share samba/both (fuse_fs.py)
"""
from __future__ import annotations

import argparse
import importlib
import logging
import logging.handlers
import os
import shutil
import signal
import stat
import subprocess
from pathlib import Path

import trio

from dlna import Dlna
from langs import lang_list
from library import LIGHT_DIR, MULTI_AUDIO_DIR, Library, Shelf
from osplatform import DriveStatus, default_program_log, default_temp_dir, drive_status
from pipeline import ENCODERS, pick_encoder
from sources import Source, discover, open_disc
from tree import FileEntry, Tree

log = logging.getLogger("bd3d_fs")

PROGRAM_LOG_MAX = 5 * 1024 * 1024    # the program's log file: size and old copies kept
PROGRAM_LOG_KEEP = 3


HERE = Path(__file__).resolve().parent
# the loading animations shipped with the program (loaders/ next to src/ or app/),
# chosen by name: --loader simple. The default one is side by side: 3D movies
# show it in 3D, 2D ones its left eye
LOADERS = HERE.parent / "loaders"
DEFAULT_LOADER = "retrowave"


def loader_video(choice: str) -> str | None:
    """--loader / --loader-3d: none, the name of a shipped animation, or a file."""
    if choice in ("", "none"):
        return None
    shipped = LOADERS / f"{choice}.mp4"
    if shipped.exists():
        return str(shipped)
    if Path(choice).expanduser().is_file():
        return str(Path(choice).expanduser())
    names = ", ".join(sorted(p.stem for p in LOADERS.glob("*.mp4"))) or "none shipped"
    raise SystemExit(f"--loader {choice}: no such file, nor a shipped animation ({names})")


def program_version(folder: Path = HERE) -> str:
    """The release (git tag) of this copy: from the `version` file that install.sh,
    update.sh and the Docker image write next to the program, or from git when it
    runs from a clone. E.g. "v1.0.0", or "v1.0.0-3-gabc1234" three commits later."""
    try:
        return (folder / "version").read_text().strip() or "unknown"
    except OSError:
        pass
    try:
        described = subprocess.run(["git", "-C", str(folder), "describe", "--tags", "--always"],
                                   capture_output=True, text=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return described.stdout.strip() or "unknown"


def log_to_file(path: str) -> None:
    """Also write the program's log to a file, with the date, rotated at
    PROGRAM_LOG_MAX: it outlives the terminal, and goes with problem reports."""
    if path == "none":
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=PROGRAM_LOG_MAX, backupCount=PROGRAM_LOG_KEEP, encoding="utf-8")
    except OSError as e:
        log.warning("cannot write the log file %s: %s", path, e)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(handler)


GONE_AFTER = 2                       # "no disc" answers in a row before we believe it
OPEN_ATTEMPTS = 3                    # a drive can fail a read now and then: retry the disc
RETRY_AFTER = 30                     # seconds between attempts


async def watch_drive(device: str, library: Shelf, audio_langs: list[str] | None) -> None:
    """A movie appears when a disc is inserted and disappears when it is ejected."""
    entries: list[FileEntry] = []
    state = "empty"                  # empty -> loaded / failed -> (eject) -> empty
    gone = attempts = 0
    retry_at = 0.0
    while True:
        status = await trio.to_thread.run_sync(drive_status, device)
        gone = gone + 1 if status == DriveStatus.EMPTY else 0
        retry = (state == "failed" and attempts < OPEN_ATTEMPTS
                 and trio.current_time() >= retry_at)
        if status == DriveStatus.DISC and (state == "empty" or retry):
            attempts += 1
            log.info("%s: %s", device, "disc inserted, opening it" if state == "empty"
                     else f"opening it again (attempt {attempts} of {OPEN_ATTEMPTS})")
            try:
                found = await trio.to_thread.run_sync(open_disc, device, audio_langs)
                entries = [e for source in found for e in library.add(source)]
                state = "loaded"
            # whatever goes wrong with one disc (cannot decrypt, not ready yet, a
            # read error, a structure the parsers do not expect) must not stop the
            # program; a few more attempts, then it waits for the disc to be ejected
            except Exception as e:  # noqa: BLE001
                # pathless (AACS) and path-bearing ("no title found", CSS) errors alike
                log.info("%s: %s", device, str(e).removeprefix(f"{device}: "))
                state, retry_at = "failed", trio.current_time() + RETRY_AFTER
                if attempts >= OPEN_ATTEMPTS:
                    log.info("%s: giving up: eject and reinsert the disc to try again", device)
        elif gone >= GONE_AFTER and state != "empty":
            log.info("%s: disc ejected", device)
            await trio.to_thread.run_sync(library.remove, entries)
            entries, state, attempts = [], "empty", 0
        elif status == DriveStatus.UNKNOWN:
            log.debug("%s: drive status %s, ignored", device, status)
        await trio.sleep(3)


async def idle_watchdog(tree: Tree) -> None:
    while True:
        await trio.sleep(10)
        for f in tree.files():
            await trio.to_thread.run_sync(f.stop_if_idle)


async def terminate_on_signal(stop: trio.Event, use_fuse: bool) -> None:
    # use_fuse is positional: nursery.start_soon() only forwards *args, not
    # kwargs, to the task it starts
    # Ctrl+C (SIGINT), docker stop / systemctl stop (SIGTERM): unmount (if
    # mounted) and exit cleanly. Left to Python, Ctrl+C would surface as a
    # KeyboardInterrupt wrapped in trio's exception groups, printed as a long
    # traceback.
    with trio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async for _ in signals:
            log.info("stopping%s", ": unmounting" if use_fuse else "")
            if use_fuse:
                import fuse_fs  # noqa: PLC0415  already loaded by main() when used
                fuse_fs.terminate()
            stop.set()
            return


async def run(tree: Tree, library: Library, drives: list[str], audio_langs: list[str] | None,
              use_fuse: bool) -> None:
    stop = trio.Event()
    async with trio.open_nursery() as nursery:
        nursery.start_soon(idle_watchdog, tree)
        nursery.start_soon(terminate_on_signal, stop, use_fuse)
        for device in drives:
            nursery.start_soon(watch_drive, device, library, audio_langs)
        if use_fuse:
            import fuse_fs  # noqa: PLC0415  already loaded by main() when used
            await fuse_fs.serve()
        else:
            await stop.wait()
        nursery.cancel_scope.cancel()


def require_fuse(share: str) -> None:
    """fuse_fs is imported only when FUSE is used (here first): --share dlna
    must start on a system without pyfuse3 and fuse3."""
    try:
        importlib.import_module("fuse_fs")
    except ImportError as e:
        raise SystemExit(f"--share {share} mounts the files with FUSE, which is not installed "
                         f"({e}): run scripts/install.sh --share {share}, "
                         "install python3-pyfuse3 and fuse3, or use --share dlna") from e


def require_samba(share: str) -> None:
    # smbd lives in /usr/sbin, which Debian leaves out of a normal user's PATH
    path = os.pathsep.join([os.environ.get("PATH", os.defpath), "/usr/sbin", "/sbin"])
    if shutil.which("smbd", path=path) is None:
        raise SystemExit(f"--share {share} needs the Samba server, but smbd is not installed: "
                         f"run scripts/install.sh --share {share}, "
                         "or sudo apt install samba for the .deb; or use --share dlna")


def tcp_port(value: str) -> int:
    """--http-port: 1-65535. Not 0: the OS would pick a port, but the DLNA
    description would still announce :0, an address nobody can reach."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {value}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range 1-65535: {port}")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expose Blu-rays (3D as Full-SBS) as virtual .ts files on a share.")
    parser.add_argument("sources", nargs="+",
                        help="Blu-ray drives (/dev/sr0: the movie appears when a disc is "
                             "inserted), ISO files, BDMV folders, 3D MKV rips, or folders "
                             "to scan recursively")
    parser.add_argument("--version", action="version", version=f"bluray3d-xr {program_version()}")
    parser.add_argument("--mount", default="/srv/bd3d", help="mount point (default /srv/bd3d)")
    parser.add_argument("--audio-lang", default="all",
                        help="audio languages to offer, e.g. ita,eng, or all (default: every "
                             "language on the disc, one track each)")
    parser.add_argument("--audio-files", choices=["per-language", "single", "both"],
                        default="per-language",
                        help="per-language: one file per language, for players without an "
                             "audio track menu (default); single: one file with all the "
                             "tracks; both: per-language files plus the single file in "
                             f"the {MULTI_AUDIO_DIR}/ folder")
    parser.add_argument("--subs", default="all",
                        help="subtitle languages to offer as extra versions drawn into the "
                             "picture, e.g. ita,eng; all (default) or none. Forced subtitles "
                             "of the audio language are always drawn in")
    parser.add_argument("--sub-depth", type=int, default=8,
                        help="3D subtitles: pixels each eye's copy is moved inward; more = "
                             "closer to you (default 8, 0 = on the screen plane)")
    parser.add_argument("--light", action=argparse.BooleanOptionalAction, default=True,
                        help=f"also offer every movie at a lower bitrate in "
                             f"{LIGHT_DIR}/, for weak Wi-Fi (default: on)")
    parser.add_argument("--debug", action="store_true",
                        help="also log every jump and the player's reads after it")
    parser.add_argument("--loader", default=DEFAULT_LOADER,
                        help="the animation shown in a loop after a jump until the movie is "
                             f"ready: {DEFAULT_LOADER} (default, 3D on 3D movies), simple, "
                             "none (the player waits), or a video file (2D, or side by side "
                             "3840x1080)")
    parser.add_argument("--loader-3d", default="none",
                        help="another animation for 3D movies only, side by side (3840x1080)")
    parser.add_argument("--encoder", choices=["auto", *ENCODERS], default="auto",
                        help="auto = NVENC if available, else x264 on the CPU")
    parser.add_argument("--share", choices=["samba", "dlna", "both"], default="samba",
                        help="samba (default): mount the files for Samba, as before. dlna: "
                             "announce them over DLNA/UPnP instead, for players that browse a "
                             "media server (TVs, Moon VR, Kodi...); neither FUSE nor Samba is "
                             "needed. both: do both")
    parser.add_argument("--http-port", type=tcp_port, default=8200,
                        help="port of the DLNA/HTTP server (--share dlna/both, default 8200)")
    # a log of our own pipeline at a documented place; the kernel's protected
    # symlinks keep other users from redirecting it
    parser.add_argument("--log-file", default=os.path.join(default_temp_dir(),
                                                            "bd3d-pipeline.log"),
                        help="output of the pipelines (FFmpeg, decoders), one section each")
    parser.add_argument("--log", default=str(default_program_log()),
                        help="the program's log is also saved here (default "
                             "~/.local/state/bluray3d-xr/bluray3d-xr.log), or none")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    sources_args: list[str] = args.sources
    audio_lang: str = args.audio_lang
    subs_arg: str = args.subs
    loader_arg: str = args.loader
    loader_3d_arg: str = args.loader_3d
    mount: str = args.mount
    share: str = args.share
    use_fuse, use_dlna = share in ("samba", "both"), share in ("dlna", "both")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.debug:
        log.setLevel(logging.DEBUG)      # ours only: pyfuse3's debug output is huge
    log_path: str = args.log
    log_to_file(log_path)

    log.info("bluray3d-xr %s", program_version())
    if log_path != "none":
        log.info("log saved in %s", log_path)
    encoder = pick_encoder(args.encoder)
    log.info("video encoder: %s", encoder)
    audio_langs = None if audio_lang.strip().lower() in ("", "all") else lang_list(audio_lang)

    drives = [a for a in sources_args if Path(a).exists() and stat.S_ISBLK(Path(a).stat().st_mode)]
    others = [a for a in sources_args if a not in drives]

    tree = Tree()
    Source.sub_depth = args.sub_depth
    subs = subs_arg.strip().lower()
    sub_langs = None if subs in ("", "all") else [] if subs == "none" else lang_list(subs_arg)
    loader, loader_3d = loader_video(loader_arg), loader_video(loader_3d_arg)
    lib = Library(tree, encoder, args.log_file, args.audio_files, args.light, sub_langs,
                  loader, loader_3d)
    for source in discover(others, audio_langs) if others else []:
        lib.add(source)
    if not tree.files() and not drives:
        raise SystemExit("no source found")
    for device in drives:
        log.info("watching %s: insert a disc", device)

    if use_fuse:
        require_fuse(share)
        require_samba(share)
        import fuse_fs  # noqa: PLC0415
        fuse_fs.mount(tree, mount)
    dlna: Dlna | None = None
    try:
        if use_dlna:
            try:
                dlna = Dlna(tree, args.http_port)
                dlna.start()
            except OSError as e:
                dlna = None  # Dlna() / start() close what they had opened before failing
                raise SystemExit(f"DLNA: cannot start on port {args.http_port}: {e}") from e
        trio.run(run, tree, lib, drives, audio_langs, use_fuse)
    except KeyboardInterrupt:
        pass
    finally:
        for f in tree.files():
            f.stop()
        if dlna is not None:
            dlna.stop()
        if use_fuse:
            import fuse_fs  # noqa: PLC0415
            fuse_fs.unmount()


if __name__ == "__main__":
    main()
