# indexer-sim: background code indexer (RAM + I/O heavy). Pure stdlib Python, no pip installs.
FROM python:3.13-slim-bookworm
COPY indexer.py /app/indexer.py
# See indexer.py for the meaning of each knob.
ENV CORPUS_DIR=/corpus CORPUS_MB=256 FILE_KB=64 RETAIN_MB=256 EVICT_CACHE=1 PASS_PAUSE_S=0 SEED=42
LABEL org.opencontainers.image.title="resourcetuner/indexer-sim"
CMD ["python", "-u", "/app/indexer.py"]
