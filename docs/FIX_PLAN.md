# 🔧 OBSCURA FIX PLAN

## Current Problems

### 1. E2E Tests Failing (401 Unauthorized)
**Root Cause:** Environment variables not reaching the uvicorn server process when run through `uv run`.

**Why:** 
- `uv run` spawns a subprocess that may not inherit env vars correctly
- `export VAR=value` in shell → `uv run python` doesn't always pass through

### 2. Package Name Issues
**Root Cause:** Project was renamed from `fv-copilot` to `obscura` but references remain.

### 3. CI/CD Failures
**Root Cause:** GitHub Actions not installing all required dependencies.

---

## Fix Steps

### STEP 1: Fix Environment Variable Passing (CRITICAL)

Option A: Use explicit env in Python wrapper script
```python
#!/usr/bin/env python3
import os
os.environ['OBSCURA_AUTH_ENABLED'] = 'false'
os.environ['OTEL_ENABLED'] = 'false'

from sdk.server import create_app
from sdk.config import ObscuraConfig

config = ObscuraConfig.from_env()
app = create_app(config)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

Option B: Fix the shell script to properly export vars before uv run

Option C: Create a config override file that tests can write to

**RECOMMENDED: Option A** - Create a dedicated test server entry point

---

### STEP 2: Clean Up fv-copilot References

Files to check:
- sync.py: LOCK_FILE path
- docs/*.md: All references
- sdk/cli.py: Error message
- .venv files (regenerate)

---

### STEP 3: Fix CI/CD

Update `.github/workflows/test.yml`:
- Install with `pip install -e ".[dev,server,telemetry]"`
- Use Python 3.13.5 (not 3.x)
- Add cache busting

---

### STEP 4: Verify E2E Tests Work

After fixes, run:
```bash
./scripts/run-e2e-tests.sh
```

---

## Implementation Priority

1. ✅ Fix pyproject.toml (already done - renamed to obscura)
2. 🔄 Create test server wrapper script
3. 🔄 Update run-e2e-tests.sh to use wrapper
4. 🔄 Clean remaining fv-copilot references
5. 🔄 Fix CI workflow
6. 🔄 Verify all tests pass

---

## Testing Checklist

- [ ] Server starts with auth disabled via env var
- [ ] E2E tests pass without 401 errors
- [ ] Unit tests still pass
- [ ] CI passes on GitHub
- [ ] TUI can connect to server

---

## Immediate Actions Needed

1. Create `scripts/test_server.py` wrapper
2. Update `scripts/run-e2e-tests.sh` to use it
3. Clean up remaining fv-copilot refs
4. Test locally
5. Push and verify CI
