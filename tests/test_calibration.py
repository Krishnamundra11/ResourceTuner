import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import calibrate_thresholds as ct  # noqa: E402


def test_pct_interpolates():
    assert ct.pct([0, 10], 50) == 5
    assert ct.pct([1, 2, 3, 4, 5], 100) == 5
    assert ct.pct([None, 3], 50) == 3
    assert ct.pct([], 50) is None


def test_fit_separable_uses_midpoint():
    neg = [0.0] * 99 + [2.0]          # p99 ~ 1.98
    pos = [20.0] * 100                # p5 = 20
    f = ct.fit(neg, pos)
    assert f["separable"] and f["value"] == round((ct.pct(neg, 99) + 20) / 2, 1)
    assert f["tpr"] == 1.0 and f["fpr"] == 0.0


def test_fit_overlapping_uses_youden():
    neg = [1, 2, 3, 4, 5, 6]
    pos = [4, 5, 6, 7, 8, 9]
    f = ct.fit(neg, pos, decimals=0)
    assert not f["separable"] and "Youden" in f["method"]
    tpr, fpr = ct.rates(neg, pos, f["value"])
    assert tpr - fpr == max(ct.rates(neg, pos, t)[0] - ct.rates(neg, pos, t)[1] for t in range(1, 10))


def test_fit_lower_is_worse():
    f = ct.fit([60, 61, 62], [5, 6, 7], higher_is_worse=False)
    assert f["separable"] and 7 < f["value"] < 60


def test_longest_streak_is_per_run():
    rows = [{"run_id": "a", "tick": i, "host": {"psi_cpu": v}} for i, v in enumerate([5, 5, 0, 5, 5, 5])]
    rows += [{"run_id": "b", "tick": 0, "host": {"psi_cpu": 5}}]
    assert ct.longest_streak(rows, ct.H("psi_cpu"), 4) == 3
