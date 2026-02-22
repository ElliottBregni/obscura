# Module Ownership

Defines module owners and required tests per change.

| Module | Owner | Required Tests | Tier |
|---|---|---|---|
| `obscura.core` | core-team | `tests/unit/obscura/core/` | stable |
| `obscura.providers` | core-team | `tests/unit/obscura/providers/` | stable |
| `obscura.auth` | security-team | `tests/unit/obscura/auth/` | stable |
| `obscura.memory` | core-team | `tests/unit/obscura/memory/` | stable |
| `obscura.tools` | tools-team | `tests/unit/obscura/tools/` | beta |
| `obscura.tools.policy` | security-team | `tests/unit/obscura/tools/test_policy.py` | beta |
| `obscura.integrations.mcp` | integrations-team | `tests/unit/obscura/integrations/mcp/` | beta |
| `obscura.integrations.a2a` | integrations-team | `tests/unit/obscura/integrations/a2a/` | experimental |
| `obscura.agent` | core-team | `tests/unit/obscura/agents/` | beta |
| `obscura.telemetry` | observability-team | `tests/unit/obscura/telemetry/` | beta |
| `obscura.server` | infra-team | `tests/unit/obscura/routes/` | beta |
| `obscura.cli` | core-team | `tests/unit/obscura/core/test_cli_*` | beta |
| `obscura.tui` | tui-team | `tests/unit/obscura/tui/` | beta |
| `obscura.parity` | core-team | `tests/unit/obscura/parity/` | experimental |
| `obscura.heartbeat` | infra-team | `tests/unit/obscura/heartbeat/` | beta |
| `obscura.openclaw_bridge` | integrations-team | `tests/unit/obscura/core/test_openclaw_bridge.py` | experimental |

## Change Policy

- **stable**: Breaking changes require RFC + migration guide. Semver major bump.
- **beta**: Breaking changes require changelog entry. Semver minor bump.
- **experimental**: Breaking changes allowed without notice. Use at own risk.

## Required Checks per PR

1. `pyright` — 0 errors, 0 warnings
2. `ruff check .` — clean
3. `pytest tests/unit/` — all pass
4. Module-specific tests for changed modules (see table above)
