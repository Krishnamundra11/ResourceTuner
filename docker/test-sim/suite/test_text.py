import os
import random
import re
from collections import Counter

SCALE = float(os.environ.get("TEST_SCALE", "1"))
WORD_RE = re.compile(r"[a-z]+")


def test_word_counts(case):
    rng = random.Random(case)
    words = [rng.choice(["alpha", "beta", "gamma", "delta", "eps"]) for _ in range(int(150_000 * SCALE))]
    text = " ".join(words)
    counts = Counter(WORD_RE.findall(text))
    assert sum(counts.values()) == len(words)
