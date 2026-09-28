# Build locally (docker compose build): edge264 is compiled with -march=native
# for the CPU of the machine that builds the image.

FROM debian:trixie-slim AS edge264
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# edge264-mvc: the only open-source decoder of the MVC dependent view (3D Blu-ray)
ARG EDGE264_REPO=https://github.com/jens-duttke/edge264-mvc.git
ARG EDGE264_COMMIT=5757e71f3decbeced1150e742e802d263fbd0df4
RUN git clone "$EDGE264_REPO" /edge264 \
 && git -C /edge264 checkout "$EDGE264_COMMIT" \
 && make -C /edge264 -j"$(nproc)"

FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg python3 python3-pyfuse3 fuse3 samba tini \
 && rm -rf /var/lib/apt/lists/*
# edge264_test finds libedge264.so.1 next to itself (rpath $ORIGIN)
COPY --from=edge264 /edge264/edge264_test /edge264/libedge264.so.1 /usr/local/bin/
COPY src/ /app/
COPY docker/smb.conf /etc/samba/smb.conf
COPY docker/entrypoint.sh /entrypoint.sh
# NVENC (optional): with the NVIDIA Container Toolkit the driver's encoder
# library is mounted into the container; without it x264 (CPU) is used.
ENV NVIDIA_DRIVER_CAPABILITIES=video,utility
EXPOSE 445
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
