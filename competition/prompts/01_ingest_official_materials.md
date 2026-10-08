# Prompt 01: Ingest Official Competition Materials

Use this prompt when official competition documents, rules, SDK, or packages arrive.

---

## OBJECTIVE

Ingest authoritative competition materials, verify inventory integrity via SHA-256 digests, extract source-backed facts without speculation, and populate the active game specification.

---

## OPERATIONAL INSTRUCTIONS

1. **Inventory Sources**:
   - Inspect every file in the supplied official source directory.
   - Record exact relative paths, byte sizes, and SHA-256 cryptographic digests.
   - Run:
     ```pwsh
     python scripts/bootstrap_competition.py <path-to-official-materials>
     # OR directly:
     python -m battlelab official ingest <path-to-official-materials> --copy --json
     ```

2. **Extract Facts Strictly From Official Evidence**:
   - Read official documentation, rulebooks, API references, and starter bot code.
   - Every extracted value must cite an exact source file and section/line.
   - Never infer or invent unstated rules, resource limits, or commands.

3. **Initialize and Populate Canonical Game Specification**:
   - Initialize canonical 23-rule specification referencing `docs/game_spec.template.yaml`:
     ```pwsh
     python -m battlelab official spec init --source-bundle <BUNDLE_HASH> --output configs/game_spec.yaml
     ```
   - For every rule in `configs/game_spec.yaml`, populate `meaning`, citations (`<relpath>#<fragment>`), and `verification_state` using canonical `RuleVerificationState` values:
     - `verification_state: DOCUMENTED` — with verified `meaning` and official citation `source_refs: ["<relpath>#<fragment>"]`.
     - `verification_state: TEST_VERIFIED` — with `meaning`, `source_refs`, and valid repository pytest node ID in `test_coverage: ["tests/...::test_..."]`.
     - `verification_state: MISSING` — when the rule is undocumented, unverified, or conflicting (`source_refs: []`).
   - Track human-facing unknowns, ambiguities, or contradictions (`UNKNOWN`, `CONFLICTING`) in `competition/COMPETITION_STATE.yaml` under `operator_decisions.unresolved` and in the final operator report, never as invalid enum values in `configs/game_spec.yaml`.
   - Validate spec:
     ```pwsh
     python -m battlelab official spec validate configs/game_spec.yaml
     ```

4. **Update Competition Workflow State**:
   - Update operational tracking in `competition/COMPETITION_STATE.yaml`:
     - Set `workflow.current_phase: PHASE_A_INGEST`.
     - Set `workflow.status: MATERIALS_INGESTED`.
     - Set `workflow.next_action` accordingly.
     - Record any operator notes in `operator_decisions`.
     - Inspect overall status:
       ```pwsh
       python -m battlelab competition status
       ```

5. **Guardrails**:
   - Do NOT implement bot strategy.
   - Do NOT configure guessed commands or unverified parameters.
   - Do NOT resolve ambiguity by assumption.

---

## STOP CONDITIONS

Stop execution and request operator guidance ONLY IF:
1. Two official sources directly contradict each other (`CONFLICTING`).
2. An essential blocker field (e.g., build toolchain, entrypoint format, language version) is completely absent from all official materials, preventing adapter configuration.

---

## REQUIRED FINAL OUTPUT FORMAT

Your final response must use exactly this structure:

```text
STATUS: [INGESTION_COMPLETE | BLOCKED]

VERIFIED FACTS:
- [Field Name]: [Value] (Source: [File#Section])
...

UNKNOWN:
- [Field Name]: [Reason why undocumented]
...

CONFLICTING:
- [Field Name]: [Source A says X vs Source B says Y]
...

BLOCKERS:
- [None | Description of blocking ambiguity]

NEXT ACTION:
- [Proceed to Prompt 02: Configure Official Adapter | Operator action required]
```
