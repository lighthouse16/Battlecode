"""Battlelab analysis module."""

from battlelab.analysis.confidence import wilson_score_interval, paired_bootstrap_difference
from battlelab.analysis.failure_classifier import FailureClassifier
from battlelab.analysis.feedback_packet import generate_feedback_packet
from battlelab.analysis.metrics import calculate_tournament_metrics, calculate_paired_experiment_metrics
from battlelab.analysis.report import generate_experiment_report

__all__ = [
    "wilson_score_interval",
    "paired_bootstrap_difference",
    "FailureClassifier",
    "generate_feedback_packet",
    "calculate_tournament_metrics",
    "calculate_paired_experiment_metrics",
    "generate_experiment_report",
]
