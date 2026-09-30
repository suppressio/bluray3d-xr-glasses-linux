🇬🇧 English | [🇮🇹 Italiano](OPTIONS.it.md)

# Options in detail

The defaults work: you only need this page to change what the share offers (languages, subtitles, quality) or how it behaves. Back to the [README](README.md).

**Where options go:**
- native path: on the command line, e.g. `bluray3d-xr --audio-lang ita,eng --subs ita,eng /dev/sr0`;
- Docker: in `.env` (see `.env.example`), then `docker compose up -d`.

| Option | `.env` (Docker) | Command line (native) | Default |
|---|---|---|---|
| Blu-ray drive | `DRIVE` | positional argument, e.g. `/dev/sr0` | — |
| Folder with ISO / BDMV / MKV | `MOVIES_DIR` | positional arguments | — |
| [Audio languages](#audio-languages) | `AUDIO_LANG` | `--audio-lang ita,eng` or `all` | `all` |
| [One file per language, one with all, or both](#audio-languages) | `AUDIO_FILES` | `--audio-files per-language\|single\|both` | `per-language` |
| [Subtitle languages](#subtitles) | `SUBS` | `--subs ita,eng`, `all` or `none` | `all` |
| [3D subtitle depth](#subtitles) (pixels) | `SUB_DEPTH` | `--sub-depth 8` | `8` |
| [Lower-bitrate copies in `Light/`](#network-and-quality) | `LIGHT=on\|off` | `--light` / `--no-light` | on |
| [Animation after a jump](#loading-animation-after-a-jump) | `LOADER` | `--loader retrowave\|simple\|none\|video.mp4`, `--loader-3d video.mp4` | `retrowave` |
| Video encoder | `ENCODER` | `--encoder auto\|nvenc\|x264` | `auto` (NVENC if available) |
| Mount point | — | `--mount` | `/srv/bd3d` |
| [Program log file](#logs) | — | `--log path` or `none` | `~/.local/state/bluray3d-xr/bluray3d-xr.log` |
| [Pipeline log](#logs) | — | `--log-file` | `/tmp/bd3d-pipeline.log` |
| [Jump log](#logs) | — | `--debug` | off |

---

## Sources

Besides a drive, the program accepts the same content in other forms (all at once is fine):

| Source | Example | Notes |
|---|---|---|
| Blu-ray drive | `/dev/sr0` | the movie appears when a disc is inserted, disappears on eject |
| ISO image | `~/Videos/3D/Tron.iso` | read and decrypted like the disc |
| BDMV folder | `~/Videos/3D/Tron/` (contains `BDMV/`) | e.g. a MakeMKV "backup"; unencrypted ones need no keys |
| VIDEO_TS folder | `~/Videos/DVD/BTTF/` (contains `VIDEO_TS/`) | a DVD copied to disk; ISO files can be Blu-ray or DVD |
| MKV rip | `~/Videos/3D/Tron.mkv` | a MakeMKV rip that kept the 3D (MVC); files without 3D are skipped |
| Folder | `~/Videos/3D` | scanned recursively for all of the above |

```console
bluray3d-xr --audio-lang ita,eng /dev/sr0 ~/Videos/3D
```

Folders are scanned at startup: restart the program to see a new file. Drives are watched continuously.

## Audio languages

By default every language on the disc is offered, one track each (the best one: DTS-HD MA, DTS, AC-3... before TrueHD). `--audio-lang ita,eng` limits the choice and sets the order.

Languages are the standard ISO 639 codes: `ita`, `eng`, `fra`, `deu`, `spa`, `jpn`... Both spellings of the languages that have two (`fra`/`fre`, `deu`/`ger`, `nld`/`dut`...) and two-letter codes (`it`, `en`) work too, for `--audio-lang` and `--subs` alike.

Each language becomes **its own file** (`ITA - movie - 3D SBS.ts`, `ENG - movie - 3D SBS.ts`; the language comes first because players cut long names). The VITURE 3D Player has no audio track menu and picks a track on its own, so this is the default.

If your player does have an audio menu (VLC does), `--audio-files single` puts all languages in one file. `both` offers both at once: the per-language files, plus the all-languages file in a `Multi-audio/` folder. Useful with several devices; the files are virtual, so the extra ones cost nothing.

## Subtitles

Subtitles from the disc (Blu-ray, 3D Blu-ray, DVD, 3D MKV) are **drawn into the picture**, like a disc player does: no external files, so they work in any player.

- Every subtitle language is an extra version of each audio file: `ITAsubITA - movie`, `ITAsubENG - movie` (Italian audio with Italian / English subtitles); in `Multi-audio/` they are `subITA - movie`.
- The plain `ITA - movie` has no subtitles, except the **forced** ones of its language (the lines for foreign-language dialogue), which are always drawn in.
- Discs often carry 10+ subtitle languages, and the default `all` offers them all, times every audio language: **set `--subs` to the ones you read**, e.g. `--subs ita,eng` (`none` for no subtitle versions).
- **3D subtitles** are drawn in both eyes, each copy moved inward by `--sub-depth` pixels (default 8), so they float slightly in front of the screen. Raise it if they look "inside" the scene; `0` puts them on the screen plane.
- **External subtitle files** also work: put them next to an ISO/BDMV/MKV with the same name (`movie.srt`, `movie.ita.srt`, also `.ass`, `.sup`...). They show up next to every virtual video with the matching name, and the player loads them as external subtitles (the VITURE 3D Player shows them correctly in both eyes).

## Network and quality

Every file has a **constant bitrate**: that is what lets a byte of the file match a second of the movie. So a file **cannot adapt to the network** the way YouTube does. Instead, every movie is offered at two bitrates:

| | Normal | `Light/` |
|---|---|---|
| **3D** (Full-SBS 3840×1080) | 24 Mbit/s (video 20) | 10 Mbit/s (video 8) |
| **2D** (1920×1080) | 15 Mbit/s (video 12) | 6.5 Mbit/s (video 5) |
| **DVD** (1024×576 PAL, 854×480 NTSC) | 5 Mbit/s (video 4) | 2.4 Mbit/s (video 1.8) |

Audio: AAC stereo 192 kbit/s per language (DTS/TrueHD are not supported by most mobile players); a file with several languages (`Multi-audio/`) is 0.22 Mbit/s bigger for each extra one. Video: H.264. `--no-light` removes the `Light/` copies.

If playback **pauses every few seconds**, the Wi-Fi cannot keep up with that bitrate (a thick wall is enough):
- open the same movie from **`Light/`**;
- in **VLC**, raise the network cache (*Settings → Advanced → Network caching*) to 5000-10000 ms: it rides out short Wi-Fi drops;
- the program notices it and says so in its log, also telling whether the movie was ready (then the network or the player is slow) or not (then the disc or the decoding is):
  ```
  Blu-ray/ITA - Ready Player One.ts: the player receives 77% of the data rate the movie needs,
  with 60s of movie ready ahead: the network (or the player) is too slow for this file,
  playback will pause (try Light/)
  ```
  If instead it says "only 0.5s of movie is ready ahead: the disc or the decoding cannot keep up", `Light/` will not help: look at the disc (scratches, fingerprints) and at the CPU.

## Loading animation after a jump

After a jump with the seek bar the movie needs a few seconds (the drive moves, then decoding starts). Meanwhile the player shows the last picture, frozen, and its clock stands still: it looks stuck, but the movie then starts **exactly where you jumped**.

So by default the file carries a loading animation, in a loop, from the landing point until the movie is ready: a neon grid running to the horizon, the disc rising on it like the sun, a loading bar jumping to the beat. On 3D movies it is in 3D too.
- you see at once that it is loading;
- but the player's clock keeps running during the animation, and the movie starts **that many seconds later** than where you jumped: under a second on a DVD, 2-5 s on a Blu-ray from the drive. The two things cannot go together: every second of the file is one second of the movie, and the seconds taken by the animation cannot be used again.

It is not shown when the player is paused (it waits for the movie's frame) nor when a file is first opened.

- `--loader simple` (Docker: `LOADER=simple`): a plainer one, a disc and a spinning arc, 2D.
- `--loader none` (Docker: `LOADER=none`): no animation. The player waits on the frozen picture and the movie starts exactly where you jumped.
- `--loader video.mp4`: your own animation, any short video that loops cleanly. 16:9: shown in both eyes on 3D movies. Side by side (3840x1080): in 3D on 3D movies, its left eye on 2D ones.
- `--loader-3d video.mp4`: a different side-by-side video for 3D movies only.

The animations shipped (`loaders/retrowave.mp4`, 1 MB, side by side; `loaders/simple.mp4`, 100 KB) are made from scratch in Blender: no images or fonts of anyone else. The scripts that render them are in [bluray3d-xr-loaders](https://github.com/suppressio/bluray3d-xr-loaders).

## Logs

- The program's log says which movies appear, which pipeline starts where, and whether the network and the disc keep up. It goes to the terminal and is also saved, with the date, in `~/.local/state/bluray3d-xr/bluray3d-xr.log` (up to 5 MB, then three older copies `.1` `.2` `.3`); `--log` saves it elsewhere, `--log none` not at all. With Docker: `docker compose logs`.
- `--log-file` is FFmpeg's and the decoders' output, one section per pipeline (the older one moves to `.1` beyond 10 MB): the first place to look when the picture is wrong.
- `--debug` also logs every jump and the player's reads in the 20 seconds after it: how long it waited, and whether it looked paused. Also the full command of each pipeline, to run it by hand ([DEVELOPMENT.md](DEVELOPMENT.md#taking-a-pipeline-apart)).
