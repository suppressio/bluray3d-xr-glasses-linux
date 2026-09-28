🇬🇧 English | [🇮🇹 Italiano](README.it.md)

# 3D Blu-ray on XR glasses, from Linux
#### _Put a 3D Blu-ray in your Linux PC's drive and watch it on XR glasses (VITURE & co.) in real 3D. The PC reads, decrypts and decodes the disc on the fly and serves it over the LAN as a side-by-side video. No rip, no conversion, no disk space._

***Note:*** _Tested with a VITURE Pro XR + VITURE Pro Neckband and its official **3D Player**. Any player able to open videos from a network share (SMB) and show side-by-side 3D should work the same way. Discs tested so far: see [Limits](#limits)._

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
   Glasses: 3D Player ► Local network ► Disks ► movie   → automatic 3D, pause, seek
```

- **Insert the disc, the movie appears.** About 15 seconds after closing the tray the file shows up in the share, named after the disc; eject the disc and it goes away.
- **Normal (2D) Blu-rays and DVDs too.** Same flow: 2D Blu-rays go in the `Blu-ray/` folder, DVDs in `DVD/`, 3D discs in `Blu-ray 3D/`.
- **Nothing is written to disk.** The `.ts` file shows a size of ~22 GB but takes no space: every piece is produced when the player reads it.
- **Seeking works.** The file has a constant bitrate, so every byte matches a precise second of the movie. When the player jumps, the PC restarts reading the disc from there (a few seconds: the optical drive has to move).
- **The glasses see a normal file.** No special app or streaming protocol: the player's own interface, 3D detection, pause and seek.

All the procedures described here are a balanced compromise between "manual" and guided processes. This is one of the possible ways to do it.

## Requirements

#### Hardware
- **A Blu-ray drive** in the Linux PC (internal or USB), which reads DVDs too.
- **A Linux PC** on the same network as the glasses. Decoding MVC is CPU work: a recent multi-core desktop CPU is plenty (on the test PC decoding runs at ~9× real time). Low-power CPUs have not been tested.
- **Optional: an NVIDIA GPU** to encode with NVENC. Without it the video is encoded by the CPU with x264 (on the same CPU still ~6× real time).
- **XR glasses + a player** that opens videos from an SMB network share and plays SBS 3D. Tested: VITURE Pro XR + Pro Neckband, official 3D Player.
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
| Touches the system | no: Samba, FUSE and libraries live in the container | yes: packages, `/opt`, `smb.conf`, `fuse.conf` (removable with `uninstall.sh`) |
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
- [ ] Insert a 3D Blu-ray and check the log:
```console
docker compose logs -f
```
```
watching /dev/sr0: insert a 3D Blu-ray
/dev/sr0: disc inserted, opening it
+ ITA - Tron - Legacy 3D - 3D SBS.ts  (125 min, audio 0x1102 ita dts)
```

Stop it with `docker compose down`. To update: `git pull && docker compose up -d --build`.

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
Stop it with `Ctrl+C`. To update to the latest version: `./scripts/update.sh`. To remove everything: `./scripts/uninstall.sh`.

The script is tested with the real drive and disc in clean Debian 13 (trixie) and Ubuntu 24.04 containers; the program itself runs daily on Debian testing.

---

## Step 2 — Watch it on the glasses

##### From your VITURE Neckband:
- [ ] Open the **3D Player**, go to the **Local network** tab and add the PC: its IP address (`hostname -I` on the PC tells you), guest / anonymous access.
- [ ] Insert the disc in the PC and wait about 15 seconds.
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
- [ ] Open it. The player recognizes the side-by-side format and switches to 3D by itself. 🎉

The first opening and every jump with the seek bar take a few seconds: that is the drive moving to the new point.


##### Other glasses and players
Anything that opens videos from an SMB share and shows SBS 3D should work. Some notes from my tests on the Neckband:
- **VLC** plays it, but you must leave the SpaceWalker interface for Android mode, start the video and _only then_ switch the glasses to 3D mode. It works but it is clumsy, and sometimes the glasses stayed stuck in 3D mode (I had to unplug the cable).
- **XPlayer2** did not work on my Neckband, with any video.
- The **3D Player cannot open a 3D Blu-ray MKV rip**: it does not start at all.

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
- **Playback pauses every few seconds**: the Wi-Fi is too slow for the file: open it from `Light/`, more in [Network and quality](OPTIONS.md#network-and-quality).
- **Nothing happens when inserting the disc**: check that your user can read the drive (`ls -l /dev/sr0`, `cdrom` group), and for Docker that the device is passed to the container.
- **The glasses do not see the share**: check that the PC and the glasses are on the same network, and the firewall (port 445/TCP). From another Linux PC: `smbclient -N -L //<pc-ip>`.
- **The picture stutters**: check the Wi-Fi first (5 GHz, close to the router). Then the log of the last pipeline (`/tmp/bd3d-pipeline.log`, or `docker compose logs`).
- **A new file in `MOVIES_DIR` does not appear**: folders are scanned at startup. Restart the program / the container (drives are watched continuously).
- **After a jump the picture freezes for a few seconds**: that is the drive moving to the new point. [OPTIONS.md](OPTIONS.md#loading-animation-after-a-jump) explains an optional loading animation, and what it costs.

---

## How it works (for the curious)

- **Reading the disc.** libbluray reads the disc (or ISO/BDMV) and decrypts it through libaacs or MakeMKV's libmmbd (`src/bluray.py`, a small ctypes binding). The movie is the longest playlist whose clips all have an MVC dependent view (`src/bdmv.py` reads playlists and clip info).
- **Both views.** The `.ssif` file of a 3D clip interleaves base (PID 0x1011) and dependent (0x1012) view in extents. `src/ssif_demux.py` pairs the two PES packets of each frame on their DTS and writes them as Annex B for edge264. It drops the Blu-ray delimiter, filler and end-of-sequence NAL units, as MakeMKV does. libbluray older than 1.4 cannot open `.ssif` files, so the same bytes are rebuilt from the two `.m2ts` files.
- **edge264-mvc** decodes both views and writes them side by side (`edge264_test -Ok`). FFmpeg alone cannot: it drops the dependent view.
- **Seeking.** A time goes through the EP_map (keyframe positions) and the extent table of the clip to a byte of the `.ssif`. Movies made of several clips (Tron: two) are played in sequence, and the audio timestamps of later clips are moved onto one timeline.
- **Audio.** `src/disc_reader.py` reads the disc once: video to edge264, the chosen audio track (with its language from the playlist) to FFmpeg through a FIFO, each from its own thread. The keyframe is forwarded with the audio as a timing anchor, so audio and video start exactly together. Measured against the MKV path: 4 ms.
- **The virtual file.** The encoder uses **constant bitrate** and the MPEG-TS muxer pads to exactly 24 Mbit/s, so byte _X_ is second _X_ / 3,000,000 of the movie. A **FUSE** file system (`src/bd3d_fs.py`) serves the files. It restarts decoding when the player jumps and pauses the pipeline when it gets too far ahead. Only one pipeline per drive runs at a time, because two would make the optical drive seek back and forth. The end of the file, which players read to get the duration, is synthetic: black and silence with the right timestamps, so the drive is not sent to the end of the disc.
- **DVD.** `src/dvd.py` reads the disc through libdvdread (libdvdcss for CSS) and its IFO files: the main title (the longest), its cells, audio languages, video standard and aspect. Seeking uses the time map to get within a few seconds, then the navigation pack of each VOBU (~0.5 s), which holds its exact time: the restart point is known precisely. FFmpeg's own DVD reader only seeks approximately (seconds off, without knowing where it landed). `src/dvd_reader.py` streams the title from there to FFmpeg, which deinterlaces and scales it to square pixels.
- **Drives.** Every few seconds the drive is asked whether a disc is in (no reads). On insertion it is opened as a Blu-ray, or else as a DVD, and its movie added; on eject it is removed.

---

## Limits

- The last ~2.7 seconds of every movie (after the end credits) are black: the tail of the file is synthetic.
- Discs tested: one per kind (3D: Tron: Legacy; 2D: Ready Player One; DVD: Back to the Future, PAL). Only AACS: BD+ discs (through MakeMKV), discs with unusual structures and NTSC DVDs are untested.
- Subtitles from the disc are drawn into the picture (one version per language), not selectable in the player; the 3D depth is fixed, not taken from the disc. Audio is converted to AAC stereo.

## Windows?

This project is Linux only. On Windows you can look at [**SyLC**](https://github.com/5ymph0en1x/SyLC), an open-source player that plays 3D Blu-ray MVC directly (MKV, ISO, BDMV) and can output SBS. I have not tried it.

## License

[MIT](LICENSE). It covers this project's code only: edge264, libbluray, libaacs, FFmpeg, Samba and the other tools it uses keep their own licenses, and are downloaded or installed from their sources, not shipped here.

## Credits and references

- [edge264-mvc](https://github.com/jens-duttke/edge264-mvc) (BSD), fork of [edge264](https://github.com/tvlabs/edge264): the MVC decoder that makes all this possible;
- [libbluray / libaacs](https://www.videolan.org/developers/libbluray.html), [FFmpeg](https://ffmpeg.org/), [Samba](https://www.samba.org/), [pyfuse3](https://github.com/libfuse/pyfuse3), [MakeMKV](https://www.makemkv.com/);
- [Play 3D Blu-ray in SBS directly from the disc…](https://cybereality.com/play-3d-blu-ray-in-sbs-directly-from-the-disc-for-playback-on-3d-monitors-xr-glasses-and-vr-headsets-using-free-and-open-source-tools/) (cybereality): a Windows approach (LAV Filters + madVR).
