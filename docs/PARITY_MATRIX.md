# Parity Matrix

| Backend | Declared Percent (Semantic) |
|---|---:|
| openai | 76.2% |
| claude | 95.2% |
| copilot | 81.0% |
| localllm | 71.4% |

Overall declared semantic parity: **81.0%**

## Contract Gate

This repository now enforces a method-level conformance gate in addition to declared semantic scoring.

- Contract definitions: `sdk/parity/contracts.py`
- Evaluator: `sdk/parity/conformance.py`
- Tests: `tests/unit/sdk/parity/test_parity_conformance.py`

### Required Contract Categories

1. Core lifecycle (`start`, `stop`)
2. Core messaging (`send`, `stream`)
3. Session surface (`create_session`, `resume_session`, `list_sessions`, `delete_session`)
4. Tooling (`register_tool`, `get_tool_registry`)
5. Hook registration (`register_hook`)
6. Native handle and loop (`native`, `run_loop`)
7. Backend-native features:
- OpenAI: `responses_api`
- Claude: `permission_modes`
- Copilot: `event_stream`
- LocalLLM: `health_check`

## Notes
- Declared matrix is derived from weighted feature declarations in `sdk/parity/profiles.py`.
- Threshold gate currently targets 79.0% (`sdk/parity/scoring.py`).
- Conformance gate requires 100% contract pass for all supported backends in unit parity tests.
