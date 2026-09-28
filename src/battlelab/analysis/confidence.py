"""Statistical evaluation, Wilson intervals, and paired bootstrap estimation."""

from __future__ import annotations

import math
import random
from typing import Sequence


def wilson_score_interval(
    successes: int, total: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Compute the Wilson score confidence interval for a binomial proportion.

    Returns (lower_bound, upper_bound) in range [0.0, 1.0].
    """
    if total <= 0:
        return (0.0, 1.0)

    # Standard normal quantile approximations
    z_values = {
        0.90: 1.64485,
        0.95: 1.95996,
        0.99: 2.57583,
    }
    z = z_values.get(round(confidence, 2), 1.95996)

    p_hat = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre_adjusted_probability = p_hat + z2 / (2.0 * total)
    adjusted_standard_deviation = math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * total)) / total)

    lower = (centre_adjusted_probability - z * adjusted_standard_deviation) / denominator
    upper = (centre_adjusted_probability + z * adjusted_standard_deviation) / denominator

    return (max(0.0, float(lower)), min(1.0, float(upper)))


def paired_bootstrap_difference(
    diffs: Sequence[float],
    num_samples: int = 1000,
    confidence: float = 0.95,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Compute paired bootstrap difference mean and confidence interval.

    Returns (mean_diff, ci_lower, ci_upper).
    """
    n = len(diffs)
    if n == 0:
        return (0.0, 0.0, 0.0)

    mean_diff = sum(diffs) / n
    if n < 2:
        return (mean_diff, mean_diff, mean_diff)

    rng = random.Random(seed)
    boot_means: list[float] = []

    for _ in range(num_samples):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        boot_means.append(sum(sample) / n)

    boot_means.sort()
    alpha = (1.0 - confidence) / 2.0
    lower_idx = int(alpha * num_samples)
    upper_idx = int((1.0 - alpha) * num_samples)
    upper_idx = min(upper_idx, num_samples - 1)

    return (mean_diff, boot_means[lower_idx], boot_means[upper_idx])
