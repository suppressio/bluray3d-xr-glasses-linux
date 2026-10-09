# Blu-ray MPLS Playlist Structure

## Q1: What fields does a Blu-ray MPLS file contain? Is there a "playlist type" or "application type" field that distinguishes main movie / episode / play-all / menu?

### Takeaway
There is no `playlist_type` or `application_type` field in the MPLS AppInfoPlayList block that classifies content as "main movie", "episode", or "play-all". The only mode flag is `playback_type` (1=sequential, 2=random, 3=shuffle), which describes playback order, not content semantics.

### Cited Findings
- MPLS file header layout: 4-byte ASCII magic "MPLS", 4-byte version (usually "0200"), followed by 32-bit big-endian offsets to the PlayList section, PlayListMark section, and ExtensionData section (zero offset means absent). All multi-byte integers are big-endian. — [Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls)
- The AppInfoPlayList block length field at offset $28–$2B is almost always 0x0000000E (14 bytes). — [Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls)
- AppInfoPlayList fields at documented offsets: `playback_type` (1 byte at $2D: 1=Standard/Sequential, 2=Random, 3=Shuffle), `playback_count` (2 bytes at $2E–$2F, used only in random/shuffle mode), `UO_mask_table` (8 bytes at $30–$37, user-operation restrictions, internal layout not publicly documented), `miscellaneous_flags` (1 byte at $38: bit 7 = random_access_flag, bit 6 = audio_mix_flag, bit 5 = lossless_bypass_flag). — [Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls); confirmed by [libbluray mpls_data.h via centos mirror](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/libbluray/bdnav/mpls_data.h)
- The libbluray struct for AppInfoPlayList is named `MPLS_AI` and adds a `mvc_base_view_r_flag` bit (MVC stereo base-view right flag) that occupies what the Wikibooks table marks as reserved bit 4; adding this bit reduced the trailing reserved skip from 13 bits to 12. — [LAV libbluray commit "Export the MPLS MVC_Base_View_R_flag"](https://gitea.1f0.de/LAV/libbluray/commit/768d9c6add9b7e42651e31be055539c4ecdd8ba2)
- PlayListMark section: each 14-byte mark entry contains a reserved byte, a `mark_type` (1=Entry Mark / chapter, 2=Link Point), a `PlayItem ID` (2 bytes), a `time_stamp` (4 bytes in 45000ths of a second), an `entry_ESPID` (2 bytes, usually 0xFFFF), and a `duration` (4 bytes, 0 = no fixed duration). — [Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls)
- There is no field in MPLS that identifies content as "main movie", "episode", or "play-all". The MPLS format is not officially published; parsers depend on third-party specs including the lw/BluRay repository and the Wikibooks bdinfo/mpls page. — [docs.rs mpls crate](https://docs.rs/mpls/latest/mpls)

### Inferences
- Content-type classification (main movie vs. episode vs. bonus) is inferred heuristically by applications from duration, PlayItem count, clip names, and the index.bdmv title table — not from a spec-defined enum in the MPLS file itself.

### Gaps
- The UO_mask_table internal bit layout is not publicly documented in any source found; the Wikibooks page leaves it as "Description pending."
- The complete `MPLS_AI` struct from the current upstream libbluray `mpls_data.h` could not be retrieved (git.stg.centos.org and code.videolan.org both returned 404 or access denied).

---

## Q2: What is the BDMV AppInfoPlayList structure? Does it carry a playlist_type or similar enum?

### Takeaway
The `AppInfoPlayList` (struct `MPLS_AI` in libbluray) carries only operational flags — playback mode, UO restrictions, audio mixing permissions, and one 3D flag — with no enum that semantically classifies playlist content.

### Cited Findings
- `MPLS_AI` fields confirmed in libbluray source: `random_access_flag` (1 bit), `audio_mix_flag` (1 bit), `lossless_bypass_flag` (1 bit), `mvc_base_view_r_flag` (1 bit). These are the only named flags beyond the playback_type byte and UO mask. — [LAV libbluray commit](https://gitea.1f0.de/LAV/libbluray/commit/768d9c6add9b7e42651e31be055539c4ecdd8ba2)
- `BLURAY_TITLE_INFO` (the public libbluray API struct) exposes: `idx` (title index), `playlist` (playlist number), `duration`, `clip_count`, `angle_count`, `chapter_count`, `mark_count`, `clips`, `chapters`, `marks`, `mvc_base_view_r_flag`, `sdr_conversion_notification_flag`. No `playlist_type` or `content_type` field is present. — [libbluray bluray.h doxygen source](https://videolan.videolan.me/libbluray/bluray_8h_source.html)
- The `BLURAY_TITLE` struct (referenced from `BLURAY_DISC_INFO`) has a `bdj` flag (1 if BD-J Java title) and a `hidden` flag (1 if not to be shown in UI). These are the only per-title classification flags exposed publicly. — [BLURAY_DISC_INFO doxygen](https://videolan.videolan.me/libbluray/structBLURAY__DISC__INFO.html)
- The CLPI (`ClipInfo`) file has a `clip_stream_type` byte and an `application_type` byte in the ClipInfo block. The `application_type` values 1–7 distinguish main-path transport streams from sub-path streams (for text subtitles, menus, slide shows, etc.). These are clip-level attributes, not playlist-level. — [Web search result citing libbluray clpi_dump.c](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/devtools/clpi_dump.c)

### Inferences
- The fact that `application_type` is at the CLPI (clip) level, not MPLS (playlist) level, means a single playlist can reference clips of different types (e.g., a main-path video clip plus a sub-path text-subtitle clip).

### Gaps
- The exact numeric values and string labels for CLPI `application_type` (the 1–7 table) could not be retrieved because the clpi_dump.c source returned 404 on every attempt. The values are known to exist from search result snippets but could not be verified.

---

## Q3: Are playlist numbers (00xxx vs 01xxx vs 0xxxx) assigned by convention, or is there a spec? Do higher playlist numbers mean alternate audio/subtitle configs?

### Takeaway
Playlist numbers are assigned by disc authors with no spec-mandated scheme. Common patterns exist (Disney uses 00800+ for language variants) but are studio conventions, not part of the BDMV specification.

### Cited Findings
- MPLS files are named with a five-digit zero-padded number and stored in `BDMV/PLAYLIST/`. There is no specification that mandates which number carries the main feature. — [fileinfo.com .MPLS extension](https://fileinfo.com/extension/mpls)
- 00001.mpls is often the first candidate to check but is not always the main movie. On the Angry Birds 2 disc, 00001 was shorter (1:28:43) than the other candidate playlists at 1:36:47, making it the wrong choice. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=80956)
- A practical heuristic is to open `index.bdmv` in a player and note which playlist it opens; "99% of the time, this is the correct playlist." — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=80956) (community claim, not spec)
- Disney discs use 00800+ playlists for language variants. On a Rogue One disc, 00800 is the English version, 00801 is Spanish, 00802 is French. Each variant shares most clips but swaps in a small set of localized clip IDs. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=55134); [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=55723)
- Lionsgate discs are sometimes described as using playlist obfuscation with many near-identical playlists. Suggested candidates to check are 00800, 00801, and 00001. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=142684) (community observation)
- The true playlist-to-title mapping is encoded in `MovieObject.bdmv`, which contains playback commands such as "play playlist number XYZ". `index.bdmv` maps title numbers to movie objects. — [MKVToolNix forum, mbunkus reply](https://help.mkvtoolnix.download/t/segment-map-for-tv-series/1513)
- MakeMKV will sometimes report "Title 00801.mpls is equal to title 00800.mpls and was skipped", indicating it detects the language-variant duplicates via the TITLES_FILTER_DUP_CLIP logic. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=138401)

### Inferences
- The 00800 series being used for language variants by Disney may reflect an internal authoring convention adopted across Disney/Marvel/Pixar discs rather than a widely-followed industry standard.

### Gaps
- No spec or BDA guidance document was found that assigns meaning to playlist number ranges. The only authoritative source for a disc's playlist assignments is `MovieObject.bdmv` / `index.bdmv`.

---

## Q4: How does a "play all" playlist typically differ structurally from a single-episode playlist (number of PlayItems, clip names, duration)?

### Takeaway
A "play-all" playlist chains multiple episode clips as consecutive PlayItems, while a single-episode playlist typically has one PlayItem referencing one M2TS clip. The structural difference is PlayItem count and total duration.

### Cited Findings
- A "PLAY ALL" playlist (.mpls) on a TV series disc produces a single title with all episodes playing sequentially; chapters for every episode should be embedded inside it. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123032)
- Single-episode playlists on a typical TV-series disc have a segment map of one clip ("2" means episode 2's clip = 00002.m2ts). A play-all playlist's segment map lists all episode clips in order ("1,2,3,4,5" for five episodes). This is how tools like MakeMKV display the distinction. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123405)
- Clip numbers in the segment map don't always run sequentially by episode order. A "PLAY ALL" playlist helps reveal the correct episode order, since the per-episode playlists alone may not make the order obvious. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123405)
- On a Westworld Season 1 disc, playlist 105 came before 107, which came before 103 — confirming that MPLS numbers do not correspond to episode order. — [MKVToolNix forum](https://help.mkvtoolnix.download/t/segment-map-for-tv-series/1513)
- Some discs place all episodes into one clip with chapter markers between them (no separate per-episode playlists). Others rely on Java code for play-all behaviour, which tools like MakeMKV cannot see directly. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123405)
- Blu-ray playlists allow cross-referencing clips without duplicating data (unlike DVD), so a play-all playlist reuses the same M2TS files already referenced by the per-episode playlists. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=144217)

### Inferences
- Because there is no content-type field in MPLS, the only reliable way to distinguish a play-all playlist from a single-episode playlist programmatically is: (a) compare PlayItem count, (b) compare total duration against known episode length, or (c) cross-reference with `MovieObject.bdmv` to see how `index.bdmv` labels the title.

### Gaps
- No source confirmed the exact MPLS PlayItem structure (fields: clip filename, in_time, out_time, still_mode) from the spec text; this is known from libbluray source code but the raw source could not be retrieved in this session.

---

## Q5: Does the CLPI (clip info) file have any flag that identifies content type?

### Takeaway
CLPI has a `clip_stream_type` byte and an `application_type` byte in its ClipInfo block. The `application_type` values (1–7) distinguish main-path movie clips from sub-path streams (menus, text subtitles, etc.), but these are per-clip flags, not per-playlist.

### Cited Findings
- The libbluray `CLPI_CLIP_INFO` struct holds both `clip_stream_type` and `application_type` as 8-bit unsigned integers. The `clpi_dump.c` tool prints both, looking up `application_type` in a named table. — [libbluray clpi_data.h (centos mirror)](https://git.stg.centos.org/source-git/libbluray/blob/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/clpi_data.h)
- `application_type` values 1–7 label clips as main-path or sub-path transport streams, covering movie main path, slide show, menu, and text subtitles among others. Value 1 is the main transport stream for a movie's main path; value 6 is a sub transport stream for text subtitles. — [web search result summarizing clpi_dump.c](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/devtools/clpi_dump.c)
- The ClipInfo parser in `clpi_parse.c` reads `clip_stream_type` and `application_type` as consecutive 8-bit values, followed by 31 reserved bits and an ATC delta flag. — [libbluray clpi_data.h via centos mirror (search result snippet)](https://git.stg.centos.org/source-git/libbluray/blob/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/clpi_data.h)
- CLPI `program_info` holds the number of program sequences, each sequence's starting address, and the PIDs of transport packets. Each program stream entry has: `pid`, `coding_type`, `format`, `rate`, `aspect`, `oc_flag`, `char_code`, `lang`. — [libbluray clpi_data.h (centos mirror)](https://git.stg.centos.org/source-git/libbluray/blob/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/clpi_data.h)

### Inferences
- A clip with `application_type` = 1 (main path, movie) is the only CLPI-level indicator that a clip is "main content". But this still doesn't distinguish whether that clip is a movie, a TV episode, or a bonus feature — all would use `application_type` = 1.

### Gaps
- The exact integer values for each `application_type` label (the full lookup table from `clpi_dump.c`) could not be directly verified because the source file returned 404 repeatedly. Only values 1 (main path movie) and 6 (sub path text subtitle) were mentioned in search snippets.

---

## Q6: Are there public sources for the Blu-ray spec or well-documented reverse-engineering references?

### Takeaway
The BDA spec is not publicly available. The best public sources are the Wikibooks User:Bdinfo/mpls page, the libbluray source code (VideoLAN GitLab), and the lw/BluRay repository referenced by Rust/Python MPLS parsers.

### Cited Findings
- The official Blu-ray Disc Association (BDA) specification (BD-ROM Part 3) is not publicly published; access requires BDA membership. — [docs.rs mpls crate](https://docs.rs/mpls/latest/mpls)
- The primary reverse-engineering reference is the Wikibooks page at [https://en.wikibooks.org/wiki/User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls), which covers the MPLS header and AppInfoPlayList byte offsets.
- libbluray (the main open-source implementation) is hosted at [code.videolan.org/videolan/libbluray](https://code.videolan.org/videolan/libbluray). Relevant source files: `src/libbluray/bdnav/mpls_parse.c`, `src/libbluray/bdnav/mpls_data.h`, `src/libbluray/bdnav/navigation.c`, `src/libbluray/bdnav/clpi_data.h`, `src/libbluray/bdnav/clpi_parse.c`, `src/libbluray/bluray.h`. Public API doxygen docs are at [videolan.videolan.me/libbluray/](https://videolan.videolan.me/libbluray/bluray_8h_source.html).
- The Rust `mpls` crate (docs.rs/mpls) and Python `pyparsebluray` (pypi.org/project/pyparsebluray) both cite the "lw/BluRay MPLS wiki" as a primary source; this is a separate GitHub repository (not found directly in this session). — [docs.rs mpls](https://docs.rs/mpls/latest/mpls); [pypi pyparsebluray](https://pypi.org/project/pyparsebluray/0.1.2)
- BDedit is a GUI tool that can open and display `index.bdmv`, `MovieObject.bdmv`, `.mpls`, `.clpi`, and `.bdjo` files, useful for inspecting real disc structures. — [Scribd BDedit doc](https://www.scribd.com/document/952826306/BDedit)
- VideoHelp forum and Doom9 forum (doom9.org) are community references, but the Doom9 forum was unreachable in this session (connection refused).

### Gaps
- The lw/BluRay GitHub repository URL was not confirmed (several parsers reference it but no direct URL was retrieved).
- The Doom9 wiki and forum were unreachable during this research session.

---

## Q7: What does libbluray's TITLES_RELEVANT flag do exactly? What criteria does it use to filter titles?

### Takeaway
`TITLES_RELEVANT` (= 0x03) is a bitwise OR of `TITLES_FILTER_DUP_TITLE` (0x01) and `TITLES_FILTER_DUP_CLIP` (0x02). It instructs `bd_get_titles()` to remove playlists that are structural duplicates of others and playlists with excessively repeated clip segments. The filtering happens in `navigation.c`, not in the MPLS parser itself.

### Cited Findings
- Macro definitions in `bluray.h`: `TITLES_ALL = 0`, `TITLES_FILTER_DUP_TITLE = 0x01`, `TITLES_FILTER_DUP_CLIP = 0x02`, `TITLES_RELEVANT = 0x03`. — [libbluray bluray.h doxygen source](https://videolan.videolan.me/libbluray/bluray_8h_source.html)
- `_filter_dup` in `navigation.c` checks whether a candidate playlist is a structural duplicate of any playlist already in the accepted list. The baseline check compares `list_count` (number of PlayItems) and per-item `in_time` / `out_time`. — [libbluray navigation.c centos mirror](https://git.stg.centos.org/source-git/libbluray/raw/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/navigation.c)
- A 2014 Handbrake patch proposed extending `_filter_dup` to also compare the stream table: counts of video, audio, PG, IG, secondary audio, and secondary video streams, then per-stream attributes (stream_type, coding_type, pid, subpath_id, subclip_id, format, rate, char_code, lang). A new `_stream_cmp` helper implements the per-stream comparison. — [libbluray-devel mailing list 2014-May](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001457.html)
- `_find_repeats` counts how many PlayItems in a playlist reference the same clip with identical in_time and out_time as a given item. `_filter_repeats` rejects a playlist when any item's repeat count exceeds the configured threshold. — [libbluray navigation.c via search result snippet](https://git.stg.centos.org/source-git/libbluray/raw/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/navigation.c)
- The repeat-filter logic was copied from `mpls_dump.c` in a 2010 commit titled "filter out titles with repeating clips." — [web search result citing libbluray commit history](https://code.videolan.org/videolan/libbluray/-/blob/7cd4a904ee2589586e6dc3d57944a3377e093310/src/libbdnav/navigation.c)
- mpv's `stream_bluray.c` calls `bd_get_titles(bd, TITLES_RELEVANT, angle)` when enumerating available titles. — [mpv stream_bluray.c (tjdev mirror)](https://git.tjdev.de/mirror/mpv/src/commit/408b7eecee2283a6e373cff03bb3156bd721632f/stream/stream_bluray.c)
- The intent of the combined filter is to surface the "useful" titles: real content playlists, free of the many near-duplicate obfuscation playlists some studios (e.g. Lionsgate) plant on discs. — [web search result summarizing libbluray-devel filter-dup discussion](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)

### Inferences
- TITLES_RELEVANT primarily removes two categories: (1) alternate-language/configuration clones that differ only in which clips are swapped in (caught by `_filter_dup`), and (2) looping or menu-style playlists that repeat the same short segment many times (caught by `_filter_repeats`). This means it can incorrectly filter out a legitimate playlist if its clip structure happens to match another playlist's.

### Gaps
- Whether the 2014 Handbrake patch was eventually merged into upstream libbluray was not confirmed. The current `navigation.c` from upstream (code.videolan.org) could not be retrieved due to access restrictions.
- The exact repeat threshold value used by `_filter_repeats` was not found in any retrievable source.

---

# libbluray and MakeMKV Title Selection

## What does libbluray's TITLES_RELEVANT flag do?

### Takeaway
`TITLES_RELEVANT` combines two bitmask flags (`TITLES_FILTER_DUP_TITLE | TITLES_FILTER_DUP_CLIP`) and is passed to `bd_get_titles()` to prune playlists that share structure with others already accepted; the third argument, `min_title_length`, provides a separate duration floor in seconds.

### Cited Findings
- Flag definitions from `src/libbluray/bluray.h`: `TITLES_ALL = 0`, `TITLES_FILTER_DUP_TITLE = 0x01` (remove duplicate titles), `TITLES_FILTER_DUP_CLIP = 0x02` (remove titles that have duplicate clips), `TITLES_RELEVANT = (TITLES_FILTER_DUP_TITLE | TITLES_FILTER_DUP_CLIP)` — [VideoLAN doxygen (redirected)](https://videolan.videolan.me/libbluray/bluray_8h_source.html)
- `bd_get_titles` signature: `BD_PUBLIC uint32_t bd_get_titles(BLURAY *bd, uint8_t flags, uint32_t min_title_length);` — the third argument is a time floor below which titles are excluded — [VideoLAN doxygen](https://videolan.videolan.me/libbluray/bluray_8h_source.html)
- FFmpeg's `libavformat/bluray.c` calls `bd_get_titles(bd->bd, TITLES_RELEVANT, MIN_PLAYLIST_LENGTH)` where `MIN_PLAYLIST_LENGTH` is defined as 180 ("3 min"), then picks the playlist with the longest `info->duration` — [HuggingFace/camenduru FFmpeg mirror](https://huggingface.co/camenduru/ffmpeg-cuda/blob/main/libavformat/bluray.c)
- A 2010 VLC commit used `TITLES_RELEVANT` plus a longest-duration loop to select the main title — [vlc-commits mailing list](https://mailman.videolan.org/pipermail/vlc-commits/2010-October/003757.html)
- Internal filtering in `navigation.c` uses `_filter_dup()` to compare playlists by list_count, clip filename, and in/out times. A 2014 HandBrake-proposed patch also added stream-level comparison (stream type, coding type, PID, language, stream counts) per play item — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)
- `_filter_repeats` / `_find_repeats` in `navigation.c` drops titles that reuse the same clip with identical in/out times across multiple play items; comment in code says "Ignore titles with repeated segments" — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html); [CentOS git mirror (navigation.c)](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/libbluray/bdnav/navigation.h)
- `nav_get_title_list` in `navigation.c` reads the `BDMV/PLAYLIST` directory and builds the title list in filesystem order — [CentOS git mirror](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/libbluray/bdnav/navigation.h)
- The title list index returned by `bd_get_titles` is order-dependent: "the playlist is processed in the order that they are read from the PLAYLIST directory" — [HandBrake PR #5985 discussion](https://github.com/HandBrake/HandBrake/pull/5985)

### Inferences
- `TITLES_FILTER_DUP_CLIP` is the key flag for detecting "play-all" playlists: a play-all that references each episode clip once also makes those clips appear in their individual episode playlists, so those episode clips are "duplicate clips". However the direction of filtering is not certain from available sources: it is unclear whether the play-all or the episode playlist is dropped first.
- The `_filter_repeats` path specifically targets a playlist that reuses the same clip internally (i.e., a clip appears twice in one playlist), which is a different pattern from the play-all case.
- The 2014 stream-level patch status (merged or not) is unknown from these sources; behavior may differ between libbluray versions.

### Gaps
- The body of `_filter_dup` and `_filter_repeats` in the current upstream libbluray (as opposed to the 2014 patched version) could not be retrieved because code.videolan.org returned an Anubis bot-block, and no readable mirror of the current source was found.
- Whether the 2014 stream-comparison patch was merged upstream or remains only in downstream forks (Nixpkgs, LAV) is unknown.


## Does libbluray expose any "main title" selection beyond duration filtering?

### Takeaway
No dedicated "main title" API exists; callers that want the main feature must iterate the filtered list from `bd_get_titles()` and apply their own heuristic (typically longest duration).

### Cited Findings
- `bd_get_titles()` returns a count; callers use `bd_get_title_info(bd, i, 0)` in a loop to read each title's metadata (playlist number, duration, clip count, chapter marks). There is no single function that returns "the main title" — [VideoLAN doxygen](https://videolan.videolan.me/libbluray/bluray_8h.html)
- FFmpeg's `bluray.c` picks the playlist with the longest `info->duration` from the filtered set when no playlist is pre-selected — [HuggingFace/camenduru FFmpeg mirror](https://huggingface.co/camenduru/ffmpeg-cuda/blob/main/libavformat/bluray.c)
- mpv stores the count from `TITLES_RELEVANT` and exposes it as the "titles" property; it does not expose a "main" title index — [mpv git mirror](https://git.quad4.io/Mirrors/mpv/commit/c9a740fccd1128e43542beeec6f50bc59c2bddb9)
- `bd_get_disc_info()` returns disc-level metadata including a `first_play_supported` field and `top_menu_supported`, but these do not directly identify the main feature playlist — [VideoLAN doxygen](https://videolan.videolan.me/libbluray/bluray_8h.html)

### Inferences
- The conventional pattern (FFmpeg, VLC, HandBrake) is: call `bd_get_titles(TITLES_RELEVANT, min_len)`, then pick longest duration from the result.
- For TV series discs, this convention fails because multiple episode playlists of equal or similar length exist; callers must present all filtered titles to the user.

### Gaps
- Whether `bd_get_disc_info()` returns a "top-level playlist" identifier that could serve as a main-feature hint is not confirmed from available sources.


## How does MakeMKV select which titles to show as "main" entries?

### Takeaway
MakeMKV is closed source (the binary portion is proprietary); its title selection uses Java-based "fake playlist" detection for obfuscated discs and a segment-map identity check for duplicates. Forum evidence shows it logs "title N is the same as title M, and was therefore skipped" for dropped entries.

### Cited Findings
- MakeMKV's log emits a message of the form "title 6 is the same as title 7, and was therefore skipped" when it drops a playlist — [MakeMKV forum, Feature Request – Show Duplicate Titles](https://forum.makemkv.com/forum/viewtopic.php?p=52406)
- On discs with Java playlist obfuscation, MakeMKV uses a Java Runtime Environment to detect fake playlists and tags the identified main feature with "(FPL_MainFeature)" in the title list — [MakeMKV forum, detect fake playlists](https://forum.makemkv.com/forum/viewtopic.php?p=95219)
- Version 1.16.4 (2021) brought "much better support for discs with Java playlist obfuscation" — [VideoHelp MakeMKV version history](https://videohelp.com/software/MakeMKV/version-history)
- For byte-identical .mpls files, the MakeMKV log reports they are identical, and one is silently dropped; a user workaround is to delete the chosen .mpls from a decrypted backup so MakeMKV falls through to the alternate — [MakeMKV forum, Grand Budapest Hotel thread](https://forum.makemkv.com/forum/viewtopic.php?p=76586)
- On multi-angle discs (e.g., Grand Budapest Hotel), MakeMKV lists the same playlist once per angle; a second playlist with a different number but similar content is skipped as "equal" — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=78098)
- Users describe a duplicate using identical segment maps (same .m2ts references); the "segment map" visible in the MakeMKV UI is the list of .m2ts clips a playlist references — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=77981)

### Inferences
- MakeMKV's non-Java duplicate check appears to compare segment identity (which .m2ts clips appear in the playlist and in what order), similar to libbluray's `_filter_dup` clip-filename comparison.
- The FPL_MainFeature path for Java discs is a separate, more sophisticated analysis that runs only when Java is available.

### Gaps
- MakeMKV's binary portion is proprietary; the exact algorithm (whether it compares by duration, clip list, stream metadata, or all three) is not publicly documented.
- Whether MakeMKV uses `min_title_length` filtering before showing the title list is not confirmed.
- Version 1.17.x (2023–2024) release notes do not mention changes to title selection logic — [VideoHelp MakeMKV version history](https://videohelp.com/software/MakeMKV/version-history)


## On a TV series Blu-ray: does MakeMKV show all playlists or just primary ones?

### Takeaway
MakeMKV shows all playlists that pass its duplicate filter; episodes appear as individual titles alongside any play-all playlist. Alternate-language playlists (e.g., 00800 vs. 00801) may or may not be filtered depending on whether MakeMKV considers them identical.

### Cited Findings
- Episodes on a TV series disc appear as separate titles identified by duration and chapter count. There is no automatic labelling of episodes; user must match durations — [MakeMKV forum, How to tell which title is which episode](https://forum.makemkv.com/forum/viewtopic.php?p=7382)
- Users report that episodes generally run in either .mpls or .m2ts filename order, which helps with same-duration episodes — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=122607)
- The "play-all" title is identified by having a large chapter count and large file size; users manually exclude it when ripping individual episodes — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=122116)
- When a disc uses "fake Playlist" protection, MakeMKV may only identify episodes 1 & 2 on a 4-episode disc or show 300 identical titles — [MakeMKV forum, Mr. Selfridge S4 thread](https://forum.makemkv.com/forum/viewtopic.php?p=60109)
- On movie discs, alternate-language playlists (00800 English, 00801 Spanish/French or Japanese) differ only in opening title cards and credits; MakeMKV sometimes shows both — [MakeMKV forum Redbox thread](https://forum.makemkv.com/forum/viewtopic.php?p=68872); [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=68915)
- Disney and Fox discs follow a pattern where 00800.mpls is the English version and others are alternates; the exact filtering behavior depends on stream metadata differences — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=68914)
- A forum user observed that MakeMKV filtered episodes 3–5 on one disc as "the same as in title 1", incorrectly collapsing them — [MakeMKV forum, Feature Request – Show Duplicate Titles](https://forum.makemkv.com/forum/viewtopic.php?p=52373)

### Inferences
- The 01xxx vs. 00xxx alternate playlist pattern for TV series was not found in the searched sources; the 00800/00801 pattern is confirmed only for movie discs.
- MakeMKV's duplicate filter can produce false positives on TV series, collapsing distinct episodes if their clip structure happens to match.

### Gaps
- No source confirms how MakeMKV specifically handles the 01xxx MPLS range for TV series discs vs. 00xxx primary playlists.
- Whether MakeMKV uses clip count (number of PlayItems) as a signal to detect play-all vs. episode is not documented in any found source.


## Does libbluray use PlayItem (clip) count to detect play-all playlists?

### Takeaway
`TITLES_FILTER_DUP_CLIP` and `_filter_repeats` together address the play-all problem indirectly: the former drops titles that share clips with another title already accepted, the latter drops titles that reuse the same clip internally; neither uses raw clip count as a direct discriminator.

### Cited Findings
- `_filter_repeats` looks for play items in the same playlist that reference the same clip with the same in/out times and removes such titles — comment: "Ignore titles with repeated segments" — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)
- `BLURAY_TITLE_INFO` struct includes a clip count alongside duration and chapter data — [VideoLAN doxygen](https://videolan.videolan.me/libbluray/bluray_8h.html)
- The HandBrake PR discussion (2024) notes that `TITLES_FILTER_DUP_CLIP` removes titles with duplicate clips without dependency on which title was seen first — [HandBrake PR #5985](https://github.com/HandBrake/HandBrake/pull/5985)
- `bd_list_titles` man page (Debian 12) documents `min_title_length` as the duration floor but lists no "clip count minimum" parameter — [DOKK manpages](https://dokk.org/manpages/debian/12/libbluray-bin/bd_list_titles.1.en)

### Inferences
- A play-all playlist for a 4-episode disc would reference each of the 4 episode clips exactly once. Those same clips also appear in each individual episode playlist. `TITLES_FILTER_DUP_CLIP` would remove the play-all (or the individual episodes) depending on filesystem order.
- The `_filter_repeats` path is specifically for disc-level gimmicks (e.g., a single large playlist with all episodes interleaved that references certain clips twice).
- Neither mechanism labels a playlist as "play-all"; the effect is structural rather than semantic.

### Gaps
- Exact behavior of `TITLES_FILTER_DUP_CLIP` when a play-all and individual episode playlists are both present (which is dropped: play-all or episodes?) could not be confirmed without reading the current `navigation.c` body.


## What is libbluray/MakeMKV's "duplicate" title detection algorithm?

### Takeaway
libbluray's `_filter_dup()` does a structural comparison of MPLS play-item lists (clip filename + in/out times); a 2014 patch proposed adding stream-attribute comparison per play item to reduce false positives. MakeMKV uses a separate (closed-source) identity check based on segment maps.

### Cited Findings
- The baseline `_filter_dup` in `navigation.c` compares: `pl->list_count` (number of play items), and for each play item: clip filename, `in_time`, `out_time`. If all match, the playlist is a duplicate — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)
- The 2014 HandBrake-proposed patch to `_filter_dup` added: per-play-item stream type counts (`num_video`, `num_audio`, `num_pg`, `num_ig`, `num_secondary_audio`, `num_secondary_video`) and per-stream field comparison (`stream_type`, `coding_type`, `pid`, `subpath_id`, `subclip_id`, `format`, `rate`, `char_code`, `lang[4]`) — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)
- The patch was described as carried in Nixpkgs ("Make it smarter about filtering duplicates") — [alioth.systems nixpkgs mirror](https://git.alioth.systems/mirrors/nixpkgs/commit/66216ea6db71c59505b1144432233f6fbb1d1561)
- MakeMKV deduplicates by segment-map identity; users and the MakeMKV log message use "same" to mean the .m2ts clip sequence is identical — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=52406); [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=77981)

### Inferences
- Before the 2014 patch, a playlist with the same clips but a different audio language track (different PIDs or language codes) would pass through `_filter_dup` as different — consistent with alternate-language playlists being preserved.
- After the 2014 patch, those alternate-language playlists would be treated as duplicates and dropped — which could explain MakeMKV over-filtering on some discs.

### Gaps
- Whether the 2014 stream-comparison patch was ever merged into the official libbluray repository (not just Nixpkgs) is unknown.
- Whether libbluray's current upstream `_filter_dup` uses baseline or patched comparison is not confirmed.


## Does HandBrake use libbluray, or a separate MPLS parser?

### Takeaway
HandBrake uses libbluray directly through its `libhb/bd.c` wrapper; there is no separate MPLS parser. The wrapper calls `bd_get_titles()` with `TITLES_FILTER_DUP_CLIP` (always) plus `TITLES_FILTER_DUP_TITLE` (unless the user enables "keep duplicate titles"), then sorts results by MPLS number.

### Cited Findings
- HandBrake's `libhb/bd.c` (the Blu-ray handler, confirmed file exists in the repo) includes `libbluray/bluray.h` and calls `bd_open()`, `bd_get_titles()`, `bd_get_title_info()`, `bd_select_playlist()`, and `bd_select_angle()` — [HandBrake bd.c, fetched via GitHub API from HandBrake/HandBrake master]
- The flags used: `uint8_t flags = TITLES_FILTER_DUP_CLIP; if (!keep_duplicate_titles) { flags |= TITLES_FILTER_DUP_TITLE; }` — `min_title_length` is 0 (no duration floor) — [HandBrake bd.c, fetched via GitHub API]
- After scanning, `qsort(d->title_info, d->title_count, sizeof(BLURAY_TITLE_INFO*), title_info_compare_mpls)` sorts titles by MPLS number — [HandBrake bd.c, fetched via GitHub API]
- HandBrake PR #5985 (merged July 29, 2024, milestone 1.9.0) added `--keep-duplicate-titles` CLI flag and corresponding GUI preferences for Mac, Linux, and Windows to expose the `TITLES_FILTER_DUP_TITLE` toggle — [HandBrake PR #5985](https://github.com/HandBrake/HandBrake/pull/5985)
- `hb_bd_init()` accepts a `keep_duplicate_titles` integer parameter stored in `hb_bd_t` — [HandBrake bd.c, fetched via GitHub API]
- When libbluray cannot open the disc (`bd == NULL`), HandBrake logs "bd: not a bd - trying as a stream/file instead" and falls back to DVD/stream handling — [HandBrake bd.c, fetched via GitHub API]; confirmed by user forum reports [HandBrake forum](https://forum.handbrake.fr/viewtopic.php?p=108684)

### Inferences
- HandBrake never picks a "main title" automatically; it scans all filtered titles, sorts by MPLS number, and presents the list to the user. The user or CLI caller selects the title.
- Setting `min_title_length=0` means HandBrake shows even very short playlists (trailers, menus) if they pass the duplicate filter.
- The MPLS sort means title 0 in HandBrake corresponds to the lowest-numbered .mpls file, not the longest or the first encountered.

### Gaps
- The `title_info_compare_mpls` sort comparator function body (ascending or descending MPLS order) was not retrieved.
- HandBrake's behavior when libbluray returns 0 titles but a `BDMV/index.bdmv` exists (e.g., encrypted disc) was not investigated.

---

# Blu-ray Tools and Edge Cases: Consumer/Prosumer Playlist Handling

## VLC + libbluray: Title Presentation

### Takeaway
VLC relies entirely on libbluray for title enumeration. libbluray passes all non-filtered titles to VLC; VLC itself does not add any additional filtering layer. The effective set shown depends on what libbluray's `bd_get_titles()` returns after applying its built-in duplicate and length filters. Without BD-J (Java) support, menus are dropped and playback falls back to the main title as detected by libbluray.

### Cited Findings
- VLC's Blu-ray title log reports counts split by type, e.g. `HDMV Titles: 19, BD-J Titles: 0, Other: 0`; these are diagnostic, not a user-configurable filter — [VideoLAN forum thread](https://forum.videolan.org/viewtopic.php?p=484741)
- When BD-J menus are unsupported, libbluray logs `BD-J menus not supported` and then `Playing without menus`; a disc with `BD-J Titles: 79` showed many titles skipped due to missing Java support — [Ubuntu 18.04 VLC/Blu-ray forum thread](https://forum.videolan.org/viewtopic.php?p=493255)
- VLC title numbers are used by community members as a reference: e.g., "the title VLC plays is #26", and they cross-check against MakeMKV title listings — [MakeMKV forum: playlist obfuscation thread](https://forum.makemkv.com/forum/viewtopic.php?p=139239)
- For menu support, libbluray also requires BD-J jar files; without them, only HDMV-based discs get menus — [MakeMKV forum: BD-J install thread](https://forum.makemkv.com/forum/viewtopic.php?p=186594)
- Disc-specific decryption keys (VUK from AACS keydb) are also required for commercial discs; libbluray alone does not decrypt — general libbluray documentation

### Inferences
- VLC effectively exposes whatever libbluray's duplicate-filter and minimum-length filter pass through; for a disc with 300+ fake playlists, VLC would show a large title list unless libbluray's duplicate filter removes them.
- Without BD-J (the typical Linux situation), VLC falls back to the libbluray main-title selection heuristic, which may not match disc menus that rely on Java for "Play All" logic.

### Gaps
- No official VLC documentation found that specifies a maximum or minimum title count shown to the user.
- VLC's own filtering layer (if any) between libbluray's title list and the UI title list was not confirmed in public source or docs.


## HandBrake Blu-ray Scanning Heuristics

### Takeaway
HandBrake scans all titles above a configurable minimum duration (default 10 seconds), delegates duplicate filtering entirely to libbluray via `TITLES_FILTER_DUP_CLIP`/`TITLES_FILTER_DUP_TITLE` flags, then selects the "main feature" using `hb_bd_main_feature()`, which picks the longest title weighted by video format rank. There is no episode detection heuristic built into HandBrake itself.

### Cited Findings
- HandBrakeCLI `--min-duration` defaults to 10 seconds; titles shorter than this are not scanned; this rule only applies to disc-based sources (Blu-ray and DVD), not regular video files — [HandBrakeCLI man page (Debian)](https://dyn.manpages.debian.org/testing/handbrake-cli/HandBrakeCLI.1); [HandBrake 1.3.0 release notes (Neowin)](https://www.neowin.net/news/handbrake-130)
- HandBrake's `scan.c`: `hb_bd_init()` is called if the input path has no known video extension; it always passes `TITLES_FILTER_DUP_CLIP` and by default also `TITLES_FILTER_DUP_TITLE` to libbluray, unless `keep_duplicate_titles` is set — [HandBrake GitHub: libhb/scan.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/scan.c)
- HandBrake's `hb_bd_main_feature()` picks the title with the longest duration among those whose duration is ≥70% of the longest seen; ties are broken by video format rank (a lookup table), then duration, then chapter count — [HandBrake GitHub: libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)
- Chapters that start within 1.5 seconds of a title's end are dropped from that title — [HandBrake GitHub: libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)
- Titles where `DecodePreviews()` finds no decodable frames are removed from results after scanning — [HandBrake GitHub: libhb/scan.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/scan.c)
- Third-party script AutoHandbrake adds an episode-grouping heuristic: it looks for a group of sequential, similar-length titles matching a minimum duration, then offers to rip them as numbered episodes — [AutoHandbrake GitHub](https://github.com/marxjohnson/AutoHandbrake)
- A community workaround for anime discs (Squid Girl) was to make a decrypted backup with MakeMKV, then run HandBrake on the backup folder to get a single large file with chapter markers, which could then be split — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=82552)

### Inferences
- For a TV series disc with 4 episodes of ~45 minutes, HandBrake will likely show all 4 as separate titles (plus any extras above 10 seconds) because no one title is 70%+ of the others—so the main-feature heuristic may not collapse them. The user still needs to know which title number maps to which episode.
- For "play all" playlists that are simply the longest title, HandBrake's main-feature selection will prefer them, potentially surfacing the play-all as the default encode target.

### Gaps
- The exact default value for `max_duration` in HandBrake's Blu-ray scanning is not confirmed in public documentation.
- HandBrake's video format rank lookup table values and what they encode (codec? resolution?) were not confirmed from the source snippet seen.


## Plex Blu-ray Disc Scanning

### Takeaway
Plex does not support direct Blu-ray disc or BDMV folder scanning for episode matching. It expects individual pre-ripped video files named following SxxExx conventions. Attempting to point Plex at a BDMV structure results in either no recognition or incorrect file-to-episode mapping.

### Cited Findings
- A Plex forum user who asked how to play BDMV folders was told "No," with a pointer to rip with MakeMKV instead — [Plex forum: BDMV play thread](https://forums.plex.tv/t/how-do-i-mak-plex-play-dbmv-folders/313948)
- Plex's scanning is based on file naming conventions (SxxExx) that match against TheTVDB or TheMovieDB; the scanner is "relatively flexible for movies, pretty strict for TV shows" — [Plex support](https://support.plex.tv/?p=46272)
- An Infuse + Plex user found that pointing Plex at a BDMV caused numbered `.m2ts` files to appear instead of movies — [Infuse community thread](https://community.firecore.com/t/plex-infuse-bdmv/20496)
- Plex's DVD scanner mis-identified a 4-episode TV DVD as episodes 1–16 by running VOB files directly instead of IFO-guided navigation — [Plex forum: TV DVD scanner](https://forums.plex.tv/t/tv-dvd/6074)
- Plex stacked-media (multi-part files) has limits: max 8 parts, and not all Plex clients support it — [Plex forum: complex naming](https://forums.plex.tv/t/complex-media-naming-organization-question/141171)
- A Jellyfin forum note states that "finding out what a BDMV contains can be either very hard or impossible" — [Jellyfin forum](https://forum.jellyfin.org/printthread.php?tid=3258)

### Inferences
- For BD disc rips to work correctly in Plex, each episode must be individually demuxed/remuxed to its own MKV with correct SxxExx naming; the underlying MPLS selection must be done by the user or by MakeMKV before any Plex involvement.
- No media server (Plex, Emby, Jellyfin) implements reliable MPLS-level episode detection; the responsibility sits with ripping tools.

### Gaps
- No official Plex documentation on Blu-ray disc image support was found; the confirmed answer is that disc images/BDMV are unsupported.
- Emby's BDMV support (broken in version 4.9.5.0 per a forum thread) was not fully investigated.


## TV Series Blu-ray Structural Patterns Causing Problems

### Takeaway
The most common problem structures are: (1) fake/decoy playlists (hundreds of near-identical MPLS files designed to deter ripping), (2) "play all" playlists built with a single long segment instead of multiple episode-length segments with chapter markers, (3) BD-J Java-driven play ordering that hides episode sequence from static playlist inspection, and (4) duplicate title entries where only one has chapter information.

### Cited Findings
- Fake/decoy playlists: discs like *John Wick* have over 300 duplicate title entries; *Laurel & Hardy Year Two* also uses this pattern; "usually when there are fake playlists there are hundreds of them" — [MakeMKV forum: fake playlist help](https://forum.makemkv.com/forum/viewtopic.php?p=173579); [MakeMKV forum: fake playlist feature request](https://forum.makemkv.com/forum/viewtopic.php?p=196100)
- "Play All" can be implemented three different ways: (a) a single MPLS listing all episode segments in order, (b) a single segment containing all episodes with chapter markers, or (c) Java code that sequences playback — only cases (a) and (b) are detectable from MPLS inspection — [MakeMKV forum: segment map explanation](https://forum.makemkv.com/forum/viewtopic.php?p=53072)
- *Band of Brothers* Blu-ray disc 3: multiple titles of identical duration (4 titles at 1:07:20, 4 at 55:42); "hidden tracks" distinguish the 4 variants of each episode, not duration — [VideoHelp forum: Which Title thread](https://forum.videohelp.com/threads/350950-Which-Title)
- *Rick and Morty* Season 1 Blu-ray: rip failures on specific titles (7, 8, and 11); title 11 is "a combination of all episodes in 1 large file"; disabling subtitles on failed tracks allows successful rip — [MakeMKV forum: Rick and Morty S1 rip errors](https://forum.makemkv.com/forum/viewtopic.php?p=56602)
- *Rick and Morty* Season 3: video present but no audio — [MakeMKV forum: Rick and Morty S3](https://forum.makemkv.com/forum/viewtopic.php?p=72343)
- *Squid Girl* (anime): MakeMKV produced fragments instead of episodes; fix was a decrypted backup processed by HandBrake to yield a single file with chapter markers — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=82552)
- *Adventure Time* Australian Season 7: only 2-minute snippets per episode; normal structure is a single track with all episodes — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=82555)
- *Transformers Prime* Season 1 Disc 3: one episode missing; debug log showed the relevant playlist skipped as a duplicate — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63515)
- *South Park* Season 15 Disc 2: episode 4 consistently not detected across multiple MakeMKV versions — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63510)
- *Star Trek: TNG* Blu-ray set: random pressing defects causing individual episodes to be unreadable on specific discs — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63507)
- *Poldark* Season 2: playlist numbering does not match episode order; "don't just assume that title 205 is episode 1" — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53072)
- Prison Break Season 1 (German release): all discs contain same tracks multiple times; MakeMKV cannot display differences — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=80252)

### Inferences
- Duplicate playlists with near-identical durations are the dominant anti-ripping technique; a detector relying solely on duration will fail here.
- The segment map (PlayItem list) is more discriminating than duration: a play-all playlist typically has N segments (one per episode), while a single-episode playlist has 1.

### Gaps
- No specific structural documentation for *Cowboy Bebop* Blu-ray was found (reported problems were disc readability/scratches, not MPLS structure).
- No specific structural documentation for *The Prisoner* Blu-ray was found in ripping community sources.
- No confirmed community reports on *Rick and Morty* Blu-ray whether the failure is due to playlist structure or disc mastering defects.


## PlayItem Count as Distinguisher: Play-All vs. Single Episode

### Takeaway
Segment/PlayItem count is a reliable but not sufficient heuristic: a play-all playlist typically lists N clips (one per episode) in its segment map, while a single-episode playlist lists 1 or 2. However, three important edge cases exist: (1) all-episodes-in-one-segment discs use 1 segment for a play-all, detectable only by duration + chapter count; (2) some discs pad MPLS files to hide real runtime; (3) Java-driven play-alls have no corresponding MPLS at all.

### Cited Findings
- Confirmed heuristic from MakeMKV community: "If there's five episodes on each disc, you may see a segment map like '1,2,3,4,5'...if you also see a .mpls title with a segment map of '1' then you know that is the first episode by itself" — [MakeMKV forum: segment map explanation](https://forum.makemkv.com/forum/viewtopic.php?p=53072)
- Play-all MPLS is also the best way to determine episode order because episode segments are not always numbered sequentially — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53383)
- Single-segment play-all: "Some have single segment with all the episodes crammed into it, hopefully with chapter markers in between" — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53127)
- BD-J play-all: "Presumably others have a bit of Java code that provides the play-all functionality. In this case, MakeMKV doesn't really have a way to show that to you" — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53072)
- Padding: "Some Blu-rays are authored with a bazillion tracks of the same movie...the player runs a small Java program to determine which track is the right one" — [MakeMKV forum: segment map thread](https://forum.makemkv.com/forum/viewtopic.php?p=53127)
- A title appearing twice, once with chapters and once without, should prefer the version with chapters — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=136962)
- `bluback` (a Rust Blu-ray ripper library) uses a configurable minimum playlist duration (default: 900 seconds) for episode detection alongside chapter marker extraction — [bluback 0.7.1 crate docs](https://docs.rs/crate/bluback/0.7.1)
- MKVToolNix user: a 10-episode season where playlist numbers don't match episode order; recommends comparing runtimes or watching clips to establish the mapping — [MKVToolNix help forum: segment map for TV series](https://help.mkvtoolnix.download/t/segment-map-for-tv-series/1513)

### Inferences
- A robust detector should combine: (a) PlayItem/segment count to distinguish multi-episode from single-episode playlists, (b) minimum duration threshold to filter extras/trailers, (c) chapter count as a secondary signal for single-segment all-episodes case, and (d) cross-reference the play-all playlist's segment order to determine episode sequence.
- PlayItem count alone is not sufficient for the single-segment-all-episodes case; chapter markers within that segment are the only structural signal.

### Gaps
- No formal Blu-ray specification document was referenced in community sources to confirm the exact PlayItem/PlayListMark binary structure; all findings are from tool behavior and forum observation.
- The exact threshold for what constitutes a "short" extra vs. a "real" episode in automated tools varies: HandBrake default is 10 seconds, bluback default is 900 seconds; no consensus on the best value for anime (typically 22-24 min episodes) vs. drama (42-50 min).


## Community Consensus: Safest Identification Strategy

### Takeaway
The community consensus is: (1) use BDInfo to enumerate playlists, (2) use the segment map to find playlists with matching segment counts to the expected episode count, (3) cross-reference the play-all playlist's segment order for episode sequence, (4) confirm by brief playback or chapter-time comparison, and (5) check DVDCompare.net for release-specific mappings.

### Cited Findings
- DVDCompare.net is recommended for release-specific disc information including playlist-to-episode mappings — [MakeMKV forum: segment map thread](https://forum.makemkv.com/forum/viewtopic.php?p=53383); [VideoHelp forum](https://forum.videohelp.com/threads/350950-Which-Title)
- BDInfo: scan the disc/backup to get the two longest titles first and learn which MPLS file maps to each — [VideoHelp forum: Which Title thread](https://forum.videohelp.com/threads/350950-Which-Title)
- For fake playlists: right-click the disc tree in MakeMKV, unselect all, then pick real titles by checking segment maps — [MakeMKV forum: fake playlist help](https://forum.makemkv.com/forum/viewtopic.php?p=173579)
- MakeMKV's INFO window shows segment maps; comparing segment maps of individual titles against the play-all playlist confirms episode identity — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=144217)
- When two entries appear for the same title, keep the one with chapters — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=136962)
- Playlist numbers are not always sequential or ordered; always verify — [MakeMKV forum: Poldark thread](https://forum.makemkv.com/forum/viewtopic.php?p=53072)

### Inferences
- No purely automated solution exists that works for all discs; human verification (brief playback or chapter-time check) remains necessary for unusual discs.
- For an automated tool targeting common cases, the segment-map approach combined with play-all cross-referencing covers the majority of well-authored series Blu-rays.

### Gaps
- No tool other than MakeMKV and BDInfo was found that implements a reliable automated episode-detection pipeline for Blu-ray series discs.
- `bluback` (Rust) attempts this via duration + chapter extraction but is not widely used or benchmarked against problem discs; its edge case coverage is unknown.


## libbluray Internal Filtering Logic

### Takeaway
libbluray's `nav_find_main_title()` selects the longest playlist that contains no duplicate clips. Duplicate-clip and duplicate-title filtering are exposed as API flags (`TITLES_FILTER_DUP_CLIP`, `TITLES_FILTER_DUP_TITLE`). A minimum title length parameter was added to `bd_get_titles()` in 2011. As of 2019, codec type (preferring HEVC) was added as a tiebreaker.

### Cited Findings
- Original main-title selection: "find the playlist for the main title by picking the longest title that contains no duplicate clips" — [libbluray navigation.h, code.videolan.org](https://code.videolan.org/videolan/libbluray/-/blob/8014175c0c0a450d93d710edf836ca30b5938502/src/libbdnav/navigation.c)
- `_filter_dup`, `_find_repeats`, `_filter_repeats` helpers in `navigation.c` ignore titles with repeated segments identified by matching clip ID plus in and out times — [libbluray navigation.c (CentOS snapshot)](https://git.stg.centos.org/source-git/libbluray/raw/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/navigation.c)
- 2019 commit: codec comparison added; HEVC preferred over AVC/other formats in main title selection — [libbluray commit history, code.videolan.org](https://code.videolan.org/videolan/libbluray)
- 2011 commit: `min_title_length` parameter added to `bd_get_titles()` — [libbluray commit (code.videolan.org)](https://code.videolan.org/videolan/libbluray)
- HandBrake always passes `TITLES_FILTER_DUP_CLIP`; also passes `TITLES_FILTER_DUP_TITLE` unless `keep_duplicate_titles` is set — [HandBrake libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)
- HandBrake's `hb_bd_main_feature()`: a title is a candidate if duration ≥ 70% of the longest seen and video format value < 8; winner is highest-ranked format, with duration and chapter count as tiebreakers — [HandBrake libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)

### Inferences
- For discs with many fake playlists pointing to the same M2TS clips (duplicate clip content), `TITLES_FILTER_DUP_CLIP` should reduce the displayed list significantly, but only if the fake playlists literally reuse the same clip files.
- For fake playlists pointing to slightly different out-of-order assemblies of the same clips, the duplicate filter may not reduce the list because the in/out times differ.

### Gaps
- The exact current source of `nav_find_main_title()` at libbluray HEAD was not fetched (CentOS mirror 404'd); findings are from an older snapshot and commit messages.
- The video format rank lookup table in HandBrake's `hb_bd_main_feature()` was not inspected; it is not clear whether it prioritizes by codec, resolution, or both.
