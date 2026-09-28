"""Unit tests for analysis, confidence intervals, and failure classifier."""

from battlelab.analysis.confidence import wilson_score_interval, paired_bootstrap_difference
from battlelab.analysis.failure_classifier import FailureClassifier
from battlelab.core.models import FailureCategory

def test_wilson_score_interval():
    # 0 successes out of 10
    low, high = wilson_score_interval(0, 10)
    assert low == 0.0
    assert high > 0.0

    # 10 successes out of 10
    low, high = wilson_score_interval(10, 10)
    assert low < 1.0
    assert high == 1.0

    # 5 out of 10 (centered around 0.5)
    low, high = wilson_score_interval(5, 10)
    assert low < 0.5 < high

def test_paired_bootstrap():
    diffs = [1.0, 2.0, 1.5, 0.5, 2.5]
    mean, ci_low, ci_high = paired_bootstrap_difference(diffs, num_samples=500, seed=123)
    assert ci_low <= mean <= ci_high
    assert mean == 1.5

def test_failure_classifier_taxonomy():
    # 1. Bot crash
    res = FailureClassifier.classify({"crashed_a": True, "outcome": "WIN_B"})
    assert res.category == FailureCategory.BOT_CRASH
    assert res.culprit == "bot_a"

    # 2. Timeout
    res = FailureClassifier.classify({"timed_out_b": True, "outcome": "WIN_A"})
    assert res.category == FailureCategory.TIMEOUT
    assert res.culprit == "bot_b"

    # 3. Invalid action
    res = FailureClassifier.classify({"invalid_action_a": True, "outcome": "WIN_B"})
    assert res.category == FailureCategory.INVALID_ACTION
    assert res.culprit == "bot_a"

    # 4. Missing dependency inferred from stderr
    res = FailureClassifier.classify({}, stderr_text="ModuleNotFoundError: No module named 'scipy'")
    assert res.category == FailureCategory.MISSING_DEPENDENCY
    assert res.is_inference is True

    # 5. Build error inferred from stderr
    res = FailureClassifier.classify({}, stderr_text="SyntaxError: invalid syntax")
    assert res.category == FailureCategory.BUILD_FAILURE

    # 6. Gameplay loss
    res = FailureClassifier.classify({"outcome": "WIN_A", "winner": "A"})
    assert res.category == FailureCategory.GAMEPLAY_LOSS
    assert res.culprit == "bot_b"
