"""Experiment registry for lifecycle tracking and hypothesis enforcement."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from battlelab.core.identifiers import generate_experiment_id
from battlelab.core.models import Experiment
from battlelab.storage.database import Database


class ExperimentRegistry:
    """Manages experiment creation, updates, and query operations."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    def create_experiment(
        self,
        hypothesis: str,
        baseline_artifact_id: str,
        challenger_artifact_id: str,
        intended_change: str,
        evaluation_matrix: dict[str, Any] | None = None,
        acceptance_criteria: dict[str, Any] | None = None,
    ) -> Experiment:
        """Create a new experiment enforcing single primary hypothesis."""
        clean_hyp = hypothesis.strip()
        if not clean_hyp:
            raise ValueError("An explicit, non-empty hypothesis is required.")

        exp_id = generate_experiment_id()
        created_at = datetime.now(timezone.utc).isoformat()

        exp = Experiment(
            experiment_id=exp_id,
            hypothesis=clean_hyp,
            baseline_artifact_id=baseline_artifact_id,
            challenger_artifact_id=challenger_artifact_id,
            intended_change=intended_change,
            evaluation_matrix=evaluation_matrix or {},
            acceptance_criteria=acceptance_criteria or {},
            status="CREATED",
            created_at=created_at,
        )
        self.db.save_experiment(exp)
        return exp

    def get_experiment(self, experiment_id: str) -> Experiment:
        exp = self.db.get_experiment(experiment_id)
        if not exp:
            raise KeyError(f"Experiment not found: {experiment_id}")
        return exp

    def list_experiments(self) -> list[Experiment]:
        return self.db.list_experiments()

    def update_experiment(self, exp: Experiment) -> None:
        self.db.save_experiment(exp)
