"""library.py — the virtual files and the shelf that holds them: Generator (a
running decode pipeline), VirtualFile (the constant-bitrate .ts a Generator
feeds), SidecarFile (a plain passthrough, e.g. external subtitles), Library
(turns Source objects into files of the Tree) and the Shelf protocol it
implements. Synchronous and thread-safe, like tree.py: bd3d_fs.py (the
program: arguments, logging, drive watching) wires it to a front end
(fuse_fs.py, http_server.py); this module imports neither of them, nor
pyfuse3 nor trio.

The trick behind VirtualFile is a constant-bitrate MPEG-TS (-muxrate): byte X
of the file always matches second X / bytes_per_sec of the movie. So:
  - sequential reads -> keep reading from the running pipeline;
  - a jump (player seek) -> restart the pipeline from the keyframe before that
    second and drop the few bytes up to X.
"""
from __future__ import annotations

import contextlib
import copy
import errno
import functools
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
import weakref
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TextIO

from pipeline import Quality, decode_command, encoder_args, quality_for
from sources import Source, ffprobe_json
from tree import ROOT_ID, FileEntry, Folder, Tree

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
KILL_WAIT = 5                        # seconds a killed pipeline gets to exit
# a player that stopped reading this long ago is paused (its buffer is full and
# it does not play it): a jump then shows it one still frame, which must be the
# movie, not the animation
PAUSED_AFTER = 8.0
# a read waiting this long for the pipeline means the disc or the decoding is slow
# there (not the network); logged at most every SLOW_WAIT_LOG_EVERY seconds
SLOW_WAIT = 5.0
SLOW_WAIT_LOG_EVERY = 30.0
# seconds of movie ready past the player's position: with this much, a player
# pulling data slowly is held back by the network or itself, not by the disc
READY_ENOUGH = 4.0
PIPELINE_LOG_MAX = 10 * 1024 * 1024  # the pipeline log is rotated beyond this size


@functools.cache
def is_side_by_side(video: str) -> bool:
    """A full side-by-side 3D video (32:9, e.g. 3840x1080) rather than a 2D one."""
    try:
        info = ffprobe_json("-select_streams", "v:0", "-show_entries", "stream=width,height",
                            video)
        stream: dict[str, int] = info["streams"][0]
        return stream["width"] >= 3 * stream["height"]
    except (OSError, subprocess.SubprocessError, KeyError, IndexError):
        return False


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


def clock_time(seconds: float) -> str:
    """4150.2 -> '1:09:10', a position as the player shows it."""
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def open_pipeline_log(path: str, title: str) -> TextIO:
    """The pipeline log, appended to: one section per pipeline, so the output of
    earlier pipelines (before a jump, before a stall) is still there. Rotated to
    path.1 when it grows beyond PIPELINE_LOG_MAX."""
    log_file = Path(path)
    with contextlib.suppress(OSError):
        if log_file.stat().st_size > PIPELINE_LOG_MAX:
            log_file.replace(log_file.with_name(log_file.name + ".1"))
    stderr = log_file.open("a")
    stderr.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {title} ===\n")
    stderr.flush()
    return stderr


class Generator:
    """A running pipeline producing the virtual file from offset `base` on."""

    def __init__(self, vf: VirtualFile, seconds: float, *, loader: bool = False,
                 with_loader: bool = False) -> None:
        source = vf.source
        start = source.keyframe_at_or_before(seconds)
        # the stream's clock and bytes start at t0: the keyframe, or the requested
        # time when the disc's first keyframe comes after it (0.081s on Il
        # Prigioniero, #73). The pipeline must cover the offset it was started for,
        # or every read there would start another one
        t0 = min(start, seconds)
        # base aligned to TS packets, so bytes match the global grid
        self.base = int(t0 * vf.bytes_per_sec) // TS_PACKET * TS_PACKET
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
        self.path, self.bytes_per_sec, self.is_loader = vf.path, vf.bytes_per_sec, loader
        self.slow_logged = 0.0

        if loader and vf.loader:
            # same start, so the same timestamps and bytes <-> time mapping as the movie
            # frequent keyframes: the player can start it anywhere
            cmd = vf.filler_command(f"-stream_loop -1 -i {shlex.quote(vf.loader)}", t0,
                                    extra="-g 6 ", sbs=is_side_by_side(vf.loader))
            self.ahead_max = LOADER_AHEAD * vf.bytes_per_sec
        else:
            cmd = decode_command(
                source, start,
                f"{vf.encode_args} {COMMON_ARGS}"
                f"-output_ts_offset {t0:.3f} -f mpegts -muxrate {vf.muxrate} -",
            )
            log.info("%s: pipeline from %.1fs (requested %.1fs, offset %d)",
                     vf.path, start, seconds, self.base)
            if with_loader:
                self.loader = Generator(vf, seconds, loader=True)
        log.debug("  %s", cmd)
        with open_pipeline_log(vf.log_path, f"{vf.path}: {'loader' if loader else 'pipeline'} "
                               f"from {start:.1f}s") as stderr:
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
            asked = time.monotonic()
            deadline = asked + 60
            while (self.end < offset + size and not self.eof and not self.stopped
                   and time.monotonic() < deadline):
                self.cond.wait(timeout=1)
            waited = time.monotonic() - asked
            if (waited >= SLOW_WAIT and not self.is_loader
                    and time.monotonic() - self.slow_logged >= SLOW_WAIT_LOG_EVERY):
                self.slow_logged = time.monotonic()
                log.warning("%s: the player waited %.0fs for the movie at %s: the disc or the "
                            "decoding is slow here (a damaged disc, a busy drive or CPU), "
                            "not the network", self.path, waited,
                            clock_time(offset / self.bytes_per_sec))
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
        # SIGKILL waits for a process stuck in the kernel (a drive that does not
        # answer a read): do not hang an eject or the program's exit on it
        try:
            self.proc.wait(KILL_WAIT)
        except subprocess.TimeoutExpired:
            log.warning("%s: the pipeline did not exit %ds after SIGKILL (stuck on the "
                        "drive?): left behind", self.path, KILL_WAIT)


class VirtualFile:
    # the same bytes can differ between two reads (the loading animation, then
    # the movie once it is ready): front ends must not cache them (FUSE direct_io)
    changing = True

    def __init__(self, inode: int, source: Source, encoder: str, quality: Quality,
                 log_path: str, parent: int = ROOT_ID, path: str = "",
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
                       extra: str = "", *, sbs: bool = False) -> str:
        """ffmpeg writing a stand-in for the movie from second t0: `video_input`
        (black, or the loading animation fitted to the picture) and silence, with
        the same streams, timestamps and bitrate the movie has there, so the
        player can go on from one to the other. A 2D input goes in both eyes of a
        3D movie; a side-by-side one (`sbs`) is used as it is, or only its left
        eye for a 2D movie."""
        src = self.source
        w, h = (int(v) for v in src.frame_size.split("x"))
        eye = w // 2 if not src.two_d else w
        n_audio = max(1, len(src.audio_langs))
        inputs = " ".join([video_input] + ["-f lavfi -i anullsrc=r=48000:cl=stereo"] * n_audio)

        def fit(width: int, pre: str = "") -> str:
            return (f"[0:v]{pre}fps={src.frame_rate},"
                    f"scale={width}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{h}:-1:-1,setsar=1")

        if sbs and eye != w:                    # 3D picture, 3D movie
            graph = f"{fit(w)}[v]"
        elif eye != w:                          # 2D picture in both eyes
            graph = f"{fit(eye)},split[a][b];[a][b]hstack[v]"
        else:                                   # 2D movie: the left eye of a 3D picture
            graph = f"{fit(w, 'crop=iw/2:ih:0:0,' if sbs else '')}[v]"
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

    def ready_ahead(self, offset: int) -> float:
        """Seconds of movie a pipeline has made past `offset` (0: none covers it)."""
        with self.lock:
            gens = list(self.gens)
        for g in gens:
            if not g.stopped and g.buf_start <= offset <= g.end:
                return (g.end - offset) / self.bytes_per_sec
        return 0.0

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
            ahead = self.ready_ahead(offset)
            if ahead >= READY_ENOUGH:
                hint = f" (try {LIGHT_DIR}/)" if self.quality.name == "normal" else ""
                log.warning("%s: the player receives %d%% of the data rate the movie needs, "
                            "with %.0fs of movie ready ahead: the network (or the player) "
                            "is too slow for this file, playback will pause%s",
                            self.path, ratio * 100, ahead, hint)
            else:
                log.warning("%s: the player receives %d%% of the data rate the movie needs, "
                            "and only %.1fs of movie is ready ahead: the disc or the decoding "
                            "cannot keep up here (a damaged disc, a busy drive or CPU)",
                            self.path, ratio * 100, ahead)
        elif ratio >= 1.0 and self.slow_since is not None:
            log.info("%s: back to real time (%d%%)", self.path, ratio * 100)
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
        raise OSError(errno.EIO, f"{self.path}: no pipeline could serve the read")

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

    changing = False

    def __init__(self, inode: int, name: str, path: str, parent: int = ROOT_ID) -> None:
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


class Library:
    """Turns sources into files of the tree: in the folder of their kind, one per
    audio language and/or one with all languages, the same again at a lower
    bitrate in Light/, plus the external subtitles next to each video."""

    def __init__(self, tree: Tree, encoder: str, log_file: str, audio_files: str, light: bool,
                 sub_langs: list[str] | None = None, loader: str | None = None,
                 loader_3d: str | None = None) -> None:
        self.tree, self.encoder, self.log_file = tree, encoder, log_file
        self.loader, self.loader_3d = loader, loader_3d     # 3D movies: loader_3d if given
        self.audio_files, self.light = audio_files, light
        self.sub_langs = sub_langs          # None: every language; []: no subtitle versions

    def _path(self, inode: int) -> str:
        p = self.tree.path_of(inode)
        return f"{p}/" if p else ""

    def _add_movie(self, source: Source, parent: int, level: str) -> list[FileEntry]:
        quality = quality_for(source, level)
        loader = self.loader_3d if self.loader_3d and not source.two_d else self.loader
        vf = VirtualFile(self.tree.new_id(), source, self.encoder, quality, self.log_file,
                         parent, self._path(parent), loader)
        added: list[FileEntry] = [vf]
        sub = ""
        if source.sub is not None:
            sub = f", {'forced ' if source.forced_only else ''}subtitles {source.sub.lang}"
        log.info("+ %s  (%.0f min, %.1f Mbit/s, audio %s%s)", vf.path, source.duration / 60,
                 vf.muxrate / 1e6, source.audio_desc, sub)
        # external subtitles: "movie.ita.srt" -> "movie - 3D SBS.ita.srt", so that
        # players pair them with the video (whether they show them in 3D is up to them)
        added += [SidecarFile(self.tree.new_id(), vf.name[:-len(".ts")] + suffix, path, parent)
                  for suffix, path in source.sidecars]
        for e in added:
            self.tree.add(e)
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
        if self.audio_files == "single":
            variants = [source]
        elif len(variants) == 1:
            # a single variant: keep it as is (it may carry a disc-wide language
            # prefix even with one language, #80), rather than the plain source
            variants = [variants[0]]
        elif self.audio_files == "both":
            multi = source
        added: list[FileEntry] = []
        for v in variants:
            for w in self._with_subs(v):
                added += self._add_movie(w, folder, level)
        if multi is not None:
            multi_dir = self.tree.folder(MULTI_AUDIO_DIR, folder).inode
            for w in self._with_subs(multi):
                added += self._add_movie(w, multi_dir, level)
        return added

    def add(self, source: Source) -> list[FileEntry]:
        """Files go in the folder of their kind: Blu-ray 3D/, Blu-ray/ ... (a series'
        bonus titles in its Extra/ sub-folder)."""
        category = self.tree.folder(source.category).inode
        if source.subfolder:
            category = self.tree.folder(source.subfolder, category).inode
        added = self._add_set(source, category, "normal")
        if self.light:
            added += self._add_set(source, self.tree.folder(LIGHT_DIR, category).inode, "light")
        return added

    def remove(self, entries: Sequence[FileEntry]) -> None:
        for e in entries:
            e.stop()
            self.tree.remove(e)
            log.info("- %s", e.path)
        # folders left empty go too, deepest first (Multi-audio/, Light/, then the
        # folder of the kind): a folder is there only if there is a movie in it
        changed = True
        while changed:
            changed = False
            for folder in [n for n in list(self.tree.nodes.values()) if isinstance(n, Folder)]:
                if folder.inode != ROOT_ID and not self.tree.children.get(folder.inode):
                    self.tree.remove(folder)
                    changed = True


class Shelf(Protocol):
    """Where the files of an inserted disc go (Library)."""

    def add(self, source: Source) -> list[FileEntry]: ...
    def remove(self, entries: Sequence[FileEntry]) -> None: ...
