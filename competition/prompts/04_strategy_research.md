# Prompt 04: Strategy Research Loop

Use this prompt to execute iterative, hypothesis-driven bot development.
Starts only after Champion v0 is active and `research.status: READY`.

---

## RESEARCH LOOP

```text
inspect champion & replays
→ form falsifiable hypothesis
→ create challenger bot
→ scope change strictly to hypothesis
→ paired evaluation matrix
→ statistical & regression promotion gates
→ reject OR promote to Champion
→ selective ladder reality check
→ next cycle
```

---

## EXPERIMENT PRIORITIZATION

Order experiments strictly by high-leverage impact:
1. **Strategic Concepts**: Macro objectives, expansion paths, resource balance, tech transitions, terrain dominance.
2. **Algorithmic Improvements**: Pathfinding efficiency, target prioritization, threat assessment, spatial search.
3. **Parameter Tuning**: Weight calibration, threshold tuning, safety margins.

> [!IMPORTANT]
> **ANTI-PATTERNS**:
> - Never brute-force parameter grids before exploring fundamental strategic mechanisms.
> - Never promote a bot based on anecdotal single-match wins.
> - Never submit every intermediate challenger to the official ladder.
> - Never modify frozen Battlelab core infrastructure when an experiment loses.

---

## EXECUTION INSTRUCTIONS

1. **Formulate Falsifiable Hypothesis**:
   - Create an experiment spec using `competition/templates/experiment.yaml`.
   - Define:
     - Clear expected change in behavior.
     - Predicted effect on win rate or resource metrics.
     - Falsification condition.

2. **Implement Challenger Bot**:
   - Fork from active champion source.
   - Limit code modifications strictly to the stated hypothesis scope.
   - Register the challenger artifact in Battlelab.

3. **Run Paired Evaluation**:
   - Execute paired tournament using Battlelab's experiment evaluator:
     ```pwsh
     python -m battlelab experiment run <experiment_id>
     ```
   - Both challenger and baseline must play identical maps, seeds, and paired sides.

4. **Evaluate Statistical Gates**:
   - Check promotion gate criteria:
     - Win rate >= 50.0%.
     - Paired win delta 95% CI lower bound > 0.0.
     - Zero crash rate increase.
     - Zero timeout rate increase.
     - Critical opponent group regression within tolerance.
     - Runtime headroom satisfied.
   - If gates pass: promote challenger to new Champion.
   - If gates fail: mark experiment REJECTED, archive findings, revert to Champion.

5. **Update State**:
   - Update `competition/COMPETITION_STATE.yaml` with active experiment and champion status.
