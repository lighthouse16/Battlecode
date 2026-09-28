# Battlelab Experiment Protocol for AI Coding Agents

## Objective
Prevent uncontrolled bot modifications and false claims of success by enforcing a rigorous, hypothesis-driven improvement loop.

## The 12-Step Loop

1. **Observe Weakness**: Inspect logs, replays, or segment breakdowns from the latest experiment report.
2. **State Falsifiable Hypothesis**: Write exactly one testable premise (e.g., "Prioritizing corner resource nodes will increase win rate on sparse maps").
3. **Implement Challenger**: Make minimal, focused code adjustments in `bots/challengers/`.
4. **Register Artifact**: Execute `battlelab bot register bots/challengers/... --name Challenger_vN`.
5. **Run Pre-flight Checks**: Verify unit tests and adapter compatibility pass without errors.
6. **Create Experiment**: Register the experiment with `battlelab experiment create`.
7. **Execute Paired Evaluation**: Run `battlelab experiment run <exp_id>` across all configured maps and seeds with side-swapping.
8. **Analyze Regression & Confidence**: Inspect Wilson score intervals and paired bootstrap differences in `report.md`.
9. **Inspect Replays**: Examine representative replays of losses or unexpected states.
10. **Official Remote Check**: (When official SDK is active) Run a dry-run test against official sandbox.
11. **Gate Evaluation & Promotion**:
    - If criteria are met: execute `battlelab experiment promote <exp_id>`.
    - If criteria fail: Challenger is automatically rejected with explicit violation reasons recorded.
12. **Formulate Next Iteration**: Formulate the next single hypothesis without touching the champion directly.
