"""
Synthetic test data: MPEG-TS / PES packets, Blu-ray MPLS and CLPI files, DVD IFO
files and navigation packs, built from the same layouts the parsers read. No
real disc content is needed (nor shipped) to test them.
"""
from dataclasses import dataclass, field

TS = 188
SECTOR = 2048


def u16(v: int) -> bytes:
    return v.to_bytes(2, "big")


def u32(v: int) -> bytes:
    return v.to_bytes(4, "big")


# --- MPEG timestamps, PES, TS -------------------------------------------------

def ts_field(ts: int, prefix: int = 2) -> bytes:
    """A 33-bit PTS/DTS as the 5 bytes of a PES header (marker bits set)."""
    return bytes([(prefix << 4) | ((ts >> 29) & 0x0E) | 1, (ts >> 22) & 0xFF,
                  ((ts >> 14) & 0xFE) | 1, (ts >> 7) & 0xFF, ((ts << 1) & 0xFE) | 1])


def pes(payload: bytes, pts: int | None, dts: int | None = None, stream_id: int = 0xE0,
        substream: int | None = None) -> bytes:
    """A PES packet; `substream` goes first in the payload (private stream 1)."""
    header = b""
    flags = 0
    if pts is not None:
        flags = 0x80
        header = ts_field(pts, 3 if dts is not None else 2)
        if dts is not None:
            flags |= 0x40
            header += ts_field(dts, 1)
    body = bytes([0x81, flags, len(header)]) + header
    if substream is not None:
        body += bytes([substream])
    body += payload
    length = 0 if stream_id == 0xE0 and len(body) > 0xFFFF else len(body)
    return b"\x00\x00\x01" + bytes([stream_id]) + u16(length) + body


def ts_packets(pid: int, data: bytes, cc: int = 0, *, random_access: bool = False) -> list[bytes]:
    """`data` split in 188-byte TS packets, the first with payload_unit_start.
    The last one is filled with adaptation field stuffing, so the payload is exact."""
    out: list[bytes] = []
    first = True
    pos = 0
    while pos < len(data) or first:
        room = 184
        adaptation = b""
        if first and random_access:
            adaptation = bytes([1, 0x40])            # length 1: flags, random access
            room -= 2
        chunk = data[pos:pos + room]
        if len(chunk) < room:
            fill = room - len(chunk)
            if adaptation:
                adaptation = bytes([adaptation[0] + fill, 0x40]) + b"\xff" * fill
            elif fill == 1:
                adaptation = b"\x00"
            else:
                adaptation = bytes([fill - 1, 0]) + b"\xff" * (fill - 2)
        afc = 0x30 if adaptation else 0x10
        head = bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, afc | (cc & 0x0F)])
        packet = head + adaptation + chunk
        assert len(packet) == TS, len(packet)
        out.append(packet)
        pos += len(chunk)
        cc += 1
        first = False
    return out


def source_packets(packets: list[bytes]) -> bytes:
    """TS packets as BDAV source packets (4-byte TP_extra_header each)."""
    return b"".join(b"\x00\x00\x00\x00" + p for p in packets)


def crc32_mpeg2(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def pmt_section(streams: list[tuple[int, int]], pcr_pid: int = 0x1001) -> bytes:
    """PMT section (with CRC) announcing (stream_type, PID) pairs."""
    entries = b"".join(bytes([st, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, 0]) for st, pid in streams)
    body = bytes([0xC1, 0x00, 0x00, 0xE0 | (pcr_pid >> 8), pcr_pid & 0xFF, 0xF0, 0x00]) + entries
    length = 2 + len(body) + 4                                  # program number 1 + body + CRC
    section = bytes([0x02, 0xB0 | (length >> 8), length & 0xFF, 0x00, 0x01]) + body
    return section + u32(crc32_mpeg2(section))


def pmt_packet(section: bytes, pid: int = 0x100, cc: int = 0) -> bytes:
    return ts_packets(pid, b"\x00" + section, cc)[0]


# --- H.264 NAL units ---------------------------------------------------------

def nal(nal_type: int, body: bytes = b"\x11\x22", long_start: bool = True) -> bytes:
    return (b"\x00\x00\x00\x01" if long_start else b"\x00\x00\x01") + bytes([nal_type]) + body


# --- Blu-ray MPLS / CLPI -----------------------------------------------------

@dataclass
class Stream:
    pid: int
    lang: str = "eng"
    coding: int = 0x81           # audio: 0x81 ac3, 0x86 dts-hd ma...; PGS: 0x90


@dataclass
class Item:
    clip: str
    in_time: int                  # 45 kHz
    out_time: int
    audio: list[Stream] = field(default_factory=list[Stream])
    pgs: list[Stream] = field(default_factory=list[Stream])
    video_pid: int = 0x1011


def _stream_entry(pid: int) -> bytes:
    data = bytes([1]) + u16(pid) + b"\x00" * 6
    return bytes([len(data)]) + data


def _stn(item: Item) -> bytes:
    body = bytearray()
    body += _stream_entry(item.video_pid) + bytes([5, 0x1B, 0x61, 0, 0, 0])
    for a in item.audio:
        body += _stream_entry(a.pid)
        attr = bytes([a.coding, 0x31]) + a.lang.encode()
        body += bytes([len(attr)]) + attr
    for s in item.pgs:
        body += _stream_entry(s.pid)
        attr = bytes([s.coding]) + s.lang.encode()
        body += bytes([len(attr)]) + attr
    head = bytes([0, 0, 1, len(item.audio), len(item.pgs)]) + b"\x00" * 9
    return u16(len(head) + len(body)) + head + bytes(body)


def _play_item(item: Item) -> bytes:
    body = (item.clip.encode() + b"M2TS" + bytes([0, 0, 0]) + u32(item.in_time)
            + u32(item.out_time) + b"\x00" * 8 + bytes([0, 0]) + u16(0) + _stn(item))
    return u16(len(body)) + body


def _extension_data(blocks: dict[tuple[int, int], bytes]) -> bytes:
    """ExtensionData: length, start address, count, then (id1, id2, start, length)."""
    table_len = 12 + 12 * len(blocks)
    entries, payload = b"", b""
    for (id1, id2), data in blocks.items():
        entries += u16(id1) + u16(id2) + u32(table_len + len(payload)) + u32(len(data))
        payload += data
    head = u32(0) + u32(0) + b"\x00\x00\x00" + bytes([len(blocks)])
    return head + entries + payload


def mpls(items: list[Item], dep_clips: list[str] | None = None, subpath_type: int = 8) -> bytes:
    playlist = b"".join(_play_item(i) for i in items)
    playlist = u32(0) + u16(0) + u16(len(items)) + u16(0) + playlist
    data = bytearray(b"MPLS0200" + u32(0) * 3 + b"\x00" * 24)
    pos = len(data)
    data[8:12] = u32(pos)
    data += playlist
    if dep_clips:
        sp_items = b"".join(u16(9) + c.encode() + b"SSIF" for c in dep_clips)
        subpath = (b"\x00" + bytes([subpath_type]) + b"\x00\x00\x00" + bytes([len(dep_clips)])
                   + sp_items)
        subpath = u32(len(subpath)) + subpath
        block = u32(0) + u16(1) + subpath
        data[16:20] = u32(len(data))
        data += _extension_data({(2, 2): block})
    return bytes(data)


def clpi(num_packets: int, eps: list[tuple[int, int]], extents: list[int] | None = None,
         pid: int = 0x1011, other_pid: int | None = None) -> bytes:
    """CLPI with an EP_map for `pid` from (pts45, spn) entry points (pts45 a
    multiple of 256), and optionally the extent start points (extension 2.4)."""
    coarse: list[tuple[int, int, int]] = []           # (first fine index, c_pts, c_spn)
    fine: list[int] = []
    for i, (pts45, spn) in enumerate(eps):
        assert pts45 % 256 == 0
        c_pts, c_spn = (pts45 >> 18) & ~1, spn & ~0x1FFFF
        if not coarse or (coarse[-1][1], coarse[-1][2]) != (c_pts, c_spn):
            coarse.append((i, c_pts, c_spn))
        f_pts = (pts45 - (c_pts << 18)) >> 8
        fine.append((f_pts << 17) | (spn & 0x1FFFF))

    def table(n_coarse_list: list[tuple[int, int, int]], fine_list: list[int]) -> bytes:
        coarse_bytes = b"".join(((r << 46) | ((cp & 0x3FFF) << 32) | cs).to_bytes(8, "big")
                                for r, cp, cs in n_coarse_list)
        return u32(4 + len(coarse_bytes)) + coarse_bytes + b"".join(u32(f) for f in fine_list)

    streams = [(pid, coarse, fine)]
    if other_pid is not None:                     # an entry for another stream, first
        streams.insert(0, (other_pid, [(0, 0, 0)], [0]))
    ep_map = bytearray(bytes([0, len(streams)]) + b"\x00" * 12 * len(streams))
    for s, (spid, c, f) in enumerate(streams):
        start = len(ep_map)
        fields = (len(c) << 50) | (len(f) << 32) | start
        ep_map[2 + s * 12:14 + s * 12] = u16(spid) + fields.to_bytes(10, "big")
        ep_map += table(c, f)
    cpi = u32(len(ep_map) + 2) + b"\x00\x01" + bytes(ep_map)

    data = bytearray(b"HDMV0200" + b"\x00" * 52)
    data[56:60] = u32(num_packets)
    data += b"\x00" * 4
    data[16:20] = u32(len(data))
    data += cpi
    if extents is not None:
        block = u32(0) + u32(len(extents)) + b"".join(u32(e) for e in extents)
        data[24:28] = u32(len(data))
        data += _extension_data({(2, 4): block})
    return bytes(data)


# --- DVD IFO / navigation packs ---------------------------------------------

def bcd(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


def dvd_time(seconds: float, rate_code: int = 1) -> bytes:
    """hh:mm:ss:ff in BCD, frame rate code 1 = 25 fps, 3 = 29.97."""
    fps = 25 if rate_code == 1 else 30
    total = int(seconds)
    frames = round((seconds - total) * fps)
    return bytes([bcd(total // 3600), bcd(total // 60 % 60), bcd(total % 60),
                  (rate_code << 6) | bcd(frames)])


@dataclass
class DvdCell:
    duration: float
    first: int
    last: int
    vob_id: int = 1
    cell_id: int = 1
    angle: tuple[int, int] = (0, 0)       # (block mode, block type)


@dataclass
class DvdAudioAttr:
    fmt: int = 0                  # 0 AC-3, 6 DTS, 4 LPCM
    lang: str = "en"
    ext: int = 1                  # 3/4 = commentary
    number: int = 0               # stream number in the PGC control word


@dataclass
class DvdSubAttr:
    lang: str = "en"
    kind: int = 1                 # 1 normal, 9 forced, 13 commentary
    wide: int = 0                 # stream number for 16:9
    four3: int = 0                # stream number for 4:3


def vts_ifo(cells: list[DvdCell], audio: list[DvdAudioAttr], subs: list[DvdSubAttr],
            pal: bool = True, wide: bool = True, tmap_unit: int = 0, tmap: list[int] | None = None,
            vobus: list[int] | None = None, palette: list[tuple[int, int, int]] | None = None,
            pgcn: int = 1) -> bytes:
    """A VTS IFO with one title (ttn 1) whose first chapter is PGC `pgcn`."""
    v = bytearray(SECTOR * 6)
    v[0:12] = b"DVDVIDEO-VTS"
    v[0xC8:0xCC] = u32(1)                       # PTT_SRPT in sector 1
    v[0xCC:0xD0] = u32(2)                       # PGCI in sector 2
    v[0xD4:0xD8] = u32(4 if tmap is not None else 0)
    v[0xE4:0xE8] = u32(5)                       # VOBU ADMAP in sector 5
    video = ((1 if pal else 0) << 12) | ((3 if wide else 0) << 10)
    v[0x200:0x202] = u16(video)
    v[0x202:0x204] = u16(len(audio))
    for i, a in enumerate(audio):
        o = 0x204 + i * 8
        v[o] = a.fmt << 5
        v[o + 2:o + 4] = a.lang.encode()
        v[o + 5] = a.ext
    v[0x254:0x256] = u16(len(subs))
    for i, s in enumerate(subs):
        o = 0x256 + i * 6
        v[o + 2:o + 4] = s.lang.encode()
        v[o + 5] = s.kind

    ptt = SECTOR
    v[ptt:ptt + 2] = u16(1)
    v[ptt + 8:ptt + 12] = u32(12)                # chapters of title 1 at ptt+12
    v[ptt + 12:ptt + 14] = u16(pgcn)

    pgci = 2 * SECTOR
    v[pgci:pgci + 2] = u16(pgcn)
    pgc_rel = 8 + pgcn * 8
    for n in range(pgcn):                         # every PGC entry points to the same PGC
        v[pgci + 8 + n * 8 + 4:pgci + 8 + n * 8 + 8] = u32(pgc_rel)
    pgc = pgci + pgc_rel
    v[pgc + 3] = len(cells)
    v[pgc + 4:pgc + 8] = dvd_time(sum(c.duration for c in cells))
    for i, a in enumerate(audio):
        v[pgc + 0x0C + i * 2:pgc + 0x0E + i * 2] = u16(0x8000 | (a.number << 8))
    for i, s in enumerate(subs):
        v[pgc + 0x1C + i * 4:pgc + 0x20 + i * 4] = u32((1 << 31) | (s.four3 << 24) | (s.wide << 16))
    for i, (y, cr, cb) in enumerate(palette or [(16, 128, 128)] * 16):
        v[pgc + 0xA4 + i * 4:pgc + 0xA8 + i * 4] = bytes([0, y, cr, cb])
    playback, position = 0xEC, 0xEC + 24 * len(cells)
    v[pgc + 0xE8:pgc + 0xEA] = u16(playback)
    v[pgc + 0xEA:pgc + 0xEC] = u16(position)
    for c, cell in enumerate(cells):
        e = pgc + playback + c * 24
        v[e] = (cell.angle[0] << 6) | (cell.angle[1] << 4)
        v[e + 4:e + 8] = dvd_time(cell.duration)
        v[e + 8:e + 12] = u32(cell.first)
        v[e + 20:e + 24] = u32(cell.last)
        p = pgc + position + c * 4
        v[p:p + 2] = u16(cell.vob_id)
        v[p + 3] = cell.cell_id

    if tmap is not None:
        t = 4 * SECTOR
        v[t:t + 2] = u16(pgcn)
        for n in range(pgcn):
            v[t + 8 + n * 4:t + 12 + n * 4] = u32(8 + pgcn * 4)
        m = t + 8 + pgcn * 4
        v[m] = tmap_unit
        v[m + 2:m + 4] = u16(len(tmap))
        for i, s in enumerate(tmap):
            v[m + 4 + i * 4:m + 8 + i * 4] = u32(s)
    admap = 5 * SECTOR
    vobus = vobus or []
    v[admap:admap + 4] = u32(4 + 4 * len(vobus) - 1)
    for i, s in enumerate(vobus):
        v[admap + 4 + i * 4:admap + 8 + i * 4] = u32(s)
    return bytes(v)


def vmg_ifo(titles: list[tuple[int, int]]) -> bytes:
    """VIDEO_TS.IFO listing titles as (VTS, title number inside the VTS)."""
    v = bytearray(SECTOR * 2)
    v[0:12] = b"DVDVIDEO-VMG"
    v[0xC4:0xC8] = u32(1)
    tt = SECTOR
    v[tt:tt + 2] = u16(len(titles))
    for i, (vts, ttn) in enumerate(titles):
        v[tt + 8 + i * 12 + 6] = vts
        v[tt + 8 + i * 12 + 7] = ttn
    return bytes(v)


def pack_header(scr: int = 0) -> bytes:
    """MPEG-2 program stream pack header, no stuffing (14 bytes)."""
    return b"\x00\x00\x01\xba" + bytes([0x44, 0, 4, 0, 4, 1, 0x01, 0x89, 0xC3, 0xF8])


def nav_pack(vob_id: int, cell_id: int, cell_time: float, pts: int) -> bytes:
    """A navigation pack (PCI + DSI) for read_nav()."""
    s = bytearray(pack_header() + b"\x00" * (SECTOR - 14))
    s[0x26:0x2A] = b"\x00\x00\x01\xbf"
    s[0x2C] = 0
    s[0x2D + 0x0C:0x2D + 0x10] = u32(pts)
    s[0x400:0x404] = b"\x00\x00\x01\xbf"
    s[0x406] = 1
    dsi = 0x407
    s[dsi + 0x18:dsi + 0x1A] = u16(vob_id)
    s[dsi + 0x1B] = cell_id
    s[dsi + 0x1C:dsi + 0x20] = dvd_time(cell_time)
    return bytes(s)


def ps_pack(stream_id: int, pts: int | None, payload: bytes = b"\x00" * 16,
            substream: int | None = None) -> bytes:
    """A 2048-byte program stream pack with one PES."""
    p = pack_header() + pes(payload, pts, stream_id=stream_id, substream=substream)
    return p + b"\xff" * (SECTOR - len(p))
