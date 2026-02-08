# 🚀 OBSCURA COMPLETION PLAN

## Current State Assessment

### ✅ What's Working
- Project renamed from fv-copilot to obscura
- Core SDK structure in place
- 5 TUI screens implemented (dashboard, chat, plan, code, diff)
- Server starts with auth disabled via env vars
- E2E test framework ready

### ❌ What's Broken
- Routes still enforce auth via `Depends()` even when middleware disabled
- TUI not connected to real API
- E2E tests fail with 401
- No complete working examples

---

## PHASE 1: Fix Auth Bypass (Priority 1)

**Goal:** Make all API endpoints work without auth when `auth_enabled=false`

**Changes Needed:**
1. Create conditional auth dependency that returns mock user when auth disabled
2. Update all route handlers to use this dependency
3. Ensure all endpoints work without Authorization header

**Files to Modify:**
- `sdk/auth/rbac.py` - Add `get_current_user_optional()` 
- `sdk/server.py` - Update all `Depends()` calls

---

## PHASE 2: TUI API Integration (Priority 2)

**Goal:** Connect TUI to real Obscura API

**Changes Needed:**
1. Create TUI API client module
2. Replace mock data with real API calls
3. Add WebSocket support for real-time updates
4. Test all screens with live data

**Files to Create/Modify:**
- `obscura/tui/client.py` - API client
- `obscura/tui/screens/*.py` - Replace mocks

---

## PHASE 3: QA Testing (Priority 3)

**Goal:** Full test coverage and validation

**Test Plan:**
1. **Unit Tests** - Verify all components
2. **Integration Tests** - API + database
3. **E2E Tests** - Full workflows
4. **Performance Tests** - Load testing
5. **Security Tests** - Auth bypass validation

---

## PHASE 4: UAT & Documentation (Priority 4)

**Goal:** Working examples and user guide

**Deliverables:**
1. Complete README with examples
2. Working demo video/script
3. Troubleshooting guide
4. API reference docs
5. Deployment guide

---

## PHASE 5: Multi-Agent Demo (Priority 5)

**Goal:** Spin up 10 agents and demonstrate coordination

**Demo Scenario:**
1. Code review workflow
2. Multi-agent task decomposition
3. Memory sharing between agents
4. Real-time collaboration

---

## Execution Order

1. **Fix auth bypass** (30 min)
2. **Verify server works without auth** (10 min)
3. **Run E2E tests** (10 min)
4. **Create working examples** (30 min)
5. **Full QA validation** (30 min)
6. **Documentation** (30 min)
7. **Multi-agent demo** (20 min)

**Total Estimated Time:** ~3 hours

---

## Success Criteria

- [ ] All API endpoints work without auth when disabled
- [ ] E2E tests pass
- [ ] TUI connects to real API
- [ ] 10 agents can run simultaneously
- [ ] Complete user guide with examples
- [ ] CI passes

---

## Agents Needed

| # | Agent | Task |
|---|-------|------|
| 1 | AuthFix | Fix auth bypass |
| 2 | ServerTest | Verify server |
| 3 | E2ETest | Run E2E tests |
| 4 | TUIClient | Build TUI API client |
| 5 | TUIConnect | Connect TUI screens |
| 6 | ExampleDev | Create working examples |
| 7 | QALead | QA validation |
| 8 | DocWriter | Documentation |
| 9 | DemoSetup | Multi-agent demo |
| 10 | FinalCheck | Final validation |
