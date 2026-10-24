# One stress-ng image source, built three times as cpu-hog / ram-hog / io-hog
# (docker build --build-arg MODE=cpu|ram|io). Each tag defaults to its own mode;
# intensity is controlled by env vars (see entrypoint.sh).
FROM alpine:3.20
RUN apk add --no-cache stress-ng
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
ARG MODE=cpu
ENV MODE=${MODE}
LABEL org.opencontainers.image.title="resourcetuner/${MODE}-hog"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
