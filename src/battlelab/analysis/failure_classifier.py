"""Failure classification taxonomy and heuristic inference engine."""

from __future__ import annotations

from typing import Any

from battlelab.core.models import FailureCategory, FailureClassification


class FailureClassifier:
    """Classifies match and system failures into taxonomy with supporting evidence."""

    @staticmethod
    def classify(
        match_data: dict[str, Any],
        stdout_text: str = "",
        stderr_text: str = "",
    ) -> FailureClassification:
        """Classify failure given match results, exit codes, and output text."""
        # 1. Check direct flags from match execution
        crashed_a = bool(match_data.get("crashed_a"))
        crashed_b = bool(match_data.get("crashed_b"))
        timed_out_a = bool(match_data.get("timed_out_a"))
        timed_out_b = bool(match_data.get("timed_out_b"))
        invalid_a = bool(match_data.get("invalid_action_a"))
        invalid_b = bool(match_data.get("invalid_action_b"))
        outcome = match_data.get("outcome")

        # Check explicit crash
        if crashed_a:
            return FailureClassification(
                category=FailureCategory.BOT_CRASH,
                culprit="bot_a",
                evidence="Bot A raised unhandled exception or non-zero exit",
                confidence=1.0,
                is_inference=False,
            )
        if crashed_b:
            return FailureClassification(
                category=FailureCategory.BOT_CRASH,
                culprit="bot_b",
                evidence="Bot B raised unhandled exception or non-zero exit",
                confidence=1.0,
                is_inference=False,
            )

        # Check timeout
        if timed_out_a:
            return FailureClassification(
                category=FailureCategory.TIMEOUT,
                culprit="bot_a",
                evidence="Bot A exceeded turn or match execution time limit",
                confidence=1.0,
                is_inference=False,
            )
        if timed_out_b:
            return FailureClassification(
                category=FailureCategory.TIMEOUT,
                culprit="bot_b",
                evidence="Bot B exceeded turn or match execution time limit",
                confidence=1.0,
                is_inference=False,
            )

        # Check invalid action
        if invalid_a:
            return FailureClassification(
                category=FailureCategory.INVALID_ACTION,
                culprit="bot_a",
                evidence="Bot A submitted illegal action rejected by rules",
                confidence=1.0,
                is_inference=False,
            )
        if invalid_b:
            return FailureClassification(
                category=FailureCategory.INVALID_ACTION,
                culprit="bot_b",
                evidence="Bot B submitted illegal action rejected by rules",
                confidence=1.0,
                is_inference=False,
            )

        # 2. Check logs for missing dependency or protocol violation
        combined_logs = (stdout_text + "\n" + stderr_text).lower()
        if "modulenotfounderror" in combined_logs or "importerror" in combined_logs:
            return FailureClassification(
                category=FailureCategory.MISSING_DEPENDENCY,
                culprit="bot",
                evidence="Log indicates missing Python import/dependency",
                confidence=0.9,
                is_inference=True,
            )

        if "syntaxerror" in combined_logs or "indentationerror" in combined_logs:
            return FailureClassification(
                category=FailureCategory.BUILD_FAILURE,
                culprit="bot",
                evidence="Syntax or compilation error in bot source",
                confidence=0.95,
                is_inference=True,
            )

        # 3. Check infrastructure failure vs gameplay loss
        if outcome == "INFRASTRUCTURE_FAILURE":
            return FailureClassification(
                category=FailureCategory.UNKNOWN_INFRASTRUCTURE,
                culprit="system",
                evidence="Match marked as infrastructure failure with no bot exception",
                confidence=0.7,
                is_inference=True,
            )

        # Legitimate gameplay outcome
        winner = match_data.get("winner")
        loser = "bot_b" if winner == "A" else "bot_a" if winner == "B" else None
        return FailureClassification(
            category=FailureCategory.GAMEPLAY_LOSS,
            culprit=loser,
            evidence=f"Normal gameplay outcome ({outcome})",
            confidence=1.0,
            is_inference=False,
        )
