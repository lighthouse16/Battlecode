"""End-to-end demonstration script fulfilling all Section 19 requirements."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from battlelab.adapters import get_adapter
from battlelab.bots.registry import BotRegistry
from battlelab.core.errors import PromotionGateError
from battlelab.core.models import MatchSpec
from battlelab.experiments.evaluator import ExperimentEvaluator
from battlelab.experiments.promotion import PromotionGate
from battlelab.experiments.registry import ExperimentRegistry
from battlelab.matches.matrix import generate_match_matrix
from battlelab.matches.scheduler import TournamentScheduler
from battlelab.storage.database import Database
from battlelab.telemetry.replay_store import ReplayStore


def run_demo():
    print("=" * 70)
    print("STARTING BATTLELAB END-TO-END DEMONSTRATION")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        old_env = os.environ.copy()

        data_dir = temp_path / "data"
        artifacts_dir = data_dir / "artifacts"
        replays_dir = data_dir / "replays"
        reports_dir = data_dir / "reports"
        runs_dir = data_dir / "runs"
        db_path = data_dir / "battlelab.db"
        champion_manifest = temp_path / "champion_manifest.json"

        orig_champ = Path("bots/champion/champion_manifest.json")
        if orig_champ.exists():
            shutil.copy2(orig_champ, champion_manifest)
        else:
            champion_manifest.write_text("{}", encoding="utf-8")

        os.environ["BATTLELAB_DATA_DIR"] = str(data_dir)
        os.environ["BATTLELAB_ARTIFACTS_DIR"] = str(artifacts_dir)
        os.environ["BATTLELAB_REPLAY_DIR"] = str(replays_dir)
        os.environ["BATTLELAB_REPORTS_DIR"] = str(reports_dir)
        os.environ["BATTLELAB_RUNS_DIR"] = str(runs_dir)
        os.environ["BATTLELAB_DATABASE_PATH"] = str(db_path)
        os.environ["BATTLELAB_CHAMPION_MANIFEST"] = str(champion_manifest)

        try:
            db = Database()
            registry = BotRegistry(db)
            exp_reg = ExperimentRegistry(db)
            evaluator = ExperimentEvaluator(db)
            gate = PromotionGate(db)
            replay_store = ReplayStore()

            # Step 1 & 2: Register baseline and challenger bots
            print("\n[Step 1 & 2] Registering bots...")
            baseline = registry.register_bot(
                source_path="bots/baselines/random_bot.py",
                display_name="BaselineRandom",
                tags=["baseline", "policy:random"],
            )
            print(f"  Registered Baseline:   {baseline.artifact_id} ({baseline.display_name})")

            challenger_good = registry.register_bot(
                source_path="bots/challengers/challenger_v1.py",
                display_name="ChallengerResourceGreedy",
                tags=["challenger", "policy:resource"],
            )
            print(
                f"  Registered Challenger: {challenger_good.artifact_id} ({challenger_good.display_name})"
            )

            adversary_crash = registry.register_bot(
                source_path="bots/adversaries/crash_bot.py",
                display_name="AdversaryCrash",
                tags=["fail:crash"],
            )
            print(
                f"  Registered Adversary:  {adversary_crash.artifact_id} ({adversary_crash.display_name})"
            )

            # Step 3 & 4: Create Accepted Experiment (Good Challenger vs Baseline)
            print("\n[Step 3 & 4] Running Accepted Experiment (Challenger vs Baseline)...")
            exp_accepted = exp_reg.create_experiment(
                hypothesis="Greedy tile evaluation achieves superior resource accumulation over random movements",
                baseline_artifact_id=baseline.artifact_id,
                challenger_artifact_id=challenger_good.artifact_id,
                intended_change="Navigate towards highest value available tiles",
            )
            res_accepted = evaluator.run_experiment(exp_accepted.experiment_id, max_workers=2)
            print(f"  Experiment Completed: {exp_accepted.experiment_id}")
            print(f"  Report generated at:  {res_accepted['report_path']}")
            print(f"  Packet generated at:  {res_accepted['packet_path']}")

            # Step 5: Inject Controlled Failure (Rejected Experiment)
            print("\n[Step 5] Running Rejected Experiment (Crash Adversary vs Baseline)...")
            exp_rejected = exp_reg.create_experiment(
                hypothesis="Crash bot must be rejected by promotion gates due to crashes",
                baseline_artifact_id=baseline.artifact_id,
                challenger_artifact_id=adversary_crash.artifact_id,
                intended_change="Injected unhandled runtime crash",
            )
            res_rejected = evaluator.run_experiment(exp_rejected.experiment_id, max_workers=2)
            print(f"  Experiment Completed: {exp_rejected.experiment_id}")
            print(
                f"  Crashes detected in metrics: {res_rejected['gate_results']['metrics_evaluated']['crash_rate'] * 100:.1f}%"
            )

            # Step 6: Interrupted Tournament Resume
            print("\n[Step 6] Testing Interrupted Tournament Resume...")
            sched = TournamentScheduler(db, max_workers=2)
            trn_id = "demo_resumable_trn"
            specs = generate_match_matrix(
                bot_a_id=challenger_good.artifact_id,
                opponents=[baseline.artifact_id],
                maps=["grid_tiny_4x4", "grid_classic_8x8"],
                seeds=[501, 502],
                paired_sides=True,
                tournament_id=trn_id,
            )
            sched.create_tournament(trn_id, "Resumable Demo Tournament", specs)
            # Stop after 2 matches
            res_stop = sched.run_tournament(trn_id, stop_after=2)
            print(
                f"  Tournament paused: Status={res_stop['status']} ({res_stop['completed_matches']}/{res_stop['total_matches']})"
            )
            assert res_stop["status"] == "INTERRUPTED"
            assert res_stop["completed_matches"] == 2
            assert res_stop["total_matches"] == 8

            # Resume
            res_resumed = sched.run_tournament(trn_id)
            print(
                f"  Tournament resumed: Status={res_resumed['status']} ({res_resumed['completed_matches']}/{res_resumed['total_matches']})"
            )
            assert res_resumed["status"] == "COMPLETED"
            assert res_resumed["completed_matches"] == 8
            assert res_resumed["total_matches"] == 8

            # Step 7 & 8: Verify Markdown Report and JSON Feedback Packet
            print("\n[Step 7 & 8] Inspecting Generated Report and AI Feedback Packet...")
            report_content = Path(res_accepted["report_path"]).read_text(encoding="utf-8")
            assert "Executive Summary" in report_content
            print("  Report preview (first 4 lines):")
            for line in report_content.splitlines()[:4]:
                print(f"    {line}")

            with open(res_accepted["packet_path"], "r", encoding="utf-8") as f:
                packet = json.load(f)
            print(f"  Feedback packet schema: {packet['schema_version']}")
            print(
                f"  AI actionable focus:    {packet['ai_actionable_insights']['suggested_focus_area']}"
            )

            # Step 9: Exercise Promotion Gates (One Acceptance, One Rejection)
            print("\n[Step 9] Exercising Promotion Gates...")
            # 9a. Rejection
            rejected_ok = False
            try:
                gate.promote(exp_rejected.experiment_id, dry_run=False)
                print("  ERROR: Rejected experiment was incorrectly allowed to promote!")
            except PromotionGateError as e:
                rejected_ok = True
                print("  [CONFIRMED REJECTION] Gate blocked promotion as expected:")
                for v in e.violations:
                    print(f"    - {v}")
            assert rejected_ok, "Buggy adversary was not blocked by promotion gate!"

            # 9b. Acceptance
            prom_accepted = gate.promote(exp_accepted.experiment_id, dry_run=False)
            print(
                f"  [CONFIRMED PROMOTION] Promoted {prom_accepted['champion_artifact_id']} as Champion!"
            )
            active_champ = registry.get_champion_artifact()
            assert active_champ is not None
            assert active_champ.artifact_id == challenger_good.artifact_id
            print(f"  Active Champion in manifest: {active_champ.artifact_id}")

            # Step 10: Reproduce Stored Match
            print("\n[Step 10] Reproducing Stored Match from Manifest...")
            sample_match = db.list_matches_by_experiment(exp_accepted.experiment_id)[0]
            spec_dict = json.loads(sample_match["spec_json"])
            spec = MatchSpec.from_dict(spec_dict)
            adapter = get_adapter(spec.adapter_name)
            bot_a = registry.get_artifact(spec.bot_a_id)
            bot_b = registry.get_artifact(spec.bot_b_id)
            reproduced_res = adapter.run_local_match(
                spec=spec,
                bot_a=bot_a,
                bot_b=bot_b,
                work_dir=runs_dir / "reproduce_demo",
            )
            print(
                f"  Original Match Outcome:   {sample_match['outcome']} (Score: {sample_match['score_a']} vs {sample_match['score_b']})"
            )
            print(
                f"  Reproduced Match Outcome: {reproduced_res.outcome.value} (Score: {reproduced_res.score_a} vs {reproduced_res.score_b})"
            )
            assert sample_match["outcome"] == reproduced_res.outcome.value
            assert sample_match["score_a"] == reproduced_res.score_a
            assert sample_match["score_b"] == reproduced_res.score_b

            # Step 11: Verify Replay Hashes
            print("\n[Step 11] Verifying Stored Replay Hashes...")
            replay_hash = sample_match["replay_hash"]
            valid = replay_store.verify_stored_replay(replay_hash)
            print(f"  Replay {replay_hash} integrity: {'VALID' if valid else 'INVALID'}")
            assert valid is True

            # Step 12: Audited Rollback
            print("\n[Step 12] Performing Audited Champion Rollback...")
            rollback_res = gate.rollback(
                historical_artifact_id=baseline.artifact_id,
                reason="E2E demonstration: verifying audited rollback mechanism",
                actor="e2e_tester",
            )
            print(f"  Champion rolled back to: {rollback_res['champion_artifact_id']}")
            rolled_back_champ = registry.get_champion_artifact()
            assert rolled_back_champ is not None
            assert rolled_back_champ.artifact_id == baseline.artifact_id
            print("  Audited rollback verified.")

            print("\n" + "=" * 70)
            print("ALL 12 END-TO-END DEMONSTRATION STEPS COMPLETED SUCCESSFULLY!")
            print("=" * 70)
        finally:
            os.environ.clear()
            os.environ.update(old_env)


if __name__ == "__main__":
    run_demo()
