# Replay and Data Retention Policy

## Storage Layout
- Replays are stored in `data/replays/<sha256>.jsonl`.
- Structured metadata (match IDs, scores, outcomes, durations) is stored in SQLite (`data/battlelab.db`).
- Experiment summaries are saved in `data/reports/<experiment_id>/`.

## Retention Rules
1. **Always Retained (100%)**:
   - Matches with crashes (`BOT_CRASH`, `ENGINE_CRASH`).
   - Matches with timeouts (`TIMEOUT`).
   - Matches with illegal actions (`INVALID_ACTION`).
   - Representative losses identified as worst regressions.
2. **Routine Matches**:
   - Routine wins during large tournaments (>100 matches) are downsampled according to `storage.yaml:win_sample_rate`.
   - Small experiment runs (<50 matches) retain all replays by default.
3. **Integrity & Verification**:
   - Replays must be valid JSONL with a META header on line 1.
   - Run `battlelab replay verify <replay_hash>` to verify file checksum against filename.
