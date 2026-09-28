"""Unit tests for configuration validation."""

from battlelab.config.validation import validate_evaluation_config, validate_promotion_config

def test_evaluation_validation():
    # Valid config
    valid_cfg = {
        "adapter": "mock",
        "maps": ["grid_classic_8x8"],
        "seeds": [42, 137],
        "time_limit_ms": 5000,
        "max_workers": 2,
    }
    assert len(validate_evaluation_config(valid_cfg)) == 0

    # Missing adapter and empty maps
    invalid_cfg = {
        "maps": [],
        "seeds": [1],
        "time_limit_ms": -10,
        "max_workers": 0,
    }
    errs = validate_evaluation_config(invalid_cfg)
    assert len(errs) >= 3

def test_promotion_validation():
    valid_prom = {
        "min_sample_size": 10,
        "min_win_rate": 0.55,
        "max_crash_rate": 0.0,
        "max_timeout_rate": 0.0,
        "max_invalid_action_rate": 0.0,
    }
    assert len(validate_promotion_config(valid_prom)) == 0

    invalid_prom = {
        "min_sample_size": -5,
        "min_win_rate": 1.5,
        "max_crash_rate": -0.1,
    }
    errs = validate_promotion_config(invalid_prom)
    assert len(errs) >= 3
