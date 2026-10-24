# test-sim: TESTING workload — runs a real pytest suite repeatedly (CPU + RAM heavy).
# pytest/pytest-xdist are installed at IMAGE BUILD time, so experiments need no network.
FROM python:3.13-slim-bookworm
RUN pip install --no-cache-dir pytest==8.3.4 pytest-xdist==3.6.1
COPY suite /suite
COPY runner.py /app/runner.py
# WORKERS: pytest-xdist workers (CPU intensity; 0 = serial). TEST_COUNT: cases per test.
# TEST_SCALE: work per case. MB_PER_WORKER: dataset each worker holds (RAM knob).
# ITERATIONS: 0 = forever, N = N suite runs then exit. PAUSE_S: gap between runs.
ENV WORKERS=4 TEST_COUNT=100 TEST_SCALE=1 MB_PER_WORKER=256 ITERATIONS=0 PAUSE_S=0
LABEL org.opencontainers.image.title="resourcetuner/test-sim"
CMD ["python", "-u", "/app/runner.py"]
