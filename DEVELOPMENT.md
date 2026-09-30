# Development

🌍 [Italiano](DEVELOPMENT.it.md)

How to run the program from a clone, where things are in the code, what the tests check and how to read the logs. For what the program does and how, see [How it works](README.md#how-it-works) in the README.

---

## Setting up

1. Run the native install once (**Path B** in the [README](README.md#path-b--native-script-debian--ubuntu)), including the Samba share. It installs everything a clone needs as well:
   - the system packages (FFmpeg, libbluray, libdvdread, pyfuse3, trio);
   - the edge264 decoder in `/opt/bluray3d-xr/bin`;
   - the mount point `/srv/bd3d`;
   - `user_allow_other` in `/etc/fuse.conf`.
2. Start the program from the clone instead of the installed copy:

   ```bash
   cd src
   PATH=/opt/bluray3d-xr/bin:$PATH python3 bd3d_fs.py --debug /dev/sr0
   ```

   The first log line gives the version, e.g. `bluray3d-xr v1.1.0-2-gabc1234`: two commits after v1.1.0.
3. Stop it with **Ctrl+C**: it unmounts `/srv/bd3d` and removes its temporary files.

- **Only one copy at a time.** The installed one and the one in the clone use the same mount point. The second refuses to start, with a message that says so.
- **After a crash, or a `kill -9`,** the mount point stays busy: `fusermount3 -u /srv/bd3d`.
- **VS Code:** open the folder and pick `.venv` as the interpreter; `scripts/check.sh` creates it. `.vscode/` turns Pylint off, because the checks are the ones in `pyproject.toml`.

---

## Where things are

| File | What it does |
|---|---|
| `src/bd3d_fs.py` | The FUSE file system and the command line. It holds the virtual files, the drive watcher and the pipelines (start, jump, pause, stop), and paints the loading picture. |
| `src/sources.py` | What a movie is made of: `BlurayDiscSource`, `DvdSource`, `MkvSource`. Each one says how to read its video from a given time (`video_command`), and gives its audio, subtitles and keyframes. |
| `src/pipeline.py` | The decode chain: reader → decoder → FFmpeg encoder at constant bitrate. Also the encoder choice (NVENC or x264) and the quality. |
| `src/bluray.py` | A small ctypes binding to libbluray: it opens a disc, ISO or folder, and reads its files decrypted. |
| `src/bdmv.py` | Blu-ray navigation files: playlists (MPLS), clip info (CLPI), seeking in the `.ssif`. |
| `src/disc_reader.py` | Reads a Blu-ray title from a given time. 3D: video for edge264 on stdout and audio into a FIFO. 2D: one filtered TS stream. It runs as its own process. |
| `src/ssif_demux.py` | Splits the 3D `.ssif` stream into H.264 plus MVC for edge264. |
| `src/dvd.py` | libdvdread through ctypes, the IFO files, the choice of the main title or the episodes, and the exact seek. |
| `src/dvd_reader.py` | Reads a DVD title from a given time as an MPEG program stream for FFmpeg. It runs as its own process. |
| `src/langs.py` | One spelling per language (ISO 639-2). |
| `loaders/` | The loading animations shipped with the program. The Blender scripts that render them are in [bluray3d-xr-loaders](https://github.com/suppressio/bluray3d-xr-loaders). |
| `tools/bdprobe.c` | The first probe of the project (roadmap phase 0): it opens a disc with libbluray and dumps the start of the 3D stream. |
| `typings/pyfuse3/` | Type stubs for pyfuse3: its versions 3.3, 3.4 and 3.5 declare their types differently. |

The path of a piece of the file, from the player to the disc: the player reads bytes from a file in `/srv/bd3d` → `bd3d_fs.py` turns the byte into a time (the bitrate is constant) → if no pipeline is at that time, it starts one: the source's reader (`disc_reader.py`, `dvd_reader.py` or FFmpeg for MKV), then the decoder (edge264 for 3D, FFmpeg otherwise), then FFmpeg encoding the `.ts` → the bytes go back to the player.

The code, its comments and the log messages are in English. The documentation is in two languages: every `.md` file has an `.it.md` twin, to be changed together.

---

## Checks and tests

```bash
scripts/check.sh                  # what GitHub runs on every push
scripts/check.sh --integration    # also the tests on the disc in the drive
```

`check.sh` runs, in this order:
- **ruff**: style and common mistakes, with a broad rule set in `pyproject.toml`;
- **pyright**: types, in strict mode;
- **shellcheck**: the scripts in `scripts/` and `docker/`;
- **the unit tests**.

The first run creates `.venv` with the pinned tools from `requirements-dev.txt`. The program's own dependencies come from the system. GitHub runs the same script on Ubuntu 24.04 (`.github/workflows/check.yml`).

### Unit tests (`tests/`)

They need no disc and no drive, and take a few seconds.
- `tests/builders.py` writes small but real MPLS, CLPI and IFO files and TS streams.
- `tests/fakes.py` stands in for libbluray and libdvdread (`FakeBluray`, `FakeDvd`).

One test file per module: `test_bdmv.py` for `bdmv.py`, and so on. A fix comes with the test that would have caught the bug.

### Integration tests (`tests/integration/`)

They run on the disc in the drive (`BD3D_TEST_DRIVE`, default `/dev/sr0`) or on a 3D MKV (`BD3D_TEST_MKV=~/Videos/movie.mkv`). They check:
- **what the disc holds:** title, duration, languages, subtitles, the files it would show;
- **keyframes:** where a jump lands, at six points of the movie;
- **decoder input:** the bytes handed to edge264 or FFmpeg at 37% of the movie, demuxed and decrypted;
- **decoded 3D frames:** 48 frames at 50%, decoded by edge264 (3D only);
- **the virtual file:** a piece read through the real pipeline, with the right streams, picture size and timestamps.

The first run on a disc records a reference in `~/.cache/bluray3d-xr/reference/`: hashes only, no disc content. Later runs must match it byte for byte. After a change that is meant to alter the output (a new filter, a different stream order), record it again:

```bash
BD3D_UPDATE_REFERENCE=1 scripts/check.sh --integration
```

Which tests matter for which change:

| Change in | Run |
|---|---|
| any file | `scripts/check.sh` |
| `bdmv.py`, `disc_reader.py`, `ssif_demux.py`, `bluray.py` | with a Blu-ray in the drive (3D if you can): `--integration` |
| `dvd.py`, `dvd_reader.py` | with a DVD in the drive: `--integration` |
| `bd3d_fs.py` (jumps, pipelines) | the tests, then a real session on the glasses or VLC, with jumps back and forth |
| `scripts/install.sh`, `update.sh` | a clean Debian 13 and Ubuntu 24.04 container |

---

## Logs

### The program's log

It goes to the terminal and to `~/.local/state/bluray3d-xr/bluray3d-xr.log`, with the date (`--log` to change it; with Docker: `docker compose logs -f`). The lines to know:

| Line | Meaning |
|---|---|
| `bluray3d-xr v1.1.0` | the version: the first thing to ask for in a bug report |
| `video encoder: nvenc` | NVENC or x264, chosen at start |
| `watching /dev/sr0: insert a Blu-ray` | a drive is watched, even when it is empty |
| `/dev/sr0: disc inserted, opening it` / `disc ejected` | the drive saw a disc go in or out |
| `+ Blu-ray 3D/ITA - Movie - 3D SBS.ts (120 min, 24.0 Mbit/s, audio …)` | a file appeared: its length, bitrate and tracks |
| `- …` | a file went away (eject) |
| `…: pipeline from 1234.5s (requested 1236.0s, offset …)` | a player read there: decoding starts from the keyframe before the requested time |
| `…: movie ready, 0.6s after the jump (…)` | after a jump, how long the loading animation ran |
| `…: ignoring the player's late reads for the position it left` | normal after a jump: the player still asks for the old position for a moment |
| `…: stopping its pipeline, … is reading the same disc` | another file on the same disc started playing: one disc, one pipeline |
| `/dev/sr0: AACS, decrypted with libaacs` | the disc's protection and what opened it (`MakeMKV (libmmbd)`, or `not encrypted`) |
| `…: the player receives 87% of the data rate …, with 60s of movie ready ahead …` / `back to real time` | the movie is ready but the player pulls it slowly: the network (try `Light/`) or the player itself; then it recovers |
| `…: the player receives 62% …, and only 0.5s of movie is ready ahead …` | the player is waiting for us: the disc or the decoding is slow there |
| `…: the player waited 12s for the movie at 1:09:10 …` | one read waited that long for the pipeline: a damaged or dirty spot on the disc, a busy drive or CPU |
| `…: no reads for 120s, stopping a pipeline` | the player stopped or paused for a long time |
| `…: read at … failed` + traceback | a bug, or an unreadable disc: the player gets a read error, the program goes on |

`--debug` adds:
- every jump and the player's reads in the 20 seconds after it: how long it waited, and whether it looked paused;
- the full command of each pipeline, which can be copied and run by hand (see below);
- the drive states that were ignored.

### The pipeline log

`--log-file` (default `/tmp/bd3d-pipeline.log`) holds the output of the readers, the decoders and FFmpeg, one section per pipeline headed `=== date time file: pipeline from 1234.5s ===` (beyond 10 MB the older part moves to `.1`). Look here first when the picture or the sound is wrong. Useful lines:
- `disc_reader: clip 00098, keyframe -0.006s, ssif offset 0`: where the Blu-ray reader started;
- `dvd_reader: title 2, VOBU at 600.040s (sector 123456)`: where the DVD reader started;
- `dvd_reader: sectors X-Y unreadable, going on from Z`: a damaged spot, skipped;
- FFmpeg warnings: "Could not find codec parameters" at the start is harmless, the stream is found a moment later. An `Error` line is not.

---

## Taking a pipeline apart

The `--debug` command of a pipeline gives the playlist, title and tracks. Each stage can then be run alone, from `src/`, with `PATH=/opt/bluray3d-xr/bin:$PATH`. `ffplay` shows the result; on Debian it is a package of its own (`sudo apt install ffplay`).

```bash
# 2D Blu-ray: the filtered stream, straight into a player
python3 disc_reader.py /dev/sr0 --playlist 00800 --start 600 --mode 2d | ffplay -

# 3D Blu-ray: the two views decoded side by side
python3 disc_reader.py /dev/sr0 --playlist 00070 --start 600 | edge264_test - -Ok | ffplay -f yuv4mpegpipe -

# DVD: the title with its first audio track
python3 dvd_reader.py /dev/sr0 --title 2 --start 600 --audio 0x80 | ffplay -f mpeg -
```

With MakeMKV's decryption, the command starts with `LIBAACS_PATH=libmmbd LIBBDPLUS_PATH=libmmbd`, as in the `--debug` line.

A piece of a virtual file, as a player gets it:

```bash
f="/srv/bd3d/Blu-ray 3D/ITA - Movie - 3D SBS.ts"
dd if="$f" bs=188 skip=5000000 count=20000 status=none > /tmp/piece.ts
ffprobe -hide_banner /tmp/piece.ts
```

---

## Releases

1. Everything committed, `scripts/check.sh` clean, GitHub's check green.
2. An annotated tag and the release: `git tag -a v1.2.0 -m v1.2.0 && git push origin v1.2.0`, then `gh release create v1.2.0` with the notes.
3. `install.sh`, `update.sh` and the Docker image write the tag into a `version` file next to the program. Users update with `scripts/update.sh`.

The version numbers follow what changes for users: a new feature or a new kind of disc is a minor release (1.1 → 1.2), a fix only is a patch (1.1.0 → 1.1.1).
