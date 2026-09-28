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
import os
import queue
import sys
import threading

from bdmv import open_ssif, parse_clpi, parse_mpls, ssif_seek
from bluray import Disc
from ssif_demux import BASE_PID, SOURCE_PACKET, SsifDemuxer, _timestamp

PAT_PID, PMT_PID = 0x0000, 0x0100


def _crc32_mpeg2(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def _pmt_streams(section: bytes) -> list[tuple[int, int, int]]:
    """(offset, length, PID) of each elementary stream entry of a PMT section."""
    pos = 12 + (((section[10] & 0x0F) << 8) | section[11])
    end = 3 + (((section[1] & 0x0F) << 8) | section[2]) - 4
    out = []
    while pos + 5 <= end:
        length = 5 + (((section[pos + 3] & 0x0F) << 8) | section[pos + 4])
        out.append((pos, length, ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]))
        pos += length
    return out


def _pmt_has(section: bytes, pid: int) -> bool:
    return any(p == pid for _, _, p in _pmt_streams(section))


def filter_pmt(section: bytes, keep: set[int]) -> bytes:
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


class VideoWriter:
    """Writes Annex B to stdout from its own thread: while ffmpeg analyses the audio
    input it does not read the video, and the disc must keep being read meanwhile."""

    def __init__(self, out):
        self.q: queue.Queue = queue.Queue(maxsize=3000)    # access units (~ 1-2 minutes)
        self.broken = False
        self.thread = threading.Thread(target=self._run, args=(out,), daemon=True)
        self.thread.start()

    def _run(self, out):
        while (au := self.q.get()) is not None:
            if self.broken:
                continue
            try:
                out.write(au)
            except BrokenPipeError:
                self.broken = True
        try:
            out.flush()
        except BrokenPipeError:
            pass

    def write(self, au: bytes):
        self.q.put(au)

    def close(self):
        self.q.put(None)
        self.thread.join()


class AudioTap:
    """Forwards the TS packets of PAT, PMT, the chosen audio PIDs and the anchor frame."""

    def __init__(self, path: str, audio_pids: set[int], start_pts: int):
        self.pids = audio_pids
        self.keep = audio_pids | {BASE_PID}
        self.pmt = bytearray()           # PMT section being reassembled
        self.pmt_cc = 0
        self.start = start_pts & ~0x1FF
        self.anchor = "waiting"          # waiting -> copying -> done
        self.anchor_pts = None           # exact PTS of the keyframe, from its PES header
        self.started: set[int] = set()   # audio PIDs already forwarding
        self.keeping: dict[int, bool] = {}   # per audio PID: is the current PES kept
        self.delta = 0                   # added to audio PTS/DTS of the current clip
        self.lo = self.hi = None         # clip in/out time (90 kHz, clip timeline)
        self.q: queue.Queue = queue.Queue(maxsize=4096)   # ~ tens of MB of audio
        self.thread = threading.Thread(target=self._writer, args=(path,), daemon=True)
        self.thread.start()

    def _writer(self, path: str):
        try:
            with open(path, "wb") as f:  # opening a FIFO blocks until ffmpeg opens it
                while (chunk := self.q.get()) is not None:
                    f.write(chunk)
        except BrokenPipeError:          # ffmpeg stopped reading (end, seek, stop)
            while self.q.get() is not None:
                pass

    def _pmt(self, p, pusi) -> bytes:
        """Reassemble the source PMT and re-emit it filtered, as one TS packet."""
        hdr = 5 + p[4] if (p[3] >> 4) & 2 else 4
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
        pkt = bytes([0x47, 0x40 | (PMT_PID >> 8), PMT_PID & 0xFF, 0x10 | self.pmt_cc, 0])
        self.pmt_cc = (self.pmt_cc + 1) & 0x0F
        return (pkt + section).ljust(188, b"\xff")

    def set_clip(self, delta: int, in_pts: int, out_pts: int):
        """Next play item: its audio keeps only [in, out) and is moved by `delta`
        onto the timeline of the first clip, since every clip restarts its PTS."""
        self.delta, self.lo, self.hi = delta, in_pts, out_pts

    @staticmethod
    def _shift_timestamps(p: bytearray, delta: int):
        """Add delta to PTS (and DTS) of the PES header starting in TS packet p."""
        hdr = 5 + p[4] if (p[3] >> 4) & 2 else 4
        fields = [hdr + 9] + ([hdr + 14] if p[hdr + 7] & 0x40 else [])
        for o in fields:
            ts = (_timestamp(p[o:o + 5]) + delta) % (1 << 33)
            p[o] = (p[o] & 0xF1) | ((ts >> 29) & 0x0E)
            p[o + 1] = (ts >> 22) & 0xFF
            p[o + 2] = ((ts >> 14) & 0xFE) | 1
            p[o + 3] = (ts >> 7) & 0xFF
            p[o + 4] = ((ts << 1) & 0xFE) | 1

    @staticmethod
    def _pes_pts(p) -> "int | None":
        """PTS in the PES header that starts in this TS packet, if any."""
        hdr = 5 + p[4] if (p[3] >> 4) & 2 else 4
        if p[hdr:hdr + 3] != b"\x00\x00\x01" or not p[hdr + 7] & 0x80:
            return None
        return _timestamp(p[hdr + 9:hdr + 14])

    def feed(self, unit: bytes):
        out = bytearray()
        for off in range(0, len(unit) - SOURCE_PACKET + 1, SOURCE_PACKET):
            p = unit[off + 4:off + SOURCE_PACKET]
            pid = ((p[1] & 0x1F) << 8) | p[2]
            pusi = p[1] & 0x40
            if pid == PMT_PID:
                out += self._pmt(p, pusi)
            elif pid == BASE_PID and self.anchor != "done":
                if pusi:
                    if self.anchor == "copying":
                        self.anchor = "done"
                        continue
                    pts = self._pes_pts(p)
                    if pts is not None and pts & ~0x1FF == self.start:
                        self.anchor = "copying"
                        self.anchor_pts = pts
                if self.anchor == "copying":
                    out += p
            elif pid in (PAT_PID,):
                out += p
            elif pid in self.pids:
                if pusi:
                    pts = self._pes_pts(p)
                    keep = pts is not None and self.anchor_pts is not None
                    if keep and pid not in self.started:
                        # Video travels ahead of its presentation time in a TS,
                        # audio almost on time: audio right after the keyframe in
                        # the stream still has earlier PTS. Start each track at
                        # its first PES presented at or after the keyframe.
                        keep = (pts - self.anchor_pts) % (1 << 33) < 1 << 32
                    if keep and self.lo is not None:
                        keep = self.lo <= pts < self.hi
                    self.keeping[pid] = keep
                    if keep:
                        self.started.add(pid)
                        if self.delta:
                            p = bytearray(p)
                            self._shift_timestamps(p, self.delta)
                if self.keeping.get(pid):
                    out += p
        if out:
            self.q.put(bytes(out))

    def close(self):
        self.q.put(None)
        self.thread.join()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("disc", help="/dev/sr0, an .iso or a folder containing BDMV/")
    ap.add_argument("--playlist", required=True, help="e.g. 00070")
    ap.add_argument("--start", type=float, default=0.0,
                    help="seconds from the start of the title (all its clips)")
    ap.add_argument("--audio-pid", action="append", default=[],
                    help="audio PID to forward (repeatable), e.g. 0x1102")
    ap.add_argument("--audio-out", help="file or FIFO for the audio TS")
    args = ap.parse_args()

    out = sys.stdout.buffer
    with Disc(args.disc) as disc:
        items = parse_mpls(disc.read_file(f"BDMV/PLAYLIST/{args.playlist}.mpls"))
        starts = [0.0]
        for it in items[:-1]:
            starts.append(starts[-1] + it.duration)
        first = max(i for i, t in enumerate(starts) if t <= max(0.0, args.start))

        video = VideoWriter(out)
        tap = None
        try:
            for i in range(first, len(items)):
                item = items[i]
                base = parse_clpi(disc.read_file(f"BDMV/CLIPINF/{item.clip}.clpi"))
                dep = parse_clpi(disc.read_file(f"BDMV/CLIPINF/{item.dep_clip}.clpi"),
                                 pid=0x1012)
                if i == first:
                    sp = ssif_seek(item, base, dep, args.start - starts[i])
                    offset, demux = sp.ssif_offset, SsifDemuxer(start_pts=sp.pts90)
                    print(f"disc_reader: clip {item.clip}, keyframe {starts[i] + sp.time:.3f}s,"
                          f" ssif offset {offset}", file=sys.stderr)
                    if args.audio_out:
                        tap = AudioTap(args.audio_out,
                                       {int(p, 0) for p in args.audio_pid}, sp.pts90)
                else:
                    # the next clip continues from its first byte; each clip
                    # restarts its timestamps, so its audio is moved onto the
                    # timeline of the first one (the video is just a sequence
                    # of frames, numbered by the encoder)
                    offset, demux = 0, SsifDemuxer()
                    print(f"disc_reader: clip {item.clip} from {starts[i]:.3f}s", file=sys.stderr)
                if tap:
                    ref = items[first]
                    delta = round((starts[i] - starts[first]) * 90000) \
                        - 2 * (item.in_time - ref.in_time)
                    tap.set_clip(delta, 2 * item.in_time, 2 * item.out_time)
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
            try:
                sys.stdout.close()
            except BrokenPipeError:
                pass


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
