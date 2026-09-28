"""Unit tests for battlelab core: models, hashing, identifiers, errors."""

import json
from pathlib import Path
from battlelab.core.hashing import hash_bytes, hash_dict, hash_file
from battlelab.core.identifiers import generate_match_id, generate_artifact_id, generate_experiment_id
from battlelab.core.models import (
    MatchOutcome,
    FailureCategory,
    FailureClassification,
    BotArtifact,
    MatchSpec,
    MatchResult,
    Experiment,
)

def test_hashing_canonical(tmp_path: Path):
    d1 = {"b": 2, "a": 1}
    d2 = {"a": 1, "b": 2}
    assert hash_dict(d1) == hash_dict(d2)

    test_file = tmp_path / "hello.txt"
    test_file.write_text("battlelab", encoding="utf-8")
    assert hash_file(test_file) == hash_bytes(b"battlelab")

def test_identifiers():
    spec = {
        "adapter_name": "mock",
        "adapter_version": "0.1.0",
        "bot_a_id": "art_1",
        "bot_b_id": "art_2",
        "map_name": "grid_8x8",
        "seed": 42,
        "side_assignment": {"A": "side_0", "B": "side_1"},
        "time_limit_ms": 5000,
    }
    id1 = generate_match_id(spec)
    id2 = generate_match_id(dict(spec))
    assert id1 == id2
    assert id1.startswith("m_")

    art_id = generate_artifact_id("source_sha", "build_sha")
    assert art_id.startswith("art_")

    exp_id = generate_experiment_id()
    assert exp_id.startswith("exp_")

def test_models_serialization():
    fc = FailureClassification(
        category=FailureCategory.BOT_CRASH,
        culprit="bot_a",
        evidence="Segfault in bot process",
        confidence=0.95,
        is_inference=True,
    )
    res = MatchResult(
        match_id="m_123",
        outcome=MatchOutcome.WIN_B,
        winner="B",
        score_a=10.0,
        score_b=25.0,
        duration_ms=120.5,
        failure_classification=fc,
    )
    d = res.to_dict()
    assert d["outcome"] == "WIN_B"
    assert d["failure_classification"]["category"] == "BOT_CRASH"

    res_restored = MatchResult.from_dict(d)
    assert res_restored.outcome == MatchOutcome.WIN_B
    assert res_restored.failure_classification is not None
    assert res_restored.failure_classification.category == FailureCategory.BOT_CRASH
