# Build locally (docker compose build): edge264 is compiled with -march=native
# for the CPU of the machine that builds the image.

FROM debian:trixie-slim AS edge264
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# edge264-mvc: the only open-source decoder of the MVC dependent view (3D Blu-ray)
ARG EDGE264_REPO=https://github.com/jens-duttke/edge264-mvc.git
# edge264-mvc release v2026.09.22
ARG EDGE264_COMMIT=44e30b66d43a17418e0b342eba54d69d0c3be829
RUN git clone "$EDGE264_REPO" /edge264 \
 && git -C /edge264 checkout "$EDGE264_COMMIT" \
 && make -C /edge264 -j"$(nproc)"

# the release (git tag) being built, e.g. v1.0.0, for bluray3d-xr --version and the log;
# its own stage, so a new commit does not rebuild edge264
FROM edge264 AS version
COPY .git /repo/.git
RUN git -c safe.directory='*' -C /repo describe --tags --always > /version 2>/dev/null \
 || echo unknown > /version

FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg python3 python3-pyfuse3 fuse3 samba tini libbluray2 libaacs0 libbdplus0 libdvdread8 \
 && rm -rf /var/lib/apt/lists/*
# edge264_test finds libedge264.so.1 next to itself (rpath $ORIGIN)
COPY --from=edge264 /edge264/edge264_test /edge264/libedge264.so.1 /usr/local/bin/
COPY src/ /app/
COPY --from=version /version /app/version
COPY docker/smb.conf /etc/samba/smb.conf
COPY docker/entrypoint.sh /entrypoint.sh
# NVENC (optional): with the NVIDIA Container Toolkit the driver's encoder
# library is mounted into the container; without it x264 (CPU) is used.
ENV NVIDIA_DRIVER_CAPABILITIES=video,utility
EXPOSE 445
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
