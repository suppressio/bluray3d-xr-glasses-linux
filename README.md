🇬🇧 English | [🇮🇹 Italiano](README.it.md)

# Blu-rays and DVDs on every screen at home, 3D included, from Linux
#### _Put a disc in your Linux PC's drive and watch it on any device at home: TV, tablet, phone, XR glasses. The PC reads, decrypts and decodes the disc on the fly and serves it over the LAN as an ordinary video file. 3D Blu-rays become real side-by-side 3D for XR glasses (VITURE & co.). No rip, no conversion, no disk space._

***Note:*** _Any player able to open videos from a network share (SMB) should work; for 3D it must also show side-by-side video. Devices, players and discs tested so far, with a guide for each: [**TESTED.md**](TESTED.md)._

![The VITURE 3D Player opening the share: one file per audio language, with and without Italian subtitles](docs/viture-3d-player.png)

---

## The problem

A 3D Blu-ray does not contain two videos side by side. It stores the 3D as **MVC** (H.264 _Multiview Video Coding_): a normal 2D video for the left eye plus a "dependent view" with the differences for the right eye.

- XR glasses (and almost every 3D video player) want **SBS**, _side-by-side_: both eyes in one frame.
- Practically **nothing decodes MVC** outside dedicated Blu-ray players: Android and its players cannot, and FFmpeg silently drops the dependent view (you get 2D).
- The usual answer is to **rip and convert** every movie to SBS: hours of work and 10-20 GB for each movie.

## The idea

The PC reads the disc and decodes the MVC **while you watch**, and serves the result to the glasses as if it were an ordinary SBS file.

```
3D Blu-ray in the drive (or an ISO / BDMV folder / MKV rip)
        │  libbluray + libaacs: read and decrypt on the fly
        ▼
   base + dependent view ─► edge264 (MVC → SBS 3840×1080) ─► encoder (NVENC or x264) + audio
        │
        ▼
   "ITA - movie - 3D SBS.ts"  a virtual file in an SMB share (it does not exist on disk)
        │   Wi-Fi
        ▼
   Glasses / headset: player ► network share ► Disks ► movie   → 3D, pause, seek
```

- **Insert the disc, the movie appears.** About 15 seconds after closing the tray the file shows up in the share, named after the disc; eject the disc and it goes away.
- **3D Blu-rays, 2D Blu-rays and DVDs.** Same flow for all: 3D discs go in the `Blu-ray 3D/` folder, 2D Blu-rays in `Blu-ray/`, DVDs in `DVD/`.
- **Any device at home.** A TV, tablet or phone with a player that opens network shares plays the Blu-rays and DVDs; the 3D files need a player that shows side-by-side 3D (XR glasses, VR headsets). Several devices can watch at once, even the same disc in two languages.
- **Nothing is written to disk.** The `.ts` file shows a size of ~22 GB but takes no space: every piece is produced when the player reads it.
- **Seeking works.** The file has a constant bitrate, so every byte matches a precise second of the movie. When the player jumps, the PC restarts reading the disc from there (a few seconds: the optical drive has to move).
- **Players see a normal file.** No special app, server or streaming protocol: the player's own interface, pause, seek, and 3D detection.

All the procedures described here are a balanced compromise between "manual" and guided processes. This is one of the possible ways to do it.

## Requirements

#### Hardware
- **A Blu-ray drive** in the Linux PC (internal or USB), which reads DVDs too. Its name is usually `/dev/sr0`; with more than one drive, `lsblk -d -o NAME,MODEL | grep sr` tells which is which.
- **A Linux PC** on the same network as the glasses. Decoding MVC is CPU work: a recent multi-core desktop CPU is plenty (on the test PC decoding runs at ~9× real time). Low-power CPUs have not been tested.
- **Optional: an NVIDIA GPU** to encode with NVENC. Without it the video is encoded by the CPU with x264 (on the same CPU still ~6× real time).
- **XR glasses + a player** that opens videos from an SMB network share and plays SBS 3D ([tested ones](TESTED.md)).
- **RAM**: about 0.5 GB free while a movie plays. Nothing is written to disk: the pipeline prepares up to ~256 MB ahead of the player in memory and keeps ~190 MB behind for short jumps back, plus 16 MB (start and end) per file opened.
- **A good Wi-Fi connection** (5 GHz recommended): 15 Mbit/s for 2D, 24 Mbit/s for 3D, less with the `Light/` copies (see [Network and quality](OPTIONS.md#network-and-quality)).

#### Decryption: bring your own keys
Commercial Blu-rays are encrypted (AACS, some also BD+). The project **does not provide or download any key**; it uses what you have, in this order:
1. **libaacs + `KEYDB.cfg`**: fully open source. Put a key database in `~/.config/aacs/KEYDB.cfg` (for Docker: the folder you set as `AACS_DIR`).
2. **MakeMKV**, if installed and registered: its `libmmbd` library decrypts in place of libaacs (BD+ too).
3. Unencrypted **ISO / BDMV folders** need no keys.

DVDs (CSS) are decrypted by **libdvdcss**, which you install yourself: on Debian/Ubuntu `sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg` (Debian: `contrib` section). It is not in the Docker image; `docker-compose.yml` has a commented line to use the host's.

> ⚖️ In many countries (Italy and most of the EU included) circumventing copy protection is not allowed, not even for a private copy. Check the law where you live.

#### Software on the PC
- **Docker** (path A) _or_ a **Debian/Ubuntu** system (path B). Everything else is installed for you:
  - [edge264-mvc](https://github.com/jens-duttke/edge264-mvc): the only open-source decoder of the MVC dependent view;
  - libbluray, libaacs, libbdplus (Blu-ray), libdvdread (DVD), FFmpeg, Samba (the network share), FUSE + pyfuse3 (the virtual file).

---

## Step 1 — Install: choose your path

| | **A. Docker** | **B. Native script** |
|---|---|---|
| For | anyone who already uses Docker | Debian / Ubuntu |
| Touches the system | no: Samba, FUSE and libraries live in the container | yes: packages, `/opt`, `smb.conf`, `fuse.conf` (`uninstall.sh` removes what it added) |
| Decryption | libaacs + your KEYDB (MakeMKV is not in the image) | libaacs + KEYDB, or MakeMKV if installed |
| NVIDIA encoding | needs the NVIDIA Container Toolkit | works out of the box |
| Samba already installed on the PC | conflicts on port 445: stop it first | the share is added next to yours |

Get the project first:
```console
git clone https://github.com/suppressio/bluray3d-xr-glasses-linux.git
cd bluray3d-xr-glasses-linux
```

### Path A — Docker

- [ ] Create your settings:
```console
cp .env.example .env
```
- [ ] Edit `.env`:
  - `DRIVE=/dev/sr0` to watch the Blu-ray drive, and in `docker-compose.yml` uncomment the `- /dev/sr0` line under `devices`;
  - `AACS_DIR` = the folder containing your `KEYDB.cfg`;
  - `MOVIES_DIR` = a folder with ISO, BDMV folders or MKV rips (can be empty if you only use the drive);
  - `AUDIO_LANG` and `SUBS` = the audio and subtitle languages you want, e.g. `ita,eng` (the rest: [OPTIONS.md](OPTIONS.md)).
- [ ] Build and start (the first build takes a few minutes: it compiles edge264 for your CPU):
```console
docker compose up -d --build
```
- [ ] Insert a disc (3D or 2D Blu-ray, DVD) and check the log:
```console
docker compose logs -f
```
```
watching /dev/sr0: insert a Blu-ray
/dev/sr0: disc inserted, opening it
+ Blu-ray 3D/ITA - Tron - Legacy 3D - 3D SBS.ts  (125 min, 24.0 Mbit/s, audio 0x1102 ita dts)
```

Stop it with `docker compose down`. Updating and uninstalling: [below](#updating-and-uninstalling).

##### NVIDIA GPU (optional)
Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), then start with both compose files:
```console
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d --build
```
The log should now say `video encoder: nvenc`.
> ⚠️ This variant is not tested yet (my PC has no Container Toolkit). NVENC is tested with the native path.

##### 💩 happens: port 445 already in use
If the PC already runs Samba, the container cannot take port 445. Players on phones and glasses usually only speak to port 445, so stop the host Samba while you watch (`sudo systemctl stop smbd`) or use path B, which adds a share to your existing Samba.

### Path B — Native script (Debian / Ubuntu)

- [ ] Run the installer as your normal user (it asks for `sudo` when needed):
```console
./scripts/install.sh
```
It installs the packages, compiles edge264, installs the `bluray3d-xr` command and adds the read-only guest share `[Disks]` to Samba. The header of [`scripts/install.sh`](scripts/install.sh) lists every change it makes.

- [ ] If `ufw` is active, the script prints the command to open the share to your LAN, e.g.:
```console
sudo ufw allow from 192.168.1.0/24 to any port 445 proto tcp
```
- [ ] Your user must be able to read the drive (on Debian/Ubuntu: the `cdrom` group, usually already set for desktop users).
- [ ] Start it when you want to watch something:
```console
bluray3d-xr --audio-lang ita,eng --subs ita,eng /dev/sr0
```
When a disc goes in, its files appear; every `pipeline from` line is the player starting or jumping:
```
11:23:59 bluray3d-xr v1.0.0
11:24:00 video encoder: nvenc
11:24:00 watching /dev/sr0: insert a Blu-ray
11:24:00 mounted on /srv/bd3d — Ctrl+C to unmount
11:24:00 /dev/sr0: disc inserted, opening it
11:24:00 + Blu-ray 3D/ITA - Tron - Legacy 3D - 3D SBS.ts  (125 min, 24.0 Mbit/s, audio 0x1102 ita dts, forced subtitles ita)
11:24:00 + Blu-ray 3D/ITAsubITA - Tron - Legacy 3D - 3D SBS.ts  (125 min, 24.0 Mbit/s, audio 0x1102 ita dts, subtitles ita)
11:24:00 + Blu-ray 3D/ENG - Tron - Legacy 3D - 3D SBS.ts  (125 min, 24.0 Mbit/s, audio 0x1100 eng dts-hd ma, forced subtitles eng)
...
11:30:24 Blu-ray 3D/Light/ITA - Tron - Legacy 3D - 3D SBS.ts: pipeline from 0.0s (requested 0.0s, offset 0)
11:30:35 Blu-ray 3D/Light/ITA - Tron - Legacy 3D - 3D SBS.ts: pipeline from 1634.8s (requested 1635.3s, offset 2043548532)
```
Stop it with `Ctrl+C`. Updating and uninstalling: [below](#updating-and-uninstalling).

The script is tested with the real drive and disc in clean Debian 13 (trixie) and Ubuntu 24.04 containers; the program itself runs daily on Debian testing.

### Updating and uninstalling

**Updating**, when a new version is out (see [Releases](https://github.com/suppressio/bluray3d-xr-glasses-linux/releases)); `bluray3d-xr --version` tells which one you have:
- native: `./scripts/update.sh` in the project folder. It downloads the latest version and copies the program to `/opt/bluray3d-xr`; it runs the whole `install.sh` again only when needed (edge264 changed, or nothing is installed). It ends with "Updated from vX to vY" and the list of changes. If the program is running, stop it and start it again.
- Docker: `git pull && docker compose up -d --build`.

**Uninstalling**:
- native: `./scripts/uninstall.sh`. It unmounts `/srv/bd3d` and removes it, removes the `[Disks]` share from `smb.conf` (only the block it added: your own shares stay), `/opt/bluray3d-xr` and the `bluray3d-xr` command. The apt packages (FFmpeg, Samba, libbluray...) and the `user_allow_other` line in `/etc/fuse.conf` stay, since other software may use them: remove them with apt if nothing needs them.
- Docker: `docker compose down --rmi all` stops the container and deletes its image.

Then delete the project folder.

---

## Step 2 — Watch it on the glasses

- [ ] Insert the disc in the PC and wait about 15 seconds.
- [ ] On the glasses, headset or TV, open the network share of the PC: many players find it by themselves under "local network", otherwise add its IP address (`hostname -I` on the PC tells you), guest / anonymous access.
- [ ] Open the **Disks** folder (go back and in again if it looks empty). 3D discs are in `Blu-ray 3D/` as `ITA - <movie> - 3D SBS.ts`, normal ones in `Blu-ray/` as `ITA - <movie>.ts`: one file per audio language, plus versions with subtitles (`ITAsubENG - ...`: Italian audio, English subtitles). `Light/` has the same movies at a lower bitrate, for weak Wi-Fi.

```
Disks/
├── Blu-ray 3D/
│   ├── ITA - Tron - Legacy 3D - 3D SBS.ts
│   ├── ITAsubENG - Tron - Legacy 3D - 3D SBS.ts
│   ├── ENG - Tron - Legacy 3D - 3D SBS.ts
│   └── Light/ ...
├── Blu-ray/
│   ├── ITA - Ready Player One.ts
│   └── Light/ ...
└── DVD/
    ├── ITA - Back To The Future.ts
    └── Light/ ...
```
- [ ] Open it. 3D files are full side by side (3840×1080, a whole 1920×1080 picture per eye): if the player does not switch to 3D by itself, choose its side-by-side (SBS) 3D mode. 🎉

The first opening and every jump with the seek bar take a few seconds: that is the drive moving to the new point.

Step by step for the VITURE 3D Player, the PICO 4's own player and VLC, and what each one can and cannot do: [**TESTED.md**](TESTED.md).

---

## Languages, subtitles, quality

The defaults offer everything on the disc. Most people want only their languages:
```console
bluray3d-xr --audio-lang ita,eng --subs ita,eng /dev/sr0
```
(Docker: `AUDIO_LANG=ita,eng` and `SUBS=ita,eng` in `.env`.) Every other option, and ISO files, BDMV/VIDEO_TS folders or MKV rips instead of a drive: [**OPTIONS.md**](OPTIONS.md).

---

## Troubleshooting

- **The movie does not appear, the log says `cannot decrypt`**: no working key for that disc. Update your `KEYDB.cfg`, or install and register MakeMKV (native path).
- **The picture freezes for about 20 seconds after closing the program while watching**: the player waits before giving up on the file that disappeared (the program itself stops in a second). Stop the video on the glasses first, then the program.
- **Playback pauses every few seconds**: the Wi-Fi is too slow for the file: open it from `Light/`, more in [Network and quality](OPTIONS.md#network-and-quality).
- **Nothing happens when inserting the disc**: check that the drive really is `/dev/sr0` (`lsblk -d -o NAME,MODEL | grep sr`), that your user can read it (`ls -l /dev/sr0`, `cdrom` group), and for Docker that the device is passed to the container.
- **The glasses do not see the share**: check that the PC and the glasses are on the same network, and the firewall (port 445/TCP). From another Linux PC: `smbclient -N -L //<pc-ip>`.
- **The picture stutters**: check the Wi-Fi first (5 GHz, close to the router). Then the log of the last pipeline (`/tmp/bd3d-pipeline.log`, or `docker compose logs`).
- **A new file in `MOVIES_DIR` does not appear**: folders are scanned at startup. Restart the program / the container (drives are watched continuously).
- **After a jump a loading animation plays for a few seconds, and the movie starts a little after the point you chose**: the drive is moving to the new point. [OPTIONS.md](OPTIONS.md#loading-animation-after-a-jump) explains why, and how to turn it off.

Reporting a problem: [open an issue](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/new/choose) with the **Something does not work** form. It asks for the version (`bluray3d-xr --version`), the disc, the player and the program's log around the problem; if you can make it happen again, run the program with `--debug`. Never paste keys.

---

## How it works

- **Reading the disc.** libbluray reads the disc (or ISO/BDMV) and decrypts it through libaacs or MakeMKV's libmmbd (`src/bluray.py`, a small ctypes binding). The movie is the longest playlist whose clips all have an MVC dependent view (`src/bdmv.py` reads playlists and clip info).
- **Both views.** The `.ssif` file of a 3D clip interleaves base (PID 0x1011) and dependent (0x1012) view in extents. `src/ssif_demux.py` pairs the two PES packets of each frame on their DTS and writes them as Annex B for edge264. It drops the Blu-ray delimiter, filler and end-of-sequence NAL units, as MakeMKV does. libbluray older than 1.4 cannot open `.ssif` files, so the same bytes are rebuilt from the two `.m2ts` files.
- **edge264-mvc** decodes both views and writes them side by side (`edge264_test -Ok`). FFmpeg alone cannot: it drops the dependent view.
- **Seeking.** A time goes through the EP_map (keyframe positions) and the extent table of the clip to a byte of the `.ssif`. Movies made of several clips (Tron: two) are played in sequence, and the audio timestamps of later clips are moved onto one timeline.
- **Audio.** `src/disc_reader.py` reads the disc once: video to edge264, the chosen audio track (with its language from the playlist) to FFmpeg through a FIFO, each from its own thread. The keyframe is forwarded with the audio as a timing anchor, so audio and video start exactly together. Measured against the MKV path: 4 ms.
- **The virtual file.** The encoder uses **constant bitrate** and the MPEG-TS muxer pads to exactly 24 Mbit/s, so byte _X_ is second _X_ / 3,000,000 of the movie. A **FUSE** file system (`src/bd3d_fs.py`) serves the files. It restarts decoding when the player jumps and pauses the pipeline when it gets too far ahead. Only one pipeline per drive runs at a time, because two would make the optical drive seek back and forth. The end of the file, which players read to get the duration, is synthetic: black and silence with the right timestamps, so the drive is not sent to the end of the disc.
- **DVD.** `src/dvd.py` reads the disc through libdvdread (libdvdcss for CSS) and its IFO files: the titles, their cells, audio languages, video standard and aspect. It offers the movie, or the episodes of a series; titles that replay cells or jump back on the disc (fake titles of copy protections) are left out. Seeking is a binary search over the navigation packs of the cell's VOBUs (~0.5 s each), which hold their exact time: the restart point is known precisely. Damaged spots are skipped, half a second at a time. FFmpeg's own DVD reader only seeks approximately (seconds off, without knowing where it landed). `src/dvd_reader.py` streams the title from there to FFmpeg, which deinterlaces and scales it to square pixels.
- **Drives.** Every few seconds the drive is asked whether a disc is in (no reads). On insertion it is opened as a Blu-ray, or else as a DVD, and its movie added; on eject it is removed.

## Development

Running it from a clone, where things are in the code, the tests and the logs: [DEVELOPMENT.md](DEVELOPMENT.md).

---

## Limits

- The last ~2.7 seconds of every movie (after the end credits) are black: the tail of the file is synthetic.
- Tested on a handful of discs ([TESTED.md](TESTED.md#discs)), all AACS only: BD+ discs (through MakeMKV) and NTSC DVDs are untested.
- Subtitles from the disc are drawn into the picture (one version per language), not selectable in the player; the 3D depth is fixed, not taken from the disc. Audio is converted to AAC stereo.

## Windows?

This project is Linux only. On Windows you can look at [**SyLC**](https://github.com/5ymph0en1x/SyLC), an open-source player that plays 3D Blu-ray MVC directly (MKV, ISO, BDMV) and can output SBS. I have not tried it.

## License

[MIT](LICENSE). It covers this project's code only: edge264, libbluray, libaacs, FFmpeg, Samba and the other tools it uses keep their own licenses, and are downloaded or installed from their sources, not shipped here.

## Credits and references

- [edge264-mvc](https://github.com/jens-duttke/edge264-mvc) (BSD), fork of [edge264](https://github.com/tvlabs/edge264): the MVC decoder that makes all this possible;
- [libbluray / libaacs](https://www.videolan.org/developers/libbluray.html), [FFmpeg](https://ffmpeg.org/), [Samba](https://www.samba.org/), [pyfuse3](https://github.com/libfuse/pyfuse3), [MakeMKV](https://www.makemkv.com/);
- [Play 3D Blu-ray in SBS directly from the disc…](https://cybereality.com/play-3d-blu-ray-in-sbs-directly-from-the-disc-for-playback-on-3d-monitors-xr-glasses-and-vr-headsets-using-free-and-open-source-tools/) (cybereality): a Windows approach (LAV Filters + madVR).
