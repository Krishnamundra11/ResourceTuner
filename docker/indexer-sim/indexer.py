"""indexer-sim: a synthetic background code indexer (RAM + I/O heavy).

Mimics what IDE indexers (clangd, ctags, language servers) do: repeatedly walk a source
tree, read every file, hash it, tokenize it, and maintain an in-memory index plus a
cache of recently-read file contents, periodically persisting the index to disk.

Knobs (env vars):
  CORPUS_DIR    where the synthetic source tree lives (mount a volume to reuse it across runs)
  CORPUS_MB     total corpus size to generate (I/O volume per pass)
  FILE_KB       size of each generated file
  RETAIN_MB     in-memory cache of file contents (the RAM knob; resident once warmed up)
  EVICT_CACHE   1 = posix_fadvise(DONTNEED) after each read, so every pass re-reads from
                disk instead of the page cache (no root needed); 0 = allow cache hits
  PASS_PAUSE_S  idle gap between passes
  SEED          corpus generation seed (same seed + size = identical corpus = repeatable runs)

Emits one JSON line per pass on stdout.

Concurrency note (multi-replica design):
  Multiple replicas of this container share the same CORPUS_DIR volume.  Each replica
  therefore competes with peers when persisting the per-pass index.  We avoid corruption
  by using a **process-unique** temporary file (index.json.<pid>.tmp) instead of the
  shared "index.json.tmp".  The final atomic os.replace() step then lands on the shared
  "index.json" destination; whichever replica wins the race writes a fully-fsynced,
  complete index — any other replica's result is equally valid (they all index the same
  corpus).  Stale tmp files left by a previously-killed process are cleaned up at startup.
"""

import hashlib
import json
import os
import random
import re
import resource
import tempfile
import time
from collections import Counter, OrderedDict

CORPUS_DIR = os.environ.get("CORPUS_DIR", "/corpus")
CORPUS_MB = int(os.environ.get("CORPUS_MB", "256"))
FILE_KB = int(os.environ.get("FILE_KB", "64"))
RETAIN_MB = int(os.environ.get("RETAIN_MB", "256"))
EVICT_CACHE = os.environ.get("EVICT_CACHE", "1") == "1"
PASS_PAUSE_S = float(os.environ.get("PASS_PAUSE_S", "0"))
SEED = int(os.environ.get("SEED", "42"))

TOKEN_RE = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{2,}")
FILES_PER_DIR = 100
SRC_DIR = os.path.join(CORPUS_DIR, "src")
INDEX_DIR = os.path.join(CORPUS_DIR, ".index")
MANIFEST = os.path.join(CORPUS_DIR, "manifest.json")
INDEX_FINAL = os.path.join(INDEX_DIR, "index.json")

# Process-unique tmp filename: survives concurrent replicas on the same shared volume.
# Each process uses its own name, so writes never clobber each other's in-flight tmp.
_PID = os.getpid()
INDEX_TMP = os.path.join(INDEX_DIR, f"index.json.{_PID}.tmp")


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, "ts": round(time.time(), 3), **fields}), flush=True)


def corpus_params() -> dict:
    return {"corpus_mb": CORPUS_MB, "file_kb": FILE_KB, "seed": SEED}


def ensure_corpus() -> list[str]:
    """Generate the source tree once; reuse it if the manifest matches the parameters."""
    n_files = max(1, CORPUS_MB * 1024 // FILE_KB)
    paths = [os.path.join(SRC_DIR, f"d{i // FILES_PER_DIR:04d}", f"f{i:06d}.c") for i in range(n_files)]
    try:
        with open(MANIFEST) as f:
            if json.load(f) == corpus_params():
                return paths
    except (OSError, ValueError):
        pass

    start = time.monotonic()
    rng = random.Random(SEED)
    vocab = ["".join(rng.choices("abcdefghijklmnopqrstuvwxyz_", k=rng.randint(4, 14))) for _ in range(20000)]
    keywords = ["int", "static", "return", "struct", "const", "void", "if", "for", "while"]
    target = FILE_KB * 1024
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines, size = [], 0
        while size < target:
            words = rng.choices(vocab, k=6)
            line = f"{rng.choice(keywords)} {words[0]}({words[1]} {words[2]}) {{ {words[3]} = {words[4]}_{words[5]}; }}\n"
            lines.append(line)
            size += len(line)
        with open(path, "w") as f:
            f.write("".join(lines)[:target])
    with open(MANIFEST, "w") as f:
        json.dump(corpus_params(), f)
    emit("corpus_generated", files=n_files, mb=CORPUS_MB, duration_s=round(time.monotonic() - start, 2))
    return paths


def cleanup_stale_tmps() -> None:
    """Remove tmp files left by previously-killed replicas.

    Tmp files match 'index.json.*.tmp'.  We skip files whose PID is still alive
    (another running replica), which is a best-effort check: if the pid has been
    recycled we may clean a file that belongs to a live-but-PID-recycled process.
    In that edge case the worst outcome is that replica's next os.replace() sees
    FileNotFoundError, logs a warning, and continues — it never corrupts the final
    index because os.replace() is atomic.
    """
    if not os.path.isdir(INDEX_DIR):
        return
    for name in os.listdir(INDEX_DIR):
        if not (name.startswith("index.json.") and name.endswith(".tmp")):
            continue
        try:
            # name is "index.json.<pid>.tmp"
            pid_str = name[len("index.json."):-len(".tmp")]
            pid = int(pid_str)
        except ValueError:
            continue  # unexpected format, leave it alone
        if pid == _PID:
            continue  # ours — don't touch it
        try:
            os.kill(pid, 0)  # 0 = just check existence; no signal sent
            # process is alive → leave the file
        except ProcessLookupError:
            # pid is dead → safe to remove the stale tmp
            try:
                os.unlink(os.path.join(INDEX_DIR, name))
            except OSError:
                pass  # another replica beat us to it; harmless


def read_file(path: str) -> bytes:
    fd = os.open(path, os.O_RDONLY)
    try:
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        if EVICT_CACHE:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        return b"".join(chunks)
    finally:
        os.close(fd)


def persist_index(hashes: dict, doc_freq: Counter) -> None:
    """Write the index to a process-unique tmp file, fsync, then atomically replace.

    The final destination (index.json) is always a complete, fsynced document.
    A concurrent replica overwriting it with its own result is harmless — both
    replicas index the same corpus files and produce equivalent results.
    """
    data = json.dumps({
        "files": hashes,
        "top_tokens": [(t.decode(), c) for t, c in doc_freq.most_common(5000)],
    })
    # Write to our process-unique tmp; no other replica uses this name.
    try:
        with open(INDEX_TMP, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(INDEX_TMP, INDEX_FINAL)
    except OSError as e:
        # Log the error and continue — a missed persist is not fatal for a simulator.
        emit("persist_error", pid=_PID, error=str(e))
        # Clean up our own tmp if it still exists
        try:
            os.unlink(INDEX_TMP)
        except OSError:
            pass


def main() -> None:
    paths = ensure_corpus()
    os.makedirs(INDEX_DIR, exist_ok=True)
    cleanup_stale_tmps()

    retain_budget = RETAIN_MB * 1024 * 1024
    cache: OrderedDict[str, bytes] = OrderedDict()  # LRU of file contents (the RAM knob)
    cache_bytes = 0
    pass_no = 0
    while True:
        pass_no += 1
        start = time.monotonic()
        doc_freq: Counter[bytes] = Counter()
        hashes: dict[str, str] = {}
        bytes_read = 0
        for path in paths:
            data = read_file(path)
            bytes_read += len(data)
            hashes[path] = hashlib.sha256(data).hexdigest()
            doc_freq.update(set(TOKEN_RE.findall(data)))
            if path in cache:
                cache_bytes -= len(cache.pop(path))
            cache[path] = data
            cache_bytes += len(data)
            while cache_bytes > retain_budget and cache:
                cache_bytes -= len(cache.popitem(last=False)[1])

        # Persist the index atomically using a process-unique tmp file.
        persist_index(hashes, doc_freq)

        emit("index_pass", iteration=pass_no, files=len(paths), mb_read=round(bytes_read / 2**20, 1),
             duration_s=round(time.monotonic() - start, 2), unique_tokens=len(doc_freq),
             retained_mb=round(cache_bytes / 2**20, 1),
             max_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1))
        if PASS_PAUSE_S > 0:
            time.sleep(PASS_PAUSE_S)


if __name__ == "__main__":
    main()
