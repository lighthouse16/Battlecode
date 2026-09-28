# Execution Isolation, Scheduler Safety, and Statistical Benchmarking

## 1. Subprocess Bot Protocol

Each bot artifact exposes an executable Python entry point. The game adapter executes bots strictly from their immutable snapshot directories (`data/artifacts/<id>/`) in isolated operating system subprocesses.

### Wire Protocol (Line-Delimited JSON)
- **Turn Observation (Engine -> Bot stdin)**:
  ```json
  {
    "turn": 0,
    "max_turns": 100,
    "map": {"name": "grid_classic_8x8", "width": 8, "height": 8},
    "your_id": 0,
    "your_pos": [0, 0],
    "opponent_pos": [7, 7],
    "your_score": 0.0,
    "opponent_score": 0.0,
    "seed": 42,
    "claimed_tiles": []
  }
  ```
- **Turn Action (Bot stdout -> Engine)**:
  ```json
  {"type": "MOVE", "direction": "RIGHT"}
  ```
- **Shutdown Event**:
  ```json
  {"event": "SHUTDOWN"}
  ```

### Deadline & Process Tree Termination
- Bots run with unbuffered I/O (`python -u`).
- Hard per-turn deadlines are strictly enforced by thread-safe timeout queues (returning in < 1 second even on 10ms deadlines).
- When a process times out, crashes, or completes, `terminate_process_tree` recursively terminates the parent process and any child processes using `psutil`.

---

## 2. Scheduler Safety & Atomic Job Leasing

The `TournamentScheduler` coordinates parallel match execution across local threads and distributed workers.

### State Transitions
```
PENDING -> RUNNING -> COMPLETED
   ^          |
   |          +-----> RETRYABLE_FAILURE (attempt_count < max_attempts)
   |          |
   +----------+-----> TERMINAL_FAILURE (attempt_count >= max_attempts)
```

### Atomic Lease Protocol
- Workers lease matches using SQLite compare-and-swap (CAS) queries.
- Only matches with status `PENDING`, `RETRYABLE_FAILURE`, or expired `RUNNING` leases (`lease_expires_at < now`) can be claimed.
- Multi-scheduler concurrency testing guarantees exactly-once match execution.

---

## 3. Statistically Valid Evaluation

Evaluation matrices are configured via `configs/opponent_pool.yaml` and evaluated with paired comparisons:
- **Pairing**: Every challenger-versus-opponent match is joined with an equivalent baseline-versus-opponent match under identical maps, seeds, and side assignments via a stable `pair_id`.
- **Wilson Score Intervals**: 95% confidence intervals are computed for discrete win rates:
  $$\tilde{p} = \frac{n_w + \frac{z^2}{2}}{n + z^2}$$
- **Paired Bootstrap Differences**: 1,000 resamples calculate the 95% confidence interval for score and win-rate deltas ($\Delta = \text{Challenger} - \text{Baseline}$).
- **Infrastructure Failure Segregation**: Segments and bootstraps exclude infrastructure errors (`INFRASTRUCTURE_FAILURE`), ensuring gameplay metrics are unaffected by environment anomalies.

---

## 4. Protected Promotion & Rollback

### Promotion Gates
To be promoted to champion status, a challenger must pass all criteria in `configs/promotion.yaml`:
1. Artifact integrity verified (SHA-256 match).
2. Multi-seed determinism check verified against normalized frames (excluding volatile timings).
3. Win rate $\ge 50\%$ with lower bound of 95% Wilson CI $\ge 45\%$.
4. Zero crashes, zero unhandled timeouts, zero invalid actions.
5. Max segment regression within tolerance (e.g. $\le 25\%$ win rate deficit on any single map).

### Human Override Audit
To prevent accidental regressions while maintaining human control:
- The `--force` flag is removed.
- Overrides require:
  - `--actor <name>` (human identity)
  - `--override-reason <text>` (mandatory justification)
  - `--acknowledge-risk I_ACKNOWLEDGE_STATISTICAL_RISK` (explicit confirmation)
- Overrides are permanently recorded in the `promotions` table as `MANUAL_OVERRIDE`.

### Atomic Rollback
If a champion shows unexpected regressions in the wild:
```bash
battlelab champion rollback <HISTORICAL_ARTIFACT_ID> --reason "Regression on map X" --actor "engineer_name"
```
The rollback atomically updates the champion manifest pointer and writes an audited `ROLLBACK` record to `data/battlelab.db`.
