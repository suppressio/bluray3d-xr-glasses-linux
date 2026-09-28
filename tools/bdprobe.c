/* Roadmap phase 0 probe: open a 3D Blu-ray (disc, ISO or BDMV folder) with
 * libbluray, report protection and titles, and dump the start of the main
 * title's SSIF (decrypted, if a decryption backend works) for ffprobe.
 *
 * Build (needs libbluray-dev):  gcc -O1 -o bdprobe bdprobe.c -lbluray
 * Run:  ./bdprobe /dev/sr0 ssif_head.ts 64
 *       LIBAACS_PATH=libmmbd LIBBDPLUS_PATH=libmmbd ./bdprobe /dev/sr0 ssif_head.ts 64
 * Then: ffprobe ssif_head.ts   (expect H.264 PIDs 0x1011 base + 0x1012 dependent) */
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>
#include <libbluray/bluray.h>
#include <libbluray/filesystem.h>

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <device|iso|dir> <out.ts> [MB]\n", argv[0]); return 2; }
    long mb = argc > 3 ? atol(argv[3]) : 64;
    BLURAY *bd = bd_open(argv[1], NULL);
    if (!bd) { fprintf(stderr, "bd_open failed\n"); return 1; }
    const BLURAY_DISC_INFO *di = bd_get_disc_info(bd);
    printf("bluray_detected=%d name=%s 3D=%d\n", di->bluray_detected,
           di->disc_name ? di->disc_name : "-", di->content_exist_3D);
    printf("AACS detected=%d lib=%d handled=%d error=%d mkbv=%d\n", di->aacs_detected,
           di->libaacs_detected, di->aacs_handled, di->aacs_error_code, di->aacs_mkbv);
    printf("BD+  detected=%d lib=%d handled=%d\n", di->bdplus_detected,
           di->libbdplus_detected, di->bdplus_handled);

    uint32_t n = bd_get_titles(bd, TITLES_RELEVANT, 0);
    printf("titles: %u\n", n);
    uint32_t best = 0; uint64_t best_len = 0;
    for (uint32_t i = 0; i < n; i++) {
        BLURAY_TITLE_INFO *ti = bd_get_title_info(bd, i, 0);
        if (!ti) continue;
        if (ti->duration > best_len) { best_len = ti->duration; best = i; }
        if (ti->duration / 90000 >= 600) {
            printf("  #%u playlist %05u  %3" PRIu64 " min  clips=%u  mvc_base_view_r=%d  first clip %s\n",
                   i, ti->playlist, ti->duration / 90000 / 60, ti->clip_count,
                   ti->mvc_base_view_r_flag, ti->clip_count ? ti->clips[0].clip_id : "-");
        }
        bd_free_title_info(ti);
    }
    BLURAY_TITLE_INFO *ti = bd_get_title_info(bd, best, 0);
    printf("main title #%u playlist %05u, %u clips:", best, ti->playlist, ti->clip_count);
    for (uint32_t c = 0; c < ti->clip_count && c < 12; c++) printf(" %s", ti->clips[c].clip_id);
    printf("\n");

    char path[64];
    snprintf(path, sizeof path, "BDMV/STREAM/SSIF/%s.ssif", ti->clips[0].clip_id);
    BD_FILE_H *fp = bd_open_file_dec(bd, path);
    if (!fp) { printf("cannot open %s decrypted\n", path); return 1; }
    FILE *out = fopen(argv[2], "wb");
    uint8_t buf[6144];   /* the decrypting reader takes exactly one AACS unit per call */
    int64_t total = 0;
    while (total < mb * 1024 * 1024) {
        int64_t r = fp->read(fp, buf, sizeof buf);
        if (r <= 0) break;
        fwrite(buf, 1, r, out); total += r;
    }
    fclose(out); fp->close(fp);
    printf("read %" PRId64 " bytes from %s\n", total, path);
    bd_free_title_info(ti);
    bd_close(bd);
    return 0;
}
