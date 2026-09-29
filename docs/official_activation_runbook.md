# Official Competition Activation Runbook

This runbook defines the exact sequence required when official rules and SDK are released. Every step must be executed in order.

---

### Step 1: Ingest Sources
- **Command**:
  ```bash
  python -m battlelab official ingest /path/to/official/docs/ --copy --json
  ```
- **Expected Output**:
  JSON output with `schema_version`, deterministic `bundle_hash`, and indexed file records.
- **Failure Condition**:
  Nonzero exit code, path traversal error, symlink rejection, or missing source files.

---

### Step 2: Verify Bundle Hashes
- **Command**:
  ```bash
  python -m battlelab official status --json
  ```
- **Expected Output**:
  JSON output displaying the discovered `source_bundle_hash` matching the ingested bundle.
- **Failure Condition**:
  Missing source bundle or bundle manifest integrity check failure.

---

### Step 3: Initialize Spec
- **Command**:
  ```bash
  python -m battlelab official spec init --source-bundle <BUNDLE_HASH> --output configs/game_spec.yaml
  ```
- **Expected Output**:
  `Initialized game specification at configs/game_spec.yaml` with canonical spec hash.
- **Failure Condition**:
  Unknown bundle hash, or inability to write destination file.

---

### Step 4: Manually Populate Source-Backed Rules
- **Action**:
  Open `configs/game_spec.yaml`. Review official documentation and populate all 23 rule sections. For every rule section:
  - Set `meaning` to the exact rule meaning.
  - Set `source_refs` with authoritative citations.
  - Set `verification_state` to `DOCUMENTED`.
  - Add `implementation_impacts`.
- **Expected Output**:
  Complete YAML file with 0 sections in `MISSING` state.
- **Failure Condition**:
  Inventing rules, omitting citations, or leaving required sections empty.

---

### Step 5: Validate Spec
- **Command**:
  ```bash
  python -m battlelab official spec validate configs/game_spec.yaml
  ```
- **Expected Output**:
  `Game specification at configs/game_spec.yaml is structurally valid.` with canonical hash.
- **Failure Condition**:
  Nonzero exit code, unsupported schema version, invalid state, or missing source references.

---

### Step 6: Install and Probe SDK
- **Command**:
  ```bash
  python -m battlelab official sdk probe --json
  ```
- **Expected Output**:
  JSON output reporting detected SDK version, available maps, and local execution capabilities.
- **Failure Condition**:
  SDK executable missing, version mismatch against game spec, or probe timeout.

---

### Step 7: Implement OfficialEngineBridge
- **Action**:
  Implement concrete `OfficialEngineBridge` in `src/battlelab/official/bridge.py` mapping official engine CLI commands, exit codes, output parsing, and replay extraction.
- **Expected Output**:
  Passes mypy static analysis and unit tests.
- **Failure Condition**:
  Uncaught exceptions during command construction or match result parsing.

---

### Step 8: Create OfficialMinimalBot
- **Action**:
  Implement minimal legal starter bot under `bots/baselines/official_minimal/` and register it with `python -m battlelab bot register`.
- **Expected Output**:
  Bot artifact successfully created and passes integrity verification.
- **Failure Condition**:
  Illegal bot moves, crash on turn 1, or invalid bot packaging.

---

### Step 9: Capture Golden Fixtures
- **Command**:
  Follow `docs/official_golden_fixture_workflow.md` to capture raw engine output, stderr, exit code, and replay from starter-vs-starter match.
- **Expected Output**:
  Golden fixture bundle saved in `tests/fixtures/official_golden/` with verified checksums.
- **Failure Condition**:
  Engine crash, corrupted replay, or non-reproducible output.

---

### Step 10: Run Official Contract Tests
- **Command**:
  ```bash
  pytest tests/contract/test_official_contract.py
  ```
- **Expected Output**:
  All contract tests pass against real engine bridge.
- **Failure Condition**:
  Contract test failure, timeout, or uncaught exception.

---

### Step 11: Run Deterministic Smoke Tournament
- **Command**:
  ```bash
  python -m battlelab match --adapter official --bot-a art_official_minimal --bot-b art_official_minimal --map default --seed 42
  ```
- **Expected Output**:
  Match finishes with valid outcome, scores, and replay hash.
- **Failure Condition**:
  Outcome is CRASH, or different replay hash on identical re-run with seed 42.

---

### Step 12: Compare Local and Remote Results
- **Command**:
  If remote scrimmage server is available, run remote test:
  ```bash
  python -m battlelab match ... # compare local replay hash with remote replay hash
  ```
- **Expected Output**:
  Parity between local headless execution and official server outcomes.
- **Failure Condition**:
  Rule discrepancies, timing divergences, or coordinate mismatches.

---

### Step 13: Dry-Run Activation
- **Command**:
  ```bash
  python -m battlelab official activate --dry-run
  ```
- **Expected Output**:
  `Status: READY_FOR_ACTIVATION`, `Blockers (0)`, `can_run_local: true`.
- **Failure Condition**:
  Any remaining blockers reported or `can_run_local: false`.

---

### Step 14: Activate Local Official Adapter
- **Command**:
  ```bash
  python -m battlelab official activate --acknowledge-sdk I_ACKNOWLEDGE_OFFICIAL_SDK_VALIDATION
  ```
- **Expected Output**:
  `Official adapter activated successfully.`
- **Failure Condition**:
  Missing explicit acknowledgement token or unresolved blockers.

---

### Step 15: Keep Submission Disabled
- **Verification**:
  ```bash
  python -m battlelab official status --json
  ```
- **Expected Output**:
  `can_run_local: true`, `can_submit: false`.
- **Failure Condition**:
  `can_submit` set to true without authoritative submission endpoint validation.
