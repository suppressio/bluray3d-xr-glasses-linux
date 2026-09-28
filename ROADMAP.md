🇬🇧 English | [🇮🇹 Italiano](ROADMAP.it.md)

# Roadmap: from the disc, with no rip

Today the project starts from an **MKV rip** that kept the 3D (MVC). That is useful
if you already have such rips and do not want to convert them, but it is one step
more than needed: whoever has the disc and accepts converting can already rip
straight to SBS with existing tools.

The real goal is to **read the 3D Blu-ray itself** (disc in the drive, ISO or BDMV
folder), decrypt and decode it on the fly, and serve it to the glasses exactly
as today: no rip, no conversion, no disk space.

## What is already in place

- **The pipeline does not care where the movie comes from.** `src/sources.py`
  defines a `Source`: an Annex B H.264 stream with the MVC dependent view, the
  audio, the duration, and the keyframe to start from when seeking. `MkvSource`
  implements it for rips; a `BlurayDiscSource` only has to implement the same
  four things. Decoder, encoder, virtual file and SMB share stay as they are.
- **Available on Debian today**: libbluray (with `bd_open_file_dec`,
  `bd_get_clpi`, `bd_get_playlist_info`), libudfread, libaacs, libbdplus.

## The open problem: decryption

Commercial discs use AACS (and some BD+). libbluray decrypts through a plugin
chosen at runtime, so the project does not have to depend on any single tool.
Three backends, tried in this order (the project **never ships or downloads keys**):

1. **libaacs + KEYDB.cfg** — fully open source (VideoLAN). The user provides a key
   database in `~/.config/aacs/` and keeps it updated (newer discs revoke old
   keys; some drives also need a host certificate). Used when a KEYDB is found.
2. **libmmbd** (from MakeMKV) — drop-in replacement for libaacs/libbdplus that
   uses MakeMKV's keys (`LIBAACS_PATH=libmmbd LIBBDPLUS_PATH=libmmbd`). Opens
   almost everything, BD+ included, but it is proprietary: it runs `makemkvcon`
   internally, so MakeMKV must be installed, working, and registered (free beta
   key, renewed periodically).
3. **Already-decrypted ISO / BDMV folder** — no keys at runtime (the disc was
   decrypted once, with any tool). A full copy (~45 GB), so an intermediate
   step again, but no dependency while watching.

Legal note: in many countries (Italy and most of the EU included) circumventing
copy protection is not covered by the private-copy exception, whatever the tool.

Phase 0 result on the test PC (Tron: Legacy 3D, 2026-09-28) — **all three backends work**:

| Backend | AACS | Decrypted SSIF | Notes |
|---|---|---|---|
| libaacs + KEYDB.cfg | handled (MKB v19) | ✅ 64 MB in ~7 s incl. spin-up | fully open source |
| libmmbd (MakeMKV 2.0.0) | handled | ✅ ~15 s (starts `makemkvcon`) | same payload as libaacs; only the copy-permission bits of each packet header differ |
| decrypted BDMV folder | not present | ✅ byte-identical to the disc | no key library loaded at all |

Findings: the disc is 3D, AACS only (no BD+). The main playlist 00070 is made of clips
00131 + 00133, and the base view is the left eye. The decrypted SSIF carries PID
`0x1011` (base, H.264 High 1920×1080), PID `0x1012` (MVC dependent), audio and PGS.
The decrypting reader only accepts reads of exactly one AACS unit (6144 bytes).
MakeMKV built for FFmpeg 7 does not start on FFmpeg 8 systems (rebuild
makemkv-oss). The beta key goes in `~/.MakeMKV/settings.conf`
(`app_Key = "T-..."`): `makemkvcon reg` rejects it.

## Phases

Ordered from the cheapest to the most expensive, so that we can stop early.

- [x] **Phase 0 — Access**, once per decryption backend (libaacs, libmmbd,
  decrypted BDMV). With the disc in the drive: libbluray opens it, lists the
  playlists and finds the main 3D one. The SSIF of its clip is readable
  decrypted through `bd_open_file_dec`, and ffprobe on those bytes shows both
  `0x1011` and `0x1012`. _Go/no-go: the disc decrypts with at least one backend._
- [x] **Phase 1 — Demux.** `src/ssif_demux.py` turns the SSIF transport stream
  into Annex B with base and dependent NAL units of each access unit in decode
  order, the same thing edge264 gets from the MKV today. How it works: every
  frame is one PES per view and both share the same DTS, so the frames are
  paired on DTS and emitted in base-view order. The Blu-ray delimiter NAL
  (type 24) is dropped, as MakeMKV does. Result on Tron: Legacy 3D (disc
  decrypted with libaacs): **3730 of 3730 decoded SBS frames bit-identical** to
  the MKV path (framemd5 of edge264's output, first 2.6 minutes). Demux speed
  ~390 MB/s in Python, versus the ~6 MB/s needed.
- [x] **Phase 2 — Seek.** `src/bdmv.py` reads the playlist (MPLS, with the
  3D sub path naming the dependent clips) and the clip info (CLPI: EP_map and
  extent start points). `ssif_seek()` maps a time to the base-view entry point
  (keyframe), then to the extent holding it. Reading starts at the dependent
  extent right before it, because the .ssif interleaves D0 B0 D1 B1 ... and the
  demuxer drops frames before the keyframe. `src/bluray.py` is a small ctypes
  binding to libbluray (open, decrypted read, seek by 6144-byte units).
  Result on Tron: Legacy 3D at 5, 30 and 50 minutes: same keyframe as the MKV
  path (within 3 ms, the EP_map resolution), **all decoded SBS frames
  identical** to the MKV path started there. Seek + 48 MB read from the drive:
  2-3.5 s. Entry points every 0.9 s on average (max 2 s).
  Note for phase 3: align audio on the real PTS of the first frame, not on the
  EP_map time (truncated to 5.7 ms).
- [x] **Phase 3 — Audio.** `src/disc_reader.py` reads the disc once and splits
  the stream. Annex B video goes to stdout for edge264. The chosen audio track
  goes, as a small MPEG-TS, to a file or FIFO for ffmpeg. Each has its own
  thread and queue: ffmpeg opens its inputs one at a time and does not read the
  video while it analyses the audio, and with a shared, blocking reader the
  pipeline stalled. Audio languages come from the playlist STN table (Tron:
  `0x1102` = ita DTS). Three details: (1) the .ssif carries two PMTs on the
  same PID (base and dependent clip), so only the base one is forwarded,
  filtered to the forwarded streams (new CRC); ffmpeg otherwise waits for
  streams that never come. (2) Video travels ahead of its presentation time,
  so audio starts at the first PES with PTS ≥ the keyframe's. (3) The keyframe
  itself is forwarded in the audio TS as a timing anchor (never mapped), so
  ffmpeg's start time for that input is exactly the video's. Result at 50 min
  vs the MKV path: 719/719 video frames identical, audio offset 3.9 ms.
- [ ] **Phase 4 — `BlurayDiscSource`.** Disc drive, ISO and BDMV folder as
  sources. **One virtual file per movie** (the main 3D playlist, not the whole
  disc), named from the disc metadata. For a drive the file **appears when a
  disc is inserted and disappears when it is ejected**, so the file system has
  to watch the drives. A single generic file whose content changes with the
  disc would confuse players and SMB clients, which remember resume position,
  duration and size by file name. Playlists made of several clips (Tron: 2),
  2D/3D title detection. External subtitles: a user-provided `.srt` in a folder
  named after the movie shows up next to it. Extracting them from the disc
  means reading the whole disc, so that is left for later (progressive
  extraction while watching, cached).
- [ ] **Phase 5 — Long run on a real drive.** A whole movie plus seeks back and
  forth. Check the drive throughput: the SSIF is ~1.3× the 1× BD speed, well
  within any drive, but spin-up and seek latency of an optical drive are real.
  Test on several discs, not just one.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Decryption keys (AACS revocations, BD+) | new discs stop opening even if the code is right | rely on libmmbd/MakeMKV or a user-maintained KEYDB; never ship keys |
| Demux bugs (wrong base/dependent interleaving) | wrong 3D with no visible error | Phase 1 validates against the MKV path frame by frame |
| Seek granularity / latency on a real drive | worse experience than with the MKV | measure in Phase 2 and 5 before claiming an improvement |
| Unusual disc structures (seamless branching, several clips) | some titles fail | Phase 4, several discs in testing |
| Effort | time spent without a result | phases ordered to stop early |

## Stop if

- the discs that matter do not decrypt with libmmbd / libaacs;
- Phase 1 turns out disproportionate for the benefit;
- the MKV path is enough for real use.
