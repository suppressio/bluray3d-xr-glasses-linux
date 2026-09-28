#!/usr/bin/env python3
"""
ssif_demux.py — 3D Blu-ray SSIF transport stream -> Annex B H.264 + MVC for edge264.

A 3D Blu-ray keeps the two views in separate streams: PID 0x1011 is the base
view (plain H.264, left eye on most discs) and PID 0x1012 the MVC dependent
view. The .ssif file interleaves them in extents of many frames each, a
dependent extent before the matching base extent.

edge264 wants each access unit whole: the base view NAL units followed by the
dependent view NAL units of the same frame. Every frame is one PES packet per
view, and the two PES packets of a frame share the same DTS. So PES payloads
are queued per view and emitted pairwise, in base-view order.

The dependent PES starts with a Blu-ray specific delimiter (NAL type 24).
It is dropped, as MakeMKV does, so edge264 gets the same stream it gets from
an MKV rip.

Input: decrypted BDAV transport stream (192-byte source packets), e.g. read
through libbluray's bd_open_file_dec(). Output: Annex B on stdout.

Usage:
    python3 ssif_demux.py 00131.ssif > movie.264
    <decrypted ssif bytes> | python3 ssif_demux.py - | edge264_test - -Ok | ...
"""
import sys
from collections import deque
from typing import BinaryIO, Iterator, Optional

BASE_PID = 0x1011
DEP_PID = 0x1012
SOURCE_PACKET = 192          # 4-byte TP_extra_header + 188-byte TS packet
NAL_BD_DELIMITER = 24
# End of sequence / end of stream / filler data. Blu-ray clips end with filler and
# an end-of-sequence NAL in both views; edge264 treats everything after an end of
# sequence as corrupt, so the next clip of the title would not decode. MakeMKV
# drops them too.
NAL_DROP = {10, 11, 12}


def _timestamp(b: bytes) -> int:
    """33-bit PTS/DTS from the 5 bytes of a PES header field."""
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def _pes_payload(pes: bytes) -> Optional[tuple[int, int, bytes]]:
    """(DTS or PTS when there is no DTS, PTS, elementary stream payload) of a complete PES."""
    if len(pes) < 9 or pes[:3] != b"\x00\x00\x01":
        return None
    flags, header_len = pes[7], pes[8]
    if not flags & 0x80:
        return None
    pts = _timestamp(pes[9:14])
    dts = _timestamp(pes[14:19]) if flags & 0x40 else pts
    return dts, pts, pes[9 + header_len:]


def _strip_nals(payload: bytes, drop: set[int]) -> bytes:
    """Remove NAL units of the given types (fast path: nothing to remove)."""
    if not any(b"\x00\x00\x01" + bytes([t]) in payload for t in drop):
        return payload
    starts = [i for i in range(len(payload) - 3)
              if payload[i] == 0 and payload[i + 1] == 0 and payload[i + 2] == 1]
    out = bytearray(payload[:starts[0]] if starts else payload)
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(payload)
        if payload[i + 3] & 0x1F not in drop:
            out += payload[i:end]
    return bytes(out)


def _drop_bd_delimiter(payload: bytes) -> bytes:
    """Remove a leading NAL unit of type 24 (Blu-ray MVC delimiter)."""
    start = payload.find(b"\x00\x00\x01")
    if start < 0 or payload[start + 3] & 0x1F != NAL_BD_DELIMITER:
        return payload
    nxt = payload.find(b"\x00\x00\x01", start + 3)
    if nxt < 0:
        return b""
    if nxt > 0 and payload[nxt - 1] == 0:  # keep the 4-byte start code of the next NAL
        nxt -= 1
    return payload[nxt:]


class SsifDemuxer:
    def __init__(self, start_pts: Optional[int] = None):
        """start_pts: 90 kHz PTS of the keyframe to start from after a jump into the
        stream (EP_map precision: low 9 bits ignored). Frames before it are dropped."""
        self._start = None if start_pts is None else start_pts & ~0x1FF
        self._pes = {BASE_PID: bytearray(), DEP_PID: bytearray()}
        self._started = {BASE_PID: False, DEP_PID: False}
        self._base: deque[tuple[int, int, bytes]] = deque()
        self._dep: dict[int, bytes] = {}
        self._tail = b""
        self.frames = 0
        self.dropped = 0   # base frames whose dependent view never came

    def _close_pes(self, pid: int):
        parsed = _pes_payload(bytes(self._pes[pid]))
        self._pes[pid] = bytearray()
        if parsed is None:
            return
        ts, pts, payload = parsed
        payload = _strip_nals(payload, NAL_DROP)
        if pid == BASE_PID:
            if self._start is not None:
                if pts & ~0x1FF != self._start:
                    return          # still before the keyframe we jumped to
                self._start = None
            self._base.append((ts, pts, payload))
        else:
            self._dep[ts] = _drop_bd_delimiter(payload)
            if len(self._dep) > 1024:  # orphans (e.g. right after a jump): forget the oldest
                del self._dep[min(self._dep)]

    def _emit_ready(self) -> Iterator[bytes]:
        while self._base:
            ts, _, payload = self._base[0]
            dep = self._dep.pop(ts, None)
            if dep is None:
                # the dependent view of this frame has not arrived yet; bound the
                # wait so a missing one cannot stall everything behind it. A base
                # frame alone is dropped: edge264 needs stereo pairs, and it only
                # happens after a jump into the stream or on damaged input.
                if len(self._base) < 256:
                    return
                self.dropped += 1
                self._base.popleft()
                continue
            self._base.popleft()
            self.frames += 1
            yield payload + dep

    def feed(self, data: bytes) -> Iterator[bytes]:
        buf = self._tail + data
        end = len(buf) - len(buf) % SOURCE_PACKET
        mv = memoryview(buf)
        for off in range(0, end, SOURCE_PACKET):
            p = mv[off + 4:off + SOURCE_PACKET]
            if p[0] != 0x47:
                continue
            pid = ((p[1] & 0x1F) << 8) | p[2]
            if pid != BASE_PID and pid != DEP_PID:
                continue
            afc = (p[3] >> 4) & 3
            if not afc & 1:
                continue
            i = 5 + p[4] if afc & 2 else 4
            if p[1] & 0x40:  # payload_unit_start: a new PES begins
                if self._started[pid]:
                    self._close_pes(pid)
                    yield from self._emit_ready()
                self._started[pid] = True
            if self._started[pid]:
                self._pes[pid] += p[i:]
        self._tail = bytes(buf[end:])

    def flush(self) -> Iterator[bytes]:
        for pid in (DEP_PID, BASE_PID):
            if self._started[pid]:
                self._close_pes(pid)
        yield from self._emit_ready()
        self.dropped += len(self._base)  # end of stream: whatever is left has no partner
        self._base.clear()


def demux(src: BinaryIO, dst: BinaryIO, chunk: int = SOURCE_PACKET * 4096) -> SsifDemuxer:
    d = SsifDemuxer()
    while True:
        data = src.read(chunk)
        if not data:
            break
        for au in d.feed(data):
            dst.write(au)
    for au in d.flush():
        dst.write(au)
    dst.flush()
    return d


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <file.ssif | ->  > out.264")
    src = sys.stdin.buffer if sys.argv[1] == "-" else open(sys.argv[1], "rb")
    try:
        d = demux(src, sys.stdout.buffer)
    except BrokenPipeError:
        sys.exit(0)
    print(f"ssif_demux: {d.frames} stereo frames, {d.dropped} base frames dropped (no dependent view)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
