# Multi-Agent Vault System Implementation Plan

## Problem Statement
Current vault system is single-agent (Copilot-only). Need to support multiple AI systems (Copilot, Claude, Cursor, etc.) with:
- Universal content shared across all agents (any directory without .agent suffix)
- Agent-specific overrides (ANY_DIR.copilot/, ANY_DIR.claude/, etc.)
- Each agent sees merged view: universal + agent-specific (agent wins on conflicts)
- Separate watch processes per agent maintain independent overlays
- Pattern applies to ALL directories: skills/, src/, config/, tests/, docs/, etc.
- Universal directories are base; agent-specific directories override by filename

## Proposed Approach
1. Create agent registry system (agents/INDEX.md + validation)
2. Implement agent-specific namespace detection (skills.{agent}/, instructions.{agent}/, etc.)
3. Build merge logic: universal + agent-specific → composite overlay
4. Update all scripts to support --agent flag and multi-agent workflows
5. Create agent-specific CLI sync (copilot-cli/, claude-cli/, etc.)
6. Migrate existing single-agent setup to multi-agent structure
7. Update documentation and provide migration guide

## Workplan

### Phase 1: Foundation & Registry
- [ ] Create agents/INDEX.md with initial agent list (copilot, claude)
- [ ] Create agent detection function that scans vault for skills.{agent}/ patterns
- [ ] Add validation: warn if INDEX.md lists agent but no skills.{agent}/ exists
- [ ] Add validation: warn if skills.{agent}/ exists but not in INDEX.md
- [ ] Test: verify detection works with multiple agent folders

### Phase 2: Directory Structure & Namespace Support
- [ ] Create skills.copilot/ and skills.claude/ directories in vault
- [ ] Create instructions.copilot/ and instructions.claude/ directories
- [ ] Migrate existing skills/ to skills.copilot/ (preserve existing Copilot setup)
- [ ] Update .gitignore to handle *-cli/ pattern (copilot-cli/, claude-cli/, etc.)
- [ ] Ensure docs/ remains universal (no agent-specific variants for docs/)

### Phase 3: Merge Logic Implementation ✅ COMPLETE
- [x] Create merge_agent_overlay() function in watch-and-sync.sh
  - [x] Takes agent name as input
  - [x] Lists all universal directories (skills/, instructions/, docs/)
  - [x] Lists agent-specific directories (skills.{agent}/, instructions.{agent}/)
  - [x] GENERALIZED: Scans ANY directory with .agent suffix
  - [x] Creates composite file list (agent-specific overrides universal on name conflict)
  - [x] Returns merged structure
- [x] Test merge logic with sample files (universal + copilot-specific + claude-specific)
- [x] Verify override behavior: skills.copilot/python.md wins over skills/python.md
- [x] **File-level agent routing** (`filename.agent.ext`)
  - [x] Parse filename for `.copilot.`, `.claude.`, etc. patterns
  - [x] Route copilot files to `.github/` in target repo
  - [x] Route claude files to `.claude/` in target repo
  - [x] Strip agent suffix when writing to target
  - [x] Handle both directory-level AND file-level routing in single merge pass
- [x] Test file-level routing: verified with `skills.copilot.md`, `config.claude.yaml`
- [x] Applied and verified overlays in test repos

### Phase 4: Symlink Mode Updates
- [ ] Update symlink_repo() to accept --agent parameter
- [ ] Modify symlink creation to use merged overlay instead of direct vault paths
- [ ] Handle case: create temp composite directory, symlink to that? OR symlink individual files?
  - Decision needed: symlink strategy for merged content
- [ ] Update remove-links.sh to be agent-aware (remove links for specific agent or all)
- [ ] Test: create symlinks for copilot, verify .github/ has universal + copilot files
- [ ] Test: create symlinks for claude, verify .github/ has universal + claude files

### Phase 5: Watch Mode Multi-Agent Support
- [ ] Add --agent flag to watch_mode() function
- [ ] Update fswatch paths to watch both universal and agent-specific dirs
- [ ] Modify sync logic to apply merge overlay when copying to repo
- [ ] Add process locking per agent (separate PID files per agent watch)
- [ ] Test: run --mode watch --agent copilot, verify correct overlay synced
- [ ] Test: run two watchers (copilot + claude) simultaneously, verify no conflicts

### Phase 6: CLI State Sync (Agent-Specific)
- [ ] Rename copilot-cli/ → maintain as-is (Copilot state)
- [ ] Add support for claude-cli/ (sync from ~/.claude if exists)
- [ ] Generalize CLI sync logic: detect which agent CLIs exist, sync all active ones
- [ ] Update fswatch watchers to handle multiple CLI state directories
- [ ] Test: sync both ~/.copilot ↔ copilot-cli/ and ~/.claude ↔ claude-cli/

### Phase 7: Git Hook Updates
- [ ] Update post-merge hook to be agent-aware
- [ ] Detect which agent(s) have symlinks in repo, repair all
- [ ] Update merge-and-relink.sh to support --agent flag
- [ ] Test: break symlinks with git merge, verify auto-repair for correct agent

### Phase 8: Script Consistency & Full Path Support
- [ ] Finish updating remove-links.sh for full paths from INDEX.md (leftover from previous work)
- [ ] Finish updating merge-and-relink.sh for full paths from INDEX.md (leftover from previous work)
- [ ] Ensure all scripts read repos/INDEX.md and agents/INDEX.md consistently
- [ ] Add --agent flag to all user-facing scripts where relevant

### Phase 9: Documentation & Migration Guide
- [ ] Update docs/AUTO-SYNC.md with multi-agent instructions
- [ ] Create docs/MULTI-AGENT.md explaining architecture and use cases
- [ ] Create MIGRATION.md guide for converting single-agent to multi-agent setup
- [ ] Document agent naming conventions and best practices
- [ ] Add troubleshooting section for multi-agent conflicts

### Phase 10: Testing & Validation
- [ ] Test single-agent mode (copilot-only, backward compatibility)
- [ ] Test dual-agent mode (copilot + claude simultaneously)
- [ ] Test override behavior (agent-specific wins over universal)
- [ ] Test git hook repair with multiple agents
- [ ] Test CLI state sync for multiple agents
- [ ] Test full path repos with multi-agent setup

### Phase 11: Optional Enhancements
- [ ] Create bootstrap script: setup-agent.sh --agent claude (auto-creates dirs, installs hooks)
- [ ] Add agent promotion workflow (move skills.copilot/file.md → skills/ to make universal)
- [ ] Consider: agent-specific .gitignore rules (claude-cli/ but not copilot-cli/ in some vaults)
- [ ] Consider: skill diff tool (show differences between copilot vs claude versions)

## Technical Decisions Needed

### Decision 1: Symlink Strategy for Merged Content
**Options:**
A) Create temp composite directory, symlink .github/ to that (requires build step)
B) Create individual file symlinks per merged file (more complex, many symlinks)
C) Use bind mount or overlay fs (Linux-only, macOS doesn't support)
D) Watch mode only for multi-agent, no symlink support (simpler but less performant)

**Current leaning:** D (watch mode only), because:
- Symlinks can't represent merged content without temp directories
- Watch mode already exists and handles bi-directional sync
- Agent-specific overlays change dynamically, watch mode handles this naturally

### Decision 2: CLI State Naming
**Options:**
A) copilot-cli/, claude-cli/ (mirrors system paths)
B) cli/copilot/, cli/claude/ (grouped by function)
C) agents/copilot/cli/, agents/claude/cli/ (fully namespaced)

**Current leaning:** A (flat structure), because:
- Matches existing copilot-cli/ pattern
- Clear parallel structure to ~/.copilot and ~/.claude
- Easy to add to .gitignore with pattern *-cli/

### Decision 3: Agent Detection Priority
When conflicts arise (INDEX.md says agent exists but no skills.{agent}/ found):
- Warn and skip that agent?
- Error and halt?
- Auto-create skeleton structure?

**Current leaning:** Warn and skip, because:
- Allows partial setup during development
- User might have agent in INDEX.md but not created dirs yet
- Non-blocking, permissive approach

## Migration Strategy

### For Existing Copilot-Only Vaults:
1. Run migration script (to be created):
   - Creates agents/INDEX.md with "copilot"
   - Creates skills.copilot/ and instructions.copilot/
   - Option: move existing skills/ → skills.copilot/ OR keep as universal
   - Creates copilot-cli/ from existing vault copilot state
2. Update watch-and-sync.sh calls to include --agent copilot
3. Re-run symlink creation with --agent copilot flag
4. Verify existing repos still work

### For New Multi-Agent Setup:
1. Create vault structure with agents/INDEX.md listing all agents
2. Create universal + agent-specific directories
3. Run watch-and-sync.sh --mode both --agent {agent} per agent
4. Install git hooks in all repos
5. Verify each agent sees correct overlay

## Notes & Considerations

### Code vs. Docs Distinction
- Code directories (src/, lib/, etc.) remain universal (no agent-specific variants)
- Only .github/ context files (skills/, instructions/, docs/) support agent-specific namespaces
- Rationale: Code is shared reality, context/docs are perspective-dependent

### Conflict Resolution
- File-level: agent-specific always wins over universal (by name)
- Directory-level: both universal and agent-specific dirs are scanned
- No recursive merge: skills/subdir/ and skills.copilot/subdir/ are treated as separate

### Performance Impact
- Each agent watch process = 3-4 fswatch instances (universal dirs + agent dirs + CLI state)
- N agents = N * 4 watchers = potential resource concern at scale
- Acceptable for 2-3 agents, may need optimization beyond that

### Team Scenarios
- Team using only Copilot: use copilot-only mode, no overhead
- Team using Copilot + Claude: run both watch processes, each dev uses their preferred tool
- Shared vault: commit universal skills/, each dev's agent-specific stays local OR committed if team agrees

### Edge Cases to Handle
- Agent added to INDEX.md mid-session (watcher needs restart to pick up)
- Agent-specific file deleted (should fall back to universal if exists)
- Universal file deleted (agent-specific still works)
- Both universal and agent-specific deleted (file removed from overlay)

## Success Criteria
- [ ] Can run watch-and-sync.sh --agent copilot and --agent claude simultaneously
- [ ] Each agent sees correct merged overlay (.github/ = universal + agent-specific)
- [ ] Agent-specific files override universal files with same name
- [ ] CLI state syncs correctly for multiple agents (copilot-cli/, claude-cli/)
- [ ] Git hooks repair symlinks correctly per agent
- [ ] Backward compatible: existing Copilot-only setup still works
- [ ] Documentation clear enough for team member to set up new agent
