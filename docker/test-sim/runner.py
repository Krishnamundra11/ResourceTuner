"""Run the pytest suite repeatedly; one JSON line per run (ground truth for discrete evaluation, §6).

Exit code is non-zero iff any run had failures, so `docker wait` reflects suite success.
"""

import json
import os
import re
import subprocess
import time

WORKERS = int(os.environ.get("WORKERS", "4"))
ITERATIONS = int(os.environ.get("ITERATIONS", "0"))
PAUSE_S = float(os.environ.get("PAUSE_S", "0"))

cmd = ["pytest", "-q", "-p", "no:cacheprovider", "/suite"]
if WORKERS > 0:
    cmd[1:1] = ["-n", str(WORKERS)]

any_failed = False
i = 0
while ITERATIONS == 0 or i < ITERATIONS:
    i += 1
    wall_start = time.time()
    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    duration = time.monotonic() - start
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|error)", tail)}
    ok = proc.returncode == 0
    any_failed |= not ok
    print(json.dumps({"event": "test_run", "iteration": i, "workers": WORKERS, "started_at": int(wall_start),
                      "duration_s": round(duration, 2), "passed": counts.get("passed", 0),
                      "failed": counts.get("failed", 0) + counts.get("error", 0), "success": ok}), flush=True)
    if PAUSE_S > 0:
        time.sleep(PAUSE_S)

raise SystemExit(1 if any_failed else 0)
