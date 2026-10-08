# Prompt 05: Ladder Replay Analysis

Use this prompt to ingest and evaluate official tournament ladder matches and external replays.

---

## GOVERNANCE PRINCIPLE

> [!WARNING]
> **REALITY CHECK ONLY**:
> Ladder match evidence provides an external reality check on meta trends and opponent archetypes.
> It is **NOT** an automatic trigger for code changes.
> Never modify the active Champion directly based on ladder matches.
> Never overfit bot logic to defeat a single external opponent.
> Every derived insight must be formulated as a hypothesis and proven through local paired evaluation.

---

## ANALYSIS WORKFLOW

1. **Ingest External Match Data**:
   - Save official match replay files and metadata into an analysis directory (e.g., `data/ladder_replays/<submission_id>/`).
   - Run replay parser or official replay viewer.

2. **Categorize Observations Explicitly**:
   Classify all conclusions under one of three strict epistemological categories:
   - `OBSERVED`: Direct factual data extracted from telemetry (e.g., unit died on turn 42, turn time spiked to 92ms).
   - `INFERRED`: Logical deduction based on patterns (e.g., opponent likely prioritizes economy over defense based on early unit composition).
   - `UNKNOWN`: Unobservable factors (e.g., opponent's internal state machine, hidden fog-of-war decisions).

3. **Diagnose Loss Mechanisms**:
   - **Tactical**: Combat positioning, engagement timing, local numerical disadvantages.
   - **Strategic**: Delayed expansion, tech tree bottlenecks, resource starvation.
   - **Runtime**: Latency spikes, memory pressure, edge-case timeouts.
   - **Exploit Exposure**: Vulnerability to cheese strategies or boundary condition probing.

4. **Identify Meta Trends**:
   - Group observed opponents into functional archetypes (e.g., Rush, Swarm, Boom, Defensive).
   - Assess performance correlations across different map layouts and spawn geometries.

5. **Generate Falsifiable Hypotheses**:
   - Translate weaknesses into testable candidate hypotheses for `Prompt 04: Strategy Research Loop`.
   - Document in `competition/templates/ladder_report.md`.

---

## OUTPUT FORMAT

Produce a completed report following `competition/templates/ladder_report.md`.
Conclude with prioritized candidate hypotheses ready for the standard experiment pipeline.
