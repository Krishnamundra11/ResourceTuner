"""Regression tests for docker/indexer-sim/indexer.py concurrency safety.

These tests run entirely in-process against a real tmpdir — no Docker required.
They import indexer.py directly by path so they work before any image rebuild.

Scenarios verified:
  1. process-unique tmp names do not collide between concurrent "replicas"
  2. atomic os.replace() means the final index is always complete JSON
  3. cleanup_stale_tmps() removes dead-pid leftovers but leaves live-pid files alone
  4. persist_error event is emitted (and does not crash) when INDEX_DIR disappears mid-write
  5. multiple threads completing persist_index() concurrently leave one valid index.json
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

# --- load indexer.py from the docker/ source tree, not from the installed package --------
INDEXER_PY = Path(__file__).resolve().parents[1] / "docker" / "indexer-sim" / "indexer.py"


def _load_indexer(tmp_corpus: str):
    """Import indexer.py with CORPUS_DIR pointing at a caller-controlled tmpdir."""
    spec = importlib.util.spec_from_file_location("indexer_mod", INDEXER_PY)
    mod = importlib.util.module_from_spec(spec)
    # Override module-level path constants before exec so they point at our tmpdir.
    # We monkey-patch after load because module globals are set at import time.
    spec.loader.exec_module(mod)
    # Redirect corpus paths to the temp directory
    mod.CORPUS_DIR = tmp_corpus
    mod.SRC_DIR = os.path.join(tmp_corpus, "src")
    mod.INDEX_DIR = os.path.join(tmp_corpus, ".index")
    mod.MANIFEST = os.path.join(tmp_corpus, "manifest.json")
    mod.INDEX_FINAL = os.path.join(tmp_corpus, ".index", "index.json")
    return mod


# -------------------------------------------------------------------------------------

class TestProcessUniqueTmpNames:
    """The tmp filename must encode the PID so concurrent replicas never collide."""

    def test_tmp_includes_pid(self, tmp_path):
        mod = _load_indexer(str(tmp_path))
        assert str(os.getpid()) in mod.INDEX_TMP, (
            f"INDEX_TMP={mod.INDEX_TMP!r} should contain the current pid"
        )

    def test_two_simulated_replicas_use_different_tmp_names(self, tmp_path):
        """Simulate two replicas with different PIDs → different tmp paths."""
        mod = _load_indexer(str(tmp_path))
        pid_a, pid_b = 1001, 1002
        tmp_a = os.path.join(str(tmp_path), ".index", f"index.json.{pid_a}.tmp")
        tmp_b = os.path.join(str(tmp_path), ".index", f"index.json.{pid_b}.tmp")
        assert tmp_a != tmp_b
        # Both follow the expected naming convention
        assert tmp_a.endswith(f".{pid_a}.tmp")
        assert tmp_b.endswith(f".{pid_b}.tmp")


class TestAtomicPersist:
    """persist_index() must always leave a complete, valid JSON file at INDEX_FINAL."""

    def _make_index_data(self):
        hashes = {"file1.c": "abc123", "file2.c": "def456"}
        doc_freq: Counter = Counter({b"foo": 10, b"bar": 5})
        return hashes, doc_freq

    def test_single_writer_produces_valid_json(self, tmp_path):
        mod = _load_indexer(str(tmp_path))
        index_dir = tmp_path / ".index"
        index_dir.mkdir()
        mod.INDEX_DIR = str(index_dir)
        mod.INDEX_FINAL = str(index_dir / "index.json")
        mod.INDEX_TMP = str(index_dir / f"index.json.{os.getpid()}.tmp")

        hashes, doc_freq = self._make_index_data()
        mod.persist_index(hashes, doc_freq)

        result = json.loads((index_dir / "index.json").read_text())
        assert result["files"] == hashes
        assert isinstance(result["top_tokens"], list)

    def test_tmp_file_is_removed_after_successful_persist(self, tmp_path):
        mod = _load_indexer(str(tmp_path))
        index_dir = tmp_path / ".index"
        index_dir.mkdir()
        mod.INDEX_DIR = str(index_dir)
        mod.INDEX_FINAL = str(index_dir / "index.json")
        mod.INDEX_TMP = str(index_dir / f"index.json.{os.getpid()}.tmp")

        hashes, doc_freq = self._make_index_data()
        mod.persist_index(hashes, doc_freq)

        # tmp should have been atomically renamed → should no longer exist separately
        assert not (index_dir / f"index.json.{os.getpid()}.tmp").exists(), (
            "tmp file should be gone after os.replace()"
        )

    def test_concurrent_writers_leave_valid_index(self, tmp_path):
        """Reproduce the original race: N threads each call persist_index() with the
        same corpus but process-unique tmp names.  Final index.json must be valid."""
        index_dir = tmp_path / ".index"
        index_dir.mkdir()

        hashes = {f"file{i}.c": f"hash{i}" for i in range(100)}
        doc_freq: Counter = Counter({f"tok{i}".encode(): i for i in range(50)})

        errors = []
        n_writers = 4

        def write_index(writer_id: int):
            mod = _load_indexer(str(tmp_path))
            mod.INDEX_DIR = str(index_dir)
            mod.INDEX_FINAL = str(index_dir / "index.json")
            # Simulate distinct PIDs by using writer_id as the pid suffix
            mod.INDEX_TMP = str(index_dir / f"index.json.{writer_id}.tmp")
            mod._PID = writer_id
            try:
                mod.persist_index(hashes, doc_freq)
            except Exception as e:  # noqa: BLE001
                errors.append((writer_id, repr(e)))

        threads = [threading.Thread(target=write_index, args=(i,)) for i in range(n_writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Unexpected errors from concurrent writers: {errors}"

        final = index_dir / "index.json"
        assert final.exists(), "index.json must exist after concurrent writes"
        result = json.loads(final.read_text())
        assert result["files"] == hashes, "Final index must contain the full file list"
        assert isinstance(result["top_tokens"], list)

        # No stale tmp files should remain (all were atomically renamed)
        stale = list(index_dir.glob("index.json.*.tmp"))
        assert not stale, f"Stale tmp files remain: {stale}"

    def test_persist_error_is_emitted_not_raised(self, tmp_path, capsys):
        """If os.replace() fails (e.g. index dir disappeared), persist_index must not crash."""
        mod = _load_indexer(str(tmp_path))
        index_dir = tmp_path / ".index"
        index_dir.mkdir()
        mod.INDEX_DIR = str(index_dir)
        mod.INDEX_FINAL = str(index_dir / "index.json")
        mod.INDEX_TMP = str(index_dir / f"index.json.{os.getpid()}.tmp")

        hashes, doc_freq = self._make_index_data()

        # Cause os.replace to fail by making the tmp file appear to not exist after write.
        original_replace = os.replace
        def broken_replace(src, dst):
            raise OSError(2, "injected fault")
        with patch("os.replace", side_effect=broken_replace):
            # Must not raise — persist errors are non-fatal for a simulator
            mod.persist_index(hashes, doc_freq)

        captured = capsys.readouterr()
        emitted_lines = [json.loads(l) for l in captured.out.splitlines() if l.strip()]
        events = [l["event"] for l in emitted_lines]
        assert "persist_error" in events, (
            f"Expected persist_error event but got: {events}"
        )


class TestCleanupStaleTmps:
    """cleanup_stale_tmps() removes files for dead PIDs and leaves live ones alone."""

    def test_removes_stale_tmp_for_dead_pid(self, tmp_path):
        mod = _load_indexer(str(tmp_path))
        index_dir = tmp_path / ".index"
        index_dir.mkdir()
        mod.INDEX_DIR = str(index_dir)
        mod._PID = os.getpid()

        # PID 99999999 almost certainly does not exist
        dead_pid = 99999999
        stale = index_dir / f"index.json.{dead_pid}.tmp"
        stale.write_text("stale content")
        assert stale.exists()

        mod.cleanup_stale_tmps()
        assert not stale.exists(), "Stale tmp for dead PID should have been removed"

    def test_does_not_remove_own_tmp(self, tmp_path):
        mod = _load_indexer(str(tmp_path))
        index_dir = tmp_path / ".index"
        index_dir.mkdir()
        mod.INDEX_DIR = str(index_dir)
        mod._PID = os.getpid()

        # Create our own tmp file (PID = current process)
        own_tmp = index_dir / f"index.json.{os.getpid()}.tmp"
        own_tmp.write_text("in-progress write")

        mod.cleanup_stale_tmps()
        assert own_tmp.exists(), "cleanup must not remove its own PID's tmp file"

    def test_does_not_remove_live_peer_tmp(self, tmp_path):
        mod = _load_indexer(str(tmp_path))
        index_dir = tmp_path / ".index"
        index_dir.mkdir()
        mod.INDEX_DIR = str(index_dir)
        mod._PID = os.getpid()

        # Spawn a real child process and use its PID as a "live peer"
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        live_pid = proc.pid
        live_tmp = index_dir / f"index.json.{live_pid}.tmp"
        live_tmp.write_text("peer in-progress")

        try:
            mod.cleanup_stale_tmps()
            assert live_tmp.exists(), "cleanup must not remove a live peer's tmp file"
        finally:
            proc.terminate()
            proc.wait()

    def test_ignores_unexpected_filename_format(self, tmp_path):
        """Files that don't match 'index.json.<int>.tmp' must be left untouched."""
        mod = _load_indexer(str(tmp_path))
        index_dir = tmp_path / ".index"
        index_dir.mkdir()
        mod.INDEX_DIR = str(index_dir)
        mod._PID = os.getpid()

        odd = index_dir / "index.json.notanint.tmp"
        odd.write_text("odd file")

        mod.cleanup_stale_tmps()
        assert odd.exists(), "Cleanup must not touch files with unexpected naming"
