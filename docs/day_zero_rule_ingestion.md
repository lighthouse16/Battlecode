# Day-Zero Rule Ingestion Workflow

## Purpose
When the official Autumn Battlecode competition documentation and SDK become available, follow this exact protocol to ingest official rules without guessing or carrying over assumptions from prior seasons.

## Workflow Sequence

### Stage 1: Source Ingestion
1. Place official rulebook PDFs, specification documents, or SDK release archives into an external directory (e.g. `raw_official_sources/`).
2. Run source ingestion:
   ```bash
   battlelab official ingest raw_official_sources/ --copy --json
   ```
3. Record the generated deterministic `bundle_hash` (e.g., `a1b2c3d4...`).
4. Verify the generated manifest at `data/official/source_bundles/<bundle_hash>/source_manifest.json`.

### Stage 2: Initialize & Populate Typed Game Specification
1. Initialize the typed game specification from the verified bundle:
   ```bash
   battlelab official spec init \
     --source-bundle <bundle_hash> \
     --output configs/game_spec.yaml
   ```
2. Manually populate all 23 rule sections in `configs/game_spec.yaml`:
   - Set `meaning` to the exact rule meaning.
   - Add authoritative document citations in `source_refs` (e.g., `["Rulebook.pdf p. 4, Section 2.1"]`).
   - Change `verification_state` from `MISSING` to `DOCUMENTED`.
   - List `implementation_impacts` (e.g. adapter outcome mapping, bot action schemas).
   - Once automated tests verify the rule, add test names to `test_coverage` and update `verification_state` to `TEST_VERIFIED`.
3. Validate the specification:
   ```bash
   battlelab official spec validate configs/game_spec.yaml
   ```
   Ensure validation succeeds with zero errors.

### Stage 3: SDK Installation & Bridge Implementation
1. Install official SDK following official distribution instructions.
2. Implement `OfficialEngineBridge` in `src/battlelab/official/bridge.py` (or inject custom bridge).
3. Probe SDK:
   ```bash
   battlelab official sdk probe
   ```
4. Create the minimal legal official bot in `bots/baselines/official_minimal/`.
5. Capture golden fixtures according to `docs/official_golden_fixture_workflow.md`.
6. Run contract and readiness checks:
   ```bash
   battlelab official readiness --check
   battlelab official activate --dry-run
   ```
