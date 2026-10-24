# build-sim: repeatedly does a clean build of a real C project (zstd 1.5.6).
# Source is downloaded and checksum-verified at IMAGE BUILD time, so experiments need no network.
# Measured on the project VM: ~41s at JOBS=4, ~130s at JOBS=1 — long enough for the
# completion-time-delta evaluation mode (§6), and it scales with parallelism.
FROM alpine:3.20
RUN apk add --no-cache build-base
ARG ZSTD_VERSION=1.5.6
ARG ZSTD_SHA256=8c29e06cf42aacc1eafc4077ae2ec6c6fcb96a626157e0593d5e82a34fd403c1
RUN wget -q -O /tmp/zstd.tgz https://github.com/facebook/zstd/releases/download/v${ZSTD_VERSION}/zstd-${ZSTD_VERSION}.tar.gz \
 && echo "${ZSTD_SHA256}  /tmp/zstd.tgz" | sha256sum -c - \
 && mkdir /src && tar xzf /tmp/zstd.tgz -C /src --strip-components=1 && rm /tmp/zstd.tgz
COPY build_loop.sh /usr/local/bin/build_loop.sh
# JOBS: make -j parallelism (CPU intensity). ITERATIONS: 0 = forever, N = N builds then exit.
# PAUSE_S: idle gap between builds.
ENV JOBS=4 ITERATIONS=0 PAUSE_S=0
LABEL org.opencontainers.image.title="resourcetuner/build-sim"
CMD ["/usr/local/bin/build_loop.sh"]
