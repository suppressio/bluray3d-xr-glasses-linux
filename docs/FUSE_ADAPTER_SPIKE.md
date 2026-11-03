🌐 **English** | [Italiano](FUSE_ADAPTER_SPIKE.it.md)

# FUSE adapter spike: mfusepy vs pyfuse3 on Linux

> Spike for [#129](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/129), approach issue for [#7](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/7) (cross-platform port). Run on a cloud container (Debian sid, AMD Ryzen 9 5900X, 24 threads), not the project's Debian 13/Ubuntu 24.04 target and not real hardware: no optical drive, no glasses, no Samba client. The numbers below bound what a throwaway mount can show; the go/no-go on real hardware is listed under *What's left to a person*.

## Summary

**mfusepy holds up as the Linux FUSE adapter for the #7 port**, with one real gap (`max_read`) that does not matter at this project's bitrate and one open question (thread ordering) that #6 needs to answer anyway. Nothing rules it out.

| | pyfuse3 (today) | mfusepy 3.1.1 |
|---|---|---|
| Sequential read, one stream | ~300 MB/s | ~310 MB/s |
| Two streams at once (start + end of file) | ~625 MB/s aggregate, no serialization | ~620 MB/s aggregate, no serialization |
| Largest single read() call the kernel sent | 1 MiB | 128 KiB (`max_read=` mount option had no effect) |
| `direct_io` (VirtualFile.changing) | honored (already in production) | honored — verified below |
| who opened the file (PID) | the real reader's PID, not a FUSE-internal thread | same, same code path (`/proc/<pid>/status`) |
| threads | trio event loop + thread-pool offload | native libfuse3 multi-threaded loop |
| in Debian/Ubuntu repos | yes (`python3-pyfuse3`) | no (PyPI only) |

Both are three orders of magnitude past the ~3 MB/s a 3D stream needs, so throughput is a non-issue either way. The two things worth carrying into the port:
- **`max_read` is stuck at 128 KiB with mfusepy** in this version: the `max_read=` mount option (the pre-FUSE-3 way to raise it) is silently ignored, and `fuse_conn_info` in this build has no `max_pages`/`max_read` field to set it programmatically. Eight times more `read()` calls for the same bytes, each crossing the ctypes boundary — immaterial at 3 MB/s, but worth re-checking if a future codec needs a much higher bitrate.
- **mfusepy's default multi-threaded loop reordered a single stream's reads often enough to matter**: reading 100 MB sequentially from one mount point restarted `library.py`'s pipeline 32 times (`JUMP_TOLERANCE` tripped by reads arriving at the backend out of the order the player issued them, visible by comparing offsets across worker threads in the read log), against 2 for pyfuse3 doing the same read. `nothreads=True` (one worker, libfuse serializes requests) brought it down to 5 without losing the "two readers do not block each other" property (two *different* open files/streams still progress together; it is same-stream reordering that drops). This is exactly the ordering question [#6](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/6) already has to answer for plain threads; it is not new scope, just flagged here so #6 inherits it instead of rediscovering it.

## What was measured, and how

A throwaway Tree with one `VirtualFile` (`src/library.py`, `src/tree.py`), fed by the same fake pipeline `tests/test_library.py` uses instead of real ffmpeg/a real disc (so production code is untouched and the backing data is fast enough that the front end, not the data source, is the bottleneck). Two mounts, one real `src/fuse_fs.py::Bd3dFS` unmodified and one throwaway `mfusepy.Operations` subclass (path-based, no equivalent exists in the product), both logging every `read()` call (offset, size, caller PID, serving thread, start/end time) to a JSONL file read back for the numbers above. Both ran under `systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=0`; neither came close to the cap (the backing buffer tops out around the 256 MB `AHEAD_MAX` per stream).

Code: `tools/fuse_adapter_spike/` (`common.py` the shared Tree/VirtualFile setup, `serve_pyfuse3.py`, `serve_mfusepy.py`). Kept in the repo as a reproducible probe, same spirit as `tools/bdprobe.c`; not wired into `scripts/check.sh` (it needs `pip install mfusepy==3.1.1`, not a project dependency — see *Packaging* below).

To reproduce:
```bash
.venv/bin/pip install mfusepy==3.1.1   # not in requirements-dev.txt: see Packaging
.venv/bin/python tools/fuse_adapter_spike/serve_pyfuse3.py /tmp/m1 /tmp/m1.readlog &
.venv/bin/python tools/fuse_adapter_spike/serve_mfusepy.py /tmp/m2 /tmp/m2.readlog &
dd if="/tmp/m1/Movie - 3D SBS.ts" bs=1M count=300 of=/dev/null status=progress
fusermount3 -u /tmp/m1; fusermount3 -u /tmp/m2
```

### direct_io, verified with a before/after

`VirtualFile.changing = True` means the same offset can return different bytes on two reads (the loading animation, then the movie); without `direct_io` the kernel would serve the second read from its page cache instead of calling back in. Checked by `pread()`-ing the same (offset, size) five times through one open fd:
- with `direct_io` set (both adapters' normal path): **5/5** calls reached the backend's `read()`, each with the exact offset asked for.
- with it forced off (a control run, `vf.changing = False`): **1** call reached the backend, kernel-page-aligned (`123453440`, a multiple of 4096) and sized to cover the request plus read-ahead; the other four were served from cache.

mfusepy exposes this through `raw_fi=True` (the adapter sets `fi.direct_io = 1` in `open()`, mirroring `Bd3dFS.open()`'s `pyfuse3.FileInfo(direct_io=...)`) and it behaves identically to pyfuse3's already-production path.

### Who opened the file

`fuse_get_context()[2]` (mfusepy) and `RequestContext.pid` (pyfuse3) both give the *kernel's* `fuse_in_header.pid`, which — as `fuse_fs.py`'s `_process()` already documents — is the calling **thread**, not its process group leader; both adapters need the same `/proc/<pid>/status` → `Tgid` lookup `Bd3dFS` already does, and both resolved a `dd` subprocess correctly through it (`pid 326435 (dd)` style) in every run above. Nothing to change in `_process()`'s logic, only where it is called from.

## Packaging

`apt-cache search`/`policy` on Debian sid: no `python3-mfusepy`, at any version — PyPI only (`mfusepy==3.1.1`, pure Python, one file, ctypes bindings to libfuse, ISC license). It needs the same `fuse3` system package Samba sharing already installs (`libfuse3-4`, via `scripts/install.sh`'s `fuse3` package) — nothing new there, only the Python binding changes.

Debian's system Python is externally managed (`pip install` refuses without `--break-system-packages` or a venv), and this project's own dependencies are meant to come from the system (`AGENTS.md`), not pip at install time. Given mfusepy is a single ISC-licensed file with no further Python dependencies, **vendoring it into the repo** (next to `typings/pyfuse3/`, which already vendors stubs for the same reason: packaging friction) reads as the practical choice for the port sub-issues, over pip-at-install-time or waiting for Debian/Ubuntu to package it.

Dropping pyfuse3 drops `python3-pyfuse3` (already a `Recommends` in `scripts/build-deb.sh`) **and `python3-trio`**: `grep -rln "import trio" src/` finds only `fuse_fs.py` (`bd3d_fs.py` dropped trio for plain threads already, commit `a13747a`). One unrelated finding along the way: `python3-trio` is currently a hard `Depends` in `build-deb.sh` even for `--share dlna` installs that never touch `fuse_fs.py` — already wrong today, independent of this spike; listed as a follow-up below rather than fixed here (out of scope: no product/packaging changes in this issue).

## Out of scope here

Confirmed not touched, per the issue: no change to `src/`, no macOS/Windows work (#95, #102). The throwaway code lives entirely under `tools/fuse_adapter_spike/`.

## What's left to a person

- Real hardware: Debian 13/Ubuntu 24.04 (not sid), a real disc, actual 3D playback on the glasses — this spike only had a cloud container and a synthetic backing file.
- Re-run the `max_read`/thread-ordering checks against whatever `src/fuse_fs.py` rewrite the #7 sub-issues produce, since both are measured here against a throwaway adapter, not the real one.
