import hashlib
import json


def test_sort_records(dataset, case):
    _, records = dataset
    ordered = sorted(records, key=lambda r: (r["score"], r["id"] + case))
    assert all(ordered[i]["score"] <= ordered[i + 1]["score"] for i in range(len(ordered) - 1))


def test_json_roundtrip(dataset, case):
    _, records = dataset
    chunk = records[case * 100:(case + 1) * 100 + 5000]
    assert json.loads(json.dumps(chunk)) == chunk


def test_blob_digest_stable(dataset, case):
    blob, _ = dataset
    window = memoryview(blob)[case * 4096:case * 4096 + 64 * 1024 * 1024]
    assert hashlib.sha256(window).digest() == hashlib.sha256(bytes(window)).digest()
