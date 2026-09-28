# Official Competition Integration Checklist

Complete every item before declaring the official adapter active:

- [ ] 1. Obtain official documentation and calculate SHA-256 hash.
- [ ] 2. Populate `configs/game_spec.yaml` using `docs/day_zero_rule_ingestion.md`.
- [ ] 3. Verify official engine CLI executable runs locally without errors.
- [ ] 4. Implement `OfficialAdapter` in `src/battlelab/adapters/official/adapter.py`.
- [ ] 5. Implement `parse_replay()` for official replay format.
- [ ] 6. Build `OfficialMinimalBot` satisfying all legal action constraints.
- [ ] 7. Run adapter contract test against official local engine:
      `pytest tests/contract/test_official_adapter.py`
- [ ] 8. Verify local determinism: identical seed produces identical score and replay hash.
- [ ] 9. Verify official ladder test with dry-run submission flag enabled.
- [ ] 10. Confirm credentials configured only in `.env` (never committed to git).
- [ ] 11. Run 10-match smoke tournament between `OfficialMinimalBot` and official starter.
- [ ] 12. Switch default adapter in `configs/evaluation.yaml` to `official`.
