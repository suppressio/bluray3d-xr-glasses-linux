#!/usr/bin/env python3
"""
dvd_reader.py — a DVD-Video title from a given time, decrypted, as an MPEG program
stream on stdout for ffmpeg (-f mpeg): video plus the chosen audio streams.

Starts at the VOBU found by dvd.seek() (its time is exact), goes on cell after
cell. Audio starts at its first packet presented at or after the first video
frame, so both start together. Navigation, padding, subpicture and other audio
packs are dropped. If a cell restarts its timestamps (some discs do between
VOBs), its packets are moved back onto one timeline.

Usage:
    dvd_reader.py /dev/sr0 --title 1 --start 3000 --audio 0x81 | ffmpeg -f mpeg -i - ...
"""
import argparse
import os
import sys

from dvd import SECTOR, Dvd, parse_title, read_nav, seek, _titles

CHUNK = 64                                  # sectors per read


def _timestamp(b, o: int) -> int:
    return (((b[o] >> 1) & 7) << 30) | (b[o + 1] << 22) | ((b[o + 2] >> 1) << 15) \
        | (b[o + 3] << 7) | (b[o + 4] >> 1)


def _shift(pack: bytearray, pes: int, delta: int):
    """Add delta to the PTS (and DTS) of the PES header at `pes`."""
    fields = [pes + 9] + ([pes + 14] if pack[pes + 7] & 0x40 else [])
    for o in fields:
        ts = (_timestamp(pack, o) + delta) % (1 << 33)
        pack[o] = (pack[o] & 0xF1) | ((ts >> 29) & 0x0E)
        pack[o + 1] = (ts >> 22) & 0xFF
        pack[o + 2] = ((ts >> 14) & 0xFE) | 1
        pack[o + 3] = (ts >> 7) & 0xFF
        pack[o + 4] = ((ts << 1) & 0xFE) | 1


def empty_subpicture_pack(pack_header: bytes, stream: int, pts: int) -> bytes:
    """A 2048-byte pack holding a subpicture that shows nothing, at `pts`.

    ffmpeg creates a program-stream track only when a packet of it shows up while
    it probes the start: after a seek into a silent scene the subtitle track
    would not exist and could not be drawn. This announces it right away."""
    spu = bytes([0x00, 0x0A, 0x00, 0x04,          # SPU size 10, control at offset 4
                 0x00, 0x00, 0x00, 0x04,          # date 0, next control = this one
                 0x02, 0xFF])                     # STP_DSP (hide), end
    ts = bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1,
                (pts >> 7) & 0xFF, ((pts << 1) & 0xFE) | 1])
    body = bytes([0x81, 0x80, 0x05]) + ts + bytes([stream]) + spu
    pes = b"\x00\x00\x01\xbd" + len(body).to_bytes(2, "big") + body
    pad = SECTOR - len(pack_header) - len(pes) - 6
    return pack_header + pes + b"\x00\x00\x01\xbe" + pad.to_bytes(2, "big") + b"\xff" * pad


class PackFilter:
    def __init__(self, audio: set[int], anchor_pts: int, sub: int = None):
        self.audio, self.anchor, self.sub = audio, anchor_pts, sub
        self.started: set[int] = set()
        self.delta = 0

    def keep(self, pack: bytes):
        """The pack to write (possibly with shifted timestamps), or None to drop it."""
        if pack[:4] != b"\x00\x00\x01\xba":
            return None
        p = 14 + (pack[13] & 7)
        if pack[p:p + 4] == b"\x00\x00\x01\xbb":                  # system header
            p += 6 + int.from_bytes(pack[p + 4:p + 6], "big")
        if pack[p:p + 3] != b"\x00\x00\x01":
            return None
        sid = pack[p + 3]
        if sid == 0xE0:
            stream = 0x1E0
        elif sid == 0xBD:                           # private 1: AC-3/DTS/LPCM, subpictures
            stream = pack[p + 9 + pack[p + 8]]
            if stream == self.sub:
                return self._shifted(pack, p)
        elif 0xC0 <= sid <= 0xDF:                                  # MPEG audio
            stream = 0x100 | sid
        else:
            return None
        if stream != 0x1E0 and stream not in self.audio:
            return None
        has_pts = pack[p + 7] & 0x80
        if stream != 0x1E0 and stream not in self.started:
            # video travels ahead of its presentation time: audio right after
            # the first VOBU still belongs before it. Start at the first packet
            # presented at or after the first frame.
            if not has_pts or (_timestamp(pack, p + 9) + self.delta - self.anchor) % (1 << 33) >= 1 << 32:
                return None
            self.started.add(stream)
        return self._shifted(pack, p) if has_pts else pack

    def _shifted(self, pack, p):
        if self.delta and pack[p + 7] & 0x80:
            pack = bytearray(pack)
            _shift(pack, p, self.delta)
        return pack


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dvd", help="/dev/sr0, an .iso or a folder containing VIDEO_TS/")
    ap.add_argument("--title", type=int, required=True)
    ap.add_argument("--start", type=float, default=0.0, help="seconds from the title start")
    ap.add_argument("--audio", action="append", default=[], help="audio stream id, e.g. 0x81")
    ap.add_argument("--sub", help="subpicture stream id to pass on, e.g. 0x21")
    args = ap.parse_args()

    out = sys.stdout.buffer
    with Dvd(args.dvd) as dvd:
        _, vts, ttn = next(t for t in _titles(dvd.ifo(0)) if t[0] == args.title)
        title = parse_title(dvd.ifo(vts), args.title, vts, ttn)
        vobs = dvd.title_vobs(vts)
        nav = seek(vobs, title, args.start)
        print(f"dvd_reader: title {args.title}, VOBU at {nav.time:.3f}s (sector {nav.sector})",
              file=sys.stderr)
        sub = int(args.sub, 0) if args.sub else None
        filt = PackFilter({int(a, 0) for a in args.audio}, nav.pts, sub)
        announce = sub is not None
        first = next(i for i, c in enumerate(title.cells) if c.first <= nav.sector <= c.last)
        try:
            for i in range(first, len(title.cells)):
                cell = title.cells[i]
                sector = nav.sector if i == first else cell.first
                if i != first:
                    # timestamps should go on from where the previous cell left
                    # them; some discs restart them at a new VOB: move them back
                    cn = read_nav(vobs, title, cell.first)
                    if cn is not None:
                        expected = nav.pts + round((cn.time - nav.time) * 90000)
                        delta = (expected - cn.pts) % (1 << 33)
                        delta = delta - (1 << 33) if delta >= 1 << 32 else delta
                        filt.delta = delta if abs(delta) > 1800 else 0
                while sector <= cell.last:
                    count = min(CHUNK, cell.last - sector + 1)
                    data = vobs.read(sector, count)
                    if not data:
                        break
                    kept = bytearray()
                    for o in range(0, len(data), SECTOR):
                        pack = filt.keep(data[o:o + SECTOR])
                        if pack is not None:
                            if announce:
                                kept += empty_subpicture_pack(pack[:14 + (pack[13] & 7)],
                                                              sub, nav.pts)
                                announce = False
                            kept += pack
                    if kept:
                        out.write(kept)
                    sector += count
            out.flush()
        except BrokenPipeError:
            pass
        finally:
            vobs.close()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    # the consumer may be gone (seek, stop): keep Python quiet when it flushes stdout at exit
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
