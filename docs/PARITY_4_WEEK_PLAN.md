# 4-Week Parity Execution Plan (Parallel Workstreams)

This plan maps to four parallel workstreams as if run by separate agents.

## Agent A (Week 1): Object Model + Feature Taxonomy
- Own `sdk/parity/models.py`
- Own `sdk/parity/features.py`
- Keep all parity concepts in typed dataclasses/enums
- Deliver test coverage for model and taxonomy integrity
- Status: **executing**
- Completed:
  - Added method-contract and conformance dataclasses to `sdk/parity/models.py`
  - Added contract definitions in `sdk/parity/contracts.py`

## Agent B (Week 2): Scenario Runner + Semantic Conformance Scenarios
- Own `sdk/parity/scenarios.py`
- Own `sdk/parity/runner.py`
- Define declarative scenario specs + expectations
- Ensure scenario checks are backend-agnostic and readable
- Status: **executing**
- Completed:
  - Added backend-agnostic method conformance evaluator in `sdk/parity/conformance.py`
  - Added backend conformance tests in `tests/unit/sdk/parity/test_parity_conformance.py`

## Agent C (Week 3): Scoring + Threshold Gate
- Own `sdk/parity/scoring.py`
- Own `tests/unit/sdk/parity/test_parity_threshold.py`
- Produce weighted backend and overall parity percentages
- Gate regressions with threshold assertion
- Status: **in progress**
- Next:
  - Blend declared semantic score with method-conformance score in one composite report
  - Raise threshold progressively to 100% once unsupported features are implemented

## Agent D (Week 4): Reporting + Docs + Alignment Tests
- Own `sdk/parity/report.py`
- Own `docs/PARITY_MATRIX.md`
- Add profile/backends alignment tests
- Keep matrix and residual risk output easy to scan
- Status: **executing**
- Completed:
  - Updated matrix doc with explicit contract gate and required categories
- Next:
  - Auto-generate matrix from parity report in CI
  - Include per-contract backend pass/fail table

## Done Criteria
- All parity package tests pass
- Threshold test passes
- Backend capability claims align with declared profiles
- Parity report generated and committed

## Current Execution Snapshot

1. Foundation implemented:
- Typed contracts
- Conformance evaluator
- Backend-specific native feature checks

2. Test gates implemented:
- Contract conformance tests across OpenAI, Claude, Copilot, LocalLLM
- Existing parity scoring and alignment tests retained

3. Remaining path to 100%:
- Implement currently partial/unsupported declared features in profiles
- Increase threshold gate until 100%
- Add behavioral scenario depth for tool lifecycle, streaming semantics, and error equivalence
