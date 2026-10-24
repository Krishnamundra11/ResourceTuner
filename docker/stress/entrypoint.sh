#!/bin/sh
# Tunable stress-ng hogs. All knobs are env vars so scenarios can set intensity levels.
set -eu
case "$MODE" in
  cpu)
    # CPU_WORKERS: busy workers (~cores used); CPU_LOAD: % load per worker (0-100).
    exec stress-ng --cpu "${CPU_WORKERS:-2}" --cpu-load "${CPU_LOAD:-100}" \
         --cpu-method matrixprod --timeout 0 ;;
  ram)
    # VM_WORKERS x VM_BYTES of resident memory. With VM_HANG=0 (default) stress-ng allocates,
    # touches every page once, then holds it forever with ~0% CPU, so this hog looks like RAM
    # contention only, not CPU (measured: 512M -> 516MiB working set, 0% CPU).
    # VM_HANG=N > 0: hold N seconds, then unmap and re-allocate (periodic churn, uses CPU).
    exec stress-ng --vm "${VM_WORKERS:-1}" --vm-bytes "${VM_BYTES:-512M}" --vm-keep \
         --vm-hang "${VM_HANG:-0}" --timeout 0 ;;
  io)
    # HDD_WORKERS writers of HDD_BYTES each. O_DIRECT by default so I/O hits the disk
    # instead of the page cache.
    mkdir -p /scratch
    exec stress-ng --hdd "${HDD_WORKERS:-1}" --hdd-bytes "${HDD_BYTES:-512M}" \
         --hdd-opts "${HDD_OPTS:-direct}" --temp-path /scratch --timeout 0 ;;
  *)
    echo "unknown MODE=$MODE (expected cpu|ram|io)" >&2; exit 2 ;;
esac
