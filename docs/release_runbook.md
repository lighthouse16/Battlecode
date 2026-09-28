# Champion Release and Promotion Runbook

## Champion State Invariants
- The current champion artifact is referenced by `bots/champion/champion_manifest.json`.
- Historical champions are immutable artifacts preserved in `data/artifacts/`.
- Never overwrite or edit files inside `bots/champion/` directly.

## Promotion Procedure
1. Confirm experiment status is `COMPLETED` and passes all criteria in `configs/promotion.yaml`:
   ```bash
   battlelab experiment promote <experiment_id> --dry-run
   ```
2. Execute promotion:
   ```bash
   battlelab experiment promote <experiment_id>
   ```
3. Verify champion manifest updated:
   ```bash
   battlelab doctor
   ```

## Emergency Rollback
If an unexpected bug is observed post-promotion:
1. Identify the previous stable champion artifact ID:
   ```bash
   battlelab bot list
   ```
2. Execute rollback:
   ```python
   from battlelab.experiments.promotion import PromotionGate
   gate = PromotionGate()
   gate.rollback("<historical_artifact_id>", reason="Observed critical issue")
   ```
3. Verify rollback via `battlelab doctor`.
