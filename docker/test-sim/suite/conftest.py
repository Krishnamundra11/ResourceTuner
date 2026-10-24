import os
import random

import pytest

TEST_COUNT = int(os.environ.get("TEST_COUNT", "100"))
SCALE = float(os.environ.get("TEST_SCALE", "1"))
MB_PER_WORKER = int(os.environ.get("MB_PER_WORKER", "256"))


@pytest.fixture(scope="session")
def dataset():
    """Per-worker dataset held for the whole session — the RAM-heavy part of a test run
    (like loading fixtures / test databases). Touched page by page so it is resident."""
    blob = bytearray(MB_PER_WORKER * 1024 * 1024)
    for off in range(0, len(blob), 4096):
        blob[off] = off % 251
    rng = random.Random(7)
    records = [{"id": i, "score": rng.random(), "name": f"user{i:06d}"} for i in range(int(50_000 * SCALE))]
    return blob, records


def pytest_generate_tests(metafunc):
    if "case" in metafunc.fixturenames:
        metafunc.parametrize("case", range(TEST_COUNT))
