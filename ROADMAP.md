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
- [ ] **Phase 2 — Seek.** Map a time to a position in the SSIF: EP_map in the
  CLPI (time → source packet) plus the SS extent layout, or a bisection on PTS.
  Measure the granularity. Keep the A/V lesson learned with MKV: video and audio
  must start from exactly the same keyframe time.
- [ ] **Phase 3 — Audio.** Take the selected audio PID from the same decrypted
  stream and feed it to the encoder. This means extending `Source.audio_input`
  beyond "a file ffmpeg can open" (e.g. a FIFO fed by the helper).
- [ ] **Phase 4 — `BlurayDiscSource`.** Disc device, ISO and BDMV folder as
  sources. Movie name from the disc metadata, playlists with several clips,
  2D/3D title detection.
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
