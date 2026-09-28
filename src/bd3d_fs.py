#!/usr/bin/env python3
"""
bd3d_fs.py — virtual file system (FUSE): every 3D Blu-ray MKV shows up as a file
"<movie> - 3D SBS.ts" (Full-SBS 3840x1080) that does not exist on disk. When a
player reads a piece of it, that piece is decoded on the fly from the MKV.

The trick is a constant-bitrate MPEG-TS (-muxrate): byte X of the file always
matches second X / bytes_per_sec of the movie. So:
  - sequential reads -> keep reading from the running pipeline;
  - a jump (player seek) -> restart the pipeline from the keyframe before that
    second and drop the few bytes up to X.

Share the mount point over SMB and a 3D player on the LAN (e.g. the VITURE
Neckband 3D Player) sees it as a regular SBS video.

Usage:
    python3 bd3d_fs.py [--mount /srv/bd3d] ~/Videos/3D [more MKVs or folders...]
Unmount: Ctrl+C (or fusermount3 -u /srv/bd3d)

Requires: python3-pyfuse3, edge264_test, ffmpeg, ffprobe
"""
import argparse
import errno
import fcntl
import logging
import os
import signal
import stat
import subprocess
import threading
import time
from typing import Optional

import pyfuse3
import trio

from pipeline import ENCODERS, decode_command, pick_encoder
from sources import BlurayDiscSource, Source, discover

log = logging.getLogger("bd3d_fs")

MUXRATE = 24_000_000                 # TS bit/s (video 20M + audio + muxer headroom)
BYTES_PER_SEC = MUXRATE // 8
TS_PACKET = 188
NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184

AHEAD_MAX = 256 * 1024 * 1024        # how far the pipeline may run ahead of the player
BEHIND_KEEP = 64 * 1024 * 1024       # how much to keep behind (slightly out-of-order reads)
JUMP_TOLERANCE = 32 * 1024 * 1024    # forward jump beyond which restarting is cheaper
EDGE_CACHE = 8 * 1024 * 1024         # file start and end stay cached (players re-read them)
IDLE_STOP = 120                      # seconds without reads before stopping the pipeline
TAIL_ALIGN = 47 * 4096               # multiple of both a TS packet (188) and a memory page

COMMON_ARGS = "-g 24 -c:a aac -ac 2 -b:a 192k "
MULTI_AUDIO_DIR = "Multi-audio"      # --audio-files both: where the all-tracks files go


def null_padding(offset: int, size: int) -> bytes:
    """Null TS packets aligned to the global 188-byte grid."""
    start = offset % TS_PACKET
    reps = (start + size) // TS_PACKET + 1
    return (NULL_PACKET * reps)[start:start + size]


class Generator:
    """A running pipeline producing the virtual file from offset `base` on."""

    def __init__(self, source: Source, seconds: float, encode_args: str, log_path: str):
        start = source.keyframe_at_or_before(seconds)
        # base aligned to TS packets, so bytes match the global grid
        self.base = int(start * BYTES_PER_SEC) // TS_PACKET * TS_PACKET
        self.buf = bytearray()
        self.buf_start = self.base     # virtual-file offset of buf[0]
        self.reader_pos = self.base
        self.eof = False
        self.stopped = False
        self.last_used = time.monotonic()
        self.cond = threading.Condition()

        cmd = decode_command(
            source, start,
            f"{encode_args} {COMMON_ARGS}"
            f"-output_ts_offset {start:.3f} -f mpegts -muxrate {MUXRATE} -",
        )
        log.info("pipeline from %.1fs (requested %.1fs, offset %d)", start, seconds, self.base)
        self.proc = subprocess.Popen(
            ["bash", "-o", "pipefail", "-c", cmd],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=open(log_path, "w"),
            start_new_session=True,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    @property
    def end(self) -> int:
        return self.buf_start + len(self.buf)

    def _pump(self):
        while True:
            with self.cond:
                # backpressure: when too far ahead we stop reading, the pipe
                # fills up and the pipeline pauses until the player catches up
                while not self.stopped and self.end - self.reader_pos > AHEAD_MAX:
                    self.cond.wait()
                if self.stopped:
                    return
            data = self.proc.stdout.read1(1 << 20)
            with self.cond:
                if not data:
                    self.eof = True
                    self.cond.notify_all()
                    return
                self.buf += data
                drop = self.reader_pos - BEHIND_KEEP - self.buf_start
                if drop > 2 * BEHIND_KEEP:  # trim in big chunks: rare memmove
                    del self.buf[:drop]
                    self.buf_start += drop
                self.cond.notify_all()

    def covers(self, offset: int) -> bool:
        with self.cond:
            return self.buf_start <= offset <= self.end + JUMP_TOLERANCE

    def read(self, offset: int, size: int) -> Optional[bytes]:
        """The bytes, or None if this pipeline was stopped meanwhile (read elsewhere)."""
        self.last_used = time.monotonic()
        with self.cond:
            self.reader_pos = offset
            self.cond.notify_all()
            deadline = time.monotonic() + 60
            while (self.end < offset + size and not self.eof and not self.stopped
                   and time.monotonic() < deadline):
                self.cond.wait(timeout=1)
            if self.stopped:
                return None
            lo = offset - self.buf_start
            data = bytes(self.buf[max(lo, 0):max(lo, 0) + size])
        if len(data) < size and self.eof:
            # the real movie ended slightly before the computed size
            data += null_padding(offset + len(data), size - len(data))
        return data

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.proc.wait()


class VirtualFile:
    def __init__(self, inode: int, source: Source, encode_args: str, log_path: str,
                 parent: int = pyfuse3.ROOT_INODE):
        self.inode = inode
        self.parent = parent
        self.source = source
        self.encode_args = encode_args
        self.log_path = log_path
        # the language goes first: players cut long names, and it must stay visible
        self.name = f"{source.label + ' - ' if source.label else ''}{source.name} - 3D SBS.ts"
        self.size = int(source.duration * BYTES_PER_SEC) // TS_PACKET * TS_PACKET
        # the tail is served synthetic; aligned so the kernel's page-aligned reads
        # never start just before it
        self.tail_start = max(0, self.size - EDGE_CACHE) // TAIL_ALIGN * TAIL_ALIGN
        self.mtime_ns = source.mtime_ns
        self.lock = threading.Lock()
        self.gens: list[Generator] = []
        self.head: Optional[bytes] = None
        self.tail: Optional[bytes] = None
        self.last_read = 0.0

    def _generator_for(self, offset: int) -> Generator:
        """A pipeline covering `offset`: an existing one, or a new one. Players read
        several places at once (playback + the end of the file for the duration),
        so a new pipeline replaces the least recently used one, not the only one."""
        with self.lock:
            self.gens = [g for g in self.gens if not g.stopped]
            for g in self.gens:
                if g.covers(offset):
                    g.last_used = time.monotonic()
                    return g
            if len(self.gens) >= self.source.max_pipelines:
                old = min(self.gens, key=lambda g: g.last_used)
                self.gens.remove(old)
                old.stop()
            g = Generator(self.source, offset / BYTES_PER_SEC, self.encode_args, self.log_path)
            self.gens.append(g)
            return g

    def _synthetic_tail(self) -> bytes:
        """The last EDGE_CACHE bytes, made of black video and silence with the same
        streams and timestamps the real movie has there. Players read the end of
        the file for its duration; decoding the real end would mean reading the
        far end of the disc while playback reads the beginning."""
        tail_start = self.tail_start
        length = self.size - tail_start
        t0 = tail_start / BYTES_PER_SEC
        n_audio = max(1, len(self.source.audio_langs))
        inputs = " ".join(["-f lavfi -i color=black:s=3840x1080:r=24000/1001"] +
                          ["-f lavfi -i anullsrc=r=48000:cl=stereo"] * n_audio)
        maps = " ".join(["-map 0:v"] + [f"-map {i + 1}:a" for i in range(n_audio)])
        langs = " ".join(f"-metadata:s:a:{i} language={lang}"
                         for i, lang in enumerate(self.source.audio_langs))
        cmd = (f"ffmpeg -nostdin -v error {inputs} {maps} {langs} -t {length / BYTES_PER_SEC + 3:.1f} "
               f"{self.encode_args} {COMMON_ARGS}"
               f"-output_ts_offset {t0:.3f} -f mpegts -muxrate {MUXRATE} -")
        out = subprocess.run(["bash", "-c", cmd], capture_output=True).stdout[:length]
        return out + null_padding(tail_start + len(out), length - len(out))

    def read(self, offset: int, size: int) -> bytes:
        size = min(size, self.size - offset)
        if size <= 0:
            return b""
        self.last_read = time.monotonic()

        # players often re-read the start (headers) and the end (duration):
        # serve them from cache instead of restarting a pipeline every time
        if self.head is not None and offset + size <= len(self.head):
            return self.head[offset:offset + size]
        tail_start = self.tail_start
        if offset + size > tail_start:
            with self.lock:
                if self.tail is None:
                    self.tail = self._synthetic_tail()
            if offset >= tail_start:
                return self.tail[offset - tail_start:offset - tail_start + size]
            # a read across the boundary (playback reaching the very end)
            return self.read(offset, tail_start - offset) + self.tail[:offset + size - tail_start]

        for _ in range(5):
            gen = self._generator_for(offset)
            data = gen.read(offset, size)
            if data is None:          # that pipeline was replaced meanwhile: try again
                continue
            if self.head is None and gen.base == 0 and gen.end >= EDGE_CACHE:
                with gen.cond:
                    if gen.buf_start == 0:
                        self.head = bytes(gen.buf[:EDGE_CACHE])
            return data
        raise pyfuse3.FUSEError(errno.EIO)

    def stop_if_idle(self):
        with self.lock:
            idle = [g for g in self.gens if time.monotonic() - g.last_used > IDLE_STOP]
            for g in idle:
                self.gens.remove(g)
        for g in idle:
            log.info("%s: no reads for %ds, stopping a pipeline", self.name, IDLE_STOP)
            g.stop()

    def stop(self):
        with self.lock:
            gens, self.gens = self.gens, []
        for g in gens:
            g.stop()


class SidecarFile:
    """A real file shown next to a virtual movie (external subtitles): plain passthrough."""

    def __init__(self, inode: int, name: str, path: str, parent: int = pyfuse3.ROOT_INODE):
        self.inode = inode
        self.parent = parent
        self.name = name
        self.path = path
        st = os.stat(path)
        self.size = st.st_size
        self.mtime_ns = st.st_mtime_ns

    def read(self, offset: int, size: int) -> bytes:
        with open(self.path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    def stop_if_idle(self):
        pass

    def stop(self):
        pass


class Folder:
    """A directory of the virtual tree (the root is pyfuse3.ROOT_INODE)."""

    def __init__(self, inode: int, name: str, parent: int = pyfuse3.ROOT_INODE):
        self.inode = inode
        self.name = name
        self.parent = parent
        self.mtime_ns = time.time_ns()


class Bd3dFS(pyfuse3.Operations):
    """The virtual tree. Entries can be added and removed while mounted (discs)."""

    def __init__(self):
        super().__init__()
        self.root = Folder(pyfuse3.ROOT_INODE, "")
        self.nodes: dict[int, object] = {self.root.inode: self.root}
        self.children: dict[int, list] = {}
        self.by_name: dict[tuple, object] = {}
        self.lock = threading.Lock()
        self._next_inode = pyfuse3.ROOT_INODE + 1

    def new_inode(self) -> int:
        with self.lock:
            self._next_inode += 1
            return self._next_inode - 1

    def add(self, e) -> None:
        with self.lock:
            self.nodes[e.inode] = e
            self.children.setdefault(e.parent, []).append(e)
            self.by_name[(e.parent, e.name.encode())] = e

    def remove(self, e) -> None:
        with self.lock:
            self.nodes.pop(e.inode, None)
            self.children.get(e.parent, []).remove(e)
            self.by_name.pop((e.parent, e.name.encode()), None)
        # drop what the kernel cached about the name, or it would linger
        pyfuse3.invalidate_entry_async(e.parent, e.name.encode(), ignore_enoent=True)

    def folder(self, name: str) -> Folder:
        """Sub-folder of the root, created on first use."""
        e = self.by_name.get((pyfuse3.ROOT_INODE, name.encode()))
        if e is None:
            e = Folder(self.new_inode(), name)
            self.add(e)
        return e

    def files(self) -> list:
        with self.lock:
            return [e for e in self.nodes.values() if not isinstance(e, Folder)]

    def _attr(self, inode: int) -> pyfuse3.EntryAttributes:
        e = self.nodes[inode]
        a = pyfuse3.EntryAttributes()
        a.st_ino = inode
        a.st_uid = os.getuid()
        a.st_gid = os.getgid()
        a.entry_timeout = 5          # short: the tree changes when discs come and go
        a.attr_timeout = 300
        a.st_blksize = 1 << 20
        if isinstance(e, Folder):
            a.st_mode = stat.S_IFDIR | 0o555
            a.st_nlink = 2
            a.st_size = 0
        else:
            a.st_mode = stat.S_IFREG | 0o444
            a.st_nlink = 1
            a.st_size = e.size
        a.st_blocks = (a.st_size + 511) // 512
        a.st_atime_ns = a.st_mtime_ns = a.st_ctime_ns = e.mtime_ns
        return a

    async def getattr(self, inode, ctx=None):
        if inode not in self.nodes:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attr(inode)

    async def lookup(self, parent_inode, name, ctx=None):
        e = self.by_name.get((parent_inode, name))
        if e is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attr(e.inode)

    async def opendir(self, inode, ctx):
        if not isinstance(self.nodes.get(inode), Folder):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, fh, start_id, token):
        with self.lock:
            entries = sorted(self.children.get(fh, []), key=lambda e: e.inode)
        for e in entries:
            if e.inode <= start_id:
                continue
            if not pyfuse3.readdir_reply(token, e.name.encode(), self._attr(e.inode), e.inode):
                break

    async def open(self, inode, flags, ctx):
        e = self.nodes.get(inode)
        if e is None or isinstance(e, Folder):
            raise pyfuse3.FUSEError(errno.ENOENT)
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise pyfuse3.FUSEError(errno.EROFS)
        return pyfuse3.FileInfo(fh=inode)

    async def statfs(self, ctx):
        # SMB clients ask for the share's free space: answer "full"
        s = pyfuse3.StatvfsData()
        s.f_bsize = s.f_frsize = 1 << 20
        s.f_blocks = sum(f.size for f in self.files()) // s.f_frsize + 1
        s.f_bfree = s.f_bavail = 0
        s.f_files = len(self.nodes)
        s.f_ffree = s.f_favail = 0
        s.f_namemax = 255
        return s

    async def read(self, fh, off, size):
        e = self.nodes.get(fh)
        if e is None:                # the disc was ejected while playing
            raise pyfuse3.FUSEError(errno.EIO)
        return await trio.to_thread.run_sync(e.read, off, size)


class Library:
    """Turns sources into files of the tree: one per audio language and/or one
    with all languages, plus the external subtitles next to each video."""

    def __init__(self, fs: Bd3dFS, encode_args: str, log_file: str, audio_files: str):
        self.fs, self.encode_args, self.log_file = fs, encode_args, log_file
        self.audio_files = audio_files

    def _add_movie(self, source: Source, parent: int) -> list:
        vf = VirtualFile(self.fs.new_inode(), source, self.encode_args, self.log_file, parent)
        added = [vf]
        where = f"{MULTI_AUDIO_DIR}/" if parent != pyfuse3.ROOT_INODE else ""
        log.info("+ %s%s  (%.0f min, audio %s)", where, vf.name, source.duration / 60,
                 source.audio_desc)
        # external subtitles: "movie.ita.srt" -> "movie - 3D SBS.ita.srt", so that
        # players pair them with the video (whether they show them in 3D is up to them)
        for suffix, path in source.sidecars:
            added.append(SidecarFile(self.fs.new_inode(), vf.name[:-len(".ts")] + suffix,
                                     path, parent))
            log.info("  + %s", added[-1].name)
        for e in added:
            self.fs.add(e)
        return added

    def add(self, source: Source) -> list:
        variants = source.variants()
        if self.audio_files == "single" or len(variants) == 1:
            return self._add_movie(source, pyfuse3.ROOT_INODE)
        added = []
        for v in variants:
            added += self._add_movie(v, pyfuse3.ROOT_INODE)
        if self.audio_files == "both":
            added += self._add_movie(source, self.fs.folder(MULTI_AUDIO_DIR).inode)
        return added

    def remove(self, entries: list) -> None:
        for e in entries:
            e.stop()
            self.fs.remove(e)
            log.info("- %s", e.name)
        # a sub-folder left empty (e.g. Multi-audio/ after the last disc) goes too
        folders = {e.parent for e in entries} - {pyfuse3.ROOT_INODE}
        for inode in folders:
            folder = self.fs.nodes.get(inode)
            if folder is not None and not self.fs.children.get(inode):
                self.fs.remove(folder)


CDROM_DRIVE_STATUS, CDS_DISC_OK = 0x5326, 4


def _disc_present(device: str) -> bool:
    """Ask the drive whether a disc is in, without reading it."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0) == CDS_DISC_OK
    except OSError:
        return False
    finally:
        os.close(fd)


async def watch_drive(device: str, library: Library, audio_langs):
    """A movie appears when a 3D disc is inserted and disappears when it is ejected."""
    entries: list = []
    state = "empty"                  # empty -> loaded / failed -> (eject) -> empty
    while True:
        present = await trio.to_thread.run_sync(_disc_present, device)
        if present and state == "empty":
            log.info("%s: disc inserted, opening it", device)
            try:
                source = await trio.to_thread.run_sync(BlurayDiscSource, device, audio_langs)
                entries = library.add(source)
                state = "loaded"
            except Exception as e:           # not 3D, cannot decrypt, not ready yet...
                log.info("%s: %s", device, e)
                state = "failed"
        elif not present and state != "empty":
            log.info("%s: disc ejected", device)
            await trio.to_thread.run_sync(library.remove, entries)
            entries, state = [], "empty"
        await trio.sleep(3)


async def idle_watchdog(fs: Bd3dFS):
    while True:
        await trio.sleep(10)
        for f in fs.files():
            await trio.to_thread.run_sync(f.stop_if_idle)


async def terminate_on_sigterm():
    # docker stop / systemctl stop send SIGTERM: unmount cleanly as with Ctrl+C
    with trio.open_signal_receiver(signal.SIGTERM) as signals:
        async for _ in signals:
            log.info("SIGTERM received, unmounting")
            pyfuse3.terminate()
            return


async def run(fs: Bd3dFS, library: Library, drives: list[str], audio_langs):
    async with trio.open_nursery() as nursery:
        nursery.start_soon(idle_watchdog, fs)
        nursery.start_soon(terminate_on_sigterm)
        for device in drives:
            nursery.start_soon(watch_drive, device, library, audio_langs)
        await pyfuse3.main()
        nursery.cancel_scope.cancel()


def main():
    parser = argparse.ArgumentParser(
        description="Expose 3D Blu-rays (MVC) as virtual Full-SBS .ts files.")
    parser.add_argument("sources", nargs="+",
                        help="Blu-ray drives (/dev/sr0: the movie appears when a disc is "
                             "inserted), ISO files, BDMV folders, 3D MKV rips, or folders "
                             "to scan recursively")
    parser.add_argument("--mount", default="/srv/bd3d", help="mount point (default /srv/bd3d)")
    parser.add_argument("--audio-lang",
                        help="audio languages to offer, e.g. ita,eng (default: first audio track)")
    parser.add_argument("--audio-files", choices=["per-language", "single", "both"],
                        default="per-language",
                        help="per-language: one file per language, for players without an "
                             "audio track menu (default); single: one file with all the "
                             "tracks; both: per-language files plus the single file in "
                             f"the {MULTI_AUDIO_DIR}/ folder")
    parser.add_argument("--encoder", choices=["auto", *ENCODERS], default="auto",
                        help="auto = NVENC if available, else x264 on the CPU")
    parser.add_argument("--log-file", default="/tmp/bd3d-pipeline.log",
                        help="stderr of the last started pipeline")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    encoder = pick_encoder(args.encoder)
    log.info("video encoder: %s", encoder)
    audio_langs = [x.strip() for x in args.audio_lang.split(",")] if args.audio_lang else None

    drives = [a for a in args.sources if os.path.exists(a) and stat.S_ISBLK(os.stat(a).st_mode)]
    others = [a for a in args.sources if a not in drives]

    fs = Bd3dFS()
    library = Library(fs, ENCODERS[encoder], args.log_file, args.audio_files)
    for source in discover(others, audio_langs) if others else []:
        library.add(source)
    if not fs.files() and not drives:
        raise SystemExit("no 3D (MVC) source found")
    for device in drives:
        log.info("watching %s: insert a 3D Blu-ray", device)

    options = set(pyfuse3.default_options)
    options |= {"fsname=bd3d", "ro", "allow_other"}
    pyfuse3.init(fs, args.mount, options)
    log.info("mounted on %s — Ctrl+C to unmount", args.mount)
    try:
        trio.run(run, fs, library, drives, audio_langs)
    except KeyboardInterrupt:
        pass
    finally:
        for f in fs.files():
            f.stop()
        pyfuse3.close(unmount=True)


if __name__ == "__main__":
    main()
