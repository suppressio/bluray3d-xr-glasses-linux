#!/usr/bin/env python3
"""
bd3d_fs.py — virtual file system (FUSE): every Blu-ray (disc in the drive, ISO,
BDMV folder, 3D MKV rip) shows up as a file that does not exist on disk, e.g.
"Blu-ray 3D/ITA - <movie> - 3D SBS.ts" (Full-SBS 3840x1080) or
"Blu-ray/ITA - <movie>.ts". When a player reads a piece of it, that piece is
decoded on the fly from the source.

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
from __future__ import annotations  # annotations are not evaluated: pyfuse3 versions differ

import argparse
import contextlib
import copy
import errno
import fcntl
import logging
import os
import shlex
import signal
import stat
import subprocess
import threading
import time
import weakref
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, override

import pyfuse3
import trio

from langs import lang_list
from pipeline import ENCODERS, Quality, decode_command, encoder_args, pick_encoder, quality_for
from sources import Source, discover, open_disc

log = logging.getLogger("bd3d_fs")

TS_PACKET = 188
NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184

AHEAD_MAX = 256 * 1024 * 1024        # how far the pipeline may run ahead of the player
BEHIND_KEEP = 64 * 1024 * 1024       # how much to keep behind (slightly out-of-order reads)
JUMP_TOLERANCE = 32 * 1024 * 1024    # forward jump beyond which restarting is cheaper
EDGE_CACHE = 8 * 1024 * 1024         # file start and end stay cached (players re-read them)
IDLE_STOP = 120                      # seconds without reads before stopping the pipeline
TAIL_ALIGN = 47 * 4096               # multiple of both a TS packet (188) and a memory page

COMMON_ARGS = "-g 24 -c:a aac -ac 2 -b:a 192k "
AUDIO_TRACK_MUX = 220_000            # TS bit/s per AAC 192k track, with muxer headroom
MULTI_AUDIO_DIR = "Multi-audio"      # --audio-files both: where the all-tracks files go
SUB_TAG = "sub"                      # ITAsubENG: Italian audio, English subtitles
LIGHT_DIR = "Light"                  # lower-bitrate copies, for weak Wi-Fi
RATE_WINDOW = 20                     # seconds of continuous reading to judge the network
SIBLING_IDLE = 3                     # a file of the same disc unread this long is left behind
STALE_KEEP = 15                      # seconds a replaced pipeline's data still answers reads
BOTH_READ = 1.0                      # reads this close in time at two positions = one is stale
LOADER_AHEAD = 8                     # seconds of loading animation prepared ahead of the player
# the animation is handed out at the pace of playback, plus a small lead: a player
# filling its buffer with it would have to play all of it before the movie shows
LOADER_LEAD = 2.0
LOADER_RATE = 1.0
TRACE_READS = 20                     # seconds of reads logged after a jump (--debug)
# a player that stopped reading this long ago is paused (its buffer is full and
# it does not play it): a jump then shows it one still frame, which must be the
# movie, not the animation
PAUSED_AFTER = 8.0


# files served from the same optical disc (all languages, Light, Multi-audio):
# only one of them may have pipelines running, or the drive seeks back and forth
_disc_files: dict[str, weakref.WeakSet[VirtualFile]] = {}


def null_padding(offset: int, size: int) -> bytes:
    """Null TS packets aligned to the global 188-byte grid."""
    start = offset % TS_PACKET
    reps = (start + size) // TS_PACKET + 1
    return (NULL_PACKET * reps)[start:start + size]


def _is_keyframe_packet(buf: bytearray, i: int) -> bool:
    """TS packet at buf[i] starts a video PES (PID 0x100) flagged as random access."""
    return (buf[i] == 0x47 and buf[i + 1] & 0x5F == 0x41 and buf[i + 2] == 0x00
            and bool(buf[i + 3] & 0x20) and buf[i + 4] > 0 and bool(buf[i + 5] & 0x40))


class Generator:
    """A running pipeline producing the virtual file from offset `base` on."""

    def __init__(self, vf: VirtualFile, seconds: float, *, loader: bool = False,
                 with_loader: bool = False) -> None:
        source = vf.source
        start = source.keyframe_at_or_before(seconds)
        # base aligned to TS packets, so bytes match the global grid
        self.base = int(start * vf.bytes_per_sec) // TS_PACKET * TS_PACKET
        self.buf = bytearray()
        self.buf_start = self.base     # virtual-file offset of buf[0]
        self.reader_pos = self.base
        self.eof = False
        self.stopped = False
        self.last_used = time.monotonic()
        self.cond = threading.Condition()
        self.ahead_max = AHEAD_MAX
        self.started = time.monotonic()
        # the loading animation covering this pipeline's start, and the offset
        # (a keyframe of the movie) where the movie takes over from it
        self.loader: Generator | None = None
        self.switch_at: int | None = None
        self.jump_at: int | None = None       # first offset the player read

        if loader and vf.loader:
            # same start, so the same timestamps and bytes <-> time mapping as the movie
            # frequent keyframes: the player can start it anywhere
            cmd = vf.filler_command(f"-stream_loop -1 -i {shlex.quote(vf.loader)}", start,
                                    extra="-g 6 ")
            self.ahead_max = LOADER_AHEAD * vf.bytes_per_sec
        else:
            cmd = decode_command(
                source, start,
                f"{vf.encode_args} {COMMON_ARGS}"
                f"-output_ts_offset {start:.3f} -f mpegts -muxrate {vf.muxrate} -",
            )
            log.info("%s: pipeline from %.1fs (requested %.1fs, offset %d)",
                     vf.path, start, seconds, self.base)
            if with_loader:
                self.loader = Generator(vf, seconds, loader=True)
        with Path(vf.log_path).open("w") as stderr:
            self.proc = subprocess.Popen(
                ["bash", "-o", "pipefail", "-c", cmd],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
                start_new_session=True,
            )
        if self.proc.stdout is None:
            raise RuntimeError("the pipeline has no output pipe")
        self.stdout_fd = self.proc.stdout.fileno()
        threading.Thread(target=self._pump, daemon=True).start()

    @property
    def end(self) -> int:
        return self.buf_start + len(self.buf)

    def _pump(self) -> None:
        while True:
            with self.cond:
                # backpressure: when too far ahead we stop reading, the pipe
                # fills up and the pipeline pauses until the player catches up
                while not self.stopped and self.end - self.reader_pos > self.ahead_max:
                    self.cond.wait()
                if self.stopped:
                    return
            data = os.read(self.stdout_fd, 1 << 20)
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
        # while the loading animation plays, the player reads ahead of what the
        # movie has made so far: those reads are this pipeline's too, or a movie
        # slow to start would look like a jump and be restarted over and over
        loader = self.loader
        loader_end = loader.end if loader is not None else 0
        with self.cond:
            return self.buf_start <= offset <= max(self.end, loader_end) + JUMP_TOLERANCE

    def keyframe_from(self, offset: int) -> int | None:
        """Offset of the first video keyframe packet at or after `offset` already
        in the buffer: the muxer flags it (random access indicator)."""
        with self.cond:
            lo = max(offset, self.buf_start) - self.buf_start
            lo += (-(self.buf_start + lo)) % TS_PACKET        # onto the 188-byte grid
            for i in range(lo, len(self.buf) - TS_PACKET + 1, TS_PACKET):
                if _is_keyframe_packet(self.buf, i):
                    return self.buf_start + i
        return None

    def holds(self, offset: int, size: int) -> bool:
        """The bytes are already in the buffer (also after the pipeline stopped)."""
        with self.cond:
            return self.buf_start <= offset and offset + size <= self.end

    def read(self, offset: int, size: int) -> bytes | None:
        """The bytes, or None if this pipeline was stopped meanwhile (read elsewhere)."""
        self.last_used = time.monotonic()
        with self.cond:
            self.reader_pos = offset
            self.cond.notify_all()
            deadline = time.monotonic() + 60
            while (self.end < offset + size and not self.eof and not self.stopped
                   and time.monotonic() < deadline):
                self.cond.wait(timeout=1)
            if self.stopped and not (self.buf_start <= offset and offset + size <= self.end):
                return None
            lo = offset - self.buf_start
            data = bytes(self.buf[max(lo, 0):max(lo, 0) + size])
        if len(data) < size and self.eof:
            # the real movie ended slightly before the computed size
            data += null_padding(offset + len(data), size - len(data))
        return data

    def stop(self) -> None:
        loader, self.loader = self.loader, None
        if loader is not None:
            loader.stop()
        with self.cond:
            self.stopped = True
            self.cond.notify_all()
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()


class VirtualFile:
    def __init__(self, inode: int, source: Source, encoder: str, quality: Quality,
                 log_path: str, parent: int = pyfuse3.ROOT_INODE, path: str = "",
                 loader: str | None = None) -> None:
        self.inode = inode
        self.parent = parent
        self.source = source
        self.quality = quality
        # each extra audio track adds its AAC bitrate (+ muxer headroom) to the file:
        # the constant bitrate must hold everything, or the byte <-> time mapping drifts
        extra_tracks = max(0, len(source.audio_langs) - 1)
        self.muxrate = quality.muxrate + extra_tracks * AUDIO_TRACK_MUX
        self.bytes_per_sec = self.muxrate // 8
        self.encode_args = encoder_args(encoder, quality.video)
        self.log_path = log_path
        self.loader = loader
        # the language goes first: players cut long names, and it must stay visible
        label = f"{source.label} - " if source.label else ""
        self.name = f"{label}{source.name}{source.name_suffix}.ts"
        self.path = path + self.name       # for the log, e.g. "Blu-ray/Light/ITA - x.ts"
        self.size = int(source.duration * self.bytes_per_sec) // TS_PACKET * TS_PACKET
        # the tail is served synthetic; aligned so the kernel's page-aligned reads
        # never start just before it
        self.tail_start = max(0, self.size - EDGE_CACHE) // TAIL_ALIGN * TAIL_ALIGN
        self.mtime_ns = source.mtime_ns
        self.lock = threading.Lock()
        self.gens: list[Generator] = []
        # the pipeline replaced by the last jump, with its data, and until when
        # it answers: after a jump the player still has reads for the old position
        # in flight. Starting a pipeline for them would stop the new one, and the
        # two positions would take turns restarting (seen: 16 restarts in a second)
        self.stale: Generator | None = None
        self.stale_until = 0.0
        self.stale_logged = False
        self.head: bytes | None = None
        self.tail: bytes | None = None
        self.last_read = 0.0
        self.prev_read = 0.0                     # the read before the current one
        if source.max_pipelines == 1:
            _disc_files.setdefault(source.path, weakref.WeakSet()).add(self)
        # network diagnostics: the pace at which the player pulls data
        self.rate_start = 0.0            # when the current run of sequential reads began
        self.rate_bytes = 0
        self.rate_next = 0               # offset the next sequential read would start at
        self.slow_since: float | None = None

    def _stale_read(self, offset: int) -> bool:
        """A read the player sent for the position it just left: near the replaced
        pipeline while the new one is being read too. Its answer is thrown away,
        and a pipeline for it would stop the new one. If the user really goes back
        there, the new position stops being read and this turns false within
        BOTH_READ seconds."""
        now = time.monotonic()
        with self.lock:
            return (self.stale is not None and now < self.stale_until
                    and self.stale.covers(offset)
                    and any(now - g.last_used < BOTH_READ for g in self.gens))

    def _generator_for(self, offset: int, size: int) -> Generator | None:
        """A pipeline covering `offset`: an existing one, or a new one. Players read
        several places at once (playback + the end of the file for the duration),
        so a new pipeline replaces the least recently used one, not the only one."""
        with self.lock:
            self.gens = [g for g in self.gens if not g.stopped]
            for g in self.gens:
                if g.covers(offset):
                    g.last_used = time.monotonic()
                    return g
            if self.stale is not None:
                if time.monotonic() < self.stale_until and self.stale.holds(offset, size):
                    return self.stale
                if time.monotonic() >= self.stale_until:
                    self.stale = None             # free its memory
        if self._stale_read(offset):
            if not self.stale_logged:
                log.info("%s: ignoring the player's late reads for the position it left",
                         self.path)
                self.stale_logged = True
            return None
        # a new pipeline: on an optical disc, first stop the other files' ones
        # that nobody reads any more (switching from the Italian to the English
        # file, for instance). Files read right now stay: two people may be
        # watching the same disc in two languages.
        now = time.monotonic()
        for other in list(_disc_files.get(self.source.path, ())):
            if other is not self and other.gens and now - other.last_read > SIBLING_IDLE:
                log.info("%s: stopping its pipeline, %s is reading the same disc",
                         other.path, self.path)
                other.stop()
        with self.lock:
            self.gens = [g for g in self.gens if not g.stopped]
            for g in self.gens:
                if g.covers(offset):
                    return g
            if len(self.gens) >= self.source.max_pipelines:
                old = min(self.gens, key=lambda g: g.last_used)
                self.gens.remove(old)
                old.stop()
                self.stale, self.stale_until = old, time.monotonic() + STALE_KEEP
                self.stale_logged = False
            quiet = time.monotonic() - self.prev_read
            log.debug("%s: jump, no reads in the %.1fs before", self.path, quiet)
            g = Generator(self, offset / self.bytes_per_sec,
                          with_loader=bool(self.loader) and quiet < PAUSED_AFTER)
            self.gens.append(g)
            return g

    def filler_command(self, video_input: str, t0: float, duration: float | None = None,
                       extra: str = "") -> str:
        """ffmpeg writing a stand-in for the movie from second t0: `video_input`
        (black, or the loading animation fitted to the picture, in both eyes for
        3D) and silence, with the same streams, timestamps and bitrate the movie
        has there, so the player can go on from one to the other."""
        src = self.source
        w, h = (int(v) for v in src.frame_size.split("x"))
        eye = w // 2 if not src.two_d else w
        n_audio = max(1, len(src.audio_langs))
        inputs = " ".join([video_input] + ["-f lavfi -i anullsrc=r=48000:cl=stereo"] * n_audio)
        fit = (f"[0:v]fps={src.frame_rate},scale={eye}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={eye}:{h}:-1:-1,setsar=1")
        graph = f"{fit},split[a][b];[a][b]hstack[v]" if eye != w else f"{fit}[v]"
        maps = " ".join([f"-filter_complex {shlex.quote(graph)} -map '[v]'"] +
                        [f"-map {i + 1}:a" for i in range(n_audio)])
        langs = " ".join(f"-metadata:s:a:{i} language={lang}"
                         for i, lang in enumerate(src.audio_langs))
        limit = f"-t {duration:.1f} " if duration else ""
        return (f"ffmpeg -nostdin -v error {inputs} {maps} {langs} {limit}"
                f"{self.encode_args} {COMMON_ARGS}{extra}"
                f"-output_ts_offset {t0:.3f} -f mpegts -muxrate {self.muxrate} -")

    def _synthetic_tail(self) -> bytes:
        """The last EDGE_CACHE bytes, made of black video and silence with the same
        streams and timestamps the real movie has there. Players read the end of
        the file for its duration; decoding the real end would mean reading the
        far end of the disc while playback reads the beginning."""
        tail_start = self.tail_start
        length = self.size - tail_start
        t0 = tail_start / self.bytes_per_sec
        black = f"-f lavfi -i color=black:s={self.source.frame_size}:r={self.source.frame_rate}"
        cmd = self.filler_command(black, t0, length / self.bytes_per_sec + 3)
        out = subprocess.run(["bash", "-c", cmd], capture_output=True, check=False).stdout[:length]
        return out + null_padding(tail_start + len(out), length - len(out))

    def _track_rate(self, offset: int, size: int) -> None:
        """Log when the player pulls data slower than the movie plays: over Wi-Fi
        that means pauses to rebuffer. Reads come from the SMB client, so their
        pace is the network's. A pause (no reads) or a jump starts a new run."""
        now = time.monotonic()
        if offset != self.rate_next or now - self.last_read > 5:
            self.rate_start, self.rate_bytes = now, 0
        self.rate_bytes += size
        self.rate_next = offset + size
        elapsed = now - self.rate_start
        if elapsed < RATE_WINDOW:
            return
        ratio = self.rate_bytes / elapsed / self.bytes_per_sec
        if ratio < 0.9 and self.slow_since is None:
            self.slow_since = now
            hint = f" (try {LIGHT_DIR}/)" if self.quality.name == "normal" else ""
            log.warning("%s: the player receives %d%% of the data rate the movie needs: "
                        "the network is too slow for this file, playback will pause%s",
                        self.path, ratio * 100, hint)
        elif ratio >= 1.0 and self.slow_since is not None:
            log.info("%s: network back to real time (%d%%)", self.path, ratio * 100)
            self.slow_since = None
        self.rate_start, self.rate_bytes = now, 0

    def _from_edges(self, offset: int, size: int) -> bytes | None:
        """Players often re-read the start (headers) and the end (duration): both
        are served from cache instead of restarting a pipeline every time."""
        if self.head is not None and offset + size <= len(self.head):
            return self.head[offset:offset + size]
        tail_start = self.tail_start
        if offset + size <= tail_start:
            return None
        with self.lock:
            if self.tail is None:
                self.tail = self._synthetic_tail()
        if offset >= tail_start:
            return self.tail[offset - tail_start:offset - tail_start + size]
        # a read across the boundary (playback reaching the very end)
        return self.read(offset, tail_start - offset) + self.tail[:offset + size - tail_start]

    def read(self, offset: int, size: int) -> bytes:
        size = min(size, self.size - offset)
        if size <= 0:
            return b""
        self._track_rate(offset, size)
        self.prev_read, self.last_read = self.last_read, time.monotonic()
        if self.gens and time.monotonic() - self.gens[-1].started < TRACE_READS:
            g = self.gens[-1]
            log.debug("  read t+%.2fs at %.2fs (+%d KB)", time.monotonic() - g.started,
                      offset / self.bytes_per_sec, size // 1024)

        edge = self._from_edges(offset, size)
        if edge is not None:
            return edge
        for _ in range(5):
            gen = self._generator_for(offset, size)
            if gen is None:
                return null_padding(offset, size)
            data = self._through_loader(gen, offset, size) if gen.loader is not None else None
            if data is None:
                data = gen.read(offset, size)
            if data is None:          # that pipeline was replaced meanwhile: try again
                continue
            if self.head is None and gen.base == 0 and gen.end >= EDGE_CACHE:
                with gen.cond:
                    if gen.buf_start == 0:
                        self.head = bytes(gen.buf[:EDGE_CACHE])
            return data
        raise pyfuse3.FUSEError(errno.EIO)

    def _through_loader(self, gen: Generator, offset: int, size: int) -> bytes | None:
        """While the movie is not ready, the loading animation; from the movie's
        first keyframe at or after the player's position on, None (the movie)."""
        loader = gen.loader
        if loader is None:
            return None
        if gen.jump_at is None:
            gen.jump_at = offset
        jump_at = gen.jump_at
        while gen.switch_at is None:
            gen.switch_at = gen.keyframe_from(offset)
            if gen.switch_at is not None:
                log.info("%s: movie ready, %.1fs after the jump (%.1fs of animation)",
                         self.path, time.monotonic() - gen.started,
                         (gen.switch_at - jump_at) / self.bytes_per_sec)
                break
            allowed = jump_at + (LOADER_LEAD + LOADER_RATE * (time.monotonic() - gen.started)) \
                * self.bytes_per_sec
            if offset + size <= allowed or gen.loader is None:
                break
            time.sleep(0.05)
        if gen.switch_at is not None and offset >= gen.switch_at:
            gen.loader = None
            loader.stop()
            return None
        end = offset + size if gen.switch_at is None else min(offset + size, gen.switch_at)
        data = loader.read(offset, end - offset)
        if data is None:                          # stopped meanwhile
            return None
        if end < offset + size:
            rest = gen.read(end, offset + size - end)
            if rest is None:
                return None
            data += rest
        return data

    def stop_if_idle(self) -> None:
        with self.lock:
            idle = [g for g in self.gens if time.monotonic() - g.last_used > IDLE_STOP]
            for g in idle:
                self.gens.remove(g)
        for g in idle:
            log.info("%s: no reads for %ds, stopping a pipeline", self.name, IDLE_STOP)
            g.stop()

    def stop(self) -> None:
        with self.lock:
            gens, self.gens = self.gens, []
            self.stale = None
        for g in gens:
            g.stop()


class SidecarFile:
    """A real file shown next to a virtual movie (external subtitles): plain passthrough."""

    def __init__(self, inode: int, name: str, path: str,
                 parent: int = pyfuse3.ROOT_INODE) -> None:
        self.inode = inode
        self.parent = parent
        self.name = name
        self.path = path
        st = Path(path).stat()
        self.size = st.st_size
        self.mtime_ns = st.st_mtime_ns

    def read(self, offset: int, size: int) -> bytes:
        with Path(self.path).open("rb") as f:
            f.seek(offset)
            return f.read(size)

    def stop_if_idle(self) -> None:
        pass

    def stop(self) -> None:
        pass


class Folder:
    """A directory of the virtual tree (the root is pyfuse3.ROOT_INODE)."""

    def __init__(self, inode: int, name: str, parent: int = pyfuse3.ROOT_INODE) -> None:
        self.inode = inode
        self.name = name
        self.parent = parent
        self.mtime_ns = time.time_ns()


FileEntry = VirtualFile | SidecarFile
Entry = Folder | FileEntry


class Bd3dFS(pyfuse3.Operations):
    """The virtual tree. Entries can be added and removed while mounted (discs)."""

    def __init__(self) -> None:
        super().__init__()
        self.root = Folder(pyfuse3.ROOT_INODE, "")
        self.nodes: dict[int, Entry] = {self.root.inode: self.root}
        self.children: dict[int, list[Entry]] = {}
        self.by_name: dict[tuple[int, bytes], Entry] = {}
        self.lock = threading.Lock()
        self._next_inode = pyfuse3.ROOT_INODE + 1

    def new_inode(self) -> int:
        with self.lock:
            self._next_inode += 1
            return self._next_inode - 1

    def add(self, e: Entry) -> None:
        with self.lock:
            self.nodes[e.inode] = e
            self.children.setdefault(e.parent, []).append(e)
            self.by_name[(e.parent, e.name.encode())] = e

    def remove(self, e: Entry) -> None:
        with self.lock:
            self.nodes.pop(e.inode, None)
            self.children.get(e.parent, []).remove(e)
            self.by_name.pop((e.parent, e.name.encode()), None)
        # drop what the kernel cached about the name, or it would linger
        pyfuse3.invalidate_entry_async(e.parent, e.name.encode(), ignore_enoent=True)

    def folder(self, name: str, parent: int = pyfuse3.ROOT_INODE) -> Folder:
        """Sub-folder, created on first use."""
        e = self.by_name.get((parent, name.encode()))
        if e is None:
            e = Folder(self.new_inode(), name, parent)
            self.add(e)
        if not isinstance(e, Folder):
            raise TypeError(f"{name} is a file, not a folder")
        return e

    def files(self) -> list[FileEntry]:
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

    @override
    async def getattr(self, inode: int,
                      ctx: pyfuse3.RequestContext | None = None) -> pyfuse3.EntryAttributes:
        if inode not in self.nodes:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attr(inode)

    @override
    async def lookup(self, parent_inode: int, name: bytes,
                     ctx: pyfuse3.RequestContext | None = None) -> pyfuse3.EntryAttributes:
        e = self.by_name.get((parent_inode, name))
        if e is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attr(e.inode)

    @override
    async def opendir(self, inode: int, ctx: pyfuse3.RequestContext | None) -> int:
        if not isinstance(self.nodes.get(inode), Folder):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    @override
    async def readdir(self, fh: int, start_id: int, token: pyfuse3.ReaddirToken) -> None:
        with self.lock:
            entries = sorted(self.children.get(fh, []), key=lambda e: e.inode)
        for e in entries:
            if e.inode <= start_id:
                continue
            if not pyfuse3.readdir_reply(token, e.name.encode(), self._attr(e.inode), e.inode):
                break

    @override
    async def open(self, inode: int, flags: int,
                   ctx: pyfuse3.RequestContext | None) -> pyfuse3.FileInfo:
        e = self.nodes.get(inode)
        if e is None or isinstance(e, Folder):
            raise pyfuse3.FUSEError(errno.ENOENT)
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise pyfuse3.FUSEError(errno.EROFS)
        # no page cache for the movies: the same bytes can differ between two
        # reads (the loading animation, or the movie once it is ready)
        return pyfuse3.FileInfo(fh=inode, direct_io=isinstance(e, VirtualFile))

    @override
    async def statfs(self, ctx: pyfuse3.RequestContext | None) -> pyfuse3.StatvfsData:
        # SMB clients ask for the share's free space: answer "full"
        s = pyfuse3.StatvfsData()
        s.f_bsize = s.f_frsize = 1 << 20
        s.f_blocks = sum(f.size for f in self.files()) // s.f_frsize + 1
        s.f_bfree = s.f_bavail = 0
        s.f_files = len(self.nodes)
        s.f_ffree = s.f_favail = 0
        s.f_namemax = 255
        return s

    @override
    async def read(self, fh: int, off: int, size: int) -> bytes:
        e = self.nodes.get(fh)
        if e is None or isinstance(e, Folder):     # the disc was ejected while playing
            raise pyfuse3.FUSEError(errno.EIO)
        return await trio.to_thread.run_sync(e.read, off, size)


class Library:
    """Turns sources into files of the tree: in the folder of their kind, one per
    audio language and/or one with all languages, the same again at a lower
    bitrate in Light/, plus the external subtitles next to each video."""

    def __init__(self, fs: Bd3dFS, encoder: str, log_file: str, audio_files: str, light: bool,
                 sub_langs: list[str] | None = None, loader: str | None = None) -> None:
        self.fs, self.encoder, self.log_file = fs, encoder, log_file
        self.loader = loader
        self.audio_files, self.light = audio_files, light
        self.sub_langs = sub_langs          # None: every language; []: no subtitle versions

    def _path(self, inode: int) -> str:
        where, node = "", self.fs.nodes.get(inode)
        while node is not None and node.inode != pyfuse3.ROOT_INODE:
            where, node = f"{node.name}/{where}", self.fs.nodes.get(node.parent)
        return where

    def _add_movie(self, source: Source, parent: int, level: str) -> list[FileEntry]:
        quality = quality_for(source, level)
        vf = VirtualFile(self.fs.new_inode(), source, self.encoder, quality, self.log_file,
                         parent, self._path(parent), self.loader)
        added: list[FileEntry] = [vf]
        sub = ""
        if source.sub is not None:
            sub = f", {'forced ' if source.forced_only else ''}subtitles {source.sub.lang}"
        log.info("+ %s  (%.0f min, %.1f Mbit/s, audio %s%s)", vf.path, source.duration / 60,
                 vf.muxrate / 1e6, source.audio_desc, sub)
        # external subtitles: "movie.ita.srt" -> "movie - 3D SBS.ita.srt", so that
        # players pair them with the video (whether they show them in 3D is up to them)
        added += [SidecarFile(self.fs.new_inode(), vf.name[:-len(".ts")] + suffix, path, parent)
                  for suffix, path in source.sidecars]
        for e in added:
            self.fs.add(e)
        return added

    def _with_subs(self, v: Source) -> list[Source]:
        """The file without subtitles (but with the forced ones of its audio
        language, like a disc player does), then one per subtitle language:
        ITA - movie, ITAsubITA - movie, ITAsubENG - movie ..."""
        plain = copy.copy(v)
        if len(v.audio_langs) == 1:
            forced = next((s for s in v.subs if s.lang == v.audio_langs[0]), None)
            if forced is not None:
                plain.sub, plain.forced_only = forced, True
        out = [plain]
        langs = self.sub_langs
        tracks = v.subs if langs is None else \
            [s for lang in langs for s in v.subs if s.lang == lang]
        for s in tracks:
            w = copy.copy(v)
            w.sub, w.forced_only = s, False
            w.label = f"{v.label}{SUB_TAG}{s.lang.upper()}"
            out.append(w)
        return out

    def _add_set(self, source: Source, folder: int, level: str) -> list[FileEntry]:
        variants: Sequence[Source] = source.variants()
        multi = None
        if self.audio_files == "single" or len(variants) == 1:
            variants = [source]
        elif self.audio_files == "both":
            multi = source
        added: list[FileEntry] = []
        for v in variants:
            for w in self._with_subs(v):
                added += self._add_movie(w, folder, level)
        if multi is not None:
            multi_dir = self.fs.folder(MULTI_AUDIO_DIR, folder).inode
            for w in self._with_subs(multi):
                added += self._add_movie(w, multi_dir, level)
        return added

    def add(self, source: Source) -> list[FileEntry]:
        """Files go in the folder of their kind: Blu-ray 3D/, Blu-ray/ ..."""
        category = self.fs.folder(source.category).inode
        added = self._add_set(source, category, "normal")
        if self.light:
            added += self._add_set(source, self.fs.folder(LIGHT_DIR, category).inode, "light")
        return added

    def remove(self, entries: Sequence[FileEntry]) -> None:
        for e in entries:
            e.stop()
            self.fs.remove(e)
            log.info("- %s", e.path)
        # folders left empty go too, deepest first (Multi-audio/, Light/, then the
        # folder of the kind): a folder is there only if there is a movie in it
        changed = True
        while changed:
            changed = False
            for folder in [n for n in list(self.fs.nodes.values()) if isinstance(n, Folder)]:
                if folder.inode != pyfuse3.ROOT_INODE and not self.fs.children.get(folder.inode):
                    self.fs.remove(folder)
                    changed = True


class Shelf(Protocol):
    """Where the files of an inserted disc go (Library)."""

    def add(self, source: Source) -> list[FileEntry]: ...
    def remove(self, entries: Sequence[FileEntry]) -> None: ...


CDROM_DRIVE_STATUS = 0x5326
CDS_NO_DISC, CDS_TRAY_OPEN, CDS_DISC_OK = 1, 2, 4
GONE_AFTER = 2                       # "no disc" answers in a row before we believe it


def drive_status(device: str) -> int:
    """Ask the drive whether a disc is in, without reading it. A busy drive can
    answer "not ready" or fail the call: that is not an eject (0 = no idea)."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return 0
    try:
        return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
    except OSError:
        return 0
    finally:
        os.close(fd)


async def watch_drive(device: str, library: Shelf, audio_langs: list[str] | None) -> None:
    """A movie appears when a disc is inserted and disappears when it is ejected."""
    entries: list[FileEntry] = []
    state = "empty"                  # empty -> loaded / failed -> (eject) -> empty
    gone = 0
    while True:
        status = await trio.to_thread.run_sync(drive_status, device)
        gone = gone + 1 if status in (CDS_NO_DISC, CDS_TRAY_OPEN) else 0
        if status == CDS_DISC_OK and state == "empty":
            log.info("%s: disc inserted, opening it", device)
            try:
                source = await trio.to_thread.run_sync(open_disc, device, audio_langs)
                entries = library.add(source)
                state = "loaded"
            # whatever goes wrong with one disc (cannot decrypt, not ready yet, a
            # structure the parsers do not expect) must not stop the program
            except Exception as e:  # noqa: BLE001
                log.info("%s: %s", device, e)
                state = "failed"
        elif gone >= GONE_AFTER and state != "empty":
            log.info("%s: disc ejected", device)
            await trio.to_thread.run_sync(library.remove, entries)
            entries, state = [], "empty"
        elif status not in (CDS_DISC_OK, CDS_NO_DISC, CDS_TRAY_OPEN):
            log.debug("%s: drive status %d, ignored", device, status)
        await trio.sleep(3)


async def idle_watchdog(fs: Bd3dFS) -> None:
    while True:
        await trio.sleep(10)
        for f in fs.files():
            await trio.to_thread.run_sync(f.stop_if_idle)


async def terminate_on_signal() -> None:
    # Ctrl+C (SIGINT), docker stop / systemctl stop (SIGTERM): unmount and exit
    # cleanly. Left to Python, Ctrl+C would surface as a KeyboardInterrupt
    # wrapped in trio's exception groups, printed as a long traceback.
    with trio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async for _ in signals:
            log.info("stopping: unmounting")
            pyfuse3.terminate()
            return


async def run(fs: Bd3dFS, library: Library, drives: list[str],
              audio_langs: list[str] | None) -> None:
    async with trio.open_nursery() as nursery:
        nursery.start_soon(idle_watchdog, fs)
        nursery.start_soon(terminate_on_signal)
        for device in drives:
            nursery.start_soon(watch_drive, device, library, audio_langs)
        await pyfuse3.main()
        nursery.cancel_scope.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expose Blu-rays (3D as Full-SBS) as virtual .ts files on a share.")
    parser.add_argument("sources", nargs="+",
                        help="Blu-ray drives (/dev/sr0: the movie appears when a disc is "
                             "inserted), ISO files, BDMV folders, 3D MKV rips, or folders "
                             "to scan recursively")
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
                        help=f"also offer every movie at a lower bitrate in {LIGHT_DIR}/, for "
                             "weak Wi-Fi (default: on)")
    parser.add_argument("--debug", action="store_true",
                        help="also log every jump and the player's reads after it")
    parser.add_argument("--loader", default="none",
                        help="a short video shown in a loop after a jump until the movie is "
                             "ready, e.g. a spinner (default: none, the player waits)")
    parser.add_argument("--encoder", choices=["auto", *ENCODERS], default="auto",
                        help="auto = NVENC if available, else x264 on the CPU")
    # a log of our own pipeline at a documented place; the kernel's protected
    # symlinks keep other users from redirecting it
    parser.add_argument("--log-file", default="/tmp/bd3d-pipeline.log",  # noqa: S108
                        help="stderr of the last started pipeline")
    args = parser.parse_args()
    sources_args: list[str] = args.sources
    audio_lang: str = args.audio_lang
    subs_arg: str = args.subs
    loader_arg: str = args.loader
    mount: str = args.mount

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.debug:
        log.setLevel(logging.DEBUG)      # ours only: pyfuse3's debug output is huge

    encoder = pick_encoder(args.encoder)
    log.info("video encoder: %s", encoder)
    audio_langs = None if audio_lang.strip().lower() in ("", "all") else lang_list(audio_lang)

    drives = [a for a in sources_args if Path(a).exists() and stat.S_ISBLK(Path(a).stat().st_mode)]
    others = [a for a in sources_args if a not in drives]

    fs = Bd3dFS()
    Source.sub_depth = args.sub_depth
    subs = subs_arg.strip().lower()
    sub_langs = None if subs in ("", "all") else [] if subs == "none" else lang_list(subs_arg)
    loader = None if not loader_arg or loader_arg == "none" else loader_arg
    library = Library(fs, encoder, args.log_file, args.audio_files, args.light, sub_langs, loader)
    for source in discover(others, audio_langs) if others else []:
        library.add(source)
    if not fs.files() and not drives:
        raise SystemExit("no source found")
    for device in drives:
        log.info("watching %s: insert a Blu-ray", device)

    options = set(pyfuse3.default_options)
    options |= {"fsname=bd3d", "ro", "allow_other"}
    pyfuse3.init(fs, mount, options)
    log.info("mounted on %s — Ctrl+C to unmount", mount)
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
