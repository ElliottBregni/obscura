<overview>
User requested evolution of existing single-agent vault system into a multi-agent architecture supporting Copilot, Claude, Cursor, and other AI systems. The key requirement: ANY directory can have agent-specific variants using `.agent` suffix pattern (e.g., `src/`, `src.copilot/`, `src.claude/`), with agent-specific files overriding universal ones by filename. The system must work across vault content (skills, instructions, docs) AND code repositories, with each agent seeing a merged overlay of universal + agent-specific content. Approach: build agent registry with validation, generalize merge logic to handle any directory structure, test with real code/config examples, then update existing scripts to support `--agent` flag.
</overview>

<history>
1. User asked about managing MCPs and provided vision for vault overlay system
   - Initially discussed Obsidian-based vault for LLM context
   - User clarified: rename `.claude/` to `.copilot/` throughout
   - Established goal: multi-agent support where vault is source of truth

2. User requested multi-agent architecture with agent-specific overrides
   - Asked for skills.copilot/, skills.claude/ pattern with override behavior
   - Clarified: agent-specific files override universal by filename
   - Confirmed: separate watch processes per agent, merged overlays

3. **Critical pivot**: User requested generalization beyond skills/docs
   - "the same needs to apply within the code supersets and .claude .copilot files within each directory"
   - Requirement: ANY directory can have .agent variant (src/, config/, tests/, etc.)
   - This fundamentally changed scope from "context files only" to "universal pattern"

4. Implementation of Phase 1-3
   - Created agents/INDEX.md registry with copilot, claude
   - Built agent detection and validation functions
   - Created universal (skills/, instructions/) and agent-specific directories
   - Implemented generalized merge_agent_overlay() function
   - Created test content: code (src/), config (config/), tests (tests.claude/)
   - Verified override behavior and agent-only directories work correctly

5. User noted missing universal directories
   - Created universal skills/ and instructions/ alongside agent-specific variants
   - Added sample universal content (git-workflow.md, testing.md, code-review.md)
   - Verified both universal and agent-specific content merge correctly
</history>

<work_done>
Files created:
- `~/FV-Copilot/agents/INDEX.md` - Central registry of active agents (copilot, claude)
- `~/FV-Copilot/skills/git-workflow.md` - Universal skill (all agents)
- `~/FV-Copilot/skills/testing.md` - Universal skill
- `~/FV-Copilot/skills.copilot/python.md` - Copilot-specific override
- `~/FV-Copilot/skills.copilot/api-design.md` - Copilot-only skill
- `~/FV-Copilot/skills.claude/python.md` - Claude-specific override
- `~/FV-Copilot/skills.claude/database.md` - Claude-only skill
- `~/FV-Copilot/instructions/code-review.md` - Universal instruction
- `~/FV-Copilot/instructions/repo-setup.md` - Universal instruction
- `~/FV-Copilot/src/utils.py` - Universal code file (test)
- `~/FV-Copilot/src.copilot/utils.py` - Copilot-specific code override
- `~/FV-Copilot/src.claude/utils.py` - Claude-specific code override
- `~/FV-Copilot/config/api.yaml` - Universal config
- `~/FV-Copilot/config.copilot/api.yaml` - Copilot-specific config override
- `~/FV-Copilot/tests.claude/test_integration.py` - Claude-only tests

Directories created:
- `~/FV-Copilot/agents/`
- `~/FV-Copilot/skills/` (universal)
- `~/FV-Copilot/skills.copilot/`
- `~/FV-Copilot/skills.claude/`
- `~/FV-Copilot/skills.cursor/` (empty, for testing validation)
- `~/FV-Copilot/instructions/` (universal)
- `~/FV-Copilot/instructions.copilot/`
- `~/FV-Copilot/instructions.claude/`
- `~/FV-Copilot/src/`, `~/FV-Copilot/src.copilot/`, `~/FV-Copilot/src.claude/`
- `~/FV-Copilot/config/`, `~/FV-Copilot/config.copilot/`
- `~/FV-Copilot/tests.claude/`

Files modified:
- `~/FV-Copilot/watch-and-sync.sh` - Added agent detection, validation, and generalized merge logic
  - Added AGENTS_INDEX variable and --agent flag parsing
  - Created get_registered_agents() function
  - Created detect_agent_dirs() function
  - Created validate_agents() function
  - **Completely rewrote merge_agent_overlay()** to support ANY directory with .agent suffix
  - Created apply_overlay_to_target() function for copying merged files
- `~/FV-Copilot/.gitignore` - Changed `copilot-cli/` to `*-cli/` pattern for multi-agent support
- `/Users/bregnie/.copilot/session-state/c8b0ca06-e37f-42c2-9050-11605d1c64cc/plan.md` - Updated workplan tracking

Work completed:
- ✅ Phase 1: Agent registry and detection system
- ✅ Phase 2: Directory structure with universal + agent-specific variants
- ✅ Phase 3: Generalized merge logic supporting ANY directory pattern
- ✅ Testing: Verified merge behavior with skills, code, configs, tests
- ✅ Verified agent-specific overrides by filename
- ✅ Verified agent-only directories (e.g., tests.claude without universal tests/)

Current state:
- **Core merge logic is complete and tested**
- Generalized pattern works: DIR/ (universal) + DIR.agent/ (agent-specific)
- Agent-specific files override universal by filename
- System tested with /tmp/test-copilot and /tmp/test-claude overlays
- **Not yet integrated**: watch-and-sync.sh doesn't use --agent flag in main flows
- **Not yet updated**: symlink mode, git hooks, CLI sync, other helper scripts
</work_done>

<technical_details>
**Key Architectural Decisions:**

1. **Universal pattern for agent-specific overrides**
   - ANY directory can have .agent variant: `DIR/` + `DIR.copilot/` + `DIR.claude/`
   - Applies to: skills/, src/, config/, tests/, docs/, instructions/, etc.
   - Agent-specific file ALWAYS wins over universal file with same relative path
   - Rationale: Maximum flexibility—code, configs, tests can all be agent-specific

2. **Merge algorithm (bash 3.2 compatible)**
   - Step 1: Scan for universal directories (maxdepth 1, no .agent suffix, exclude hidden/cli dirs)
   - Step 2: Scan for agent-specific directories matching `*.${agent}` pattern
   - Step 3: Merge using awk associative arrays (agent files overwrite universal by key)
   - Output: `dest_path|source_path|UNIVERSAL|AGENT`
   - Bash 3.2 limitation: No associative arrays in shell, used awk instead

3. **Agent registry and validation**
   - `agents/INDEX.md` lists active agents under "## Active Agents" section
   - Parsing: `awk '/## Active Agents/,/^##/ {flag check} /^- [a-z]/ {print $2}'`
   - Validation: warns if INDEX.md lists agent without directories OR vice versa
   - Non-blocking: warnings only, doesn't halt execution

4. **Directory detection pattern**
   - Universal: `find -maxdepth 1 -type d ! -name ".*" ! -name "*-cli" ! -name "*.${agent}"`
   - Agent-specific: `find -maxdepth 1 -type d -name "*.${agent}"`
   - Excludes: hidden dirs (.*), CLI state (*-cli), other agents' dirs
   - Limitation: maxdepth 1 means nested agent-specific not supported (e.g., src/lib.copilot/)

**Issues Encountered & Resolutions:**

1. **Bash 3.2 on macOS lacks associative arrays**
   - Problem: Initial merge logic used `declare -A` (bash 4+)
   - Solution: Rewrote using awk for associative array handling
   - Trade-off: Slightly more complex, but works on default macOS bash

2. **Agent parsing matched too many lines**
   - Problem: `grep -E "^- "` matched backtick lines in INDEX.md
   - Solution: Use awk with section parsing and word-boundary check `/^- [a-z]/`
   - Learned: INDEX.md structure matters for reliable parsing

3. **Hardcoded directory list not scalable**
   - Problem: Original code only checked skills/, instructions/, docs/
   - User requirement: "same needs to apply within code supersets"
   - Solution: Generalized to find ANY directory with .agent suffix
   - Impact: Fundamental architecture change from "context files" to "universal pattern"

**Verified Behaviors:**

- **Override by filename**: `src/utils.py` can come from `src.copilot/utils.py` or `src.claude/utils.py`
- **Agent-only directories**: `tests.claude/` creates `tests/` in Claude overlay even without universal `tests/`
- **Universal content shared**: `skills/git-workflow.md` visible to all agents
- **Mixed scenarios**: Copilot has `config.copilot/api.yaml` (override), Claude sees universal `config/api.yaml`

**Unresolved Questions:**

1. Should nested agent-specific directories be supported? (e.g., `src/lib/` + `src/lib.copilot/`)
2. How to handle agent-specific directories within repos (not just vault root)?
3. Should symlink mode work with multi-agent, or watch-only for merged overlays?
4. Migration path: how to move existing skills/ to skills.copilot/ vs keeping as universal?

**Dependencies:**
- bash 3.2+ (default macOS)
- awk (for associative array emulation)
- find, grep, sed, sort, mkdir, cp, rm
- fswatch (for watch mode, not yet updated for multi-agent)
</technical_details>

<important_files>
- `~/FV-Copilot/agents/INDEX.md`
  - Central registry of active agents
  - Format: "## Active Agents" section with `- agentname` list items
  - Parsed by get_registered_agents() function
  - Used for validation and determining which .agent dirs to check

- `~/FV-Copilot/watch-and-sync.sh`
  - Main orchestration script (existing file, extensively modified)
  - Lines 7-13: Added AGENTS_INDEX, AGENT variables
  - Lines 20-27: Added --agent flag parsing
  - Lines 45-52: get_registered_agents() - parses agents/INDEX.md
  - Lines 54-72: detect_agent_dirs() - checks if agent has directories
  - Lines 74-109: validate_agents() - warns on mismatches
  - Lines 120-181: **merge_agent_overlay()** - GENERALIZED merge logic
    - Scans ANY directory for universal + .agent variants
    - Uses awk for bash 3.2 compatibility
    - Returns dest_path|source_path|type format
  - Lines 183-202: apply_overlay_to_target() - copies merged files to target
  - **Status**: Core merge functions complete, but main symlink/watch modes not yet updated

- `~/FV-Copilot/.gitignore`
  - Line 9: Changed `copilot-cli/` to `*-cli/` pattern
  - Supports multiple agent CLI directories (copilot-cli/, claude-cli/, etc.)

- `~/FV-Copilot/skills/`, `~/FV-Copilot/skills.copilot/`, `~/FV-Copilot/skills.claude/`
  - Example universal + agent-specific structure
  - Universal: git-workflow.md, testing.md (all agents see)
  - Copilot-specific: python.md (overrides universal), api-design.md (copilot-only)
  - Claude-specific: python.md (different content), database.md (claude-only)
  - Demonstrates override and agent-only patterns

- `~/FV-Copilot/src/`, `~/FV-Copilot/src.copilot/`, `~/FV-Copilot/src.claude/`
  - Test code directories proving pattern works for code
  - Universal src/utils.py: basic functions
  - src.copilot/utils.py: type hints, comma formatting
  - src.claude/utils.py: functional style with reduce
  - Verified: each agent sees correct version in overlay

- `~/FV-Copilot/config/`, `~/FV-Copilot/config.copilot/`
  - Test config directories
  - Universal config/api.yaml: basic endpoint, timeout, retry
  - config.copilot/api.yaml: adds OpenAI integration, longer timeout
  - Verified: Copilot sees override, Claude sees universal

- `~/FV-Copilot/tests.claude/`
  - Test agent-only directory (no universal tests/)
  - Proves agent-specific directories work without universal base
  - Claude gets tests/ in overlay, Copilot doesn't

- `/Users/bregnie/.copilot/session-state/c8b0ca06-e37f-42c2-9050-11605d1c64cc/plan.md`
  - Implementation plan with 11 phases
  - Tracks progress: Phase 1-3 complete, Phase 4-11 pending
  - Updated problem statement to reflect generalized pattern requirement
</important_files>

<next_steps>
**Immediate next steps** (continuing from Phase 4):

Phase 4: Update symlink mode for multi-agent
- [ ] Modify symlink_repo() to require --agent parameter
- [ ] Decision needed: symlinks with merged overlays (temp dir) vs watch-only for multi-agent?
- [ ] Test: create symlinks for copilot vs claude, verify correct overlay

Phase 5: Update watch mode for multi-agent
- [ ] Add --agent flag support to watch_mode()
- [ ] Update fswatch paths to include agent-specific directories
- [ ] Modify sync logic to use merge_agent_overlay() when copying
- [ ] Add process locking per agent (separate PID files)
- [ ] Test: run --mode watch --agent copilot and --agent claude simultaneously

Phase 6: CLI state sync per agent
- [ ] Add support for claude-cli/ (sync from ~/.claude)
- [ ] Generalize CLI sync: detect active agents, sync all
- [ ] Update fswatch to handle multiple CLI directories

Phase 7: Update git hooks for multi-agent
- [ ] Modify post-merge hook to detect which agent(s) need repair
- [ ] Update merge-and-relink.sh with --agent flag

Phase 8: Finish full path support (leftover from previous work)
- [ ] Update remove-links.sh for full paths from INDEX.md
- [ ] Update merge-and-relink.sh for full paths

Phase 9-11: Documentation, testing, optional enhancements
- [ ] Update docs/AUTO-SYNC.md with multi-agent instructions
- [ ] Create docs/MULTI-AGENT.md
- [ ] End-to-end testing with real repos

**Blocking question**: Should symlink mode support multi-agent overlays, or restrict to watch mode only? Symlinks can't represent merged content without temporary build directories.
</next_steps>