#!/bin/sh
# One JSON line per build on stdout: ground truth for discrete-workload evaluation (§6).
# Durations use /proc/uptime (monotonic, 10ms resolution): busybox `date` has no %N.
set -u
uptime_s() { cut -d' ' -f1 /proc/uptime; }
i=0
failures=0
while [ "$ITERATIONS" -eq 0 ] || [ "$i" -lt "$ITERATIONS" ]; do
  i=$((i + 1))
  make -s -C /src/programs clean >/dev/null 2>&1
  wall_start=$(date +%s)
  start=$(uptime_s)
  if make -j"$JOBS" -C /src/programs zstd >/dev/null 2>&1; then ok=true; else ok=false; failures=$((failures + 1)); fi
  end=$(uptime_s)
  awk -v i="$i" -v j="$JOBS" -v w="$wall_start" -v s="$start" -v e="$end" -v ok="$ok" \
    'BEGIN { printf "{\"event\":\"build\",\"iteration\":%d,\"jobs\":%d,\"started_at\":%d,\"duration_s\":%.2f,\"success\":%s}\n", i, j, w, e - s, ok }'
  if [ "$PAUSE_S" != "0" ]; then sleep "$PAUSE_S"; fi
done
# Non-zero exit iff any build failed, so `docker wait` reflects build success.
[ "$failures" -eq 0 ]
