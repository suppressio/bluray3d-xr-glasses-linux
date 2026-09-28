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
from sources import Source, discover

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

COMMON_ARGS = "-g 24 -c:a aac -ac 2 -b:a 192k "


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

    def read(self, offset: int, size: int) -> bytes:
        with self.cond:
            self.reader_pos = offset
            self.cond.notify_all()
            deadline = time.monotonic() + 60
            while (self.end < offset + size and not self.eof and not self.stopped
                   and time.monotonic() < deadline):
                self.cond.wait(timeout=1)
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
    def __init__(self, inode: int, source: Source, encode_args: str, log_path: str):
        self.inode = inode
        self.source = source
        self.encode_args = encode_args
        self.log_path = log_path
        self.name = f"{source.name} - 3D SBS{' - ' + source.label if source.label else ''}.ts"
        self.size = int(source.duration * BYTES_PER_SEC) // TS_PACKET * TS_PACKET
        self.mtime_ns = source.mtime_ns
        self.lock = threading.Lock()
        self.gen: Optional[Generator] = None
        self.head: Optional[bytes] = None
        self.tail: Optional[bytes] = None
        self.last_read = 0.0

    def read(self, offset: int, size: int) -> bytes:
        size = min(size, self.size - offset)
        if size <= 0:
            return b""
        self.last_read = time.monotonic()

        # players often re-read the start (headers) and the end (duration):
        # serve them from cache instead of restarting the pipeline every time
        if self.head is not None and offset + size <= len(self.head):
            return self.head[offset:offset + size]
        tail_start = self.size - EDGE_CACHE
        if self.tail is not None and offset >= tail_start:
            return self.tail[offset - tail_start:offset - tail_start + size]

        with self.lock:
            gen = self.gen
            if gen is None or gen.stopped or not gen.covers(offset):
                if gen is not None:
                    gen.stop()
                gen = self.gen = Generator(self.source, offset / BYTES_PER_SEC,
                                           self.encode_args, self.log_path)

        data = gen.read(offset, size)

        if self.head is None and gen.base == 0 and gen.end >= EDGE_CACHE:
            with gen.cond:
                if gen.buf_start == 0:
                    self.head = bytes(gen.buf[:EDGE_CACHE])
        if self.tail is None and offset >= tail_start and gen.eof and gen.buf_start <= tail_start:
            self.tail = gen.read(tail_start, EDGE_CACHE)
        return data

    def stop_if_idle(self):
        with self.lock:
            if self.gen is not None and time.monotonic() - self.last_read > IDLE_STOP:
                log.info("%s: no reads for %ds, stopping pipeline", self.name, IDLE_STOP)
                self.gen.stop()
                self.gen = None

    def stop(self):
        with self.lock:
            if self.gen is not None:
                self.gen.stop()
                self.gen = None


class SidecarFile:
    """A real file shown next to a virtual movie (external subtitles): plain passthrough."""

    def __init__(self, inode: int, name: str, path: str):
        self.inode = inode
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


class Bd3dFS(pyfuse3.Operations):
    def __init__(self, files: list):
        super().__init__()
        self.files = {f.inode: f for f in files}
        self.by_name = {f.name.encode(): f for f in files}
        self.mount_time_ns = time.time_ns()

    def _attr(self, inode: int) -> pyfuse3.EntryAttributes:
        a = pyfuse3.EntryAttributes()
        a.st_ino = inode
        a.st_uid = os.getuid()
        a.st_gid = os.getgid()
        a.entry_timeout = 300
        a.attr_timeout = 300
        a.st_blksize = 1 << 20
        if inode == pyfuse3.ROOT_INODE:
            a.st_mode = stat.S_IFDIR | 0o555
            a.st_nlink = 2
            a.st_size = 0
            t = self.mount_time_ns
        else:
            f = self.files[inode]
            a.st_mode = stat.S_IFREG | 0o444
            a.st_nlink = 1
            a.st_size = f.size
            t = f.mtime_ns
        a.st_blocks = (a.st_size + 511) // 512
        a.st_atime_ns = a.st_mtime_ns = a.st_ctime_ns = t
        return a

    async def getattr(self, inode, ctx=None):
        if inode != pyfuse3.ROOT_INODE and inode not in self.files:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attr(inode)

    async def lookup(self, parent_inode, name, ctx=None):
        f = self.by_name.get(name) if parent_inode == pyfuse3.ROOT_INODE else None
        if f is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attr(f.inode)

    async def opendir(self, inode, ctx):
        if inode != pyfuse3.ROOT_INODE:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, fh, start_id, token):
        for f in sorted(self.files.values(), key=lambda f: f.inode):
            if f.inode <= start_id:
                continue
            if not pyfuse3.readdir_reply(token, f.name.encode(), self._attr(f.inode), f.inode):
                break

    async def open(self, inode, flags, ctx):
        if inode not in self.files:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise pyfuse3.FUSEError(errno.EROFS)
        return pyfuse3.FileInfo(fh=inode)

    async def statfs(self, ctx):
        # SMB clients ask for the share's free space: answer "full"
        s = pyfuse3.StatvfsData()
        s.f_bsize = s.f_frsize = 1 << 20
        s.f_blocks = sum(f.size for f in self.files.values()) // s.f_frsize + 1
        s.f_bfree = s.f_bavail = 0
        s.f_files = len(self.files) + 1
        s.f_ffree = s.f_favail = 0
        s.f_namemax = 255
        return s

    async def read(self, fh, off, size):
        return await trio.to_thread.run_sync(self.files[fh].read, off, size)


async def idle_watchdog(files: list):
    while True:
        await trio.sleep(10)
        for f in files:
            await trio.to_thread.run_sync(f.stop_if_idle)


async def terminate_on_sigterm():
    # docker stop / systemctl stop send SIGTERM: unmount cleanly as with Ctrl+C
    with trio.open_signal_receiver(signal.SIGTERM) as signals:
        async for _ in signals:
            log.info("SIGTERM received, unmounting")
            pyfuse3.terminate()
            return


async def run(files: list):
    async with trio.open_nursery() as nursery:
        nursery.start_soon(idle_watchdog, files)
        nursery.start_soon(terminate_on_sigterm)
        await pyfuse3.main()
        nursery.cancel_scope.cancel()


def main():
    parser = argparse.ArgumentParser(
        description="Expose 3D Blu-ray MKVs (MVC) as virtual Full-SBS .ts files.")
    parser.add_argument("sources", nargs="+",
                        help="3D Blu-ray MKV files, or folders to scan recursively")
    parser.add_argument("--mount", default="/srv/bd3d", help="mount point (default /srv/bd3d)")
    parser.add_argument("--audio-lang",
                        help="audio languages, e.g. ita,eng: one file per language "
                             "(default: first audio track)")
    parser.add_argument("--encoder", choices=["auto", *ENCODERS], default="auto",
                        help="auto = NVENC if available, else x264 on the CPU")
    parser.add_argument("--log-file", default="/tmp/bd3d-pipeline.log",
                        help="stderr of the last started pipeline")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    encoder = pick_encoder(args.encoder)
    log.info("video encoder: %s", encoder)
    audio_langs = [x.strip() for x in args.audio_lang.split(",")] if args.audio_lang else None
    files = []
    for source in (v for src in discover(args.sources, audio_langs) for v in src.variants()):
        vf = VirtualFile(pyfuse3.ROOT_INODE + 1 + len(files), source, ENCODERS[encoder],
                         args.log_file)
        files.append(vf)
        log.info("%s  (%.0f min, audio %s)", vf.name, source.duration / 60, source.audio_desc)
        # external subtitles: "movie.ita.srt" -> "movie - 3D SBS.ita.srt", so that
        # players pair them with the video (whether they show them in 3D is up to them)
        for suffix, path in source.sidecars:
            sc = SidecarFile(pyfuse3.ROOT_INODE + 1 + len(files),
                             vf.name[:-len(".ts")] + suffix, path)
            files.append(sc)
            log.info("  + %s", sc.name)
    if not files:
        raise SystemExit("no 3D (MVC) source found")

    options = set(pyfuse3.default_options)
    options |= {"fsname=bd3d", "ro", "allow_other"}
    pyfuse3.init(Bd3dFS(files), args.mount, options)
    log.info("mounted on %s — Ctrl+C to unmount", args.mount)
    try:
        trio.run(run, files)
    except KeyboardInterrupt:
        pass
    finally:
        for f in files:
            f.stop()
        pyfuse3.close(unmount=True)


if __name__ == "__main__":
    main()
