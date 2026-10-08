# Prompt 02: Configure Official Adapter

Use this prompt after Prompt 01 has ingested materials and verified core execution facts.

---

## OBJECTIVE

Configure and verify Battlelab's official competition adapter subsystem against the real official SDK, preserving strict fail-closed semantics and zero invented interfaces.

---

## PREREQUISITES

- `competition/COMPETITION_STATE.yaml` shows `workflow.status: MATERIALS_INGESTED`.
- Official materials ingested with valid bundle hash (`python -m battlelab official status`).
- Build command, match command, runtime, and entrypoint rules populated in `configs/game_spec.yaml` with valid structure (`python -m battlelab official spec validate configs/game_spec.yaml`).

---

## OPERATIONAL INSTRUCTIONS

1. **Use Existing Official Subsystem**:
   - Utilize Battlelab's existing official adapter infrastructure in `src/battlelab/official/` and `src/battlelab/adapters/official/`.
   - Do NOT create a second or parallel adapter subsystem.

2. **Bind Exact SDK Executable and Identity**:
   - Locate the official engine executable and launcher script.
   - Record exact filesystem path, file size, and SHA-256 hash.
   - Verify SDK version matches official documentation.
   - Run official SDK probe via CLI:
     ```pwsh
     python -m battlelab official sdk probe <path-to-engine>
     ```

3. **Verify Map Discovery**:
   - Verify maps are discovered dynamically from the official SDK/filesystem.
   - Never rely on hardcoded map lists.

4. **Verify Starter Bot Build**:
   - Build official starter/reference bot using documented build invocation.
   - Verify output artifact satisfies manifest integrity.

5. **Verify Real Local Match Execution**:
   - Execute a real two-player match between two instances of starter/reference bot using official engine.
   - Observe real exit codes, stdout, stderr, and generated replay file.
   - Verify process cleanup under timeout or early termination.

6. **Verify Result Normalization and Replay Parsing**:
   - Verify Battlelab accurately maps official engine outcome, scores, and winner into `MatchResult`.
   - Verify replay parser parses generated official replay without corruption.
   - Confirm Battlelab outcome matches official engine stdout verdict exactly.

7. **Verify Contract and Regression Tests**:
   - Run official adapter contract tests:
     ```pwsh
     python -m pytest tests/contract/test_official_contract.py
     ```
   - All tests must pass cleanly.
   - Never mock or fake readiness in production config.

8. **Update Workflow State**:
   - Update `competition/COMPETITION_STATE.yaml`:
     - Set `workflow.current_phase: PHASE_C_OFFICIAL_ADAPTER`.
     - Set `workflow.status: ADAPTER_VERIFIED`.
     - Set `workflow.next_action: "Proceed to Prompt 03: Build Champion v0"`.
   - Inspect overall status:
     ```pwsh
     python -m battlelab competition status
     ```

---

## STOP CONDITIONS

Stop execution and request operator guidance ONLY IF:
1. Official runtime behavior contradicts official documentation (e.g. unexpected returncode, conflicting argument schema).
2. Official command or API is ambiguous or undocumented.
3. Required engine components or dependencies are missing from the host.
4. A destructive architecture or database redesign appears necessary.

---

## NEXT ACTION

Proceed to `competition/prompts/03_build_champion_v0.md`.
