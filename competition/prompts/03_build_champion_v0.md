# Prompt 03: Build Champion v0

Use this prompt after the official adapter is fully verified and contract tests are green.

---

## OBJECTIVE

Build the simplest robust legal bot that completes full matches with zero errors.
The goal is **NOT** high performance or Elo optimization.
The goal is **first valid, deterministic, full-match-completing bot**.

---

## PRINCIPLES

- Rely strictly on `VERIFIED` game mechanics from the game specification.
- Implement the minimum legal behavior (e.g., valid movements, legal resource harvesting, default passes).
- Zero speculative optimizations, zero complex heuristics, zero external ML dependencies.
- Make all actions completely deterministic where the official game rules permit.

---

## EXECUTION STEPS

0. **Verify Season-Isolated Competition Workspace**:
   - Ensure the session runs within the season workspace:
     `pwsh
     python scripts/competition_workspace.py --status
     `
   - If not isolated, activate: python scripts/competition_workspace.py --season autumn2026.

1. **Implement Baseline Bot Source**:
   - Write simple bot code in the target language (e.g., `bots/autumn2026/champion_v0/`).
   - Obey official communication protocol and response format.
   - Guard against invalid actions, illegal coordinates, and unhandled exceptions.

2. **Package and Validate Artifact**:
   - Register the bot artifact into Battlelab:
     ```pwsh
     python -m battlelab bot register <path-to-bot> --name "Champion_v0" --tags "policy:baseline,version:v0"
     ```
   - Verify cryptographic snapshot integrity and entrypoint resolution.

3. **Execute Verification Matches**:
   - Run matches across representative official maps and seeds:
     - Verify completion from turn 0 through game termination.
     - Verify zero crashes (`crashed == False`).
     - Verify zero protocol violations (`protocol_violation == False`).
     - Verify zero timeouts (`timed_out == False`).
     - Measure runtime headroom: average and maximum turn latency must remain well below official per-turn limits.

4. **Register as Initial Champion**:
   - Register artifact as initial Champion v0 in Battlelab (allowed only when no champion is currently active):
     ```pwsh
     python -m battlelab champion init <artifact_id> --reason "Initial baseline Champion v0" --actor "<operator_name>"
     # Append --allow-unverified-adapter only if testing before official adapter local verification is complete
     ```
   - Verify champion registration:
     ```pwsh
     python -m battlelab champion status
     ```

5. **Run Submission Package Pre-Checks**:
   - Validate submission package archive or folder format against official rules.
   - Verify exact artifact hash, clean git working tree, and reproducible build.
   - Run a final offline smoke check using the generated submission bundle.

6. **Update Competition State**:
   - In `competition/COMPETITION_STATE.yaml`:
     - Set `workflow.current_phase: PHASE_D_CHAMPION_V0`.
     - Set `workflow.status: CHAMPION_V0_PROMOTED`.
     - Set `workflow.next_action: "Perform smoke check and request operator submission approval"`.
     - Ensure `submission_controls.operator_approval_required: true`.
     - Record champion artifact ID and promotion details in `operator_decisions.notes`.
   - Inspect overall status:
     ```pwsh
     python -m battlelab competition status
     ```

---

## HARD STOP

> [!CAUTION]
> **OPERATOR CONTROL BOUNDARY**:
> Do NOT perform official submission to the tournament ladder automatically.
> Stop here and notify the operator that Champion v0 is `READY_FOR_SUBMISSION`.
> Await explicit operator command before submitting.
