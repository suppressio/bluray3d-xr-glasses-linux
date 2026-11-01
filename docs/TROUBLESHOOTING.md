🌐 **English** | [Italiano](TROUBLESHOOTING.it.md)

# Troubleshooting

> User guide, current: known problems with their fix, and how to report a new one.

Back to the [README](../README.md).

- **The movie does not appear, the log says `cannot decrypt`**: no working key for that disc. Update your `KEYDB.cfg`, or install and register MakeMKV (native path).
- **A Blu-ray does not appear, the log says `BD+ disc: not decrypted`**: the disc has BD+ copy protection and libbdplus could not start: its configuration `~/.config/bdplus/vm0/` is missing (see [Decryption](../README.md#decryption-bring-your-own-keys)). MakeMKV can open the disc, but its fixes go wrong after a jump in the movie, so it is not used for BD+.
- **A Blu-ray does not appear, the log says `BD+ disc: no conversion table`**: libbdplus starts, but `~/.config/bdplus/convtab/` has no table for this disc. Without it libbdplus would run the disc's BD+ program itself, which fails on newer discs and breaks up the picture, so the disc is skipped. Add the archive updates that include your disc.
- **A BD+ movie plays but breaks up after a jump**: `libbdplus.so.0` on your system may be a link to MakeMKV's `libmmbd` (an old MakeMKV recipe). The program cannot tell; remove the link and reinstall libbdplus.
- **A DVD does not appear, the log says `encrypted (CSS) and libdvdcss is missing`**: install libdvdcss (see [Decryption](../README.md#decryption-bring-your-own-keys)).
- **The picture freezes for about 20 seconds after closing the program while watching**: the player waits before giving up on the file that disappeared (the program itself stops in a second). Stop the video on the glasses first, then the program.
- **Playback pauses every few seconds**: the log says whether the network is too slow for the file (then open it from `Light/`) or the disc cannot keep up (a scratch or a fingerprint: clean it); more in [Network and quality](OPTIONS.md#network-and-quality).
- **Nothing happens when inserting the disc**: check that the drive really is `/dev/sr0` (`lsblk -d -o NAME,MODEL | grep sr`), that your user can read it (`ls -l /dev/sr0`, `cdrom` group), and for Docker that the device is passed to the container.
- **The glasses do not see the share**: check that the PC and the glasses are on the same network, and the firewall (port 445/TCP). From another Linux PC: `smbclient -N -L //<pc-ip>`.
- **The picture stutters**: check the Wi-Fi first (5 GHz, close to the router). Then the pipeline log (`/tmp/bd3d-pipeline.log`, or `docker compose logs`).
- **A new file in `MOVIES_DIR` does not appear**: folders are scanned at startup. Restart the program / the container (drives are watched continuously).
- **After a jump a loading animation plays for a few seconds, and the movie starts a little after the point you chose**: the drive is moving to the new point. [docs/OPTIONS.md](OPTIONS.md#loading-animation-after-a-jump) explains why, and how to turn it off.

Reporting a problem: [open an issue](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/new/choose) with the **Something does not work** form. It asks for the version (`bluray3d-xr --version`), the disc, the player and the program's log around the problem (it is saved in `~/.local/state/bluray3d-xr/bluray3d-xr.log`); if you can make it happen again, run the program with `--debug`. Never paste keys.
