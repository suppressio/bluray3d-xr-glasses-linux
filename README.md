🇬🇧 English | [🇮🇹 Italiano](README.it.md)

# 3D Blu-ray on XR glasses, from Linux
#### _Watch your 3D Blu-ray discs on XR glasses (VITURE & co.) in real 3D: the 3D is decoded on the fly by your Linux PC and served over the LAN as a side-by-side video. No conversion, no extra disk space._

> 🎯 **Scope today:** you already have **3D Blu-ray rips in MKV** (MakeMKV keeps the MVC 3D) and want to watch them in 3D **without converting** them. If you only have the disc and do not mind converting, existing tools can rip straight to SBS. **Next goal: play the disc itself**, with no rip at all: see the [roadmap](ROADMAP.md).

***Note:*** _Tested with a VITURE Pro XR + VITURE Pro Neckband and its official **3D Player**. Any player able to open videos from a network share (SMB) and show side-by-side 3D should work the same way._

---

## The problem

A 3D Blu-ray does not contain two videos side by side. It stores the 3D as **MVC** (H.264 _Multiview Video Coding_): a normal 2D video for the left eye plus a "dependent view" with the differences for the right eye.

- XR glasses (and almost every 3D video player) want **SBS**, _side-by-side_: both eyes in one frame.
- Practically **nothing decodes MVC** outside dedicated Blu-ray players: Android and its players cannot, and FFmpeg silently drops the dependent view (you get 2D).
- The usual answer is to **convert** every movie to SBS: hours of encoding and 10-20 GB more for each movie.

## The idea

Your PC decodes the MVC **while you watch** and serves the result to the glasses as if it were an ordinary SBS file.

```
3D Blu-ray ──MakeMKV──► movie.mkv (MVC, the 3D is still there)
                              │
                              ▼   Linux PC, while you watch
   ffmpeg (demux) ─► edge264 (MVC → SBS 3840×1080) ─► encoder (NVENC or x264) + audio
                              │
                              ▼
   "movie - 3D SBS.ts"  a virtual file in an SMB share (it does not exist on disk)
                              │   Wi-Fi
                              ▼
   Glasses: 3D Player ► Local network ► 3D ► movie   → automatic 3D, pause, seek
```

- **Nothing is written to disk.** The `.ts` file shows a size of ~22 GB but takes no space: every piece is decoded when the player reads it.
- **Seeking works.** The file has a constant bitrate, so every byte matches a precise second of the movie. When the player jumps, the PC restarts decoding from there (1-2 seconds).
- **The glasses see a normal file.** No special app or streaming protocol: the player's own interface, 3D detection, pause and seek.

All the procedures described here are a balanced compromise between "manual" and guided processes. This is one of the possible ways to do it.

## Requirements

#### Hardware
- **A Blu-ray drive** that can read your discs (only for ripping; any BD drive supported by MakeMKV).
- **A Linux PC** on the same network as the glasses. Decoding MVC is CPU work: tested on a Ryzen 9 5900X (decoding runs at ~9× real time). Weaker CPUs have not been tested.
- **Optional: an NVIDIA GPU** to encode with NVENC. Without it the video is encoded by the CPU with x264 (on the 5900X still ~6× real time).
- **XR glasses + a player** that opens videos from an SMB network share and plays SBS 3D. Tested: VITURE Pro XR + Pro Neckband, official 3D Player.
- **A good Wi-Fi connection** (5 GHz recommended): the stream is 24 Mbit/s.

#### Software on the PC
- [MakeMKV](https://www.makemkv.com/): rips the Blu-ray into an MKV **keeping the 3D (MVC)**. Linux version available (free while in beta).
- **Docker** (path A) _or_ a **Debian/Ubuntu** system (path B). Everything else is installed for you:
  - [edge264-mvc](https://github.com/jens-duttke/edge264-mvc): the only open-source decoder of the MVC dependent view;
  - FFmpeg, Samba (the network share), FUSE + pyfuse3 (the virtual file).

---

## Step 1 — Rip the Blu-ray to MKV

- [ ] Open the disc in **MakeMKV** and save the main title as MKV.

With default settings MakeMKV keeps the 3D (MVC) data inside the MKV. You do not need to do anything special: the program checks every MKV at startup and **skips the ones without 3D**, with a message in the log.

> ⚖️ Ripping discs you own for personal use is legal in some countries and not in others: check yours.

Put your 3D MKVs in one folder, e.g. `~/Videos/3D`. Subfolders are fine.

---

## Step 2 — Install: choose your path

| | **A. Docker** | **B. Native script** |
|---|---|---|
| For | anyone who already uses Docker | Debian / Ubuntu |
| Touches the system | no: Samba, FUSE and libraries live in the container | yes: packages, `/opt`, `smb.conf`, `fuse.conf` (removable with `uninstall.sh`) |
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
- [ ] Edit `.env`: set `MOVIES_DIR` to your movies folder and `AUDIO_LANG` to your language (`eng`, `ita`, `deu`, `fra`...).
- [ ] Build and start (the first build takes a few minutes: it compiles edge264 for your CPU):
```console
docker compose up -d --build
```
- [ ] Check it found your movies:
```console
docker compose logs
```
```
video encoder: x264
Tron- Legacy 3D_t04 - 3D SBS.ts  (125 min, audio #4 ita dts Surround 5.1)
mounted on /srv/bd3d — Ctrl+C to unmount
```

Stop it with `docker compose down`.

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
It installs the packages, compiles edge264, installs the `bluray3d-xr` command and adds the read-only guest share `[3D]` to Samba. The header of [`scripts/install.sh`](scripts/install.sh) lists every change it makes.

- [ ] If `ufw` is active, the script prints the command to open the share to your LAN, e.g.:
```console
sudo ufw allow from 192.168.1.0/24 to any port 445 proto tcp
```
- [ ] Start it when you want to watch something:
```console
bluray3d-xr --audio-lang eng ~/Videos/3D
```
Stop it with `Ctrl+C`. To remove everything: `./scripts/uninstall.sh`.

The script is tested on clean Debian 13 (trixie) and Ubuntu 24.04 containers; the program runs on my Debian testing PC.

---

## Step 3 — Watch it on the glasses

##### From your VITURE Neckband:
- [ ] Open the **3D Player**, go to the **Local network** tab and add the PC: its IP address (`hostname -I` on the PC tells you), guest / anonymous access.
- [ ] Open the **3D** folder: every movie is there as `<movie> - 3D SBS.ts`.
- [ ] Open it. The player recognizes the side-by-side format and switches to 3D by itself. 🎉

The first opening and every jump with the seek bar take 1-2 seconds: that is the PC restarting decoding from the new point.

**PLEASE NOTE!** In some movies part of the scenes are 2D on purpose (in _Tron: Legacy_, the "real world" parts). There the two eyes get the same picture: it is not a bug.

##### Other glasses and players
Anything that opens videos from an SMB share and shows SBS 3D should work. Some notes from my tests on the Neckband:
- **VLC** plays it, but you must leave the SpaceWalker interface for Android mode, start the video and _only then_ switch the glasses to 3D mode. It works but it is clumsy, and sometimes the glasses stayed stuck in 3D mode (I had to unplug the cable).
- **XPlayer2** did not work on my Neckband, with any video.
- The **3D Player cannot open the original MKV**: it does not start at all. This is why the project exists.

---

## Options

| Option | `.env` (Docker) | Command line (native) | Default |
|---|---|---|---|
| Movies folder(s) | `MOVIES_DIR` | positional arguments | — |
| Audio languages | `AUDIO_LANG` | `--audio-lang ita,eng` | first track |
| One file per language, one with all, or both | `AUDIO_FILES` | `--audio-files per-language\|single\|both` | `per-language` |
| Video encoder | `ENCODER` | `--encoder auto\|nvenc\|x264` | `auto` (NVENC if available) |
| Mount point | — | `--mount` | `/srv/bd3d` |
| Pipeline log | — | `--log-file` | `/tmp/bd3d-pipeline.log` |

#### Audio languages and subtitles
- **Several languages**: with `--audio-lang ita,eng` each language becomes **its own file**
  (`ITA - movie - 3D SBS.ts`, `ENG - movie - 3D SBS.ts`; the language comes first because players cut long names). The VITURE 3D Player has no audio track menu
  and picks a track on its own, so this is the default. If your player does have an audio
  menu, `--audio-files single` puts all languages in one file. `both` offers both at once:
  the per-language files, plus the all-languages file in a `Multi-audio/` folder. Useful
  with several devices; the files are virtual, so the extra ones cost nothing.
- **Subtitles**: put subtitle files next to the MKV with the same name
  (`movie.srt`, `movie.ita.srt`, also `.ass`, `.sup`...). They show up next to every
  virtual video with the matching name, and the player loads them as external subtitles.
  Whether they are shown correctly in 3D is up to the player: the VITURE 3D Player loads
  them.

Output format: H.264 Full-SBS 3840×1080 at 20 Mbit/s CBR, AAC stereo 192 kbit/s per language, in a 24 Mbit/s MPEG-TS. Audio is converted to stereo (DTS/TrueHD are not supported by most mobile players).

---

## Troubleshooting

- **A movie is missing, the log says `skipped (no MVC 3D video)`**: that MKV has no 3D data. It is a 2D rip, or the rip lost the MVC stream: rip it again with MakeMKV.
- **The glasses do not see the share**: check that the PC and the glasses are on the same network, and the firewall (port 445/TCP). From another Linux PC: `smbclient -N -L //<pc-ip>`.
- **The picture stutters**: check the Wi-Fi first (5 GHz, close to the router). Then the log of the last pipeline (`/tmp/bd3d-pipeline.log`, or `docker compose logs`).
- **A new movie does not appear**: the folder is scanned at startup. Restart the program / the container.

---

## How it works (for the curious)

- **edge264-mvc** decodes both views of the MVC stream and writes them side by side (`edge264_test -Ok`) as raw frames. FFmpeg alone cannot do it: it drops the dependent view.
- The encoder uses **constant bitrate** and the MPEG-TS muxer pads to exactly 24 Mbit/s (`-muxrate`). So byte _X_ of the virtual file is second _X_ / 3,000,000 of the movie.
- A small **FUSE** file system (`src/bd3d_fs.py`) exposes the files. Sequential reads continue from the running pipeline, which pauses itself when it gets too far ahead of the player. A jump restarts the pipeline from the previous keyframe. The first and last 8 MB stay cached, because players re-read them for headers and duration.
- **A/V sync detail**: when seeking in formats with B-frames, FFmpeg moves the seek point back by 3/23 s. Asking for exactly keyframe _K_ lands on the keyframe before it and the picture ends up ~1 s behind the audio. The video is therefore asked for _K_ + 0.2 s and the audio for exactly _K_ (see `src/pipeline.py`). Measured result: 1 ms.

---

## Windows?

This project is Linux only. On Windows you can look at [**SyLC**](https://github.com/5ymph0en1x/SyLC), an open-source player that plays 3D Blu-ray MVC directly (MKV, ISO, BDMV) and can output SBS. I have not tried it.

## Credits and references

- [edge264-mvc](https://github.com/jens-duttke/edge264-mvc) (BSD), fork of [edge264](https://github.com/tvlabs/edge264): the MVC decoder that makes all this possible;
- [FFmpeg](https://ffmpeg.org/), [Samba](https://www.samba.org/), [pyfuse3](https://github.com/libfuse/pyfuse3), [MakeMKV](https://www.makemkv.com/);
- [Play 3D Blu-ray in SBS directly from the disc…](https://cybereality.com/play-3d-blu-ray-in-sbs-directly-from-the-disc-for-playback-on-3d-monitors-xr-glasses-and-vr-headsets-using-free-and-open-source-tools/) (cybereality): a similar goal, approached differently.
