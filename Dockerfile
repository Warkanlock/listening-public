# The core image supports local media on amd64 and arm64. The default
# full target also bundles checksum-verified Sockseek on supported
# architectures. Sockseek's Linux ARM asset is 32-bit armhf.

FROM python:3.12-slim-bookworm AS sockseek
ARG TARGETARCH
COPY scripts/provision_sockseek.py /tmp/tree/scripts/provision_sockseek.py
COPY packaging/sockseek-pin.json /tmp/tree/packaging/sockseek-pin.json
RUN case "$TARGETARCH" in \
      amd64) KEY=linux-x64 ;; \
      arm) KEY=linux-arm ;; \
      arm64) echo "Sockseek has no arm64 Linux release. Build the Listening Room core target (--target core)." >&2; exit 1 ;; \
      *) echo "Unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && python3 /tmp/tree/scripts/provision_sockseek.py --app-dir /opt/sockseek-app --platform-key "$KEY"

# The frontend stage mirrors the checkout's own build (the
# Makefile's install/build targets): yarn classic against the
# frozen lockfile, then `yarn build`, whose Vite outDir is
# ../dist relative to frontend/react, landing at
# /build/frontend/dist in this stage's tree.
FROM node:22-slim AS frontend
RUN npm install --prefix /opt/listening-yarn yarn@1.22.22
ENV PATH=/opt/listening-yarn/node_modules/.bin:$PATH
WORKDIR /build
COPY frontend/react/ frontend/react/
RUN yarn --cwd frontend/react install --frozen-lockfile \
    && yarn --cwd frontend/react build

FROM python:3.12-slim-bookworm AS core
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY listening pyproject.toml LICENSE THIRD_PARTY.md ./
COPY backend/ backend/
COPY --from=frontend /build/frontend/dist/ frontend/dist/
COPY examples/ examples/
COPY scripts/ scripts/
COPY packaging/ packaging/
RUN useradd --create-home --uid 1000 listening \
    && chmod -R a+rX /app \
    && mkdir -p /config /library \
    && chown -R listening:listening /config /library
ARG LISTENING_ROOM_REVISION=""
ENV LISTENING_ROOM_CONFIG=/config/listening-room.json \
    OFFLINE_MUSIC_ROOT=/library \
    LISTENING_ROOM_INSTALL_KIND=docker \
    LISTENING_ROOM_IMAGE_REVISION=${LISTENING_ROOM_REVISION}
LABEL org.opencontainers.image.revision=${LISTENING_ROOM_REVISION}
USER listening
EXPOSE 8765
CMD ["/app/listening", "server", "start", "--foreground"]

FROM core AS full
COPY --from=sockseek /opt/sockseek-app/bin/sockseek /opt/sockseek/sockseek
ENV LISTENING_ROOM_SOCKSEEK_BINARY=/opt/sockseek/sockseek
