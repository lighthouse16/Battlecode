# Official Competition Integration Checklist

Complete every item before declaring the official adapter active in production:

- [ ] 1. Obtain authoritative official documentation and run `battlelab official ingest <path> --copy`.
- [ ] 2. Verify deterministic bundle hash at `data/official/source_bundles/<hash>/source_manifest.json`.
- [ ] 3. Initialize typed game specification via `battlelab official spec init --source-bundle <hash> --output configs/game_spec.yaml`.
- [ ] 4. Populate all 23 rule sections in `configs/game_spec.yaml` with authoritative citations and `DOCUMENTED` state.
- [ ] 5. Run `battlelab official spec validate configs/game_spec.yaml` and confirm zero errors.
- [ ] 6. Install official SDK and verify with `battlelab official sdk probe`.
- [ ] 7. Confirm detected SDK version matches configured version in `configs/game_spec.yaml`.
- [ ] 8. Implement rule-specific `OfficialEngineBridge` in `src/battlelab/official/bridge.py`.
- [ ] 9. Implement minimal legal starter bot under `bots/baselines/official_minimal/`.
- [ ] 10. Capture golden fixtures using `docs/official_golden_fixture_workflow.md`.
- [ ] 11. Run official contract tests: `pytest tests/contract/test_official_contract.py`.
- [ ] 12. Verify local determinism: identical seed reproduces bit-exact normalized gameplay and replay hash.
- [ ] 13. Run `battlelab official readiness --check` and verify all critical blockers cleared.
- [ ] 14. Execute dry-run activation: `battlelab official activate --dry-run`.
- [ ] 15. Activate official adapter: `battlelab official activate --acknowledge-sdk I_ACKNOWLEDGE_OFFICIAL_SDK_VALIDATION`.
- [ ] 16. Keep ladder submission disabled until submission endpoints are explicitly validated.
