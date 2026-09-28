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

from bdmv import parse_clpi, parse_mpls, ssif_seek
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
        self.pids = audio_pids | {PAT_PID}
        self.keep = audio_pids | {BASE_PID}
        self.pmt = bytearray()           # PMT section being reassembled
        self.pmt_cc = 0
        self.start = start_pts & ~0x1FF
        self.anchor = "waiting"          # waiting -> copying -> done
        self.anchor_pts = None           # exact PTS of the keyframe, from its PES header
        self.started: set[int] = set()   # audio PIDs already forwarding
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
            elif pid in self.pids:
                if pid not in (PAT_PID, PMT_PID) and pid not in self.started:
                    # Video travels ahead of its presentation time in a TS, audio
                    # almost on time: audio right after the keyframe in the stream
                    # still has earlier PTS. Start each track at its first PES
                    # presented at or after the keyframe.
                    if not pusi or self.anchor_pts is None:
                        continue
                    pts = self._pes_pts(p)
                    if pts is None or (pts - self.anchor_pts) % (1 << 33) >= 1 << 32:
                        continue
                    self.started.add(pid)
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
    ap.add_argument("--start", type=float, default=0.0, help="seconds from the title start")
    ap.add_argument("--audio-pid", action="append", default=[],
                    help="audio PID to forward (repeatable), e.g. 0x1102")
    ap.add_argument("--audio-out", help="file or FIFO for the audio TS")
    args = ap.parse_args()

    out = sys.stdout.buffer
    with Disc(args.disc) as disc:
        items = parse_mpls(disc.read_file(f"BDMV/PLAYLIST/{args.playlist}.mpls"))
        item = items[0]        # TODO phase 4: titles made of several play items
        base = parse_clpi(disc.read_file(f"BDMV/CLIPINF/{item.clip}.clpi"))
        dep = parse_clpi(disc.read_file(f"BDMV/CLIPINF/{item.dep_clip}.clpi"), pid=0x1012)
        sp = ssif_seek(item, base, dep, args.start)
        print(f"disc_reader: keyframe {sp.time:.3f}s, ssif offset {sp.ssif_offset}",
              file=sys.stderr)

        demux = SsifDemuxer(start_pts=sp.pts90)
        video = VideoWriter(out)
        tap = None
        if args.audio_out:
            tap = AudioTap(args.audio_out, {int(p, 0) for p in args.audio_pid}, sp.pts90)
        try:
            with disc.open(f"BDMV/STREAM/SSIF/{item.clip}.ssif") as f:
                f.seek(sp.ssif_offset)
                while (unit := f.read_unit()) and not video.broken:
                    if tap:
                        tap.feed(unit)
                    for au in demux.feed(unit):
                        video.write(au)
                for au in demux.flush():
                    video.write(au)
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
