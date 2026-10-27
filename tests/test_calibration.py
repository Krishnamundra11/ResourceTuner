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


# --- preserve_unowned_sections: --write must not wipe hand-tuned config (reported finding) -----
#
# `render_yaml` only ever produces `contention:` and `classifier:` — the two sections this script
# fits from telemetry. `evaluation:` (discrete/continuous bands EvaluationEngine reads) is hand-
# tuned, lives in the same file, and is NOT produced by this script. Before this fix, `--write`
# replaced the whole file with just `render_yaml`'s output, silently deleting `evaluation:`;
# EvaluationEngine would then fall back to its own hardcoded code defaults with no warning.

EXISTING_CONFIG = """\
# header comment line 1
# header comment line 2: has a colon too, must not be mistaken for a top-level key
contention:
  cpu:
    psi_some_avg10: 15.6
classifier:
  window_samples: 5

evaluation:
  discrete:
    improved_threshold: 0.90
    degraded_threshold: 1.10
  continuous:
    window_s: 60
"""


def test_preserve_unowned_sections_extracts_evaluation_verbatim():
    preserved = ct.preserve_unowned_sections(EXISTING_CONFIG)
    assert preserved.startswith("evaluation:")
    assert "improved_threshold: 0.90" in preserved
    assert "degraded_threshold: 1.10" in preserved
    assert "window_s: 60" in preserved


def test_preserve_unowned_sections_does_not_mistake_a_comment_colon_for_a_key():
    # A header comment line containing ":" (e.g. "# ...: has a colon too") must not be treated
    # as an unowned top-level key — that was the first version of this fix, and it grabbed the
    # entire file starting from the first comment line instead of from `evaluation:`.
    preserved = ct.preserve_unowned_sections(EXISTING_CONFIG)
    assert "header comment" not in preserved
    assert "contention:" not in preserved
    assert "classifier:" not in preserved


def test_preserve_unowned_sections_returns_empty_when_nothing_is_unowned():
    only_owned = "contention:\n  cpu:\n    psi_some_avg10: 1\nclassifier:\n  window_samples: 5\n"
    assert ct.preserve_unowned_sections(only_owned) == ""


def test_preserve_unowned_sections_handles_a_missing_or_empty_file():
    assert ct.preserve_unowned_sections("") == ""


def test_render_yaml_appends_the_preserved_section_with_one_blank_line_before_it():
    # Real calibration output (from the project's own recorded telemetry), not a hand-built
    # fake shape — `calibrate()`'s return value has many nested fields that are easy to get
    # subtly wrong by hand, and a fabricated shape would prove less than the real function does.
    rows = ct.load(None)
    calib = ct.calibrate(rows, cores=6)
    preserved = ct.preserve_unowned_sections(EXISTING_CONFIG)
    out = ct.render_yaml(calib, [], preserved)
    # Exactly one blank line between the last classifier line and `evaluation:`, matching the
    # gap the script already uses between its own contention/classifier sections.
    assert "\n\nevaluation:" in out
    assert "\n\n\nevaluation:" not in out
    assert out.rstrip().endswith("window_s: 60")


def test_write_round_trip_preserves_evaluation_byte_for_byte(tmp_path, monkeypatch):
    """End-to-end against the project's real telemetry data: --write must leave an existing
    `evaluation:` section untouched, byte for byte, while still regenerating contention/classifier."""
    cfg_path = tmp_path / "thresholds.yaml"
    cfg_path.write_text(EXISTING_CONFIG)
    report_path = tmp_path / "report.md"
    monkeypatch.setattr(ct, "CONFIG", cfg_path)
    monkeypatch.setattr(ct, "REPORT", report_path)
    monkeypatch.setattr(ct, "REPO", tmp_path)   # so main()'s final relative_to() print succeeds

    rc = ct.main(["--write", "--cores", "6"])
    assert rc == 0

    after = cfg_path.read_text()
    before_eval = EXISTING_CONFIG[EXISTING_CONFIG.index("evaluation:"):]
    after_eval = after[after.index("evaluation:"):]
    assert after_eval.rstrip() == before_eval.rstrip()
    import yaml
    parsed = yaml.safe_load(after)
    assert parsed["evaluation"]["discrete"]["improved_threshold"] == 0.90
    assert "contention" in parsed and "classifier" in parsed
    # And the regenerated sections are actually fresh, fitted values, not placeholders.
    assert isinstance(parsed["contention"]["cpu"]["psi_some_avg10"], (int, float))
