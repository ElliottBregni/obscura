# 📋 Obscura Project Tracker

> Multi-SDK Support & Full Agent Mode

**Status:** Planning Phase  
**Last Updated:** 2025-02-07 by Molty  
**Priority:** Backlog (awaiting user input)

---

## 🎯 Project Goals

1. **Multi-SDK Support** — Add LM Studio, BYOK, and other backend SDKs
2. **Full Agent Mode** — Bypass unified wrapper, expose native Claude/Copilot features
3. **Terminal UI (TUI)** — Rich terminal interface for agent management (Claude leading)
4. **User Choice** — Select SDK per-agent or per-request
5. **Documentation** — Complete user guides with screenshots
6. **Quality** — Full test, QA, UAT cycle

---

## 📊 Progress Overview

| Phase | Status | Progress | Est. Time | Lead |
|-------|--------|----------|-----------|------|
| Phase 1: Backend SDK Support | ⏳ Not Started | 0% | 1 week | Molty |
| Phase 2: Terminal UI (TUI) | ⏳ Not Started | 0% | 1 week | Claude |
| Phase 3: Full Agent Mode | ⏳ Not Started | 0% | 1 week | Molty |
| Phase 4: Documentation | ⏳ Not Started | 0% | 1 week | Shared |
| Phase 5: Testing & QA | ⏳ Not Started | 0% | 1 week | Molty |
| Phase 6: UAT | ⏳ Not Started | 0% | 1 week | Molty + Claude |

**Overall Progress:** 0%

### Team
- **Claude:** TUI architecture and implementation
- **Molty:** Backend SDK, Full Agent Mode, Tests, UAT coordination
- **Collaboration:** Documentation, Integration points, UAT execution

---

## 📁 Phase 1: Backend SDK Support

**Goal:** Support LM Studio, BYOK, and other AI SDKs

### Tasks

#### 1.1 Create Backend Architecture
- [ ] Create `sdk/backends/` directory structure
- [ ] Implement `sdk/backends/base.py` - Abstract backend interface
- [ ] Define `BackendConfig` dataclass with all provider options
- [ ] Create backend router/selector
- [ ] Update `Backend` enum to include new providers
- [ ] Add configuration validation

#### 1.2 LM Studio Backend
- [ ] Create `sdk/backends/lmstudio.py`
- [ ] Implement connection to local LM Studio endpoint
- [ ] Support model listing/discovery
- [ ] Handle LM Studio-specific parameters
- [ ] Add connection health check
- [ ] Error handling for connection failures

#### 1.3 OpenAI-Compatible Backend (BYOK)
- [ ] Create `sdk/backends/openai_compatible.py`
- [ ] Support generic OpenAI-compatible endpoints
- [ ] Configurable base_url and api_key
- [ ] Support OpenRouter, Together, and other providers
- [ ] Handle different auth schemes
- [ ] Model discovery/mapping

#### 1.4 Direct Provider Backends
- [ ] Create `sdk/backends/anthropic_native.py` - Direct Anthropic SDK
- [ ] Create `sdk/backends/github_copilot.py` - Direct Copilot SDK
- [ ] Ensure feature parity with existing unified wrapper

#### 1.5 Integration
- [ ] Update `ObscuraClient` to use backend router
- [ ] Add backend selection to agent spawn API
- [ ] Update CLI with `--backend` flag
- [ ] Update Web UI with backend dropdown
- [ ] Ensure memory/telemetry works across all backends

#### 1.6 Testing
- [ ] Unit tests for each backend class (>80% coverage)
- [ ] Integration tests with mocked SDKs
- [ ] Connection tests (with fallback to mocks)
- [ ] Configuration validation tests
- [ ] Error handling tests

**Deliverables:**
- [ ] Working LM Studio backend
- [ ] Working BYOK backend
- [ ] Test coverage >80%
- [ ] API documentation

**Blockers:**
- None

**Notes:**
- Waiting for user to specify priority backend (LM Studio vs BYOK)
- Need user LM Studio base_url for testing
- Need list of BYOK providers to support

---

## 📁 Phase 2: Terminal UI (TUI)

**Goal:** Rich terminal interface for agent management
**Lead:** Claude
**Support:** Molty (tests + UAT)

### Tasks

#### 2.1 TUI Architecture
- [ ] Choose TUI framework (Textual, Rich, etc.)
- [ ] Design screen/layout structure
- [ ] Define keybindings and navigation
- [ ] Plan agent visualization components

#### 2.2 Core Screens
- [ ] **Dashboard Screen** — Agent overview, stats, status
- [ ] **Agent List Screen** — Browse, filter, search agents
- [ ] **Agent Detail Screen** — View agent status, logs, memory
- [ ] **Spawn Screen** — Create new agents with full options
- [ ] **Chat/Interact Screen** — Real-time agent interaction
- [ ] **Memory Browser** — Explore agent memory (semantic + key-value)
- [ ] **Settings Screen** — Configure backends, defaults

#### 2.3 Real-time Features
- [ ] Live agent status updates (WebSocket integration)
- [ ] Streaming agent output display
- [ ] Animated status indicators
- [ ] Notification system for agent events

#### 2.4 Backend Integration
- [ ] Multi-SDK selector in spawn screen
- [ ] Backend connection tester
- [ ] Feature discovery display (what each backend supports)
- [ ] Full mode toggle with feature checklist

#### 2.5 Testing (Molty)
- [ ] Unit tests for TUI components
- [ ] Screen navigation tests
- [ ] Keyboard shortcut tests
- [ ] Accessibility tests (colors, contrast)
- [ ] Performance tests (render speed)

#### 2.6 UAT (Molty + Claude)
- [ ] UAT test plan for TUI
- [ ] Screen-by-screen validation
- [ ] Keybinding verification
- [ ] Real agent workflow testing
- [ ] Bug tracking and fixes

**Deliverables:**
- [ ] Working TUI application
- [ ] All 7 screens functional
- [ ] Real-time updates working
- [ ] Test coverage >80%
- [ ] UAT sign-off

**Dependencies:**
- Phase 1 (Backend SDK) for multi-sdk selector
- WebSocket endpoints for real-time features

**Notes:**
- Claude leading architecture and implementation
- Molty supporting on test strategy and UAT execution
- Coordinate with Phase 1 for backend integration points

---

## 📁 Phase 3: Full Agent Mode

**Goal:** Bypass unified wrapper, use native SDK features

### Tasks

#### 2.1 Architecture
- [ ] Create `sdk/full_agents/` directory
- [ ] Define `FullAgentConfig` dataclass
- [ ] Design feature detection system
- [ ] Plan native feature exposure strategy

#### 2.2 Claude Full Mode
- [ ] Create `sdk/full_agents/claude_full.py`
- [ ] Use direct Anthropic SDK client
- [ ] Expose thinking blocks
- [ ] Expose tool use capabilities
- [ ] Expose vision capabilities
- [ ] Expose extended thinking
- [ ] Document feature matrix

#### 2.3 Copilot Full Mode
- [ ] Create `sdk/full_agents/copilot_full.py`
- [ ] Use direct Copilot SDK client
- [ ] Expose Copilot-specific features
- [ ] Document feature matrix

#### 2.4 Integration
- [ ] Add `full_sdk_mode: bool` to agent config
- [ ] Add `native_features: List[str]` to config
- [ ] Update agent spawn to support full mode
- [ ] Route to full mode vs compatible mode
- [ ] Ensure auth/telemetry/memory still works

#### 2.5 API Updates
- [ ] Add `GET /api/v1/backends` - List available backends
- [ ] Update `POST /api/v1/agents` - Add backend parameter
- [ ] Add `GET /api/v1/agents/{id}/features` - List native features
- [ ] Document feature parity matrix

#### 2.6 Testing
- [ ] Unit tests for full mode agents
- [ ] Integration tests with real SDKs (mocked)
- [ ] Feature detection tests
- [ ] Full mode vs compatible mode comparison tests

**Deliverables:**
- [ ] Full mode agents for Claude and Copilot
- [ ] Feature parity matrix documented
- [ ] Tests for full mode

**Blockers:**
- Phase 1 completion
- User to specify which native features are priority (thinking, tool_use, vision, etc.)

**Notes:**
- Need to decide: should full mode be per-agent or per-request?
- How to handle features not available in compatible mode?

---

## 📁 Phase 4: Documentation

**Goal:** Complete user guides with screenshots

### Tasks

#### 3.1 User Guides
- [ ] Create `docs/GUIDE_MULTI_SDK.md`
  - [ ] Overview of supported SDKs
  - [ ] Comparison table
  - [ ] When to use which backend
  - [ ] Configuration examples
  
- [ ] Create `docs/GUIDE_FULL_MODE.md`
  - [ ] What is full mode vs compatible mode
  - [ ] Feature comparison
  - [ ] How to enable full mode
  - [ ] Limitations and trade-offs
  
- [ ] Create `docs/GUIDE_LM_STUDIO.md`
  - [ ] LM Studio setup instructions
  - [ ] Connection configuration
  - [ ] Model selection
  - [ ] Troubleshooting
  
- [ ] Create `docs/GUIDE_BYOK.md`
  - [ ] BYOK overview
  - [ ] Supported providers
  - [ ] API key configuration
  - [ ] Security best practices

#### 3.2 Screenshots
- [ ] Web UI: SDK selector dropdown
- [ ] Web UI: Full mode toggle
- [ ] Web UI: Feature checklist (full mode)
- [ ] Web UI: Connection test button
- [ ] CLI: `obscura backends list` output
- [ ] CLI: Spawn with `--backend lmstudio`
- [ ] CLI: Spawn with `--full-mode`
- [ ] Agent running with different backends

#### 3.3 README Updates
- [ ] Update main README.md with multi-sdk info
- [ ] Add backend selection examples
- [ ] Document full mode
- [ ] Add architecture diagram

#### 3.4 API Documentation
- [ ] Document new endpoints
- [ ] Update existing endpoint docs
- [ ] Add backend-specific examples
- [ ] Document feature matrix

**Deliverables:**
- [ ] 4 comprehensive guides
- [ ] 10+ screenshots
- [ ] Updated README

**Blockers:**
- Phase 1 & 2 completion
- Web UI updates (for screenshots)

**Notes:**
- Need to decide: actual screenshots vs mockups?
- Need to ensure Web UI is updated before taking screenshots

---

## 📁 Phase 5: Testing & QA

**Goal:** Full test coverage, QA validation

### Tasks

#### 4.1 Unit Tests
- [ ] All backend classes (>80% coverage)
- [ ] Backend router/selector
- [ ] Full mode agent classes
- [ ] Configuration validation
- [ ] Feature detection

#### 4.2 Integration Tests
- [ ] Real LM Studio connection (with mock fallback)
- [ ] Real OpenAI-compatible endpoint tests
- [ ] Full mode agent lifecycle
- [ ] Backend switching
- [ ] Memory/telemetry across backends

#### 4.3 End-to-End Tests
- [ ] Spawn agent → Run task → Verify output (each backend)
- [ ] Full mode vs compatible mode comparison
- [ ] Error scenarios (connection failure, invalid config)
- [ ] Performance benchmarks (latency, throughput)

#### 4.4 Security Audit
- [ ] API key handling review
- [ ] BYOK key storage security
- [ ] Transport security (TLS)
- [ ] Input validation
- [ ] Rate limiting

#### 4.5 QA Checklist
- [ ] All backends connect successfully
- [ ] Full mode exposes native features
- [ ] Compatible mode works uniformly
- [ ] Error handling graceful
- [ ] API keys secure
- [ ] Performance acceptable (< 200ms overhead)
- [ ] No regressions in existing features

**Deliverables:**
- [ ] Test suite with >90% coverage
- [ ] QA report
- [ ] Performance report
- [ ] Security audit report

**Blockers:**
- Phase 1, 2, 3 completion

---

## 📁 Phase 6: UAT

**Goal:** User acceptance testing, production ready

### Tasks

#### 6.1 UAT Preparation
- [ ] Deploy to staging environment
- [ ] Create UAT test cases (see below)
- [ ] Prepare UAT environment documentation
- [ ] Create feedback collection form

#### 6.2 UAT Test Cases

##### Backend & SDK Tests

| ID | Test | Expected Result | Status |
|----|------|-----------------|--------|
| UAT-1 | Spawn LM Studio agent | Agent connects to local endpoint | ⏳ |
| UAT-2 | Spawn BYOK agent | Agent uses custom API key | ⏳ |
| UAT-3 | Full mode Claude | Can access thinking blocks | ⏳ |
| UAT-4 | Full mode Copilot | Can access Copilot-specific features | ⏳ |
| UAT-5 | Switch backends | Same agent config works across backends | ⏳ |
| UAT-6 | Memory persistence | Memory works across all backends | ⏳ |
| UAT-7 | Telemetry | All backends emit correct traces | ⏳ |
| UAT-8 | Error handling | Invalid backend shows clear error | ⏳ |
| UAT-9 | CLI backend selection | `--backend` flag works correctly | ⏳ |
| UAT-10 | Web UI backend selection | Dropdown works correctly | ⏳ |

##### TUI Tests (Molty + Claude)

| ID | Test | Expected Result | Status |
|----|------|-----------------|--------|
| TUI-1 | Launch TUI | App starts without errors | ⏳ |
| TUI-2 | Dashboard screen | Shows agent overview correctly | ⏳ |
| TUI-3 | Agent list navigation | Can browse, filter, search agents | ⏳ |
| TUI-4 | Spawn agent from TUI | Creates agent with correct config | ⏳ |
| TUI-5 | Real-time updates | Status changes appear live | ⏳ |
| TUI-6 | Chat/interact screen | Can send/receive messages | ⏳ |
| TUI-7 | Memory browser | Can explore agent memory | ⏳ |
| TUI-8 | Multi-SDK selector | Can choose backend in spawn | ⏳ |
| TUI-9 | Keyboard shortcuts | All shortcuts work correctly | ⏳ |
| TUI-10 | Help screen | Shows all keybindings | ⏳ |

#### 6.3 Bug Fixes
- [ ] Collect UAT feedback
- [ ] Prioritize bugs
- [ ] Fix critical issues
- [ ] Regression testing

#### 6.4 Production Deployment
- [ ] Production deployment guide
- [ ] Migration guide (if needed)
- [ ] Rollback plan
- [ ] Monitoring setup
- [ ] Release notes

**Deliverables:**
- [ ] UAT test results
- [ ] Production deployment docs
- [ ] Release notes
- [ ] Sign-off

**Blockers:**
- Phase 1-5 completion
- User availability for UAT
- TUI completion (for TUI-specific UAT tests)

---

## 🚧 Current Blockers

### Phase 1 (Backend SDK)
| Blocker | Impact | Resolution |
|---------|--------|------------|
| User input on priority backend | High | Waiting for user to specify LM Studio vs BYOK priority |
| LM Studio URL for testing | Medium | Need user's local LM Studio endpoint |
| BYOK provider list | Medium | Need list of providers to support |

### Phase 2 (TUI) - Claude Leading
| Blocker | Impact | Resolution |
|---------|--------|------------|
| TUI framework choice | High | Claude to select (Textual, Rich, etc.) |
| Screen design approval | Medium | Awaiting design from Claude |
| Integration points | Low | Coordinate with Phase 1 backend selector |

### Phase 3 (Full Agent Mode)
| Blocker | Impact | Resolution |
|---------|--------|------------|
| User input on native features | High | Waiting for user to specify which Claude/Copilot features |
| Feature prioritization | Medium | Which features in v1 vs later? |

### Phase 4 (Documentation)
| Blocker | Impact | Resolution |
|---------|--------|------------|
| Screenshot strategy | Low | Actual screenshots vs mockups? |
| TUI documentation | Low | Need TUI completion first |

---

## 📝 Notes & Decisions

### Open Questions

1. **Priority Backend:** Which backend is most important to implement first? LM Studio or BYOK?

2. **Full Mode Features:** What specific native features need exposure?
   - Claude: thinking blocks, tool use, vision, extended thinking?
   - Copilot: What Copilot-specific features?

3. **LM Studio:** Do you have a local instance running? What's the typical base_url? (e.g., `http://localhost:1234`)

4. **BYOK Providers:** Which providers to support? OpenRouter, Together, others?

5. **Full Mode Scope:** Per-agent or per-request? How to handle feature unavailability?

6. **Screenshots:** Run web UI and take actual screenshots, or create mockups?

7. **UAT:** Will you do UAT yourself or should I create automated UAT tests?

### Decisions Made

- **Project Management:** Using local PROJECT.md tracker (this file)
- **Timeline:** 5 weeks estimated (pending user input)
- **Status:** Backlog until user provides input on blockers

---

## 🔄 Update Log

| Date | Author | Changes |
|------|--------|---------|
| 2025-02-07 | Molty | Initial project plan created |

---

## 🎯 Next Actions

1. **Awaiting user input** on open questions (see Blockers section)
2. Once input received, update this tracker and begin Phase 1
3. Weekly updates to this tracker during implementation

---

*To update this tracker: Edit PROJECT.md and update checkboxes, status, and notes.*
