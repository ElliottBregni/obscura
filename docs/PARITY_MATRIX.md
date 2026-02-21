# Parity Matrix

| Backend | Declared Percent (Semantic) |
|---|---:|
| openai | 76.2% |
| claude | 95.2% |
| copilot | 81.0% |
| localllm | 71.4% |

Overall declared semantic parity: **81.0%**

## Notes
- This matrix is derived from weighted feature declarations in `sdk/parity/profiles.py`.
- Threshold gate currently targets 79.0%; increase profile depth and supported features to raise score.
- Use scenario conformance tests under `tests/unit/sdk/parity/` to validate behavior changes before adjusting profile declarations.
