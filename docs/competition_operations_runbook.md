# Competition Operations Runbook

## Purpose

This is the primary operating procedure for a solo competitor using AI development agents.
It minimizes context switching while preserving independent evidence and explicit human control.
It does not contain game-mechanic assumptions and never performs an official submission.

The canonical schedule and checklist live in `configs/competition.yaml`. Dates whose exact lock
time has not been published are deliberately stored at day precision and displayed as
`UNCONFIRMED`.

## Responsibility Boundary

| Actor | Responsibility |
|---|---|
| Dang | Select hypothesis, approve promotion, review GitHub evidence, submit manually |
| Coding agent | Implement one scoped challenger and produce a diff |
| Battlelab | Execute matches, measure regressions, and enforce promotion/release gates |
| GitHub Actions | Re-run the repository gates independently on the reviewed commit |
| Official platform | Run the submitted deterministic artifact |

The coding agent must not write a change, declare it successful, and submit it without these
separate gates.

## The Normal Daily Loop

Start every session with:

```bash
python -m battlelab competition status
python -m battlelab competition next
```

Work on only the returned checkpoint or the single active challenger. After retaining evidence:

```bash
python -m battlelab competition complete \
  --stage <STAGE_ID> \
  --step <STEP_ID> \
  --actor Dang \
  --evidence <URL_HASH_OR_PATH> \
  --note "<CONCISE_CONTEXT>"
```

The checkpoint is written atomically to `data/competition/state.json`. Each event contains the
previous event hash and its own canonical SHA-256 hash. Duplicate completion is rejected.

Use `status --deep` at stage transitions, before promotion, and before release freeze:

```bash
python -m battlelab competition status --deep
```

This intentionally performs heavier Git and official-readiness checks.

## GitHub Review Loop

Release verification queries GitHub directly through the authenticated GitHub CLI. Confirm it is
available before the competition:

```bash
gh auth status
```

1. The coding agent implements one falsifiable change on a branch.
2. Review the complete diff on GitHub. Reject unrelated changes.
3. Require GitHub Actions to pass on the exact reviewed commit.
4. Run the Battlelab experiment and inspect segment regressions and representative losses.
5. Promote only through the normal statistical gate.
6. Keep the GitHub Actions run URL and reviewed commit SHA as release evidence.

Do not accept a local success message as CI evidence. The release gate requires the referenced
run to report `status=completed`, `conclusion=success`, and the exact reviewed commit SHA. Do not
attach evidence from another commit or repository.

## Day-0 Straight-Line Procedure

On 12 October 2026, begin with `competition next` and complete the Sprint 1 checklist in order:

1. Ingest and hash authoritative sources.
2. Populate the source-backed game specification and executable rule tests.
3. Probe the explicitly declared SDK executable.
4. Implement the official bridge and minimal legal bot.
5. Pass official readiness, golden fixtures, replay parsing, and determinism checks.
6. Submit one stable legal bot manually and retain the official receipt.

The detailed integration commands remain in `docs/official_activation_runbook.md`. If readiness
is blocked, fix the reported blocker; do not bypass or relabel it.

## Challenger Loop

At most one challenger may be active:

1. State one falsifiable hypothesis.
2. Register an immutable challenger artifact.
3. Run smoke and screening matches.
4. Stop early if it fails legality, runtime, or broad robustness checks.
5. Run the full paired matrix only for survivors.
6. Inspect failure clusters and holdout results.
7. Promote or reject; never keep an ambiguous challenger as the active champion.

The public ladder is external validation and meta evidence, not the sole training objective.

## Release Freeze

Freeze only the active champion and only after GitHub review and CI:

```bash
python -m battlelab competition release freeze \
  --artifact <CHAMPION_ARTIFACT_ID> \
  --experiment <PROMOTED_EXPERIMENT_ID> \
  --actor Dang \
  --reviewed-commit <EXACT_GIT_SHA> \
  --ci-run-url https://github.com/<OWNER>/<REPO>/actions/runs/<RUN_ID> \
  --acknowledge I_ACKNOWLEDGE_RELEASE_FREEZE \
  --note "International qualifier candidate"
```

The freeze fails if any required evidence is absent or inconsistent. It binds the release to:

- immutable artifact and source hashes;
- active champion identity;
- promoted experiment, when provided;
- competition-plan hash;
- official source bundle and game-spec hashes;
- SDK version;
- exact Git commit and synchronized upstream;
- GitHub Actions run URL;
- human actor and timestamp.

Immediately before submission:

```bash
python -m battlelab competition release verify latest
```

Then submit manually on the official platform and record its receipt as the stage checkpoint.

## Emergency Rule

Inside the configured freeze window, accept only a defect that threatens legality, crashes,
timeouts, corrupted packaging, or a clearly demonstrated critical matchup regression. Any fix
must create a new immutable artifact, pass the full release gate, and produce a new release
manifest. Never modify a frozen artifact or its manifest.

## Recovery

- If an experiment fails: reject it and keep the current champion.
- If the worktree is dirty: identify and commit or deliberately discard the exact change before
  freezing; never bypass the gate.
- If Git is not synchronized: fetch and reconcile before freezing.
- If official readiness fails: follow the named blocker to the authoritative source or test.
- If a release verification fails: do not submit; freeze a new candidate after resolving the
  discrepancy.
- If the competition config changes after operations begin: stop and review the plan change. The
  control plane reports config drift and rejects new checkpoints rather than silently rebinding
  old evidence. Commit and review the authoritative correction, then reconcile explicitly:

  ```bash
  python -m battlelab competition reconcile \
    --actor Dang \
    --reason "<WHY_THE_AUTHORITATIVE_PLAN_CHANGED>" \
    --reviewed-commit <GIT_SHA> \
    --acknowledge I_ACKNOWLEDGE_COMPETITION_PLAN_CHANGE
  ```
