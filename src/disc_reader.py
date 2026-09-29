#!/usr/bin/env python3
"""
disc_reader.py — read a 3D Blu-ray title from a disc/ISO/BDMV folder, decrypted
on the fly, from a given time:

  stdout       Annex B H.264 + MVC (base + dependent view of each frame) for edge264
  --audio-out  the selected audio track(s) as a small MPEG-TS for ffmpeg (a file
               or a FIFO), written from its own thread with a large queue, so a
               consumer that reads audio late never stalls the video

A/V alignment: ffmpeg starts each input at its own first timestamp. The audio TS
therefore also carries the first video frame (the keyframe we start from) as a
timing anchor, which is never used as a stream: the input's start time becomes
that frame's PTS and the audio lands exactly where it belongs.

Usage:
    disc_reader.py /dev/sr0 --playlist 00070 --start 3000 --audio-pid 0x1102 \
        --audio-out /tmp/audio.fifo | edge264_test - -Ok | ffmpeg ...
"""
import argparse
import contextlib
import os
import queue
import sys
import threading
from typing import BinaryIO

from bdmv import PlayItem, m2ts_seek, open_ssif, parse_clpi, parse_mpls, ssif_seek
from bluray import Disc
from ssif_demux import BASE_PID, SOURCE_PACKET, SsifDemuxer, timestamp

PAT_PID, PMT_PID = 0x0000, 0x0100

Buffer = bytes | bytearray | memoryview


def _crc32_mpeg2(data: Buffer) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def _pmt_streams(section: Buffer) -> list[tuple[int, int, int]]:
    """(offset, length, PID) of each elementary stream entry of a PMT section."""
    pos = 12 + (((section[10] & 0x0F) << 8) | section[11])
    end = 3 + (((section[1] & 0x0F) << 8) | section[2]) - 4
    out: list[tuple[int, int, int]] = []
    while pos + 5 <= end:
        length = 5 + (((section[pos + 3] & 0x0F) << 8) | section[pos + 4])
        out.append((pos, length, ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]))
        pos += length
    return out


def _pmt_has(section: Buffer, pid: int) -> bool:
    return any(p == pid for _, _, p in _pmt_streams(section))


def filter_pmt(section: Buffer, keep: set[int]) -> bytes:
    """PMT section listing only the elementary streams in `keep`, with a new CRC.

    ffmpeg analyses every stream the PMT announces before it starts: announcing
    only the forwarded ones lets it start as soon as it has seen them."""
    prog_info_len = ((section[10] & 0x0F) << 8) | section[11]
    body = bytearray(section[:12 + prog_info_len])
    for pos, length, pid in _pmt_streams(section):
        if pid in keep:
            body += section[pos:pos + length]
    length = len(body) - 3 + 4
    body[1] = (body[1] & 0xF0) | (length >> 8)
    body[2] = length & 0xFF
    return bytes(body) + _crc32_mpeg2(body).to_bytes(4, "big")


def _payload_start(p: Buffer) -> int:
    """Offset of the payload in TS packet p (after the adaptation field, if any)."""
    return 5 + p[4] if (p[3] >> 4) & 2 else 4


def pes_pts(p: Buffer) -> int | None:
    """PTS in the PES header that starts in TS packet p, if any."""
    hdr = _payload_start(p)
    if p[hdr:hdr + 3] != b"\x00\x00\x01" or not p[hdr + 7] & 0x80:
        return None
    return timestamp(p[hdr + 9:hdr + 14])


def shift_timestamps(p: bytearray, delta: int) -> None:
    """Add delta to PTS (and DTS) of the PES header starting in TS packet p."""
    hdr = _payload_start(p)
    fields = [hdr + 9] + ([hdr + 14] if p[hdr + 7] & 0x40 else [])
    for o in fields:
        ts = (timestamp(p[o:o + 5]) + delta) % (1 << 33)
        p[o] = (p[o] & 0xF1) | ((ts >> 29) & 0x0E)
        p[o + 1] = (ts >> 22) & 0xFF
        p[o + 2] = ((ts >> 14) & 0xFE) | 1
        p[o + 3] = (ts >> 7) & 0xFF
        p[o + 4] = ((ts << 1) & 0xFE) | 1


class PmtRewriter:
    """Reassembles the source PMT and re-emits it, filtered, as one TS packet."""

    def __init__(self, keep: set[int]) -> None:
        self.keep = keep
        self.pmt = bytearray()           # PMT section being reassembled
        self.cc = 0

    def feed(self, p: Buffer, pusi: bool) -> bytes:
        hdr = _payload_start(p)
        if pusi:
            self.pmt = bytearray(p[hdr + 1 + p[hdr]:])           # skip pointer field
        elif self.pmt:
            self.pmt += p[hdr:]
        if len(self.pmt) < 3 or len(self.pmt) < 3 + (((self.pmt[1] & 0x0F) << 8) | self.pmt[2]):
            return b""
        section, self.pmt = bytes(self.pmt), bytearray()
        if not _pmt_has(section, BASE_PID):
            # the .ssif interleaves two transport streams that both use PMT PID
            # 0x100: the dependent clip's PMT lists only the MVC stream. Drop it.
            return b""
        section = filter_pmt(section, self.keep)
        pkt = bytes([0x47, 0x40 | (PMT_PID >> 8), PMT_PID & 0xFF, 0x10 | self.cc, 0])
        self.cc = (self.cc + 1) & 0x0F
        return (pkt + section).ljust(188, b"\xff")


class VideoWriter:
    """Writes Annex B to stdout from its own thread: while ffmpeg analyses the audio
    input it does not read the video, and the disc must keep being read meanwhile."""

    def __init__(self, out: BinaryIO) -> None:
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=3000)  # AUs (~ 1-2 minutes)
        self.broken = False
        self.thread = threading.Thread(target=self._run, args=(out,), daemon=True)
        self.thread.start()

    def _run(self, out: BinaryIO) -> None:
        while (au := self.q.get()) is not None:
            if self.broken:
                continue
            try:
                out.write(au)
            except BrokenPipeError:
                self.broken = True
        with contextlib.suppress(BrokenPipeError):
            out.flush()

    def write(self, au: bytes) -> None:
        self.q.put(au)

    def close(self) -> None:
        self.q.put(None)
        self.thread.join()


class AudioTap:
    """Forwards the TS packets of PAT, PMT, the chosen audio PIDs and the anchor frame."""

    def __init__(self, path: str, audio_pids: set[int], start_pts: int) -> None:
        self.pids = audio_pids
        self.pmt = PmtRewriter(audio_pids | {BASE_PID})
        self.start = start_pts & ~0x1FF
        self.anchor = "waiting"          # waiting -> copying -> done
        self.anchor_pts: int | None = None   # exact PTS of the keyframe, from its PES header
        self.started: set[int] = set()   # audio PIDs already forwarding
        self.keeping: dict[int, bool] = {}   # per audio PID: is the current PES kept
        self.delta = 0                   # added to audio PTS/DTS of the current clip
        self.lo: int | None = None       # clip in/out time (90 kHz, clip timeline)
        self.hi: int | None = None
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=4096)   # ~ tens of MB of audio
        self.thread = threading.Thread(target=self._writer, args=(path,), daemon=True)
        self.thread.start()

    def _writer(self, path: str) -> None:
        try:
            with open(path, "wb") as f:  # opening a FIFO blocks until ffmpeg opens it
                while (chunk := self.q.get()) is not None:
                    f.write(chunk)
        except BrokenPipeError:          # ffmpeg stopped reading (end, seek, stop)
            while self.q.get() is not None:
                pass

    def set_clip(self, delta: int, in_pts: int, out_pts: int) -> None:
        """Next play item: its audio keeps only [in, out) and is moved by `delta`
        onto the timeline of the first clip, since every clip restarts its PTS."""
        self.delta, self.lo, self.hi = delta, in_pts, out_pts

    def _anchor(self, p: bytes, pusi: bool) -> bytes:
        """The keyframe we start from, packet by packet, then nothing."""
        if pusi:
            if self.anchor == "copying":
                self.anchor = "done"
                return b""
            pts = pes_pts(p)
            if pts is not None and pts & ~0x1FF == self.start:
                self.anchor = "copying"
                self.anchor_pts = pts
        return p if self.anchor == "copying" else b""

    def _audio(self, pid: int, p: bytes, pusi: bool) -> Buffer:
        if pusi:
            pts = pes_pts(p)
            keep = pts is not None and self.anchor_pts is not None
            if pts is not None and self.anchor_pts is not None and pid not in self.started:
                # Video travels ahead of its presentation time in a TS,
                # audio almost on time: audio right after the keyframe in
                # the stream still has earlier PTS. Start each track at
                # its first PES presented at or after the keyframe.
                keep = (pts - self.anchor_pts) % (1 << 33) < 1 << 32
            if pts is not None and keep and self.lo is not None and self.hi is not None:
                keep = self.lo <= pts < self.hi
            self.keeping[pid] = keep
            if keep:
                self.started.add(pid)
                if self.delta:
                    shifted = bytearray(p)
                    shift_timestamps(shifted, self.delta)
                    return shifted
        return p if self.keeping.get(pid) else b""

    def feed(self, unit: bytes) -> None:
        out = bytearray()
        for off in range(0, len(unit) - SOURCE_PACKET + 1, SOURCE_PACKET):
            p = unit[off + 4:off + SOURCE_PACKET]
            pid = ((p[1] & 0x1F) << 8) | p[2]
            pusi = bool(p[1] & 0x40)
            if pid == PMT_PID:
                out += self.pmt.feed(p, pusi)
            elif pid == BASE_PID and self.anchor != "done":
                out += self._anchor(p, pusi)
            elif pid == PAT_PID:
                out += p
            elif pid in self.pids:
                out += self._audio(pid, p, pusi)
        if out:
            self.q.put(bytes(out))

    def close(self) -> None:
        self.q.put(None)
        self.thread.join()


class Remux2D:
    """2D Blu-ray: the clip's transport stream filtered for ffmpeg. Keeps PAT, the
    PMT (only the forwarded streams), video 0x1011 and the chosen audio PIDs.
    Video starts at the keyframe, audio at its first PES presented at or after it,
    so both start together. PES outside the play item's [in, out) are dropped and
    later clips are moved onto the first clip's timeline, like the 3D audio."""

    def __init__(self, out: BinaryIO, audio_pids: set[int], start_pts: int) -> None:
        self.out = out
        self.pids = audio_pids | {BASE_PID}
        self.pmt = PmtRewriter(self.pids)
        self.start = start_pts & ~0x1FF
        self.anchor_pts: int | None = None
        self.keeping: dict[int, bool] = {}
        self.started: set[int] = set()
        self.delta = 0
        self.lo: int | None = None
        self.hi: int | None = None
        self.broken = False

    def set_clip(self, delta: int, in_pts: int, out_pts: int) -> None:
        self.delta, self.lo, self.hi = delta, in_pts, out_pts

    def _keep(self, pid: int, pts: int | None) -> bool:
        """Whether the PES starting now is forwarded."""
        if pts is None:
            return False
        if self.anchor_pts is None:
            # nothing until the keyframe we start from
            if pid != BASE_PID or pts & ~0x1FF != self.start:
                return False
            self.anchor_pts = pts
        elif pid != BASE_PID and pid not in self.started and \
                (pts - self.anchor_pts) % (1 << 33) >= 1 << 32:
            return False
        return self.lo is None or self.hi is None or self.lo <= pts < self.hi

    def feed(self, unit: bytes) -> None:
        buf = bytearray()
        for off in range(0, len(unit) - SOURCE_PACKET + 1, SOURCE_PACKET):
            p = unit[off + 4:off + SOURCE_PACKET]
            pid = ((p[1] & 0x1F) << 8) | p[2]
            pusi = bool(p[1] & 0x40)
            if pid == PAT_PID:
                buf += p
            elif pid == PMT_PID:
                buf += self.pmt.feed(p, pusi)
            elif pid in self.pids:
                if pusi:
                    keep = self.keeping[pid] = self._keep(pid, pes_pts(p))
                    if keep:
                        self.started.add(pid)
                        if self.delta:
                            shifted = bytearray(p)
                            shift_timestamps(shifted, self.delta)
                            buf += shifted
                            continue
                if self.keeping.get(pid):
                    buf += p
        if buf:
            try:
                self.out.write(buf)
            except BrokenPipeError:
                self.broken = True


def _clip_delta(starts: list[float], items: list[PlayItem], i: int, first: int) -> int:
    """90 kHz offset moving clip i's timestamps onto the timeline of the first one."""
    return (round((starts[i] - starts[first]) * 90000)
            - 2 * (items[i].in_time - items[first].in_time))


def run_2d(disc: Disc, items: list[PlayItem], starts: list[float], first: int, start: float,
           audio_pids: set[int]) -> None:
    out = sys.stdout.buffer
    clip = parse_clpi(disc.read_file(f"BDMV/CLIPINF/{items[first].clip}.clpi"))
    sp = m2ts_seek(items[first], clip, start - starts[first])
    remux = Remux2D(out, audio_pids, sp.pts90)
    sys.stderr.write(f"disc_reader: clip {items[first].clip}, keyframe "
                     f"{starts[first] + sp.time:.3f}s, m2ts offset {sp.ssif_offset}\n")
    try:
        for i in range(first, len(items)):
            item = items[i]
            offset = sp.ssif_offset if i == first else 0
            if i != first:
                sys.stderr.write(f"disc_reader: clip {item.clip} from {starts[i]:.3f}s\n")
            remux.set_clip(_clip_delta(starts, items, i, first), 2 * item.in_time,
                           2 * item.out_time)
            with disc.open(f"BDMV/STREAM/{item.clip}.m2ts") as f:
                f.seek(offset)
                while (unit := f.read_unit()) and not remux.broken:
                    remux.feed(unit)
            if remux.broken:
                break
    finally:
        with contextlib.suppress(BrokenPipeError):
            out.flush()
            sys.stdout.close()


def run_3d(disc: Disc, items: list[PlayItem], starts: list[float], first: int, start: float,
           audio_pids: set[int], audio_out: str | None) -> None:
    video = VideoWriter(sys.stdout.buffer)
    tap = None
    try:
        for i in range(first, len(items)):
            item = items[i]
            base = parse_clpi(disc.read_file(f"BDMV/CLIPINF/{item.clip}.clpi"))
            dep = parse_clpi(disc.read_file(f"BDMV/CLIPINF/{item.dep_clip}.clpi"), pid=0x1012)
            if i == first:
                sp = ssif_seek(item, base, dep, start - starts[i])
                offset, demux = sp.ssif_offset, SsifDemuxer(start_pts=sp.pts90)
                sys.stderr.write(f"disc_reader: clip {item.clip}, keyframe "
                                 f"{starts[i] + sp.time:.3f}s, ssif offset {offset}\n")
                if audio_out:
                    tap = AudioTap(audio_out, audio_pids, sp.pts90)
            else:
                # the next clip continues from its first byte; each clip
                # restarts its timestamps, so its audio is moved onto the
                # timeline of the first one (the video is just a sequence
                # of frames, numbered by the encoder)
                offset, demux = 0, SsifDemuxer()
                sys.stderr.write(f"disc_reader: clip {item.clip} from {starts[i]:.3f}s\n")
            if tap:
                tap.set_clip(_clip_delta(starts, items, i, first), 2 * item.in_time,
                             2 * item.out_time)
            with open_ssif(disc, item, base, dep) as f:
                f.seek(offset)
                while (unit := f.read_unit()) and not video.broken:
                    if tap:
                        tap.feed(unit)
                    for au in demux.feed(unit):
                        video.write(au)
                for au in demux.flush():
                    video.write(au)
            if video.broken:
                break
    finally:
        video.close()
        if tap:
            tap.close()
        with contextlib.suppress(BrokenPipeError):
            sys.stdout.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", maxsplit=1)[0])
    ap.add_argument("disc", help="/dev/sr0, an .iso or a folder containing BDMV/")
    ap.add_argument("--playlist", required=True, help="e.g. 00070")
    ap.add_argument("--start", type=float, default=0.0,
                    help="seconds from the start of the title (all its clips)")
    ap.add_argument("--audio-pid", action="append", default=[],
                    help="audio PID to forward (repeatable), e.g. 0x1102")
    ap.add_argument("--audio-out", help="3D: file or FIFO for the audio TS")
    ap.add_argument("--mode", choices=["3d", "2d"], default="3d",
                    help="3d: Annex B MVC on stdout + audio TS; 2d: one filtered TS on stdout")
    args = ap.parse_args()
    start: float = args.start
    pid_args: list[str] = args.audio_pid
    audio_pids = {int(p, 0) for p in pid_args}

    with Disc(args.disc) as disc:
        items = parse_mpls(disc.read_file(f"BDMV/PLAYLIST/{args.playlist}.mpls"))
        starts = [0.0]
        for it in items[:-1]:
            starts.append(starts[-1] + it.duration)
        first = max(i for i, t in enumerate(starts) if t <= max(0.0, start))
        if args.mode == "2d":
            run_2d(disc, items, starts, first, start, audio_pids)
        else:
            run_3d(disc, items, starts, first, start, audio_pids, args.audio_out)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    with contextlib.suppress(BrokenPipeError):
        main()
    # the consumer may be gone (seek, stop): keep Python quiet when it flushes stdout at exit
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
