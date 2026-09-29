🇬🇧 English | [🇮🇹 Italiano](ROADMAP.it.md)

# Roadmap

## Status

**Done: the 3D Blu-ray plays straight from the disc.** Insert the disc, the movie
appears in the share, it plays in 3D on the glasses with working seek and audio,
and it disappears on eject. It also works from ISO, BDMV folders and MKV rips.
The phases below record how it was built and verified, step by step, on Tron:
Legacy 3D.

Next steps, in this order:

- [x] **2D Blu-ray** and **DVD**, each in its own folder of the `Disks` share.
  FFmpeg's `dvdvideo` demuxer seeks only approximately (seconds off, without
  knowing where it landed), so DVDs have their own reader: libdvdread + IFO
  parsing, seek through the navigation packs (exact).
- [x] **Subtitles from the disc**, drawn into the picture like a disc player
  does (Blu-ray PGS, 2D and 3D, DVD subpictures): one version per subtitle
  language, forced subtitles always in. Reading them out as external files would
  mean reading the whole disc first, too slow. 3D ones sit at a fixed depth.
  Verified on all three kinds of disc.
- [x] **Faster jumps.** After a jump the player still sends reads for the
  position it left; on a disc (one pipeline at a time) they kept restarting the
  old position and stopping the new one. Now they are answered from the old
  data, and a jump takes 2-4 s. Optional loading animation (`--loader`) instead
  of a frozen picture while waiting.
- [x] **A default loading animation** free of rights issues: a retrowave scene
  made from scratch in Blender, side by side (3D on 3D movies, its left eye on
  2D ones), 1 MB.
- [x] **Hard DVDs.** UK series discs hide the episodes among dozens of fake
  titles that replay scrambled cells (a copy protection): those are recognized
  and each episode becomes a file. Damaged spots are skipped, titles opening
  with seconds without audio start, seeking works in titles that play a cell
  twice.
- [ ] **More discs.** Tested: Tron: Legacy 3D, Ready Player One, Cowboy Bebop,
  Back to the Future PAL, Utopia PAL; discs with BD+, several angles or NTSC
  DVDs may need work.
- [ ] **DLNA as an alternative to Samba.** The same virtual files served over
  HTTP and announced with DLNA/UPnP, for players that browse a media server
  instead of opening a share (TVs, Moon VR, Kodi...).
  An option at start: Samba, DLNA or both. It fits the design as is: the file
  has a constant bitrate, so an HTTP range request is the same as a read of the
  FUSE file, and seeking keeps working. With DLNA alone neither FUSE nor Samba
  is needed.
- [ ] **Windows and macOS.** The core (disc reading, demux, seek, pipeline)
  is already portable; the few Linux-specific parts go behind a small platform
  layer first, on Linux, with identical results in the tests. Then tests with a
  USB drive on a Mac and on a Windows PC.

Later, maybe:
- **watching together**, two people with their own glasses in sync: a shared
  live stream (RTSP/HLS via mediamtx) with shared pause/seek from a web remote.
  Two players reading the same file already works, but each has its own
  position. Limits: the VITURE 3D Player opens only SMB files, not network
  streams, and separate players stay ~1 s apart unless they support a sync
  protocol;
- a "passthrough" mode for 2D discs: serve the original stream, no re-encoding
  (full quality, but seeking depends more on the player);
- NVIDIA encoding inside Docker (the compose file exists, untested);
- VAAPI encoding for Intel and AMD GPUs (`h264_vaapi`, untested: no such GPU
  here). Without NVIDIA the CPU encodes (x264): on a recent 12-core desktop
  CPU the 3D encode alone runs at 1.4x real time on 2 cores, 3.5x on all 12;
- a `.deb` package for Debian/Ubuntu, built by GitHub Actions at every release
  and attached to it: `apt install ./bluray3d-xr_….deb` pulls the dependencies,
  `apt remove` also removes the Samba share. edge264 built for distribution
  (x86-64-v2/v3, CPU features picked at runtime), its BSD license included.

## Decryption

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

## Phases (how it was built)

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
- [x] **Phase 4 — `BlurayDiscSource`.** Disc drive, ISO and BDMV folder as
  sources (`src/sources.py`). **One virtual file per movie** (the main 3D
  playlist), named from the disc metadata. For a drive the file **appears when
  a disc is inserted and disappears when it is ejected**: a watcher asks the
  drive for a disc every 3 s without reading it. Decryption tries libaacs, then
  libmmbd. Titles made of several clips are played in sequence (Tron: 2); the
  audio timestamps of later clips are moved onto the first clip's timeline.
  What the real use taught:
  - Blu-ray clips end with filler and an end-of-sequence NAL; after the latter
    edge264 rejected the next clip. The demuxer now drops NAL 10/11/12, like
    MakeMKV.
  - The first keyframe can sit a few ms before the play item's in time; the
    movie start mapped to a negative time and nothing played.
  - Players read the end of the file (duration) while playing the beginning.
    With one pipeline per file this killed playback; with two, the optical drive
    seeked back and forth and both crawled. Now reads retry instead of returning
    nothing, there is one pipeline per disc, and the tail of the file is
    synthetic (black + silence with the right timestamps).
  - libbluray < 1.4 (Debian 13, Ubuntu 24.04) cannot open `.ssif`: it is rebuilt
    from the two `.m2ts` files, byte for byte identical.
  Result: watched from the disc on the VITURE Neckband, seeks OK (a few seconds,
  the drive), clip boundary seamless, eject/insert OK (file back 14 s after
  closing the tray).
- [~] **Phase 5 — Long run on a real drive.** Done: Tron: Legacy 3D, playback,
  seeks, eject/insert. To do: a whole movie in one go, more discs.
