"""
bdmv.py — the bits of the Blu-ray navigation files needed to play a 3D title.

  MPLS (BDMV/PLAYLIST/*.mpls)  which clips make the movie, with in/out times,
                               and (extension 2.2, sub path type 8) which clip
                               holds the MVC dependent view of each of them.
  CLPI (BDMV/CLIPINF/*.clpi)   EP_map: where each video entry point (keyframe)
                               starts, as source packet number (SPN);
                               extension 2.4: start SPN of every extent.

Layouts follow libbluray's bdnav/mpls_parse.c and clpi_parse.c. Times in the
navigation files are in 45 kHz units; PES timestamps are 90 kHz.

The .ssif of a 3D clip interleaves extents as D0 B0 D1 B1 ... (dependent before
base). The dependent frames of base extent k are in D_k, so a read that must
start from base packet s starts at the D_k whose B_k contains s.
"""
import bisect
from dataclasses import dataclass

SOURCE_PACKET = 192
AACS_UNIT_PACKETS = 32          # 6144 bytes = 32 source packets
SS_SUBPATH_TYPES = (8, 9)       # stereoscopic (MVC dependent view) sub paths


def _u16(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 2], "big")


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "big")


def _extensions(data: bytes, start: int) -> dict[tuple[int, int], int]:
    """(id1, id2) -> absolute offset of each block of an ExtensionData section."""
    if start == 0 or start + 12 > len(data):
        return {}
    count = data[start + 11]
    out = {}
    for i in range(count):
        e = start + 12 + i * 12
        out[(_u16(data, e), _u16(data, e + 2))] = start + _u32(data, e + 4)
    return out


AUDIO_CODECS = {0x80: "lpcm", 0x81: "ac3", 0x82: "dts", 0x83: "truehd", 0x84: "eac3",
                0x85: "dts-hd", 0x86: "dts-hd ma", 0xA1: "eac3", 0xA2: "dts-hd"}


@dataclass
class AudioStream:
    pid: int
    codec: str
    lang: str


def _parse_stn_audio(data: bytes, stn: int) -> list[AudioStream]:
    """Primary audio streams of a play item's STN table."""
    n_video, n_audio = data[stn + 4], data[stn + 5]
    q = stn + 16
    for _ in range(n_video):                     # skip video entries
        q += 1 + data[q]
        q += 1 + data[q]
    out = []
    for _ in range(n_audio):
        entry, q = q, q + 1 + data[q]
        attr, q = q, q + 1 + data[q]
        pid = _u16(data, entry + 2) if data[entry + 1] == 1 else _u16(data, entry + 3)
        coding = data[attr + 1]
        lang = data[attr + 3:attr + 6].decode("ascii", "replace")
        out.append(AudioStream(pid, AUDIO_CODECS.get(coding, hex(coding)), lang))
    return out


@dataclass
class PlayItem:
    clip: str            # base view clip id, e.g. "00131"
    in_time: int         # 45 kHz, clip timeline
    out_time: int
    dep_clip: str = ""   # clip with the MVC dependent view, e.g. "00132"
    audio: list = None   # AudioStream list from the STN table

    @property
    def duration(self) -> float:
        return (self.out_time - self.in_time) / 45000


def parse_mpls(data: bytes) -> list[PlayItem]:
    if data[:4] != b"MPLS":
        raise ValueError("not an MPLS file")
    pos = _u32(data, 8)
    count = _u16(data, pos + 6)
    items = []
    p = pos + 10
    for _ in range(count):
        length = _u16(data, p)
        stn = p + 34
        if data[p + 12] & 0x10:                  # multi angle: angle count, flags, extra clips
            stn += 2 + (data[p + 34] - 1) * 10
        items.append(PlayItem(clip=data[p + 2:p + 7].decode(),
                              in_time=_u32(data, p + 14), out_time=_u32(data, p + 18),
                              audio=_parse_stn_audio(data, stn)))
        p += 2 + length

    sub = _extensions(data, _u32(data, 16)).get((2, 2))
    if sub is not None:
        q = sub + 6                                  # length u32, count u16
        for _ in range(_u16(data, sub + 4)):
            length, sp_type, n_items = _u32(data, q), data[q + 5], data[q + 9]
            if sp_type in SS_SUBPATH_TYPES:
                r = q + 10
                for i in range(n_items):
                    if i < len(items):
                        items[i].dep_clip = data[r + 2:r + 7].decode()
                    r += 2 + _u16(data, r)
            q += 4 + length
    return items


@dataclass
class Clip:
    num_packets: int
    ep_pts: list[int]            # entry point times, 45 kHz (low 8 bits truncated)
    ep_spn: list[int]            # entry point source packet numbers
    extent_start: list[int]      # SPN where each extent begins (3D clips only)


def parse_clpi(data: bytes, pid: int = 0x1011) -> Clip:
    if data[:4] != b"HDMV":
        raise ValueError("not a CLPI file")
    num_packets = _u32(data, 56)

    ep_pts, ep_spn = [], []
    cpi = _u32(data, 16)
    if _u32(data, cpi):
        ep_map = cpi + 6
        for s in range(data[ep_map + 1]):
            e = ep_map + 2 + s * 12
            fields = int.from_bytes(data[e + 2:e + 12], "big")      # 80 bits after the PID
            n_coarse = (fields >> 50) & 0xFFFF
            n_fine = (fields >> 32) & 0x3FFFF
            start = ep_map + (fields & 0xFFFFFFFF)
            if _u16(data, e) != pid:
                continue
            fine_at = start + _u32(data, start)
            coarse = []
            for c in range(n_coarse):
                v = int.from_bytes(data[start + 4 + c * 8:start + 12 + c * 8], "big")
                coarse.append((v >> 46, (v >> 32) & 0x3FFF, v & 0xFFFFFFFF))
            for c, (ref, c_pts, c_spn) in enumerate(coarse):
                end = coarse[c + 1][0] if c + 1 < n_coarse else n_fine
                for f in range(ref, end):
                    v = _u32(data, fine_at + f * 4)
                    ep_pts.append(((c_pts & ~1) << 18) + (((v >> 17) & 0x7FF) << 8))
                    ep_spn.append((c_spn & ~0x1FFFF) + (v & 0x1FFFF))

    extent_start = []
    es = _extensions(data, _u32(data, 24)).get((2, 4))
    if es is not None:
        extent_start = [_u32(data, es + 8 + 4 * i) for i in range(_u32(data, es + 4))]
    return Clip(num_packets, ep_pts, ep_spn, extent_start)


@dataclass
class SeekPoint:
    time: float        # seconds from the start of the play item (keyframe time)
    pts90: int         # 90 kHz PTS of that keyframe (low 9 bits truncated)
    ssif_offset: int   # byte offset in the .ssif to start reading from (6144-aligned)


def ssif_seek(item: PlayItem, base: Clip, dep: Clip, seconds: float) -> SeekPoint:
    """Where to start reading the .ssif of `item` to decode from the keyframe <= seconds."""
    target = item.in_time + int(seconds * 45000)
    i = max(0, bisect.bisect_right(base.ep_pts, target) - 1)
    pts45, spn = base.ep_pts[i], base.ep_spn[i]
    k = max(0, bisect.bisect_right(base.extent_start, spn) - 1)
    packet = base.extent_start[k] + dep.extent_start[k]          # start of D_k in the .ssif
    offset = packet // AACS_UNIT_PACKETS * AACS_UNIT_PACKETS * SOURCE_PACKET
    return SeekPoint((pts45 - item.in_time) / 45000, pts45 * 2, offset)
