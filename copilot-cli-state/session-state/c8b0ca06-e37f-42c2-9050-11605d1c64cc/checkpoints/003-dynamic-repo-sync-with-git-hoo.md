<overview>
The user built a sophisticated vault system for managing GitHub Copilot context files (.github/, copilot-instructions.md, CLI configs) across multiple code repositories without committing to PRs during iteration. The system uses symlinks for repos (vault = source of truth) and bi-directional file sync for CLI state, with a post-merge git hook that auto-repairs symlinks broken by git operations. The architecture evolved from single-repo symlinks to a generalizable multi-repo system driven by a central INDEX.md inventory.
</overview>

<history>
1. User asked about turning on/off MCPs and managing Copilot context
   - Provided documentation on GitHub Copilot CLI capabilities
   - User provided SETUP.md requirements for vault overlay system using symlinks

2. User requested vault be called `.copilot` instead of `.claude` and clarified structure
   - Renamed vault directory
   - Clarified that `.github` should be at code directory level (where code exists), not intermediate folders
   - Only create `.github` in repo when vault has matching content

3. User asked to move MD files from git repo to vault and synchronize
   - Moved documentation to vault/repos/RepoName/
   - Created initial sync-github.sh script for creating symlinks
   - Verified symlinks working and accessible through repos

4. User wanted three auto-sync methods: git hooks, file watcher, manual command
   - Created git post-commit hook for auto-sync after commits
   - Created file watcher script (watch-and-sync.sh) with fswatch
   - Created manual sync command wrapper
   - All with dry-run safety by default

5. User wanted file watcher to also sync ~/.copilot files (CLI state)
   - Initially created "copilot-backup/" for one-way backup
   - User clarified: wants full bi-directional sync instead
   - Removed log exclusion filter so everything syncs (logs, configs, session state)

6. User questioned architecture: symlinks + watcher overhead
   - Evaluated system cost: 30MB RAM idle, 0% CPU when sleeping
   - Identified zombie process risk from multiple watcher instances
   - Added process locking to prevent duplicates

7. User asked: "why not skip watcher, just use symlinks for everything?"
   - Recognized that symlinks break with git operations (rebase/merge/reset)
   - Proposed solution: two-mode system
     - Symlink mode for repos (vault = source, lightweight)
     - Watch mode for copilot-cli (bi-directional sync, team-shareable)
   - Built unified watch-and-sync.sh with --mode flag

8. User asked: "should entire project work this way with INDEX.md?"
   - Confirmed generalization approach
   - Created repos/INDEX.md as central inventory
   - Updated all scripts to read from INDEX.md instead of hardcoded paths

9. User asked: "why not full path names in INDEX.md?"
   - Recognized current design assumed repos in ~/git/ only
   - Updated INDEX.md to support full paths (~/git/RepoName, /opt/work/RepoName, etc.)
   - Updated watch-and-sync.sh to parse and expand paths with awk + eval
   - (Still need to update remove-links.sh and merge-and-relink.sh for consistency)

10. User requested git hook to auto-repair symlinks broken by git operations
    - Created post-merge hook that detects broken symlinks after merge/rebase/pull
    - Hook calls merge-and-relink.sh --force to repair
    - Preserves any new files added while unlinked, then re-creates symlinks
    - Tested: successfully merged files and restored symlinks

11. User asked about managing repos via INDEX.md
    - Documented how to add/remove repos: edit INDEX.md, run symlink script, install hook
    - Clarified symlink-only vs watch modes and their trade-offs
</history>

<work_done>
Files created:
- `~/FV-Copilot/watch-and-sync.sh` - Unified script with 3 modes (symlink, watch, both); supports full paths from INDEX.md
- `~/FV-Copilot/remove-links.sh` - Safe symlink removal with dry-run default; currently reads simple repo names
- `~/FV-Copilot/merge-and-relink.sh` - Smart merge + relink for conflict resolution; currently reads simple repo names
- `~/FV-Copilot/install-launchd-service.sh` - macOS background service installer for persistent watching
- `~/FV-Copilot/git-hooks/post-merge` - Auto-repair hook that runs after git merge/rebase/pull
- `~/FV-Copilot/repos/INDEX.md` - Central inventory of managed repos (supports full paths)
- `~/FV-Copilot/docs/AUTO-SYNC.md` - Comprehensive documentation for all modes and commands

Files modified:
- `~/FV-Copilot/.gitignore` - Added copilot-cli/ (user-specific state, not committed)
- `~/FV-Copilot/README.md` - Updated for new structure (if needed)

Directories:
- Renamed `copilot-cli-state/` → `copilot-cli/` (cleaner naming)
- Removed old `copilot-backup/` (replaced by bi-directional sync)

Work completed:
- ✅ Multi-mode sync system (symlink for repos, watch for CLI)
- ✅ Process locking to prevent duplicate watchers
- ✅ Post-merge git hook for auto-repair
- ✅ Dynamic repo detection from INDEX.md (for watch-and-sync.sh)
- ✅ Full path support in INDEX.md (for watch-and-sync.sh)
- ✅ Comprehensive documentation
- ✅ Tested: symlink creation, removal, merging, git hook repair

Work in progress:
- [ ] Update remove-links.sh to support full paths from INDEX.md
- [ ] Update merge-and-relink.sh to support full paths from INDEX.md
- [ ] Test full path support across all scripts
</work_done>

<technical_details>
**Key Architectural Decisions:**

1. **Two-mode approach:**
   - Symlink mode for repos: vault = source of truth, fast (no copying), lightweight
   - Watch mode for copilot-cli: bi-directional sync, enables team sharing, allows multi-machine sync
   - Rationale: Symlinks break with git operations; watching overhead acceptable for user-specific state

2. **Git safety via post-merge hook:**
   - Symlinks can be silently broken by git rebase/merge/reset
   - Post-merge hook detects broken symlinks and auto-repairs using merge-and-relink.sh
   - Smart merge logic: new files copied to vault, vault version wins on conflicts
   - User never sees broken symlinks; auto-healed on commit

3. **Process locking:**
   - fswatch watchers can spawn multiple instances from repeated test runs
   - Lock file at /tmp/fv-copilot-watcher.pid prevents duplicates
   - Graceful cleanup on SIGINT/SIGTERM

4. **Central inventory (INDEX.md):**
   - All scripts read repo list from repos/INDEX.md instead of hardcoding
   - Supports full paths: `~/git/RepoName`, `/opt/work/RepoName`, absolute paths
   - Path expansion via `awk '/^[~\/]/ {print}' | while read line; eval echo "$line"`
   - Enables generalization to any number of repos across any directory structure

**Issues Encountered & Resolutions:**

1. **Symlinks break with git operations**
   - Problem: `git rebase/merge/pull` can convert symlinks to real directories silently
   - Solution: Post-merge hook detects and auto-repairs using merge-and-relink.sh
   - Status: ✅ Tested and working

2. **Zombie watcher processes from test runs**
   - Problem: Multiple `./watch-and-sync.sh` invocations created orphaned fswatch processes
   - Solution: Process locking with PID file at /tmp/fv-copilot-watcher.pid
   - Early check: if old PID running, refuse to start; if not, clean up stale lock
   - Status: ✅ Implemented, prevents duplicates

3. **Logs not visible in Obsidian**
   - Problem: User created logs in vault but they don't show in Obsidian
   - Root cause: copilot-cli/ is in .gitignore (intentional—user state, not committed)
   - Obsidian respects git ignore rules by default
   - Status: ✅ Intentional design; not a bug

4. **Symlink creation skips when real directory exists**
   - Problem: watch-and-sync.sh --mode symlink sees real .github directory and skips it
   - Rationale: Safety feature—don't blindly delete real directories
   - Solution: Use merge-and-relink.sh to handle this case (merge files, delete, create symlink)
   - Status: ✅ Correct behavior; post-merge hook uses merge-and-relink instead

5. **Tilde expansion in INDEX.md**
   - Problem: INDEX.md with `~/git/Repo` wasn't expanding the `~`
   - Root cause: grep patterns weren't detecting paths correctly
   - Solution: Changed to `awk '/^[~\/]/ {print}' | while read line; eval echo "$line"`
   - Status: ✅ Working, supports ~ and absolute paths

**Quirks & Non-Obvious Behaviors:**

1. **Symlinks show as directories to `file` command:**
   - `file ~/.github` shows "directory" because it resolves through symlink
   - Must use `[ -L ]` test to check if actually a symlink
   - This caused confusion in testing—symlink was working but appeared to be a directory

2. **Vault-only folders (skills/, instructions/) are accessible through parent symlink:**
   - No dedicated symlinks created for them (script skips if no repo match)
   - But they ARE visible at repo/.github/skills/ because parent symlink includes everything
   - This is by design—allows reference material without polluting repo structure

3. **Bi-directional sync with fswatch can create loops:**
   - If both vault and ~/.copilot change same file, last-write-wins
   - 0.5s sleep in watcher loop helps debounce but isn't foolproof
   - Not ideal but acceptable for current use case (mostly CLI auto-generates config, user edits vault)

4. **Post-merge hook runs AFTER git has finished:**
   - Hook has access to final repo state
   - Can safely detect broken symlinks and repair
   - Runs synchronously (blocking until complete) for correctness

**Unresolved Questions / Assumptions:**

1. Full path support in INDEX.md is newly added to watch-and-sync.sh but NOT yet to remove-links.sh or merge-and-relink.sh—should update for consistency
2. Assumption: All managed repos have at least vault/repos/RepoName directory—no validation of this
3. Assumption: User won't edit INDEX.md while scripts are running—could add validation
4. Watch mode watcher creates 3 fswatch processes (repos, copilot-cli, ~/.copilot)—could optimize to single process with multiple paths

**Dependencies:**
- fswatch (brew install fswatch)
- bash 4+
- awk, grep, sed, ln, mkdir, find, cp, rm
- macOS-specific: launchctl for background service

</technical_details>

<important_files>
- `~/FV-Copilot/watch-and-sync.sh`
  - Main orchestration script for all sync modes
  - Recently updated to read repos from INDEX.md with full path support
  - Supports --mode symlink|watch|both and --repo flag for single repo
  - Lines 1-50: Initialization, argument parsing, function definitions
  - Lines 60-120: symlink_mode() and symlink_repo()
  - Lines 150-250: watch_mode() with fswatch and bi-directional sync logic
  - Uses `awk '/^[~\/]/ {print}' | eval echo` to expand paths from INDEX.md

- `~/FV-Copilot/repos/INDEX.md`
  - Central inventory of managed repositories
  - Single source of truth for which repos to manage
  - Supports full paths: ~/git/RepoName, /opt/work/RepoName, absolute paths
  - Currently has: ~/git/FV-Platform-Main
  - Scripts parse lines starting with ~ or /

- `~/FV-Copilot/git-hooks/post-merge`
  - Auto-repair symlinks after git merge/rebase/pull
  - Installs as symlink in .git/hooks/post-merge of each repo
  - Detects broken symlinks: `[ -d "$REPO_ROOT/.github" ] && [ ! -L "$REPO_ROOT/.github" ]`
  - Calls merge-and-relink.sh --force to repair
  - Runs silently in background

- `~/FV-Copilot/merge-and-relink.sh`
  - Smart merge + relink for handling conflicts
  - Preserves new files added to repo while unlinked
  - Vault version wins on file conflicts
  - Currently reads simple repo names from INDEX.md (needs full path update)
  - Used by post-merge hook for auto-repair

- `~/FV-Copilot/remove-links.sh`
  - Safe symlink removal with dry-run by default
  - Finds all .github symlinks in repos and removes them
  - vault/repos/ directories preserved
  - Currently reads simple repo names from INDEX.md (needs full path update)

- `~/FV-Copilot/install-launchd-service.sh`
  - Creates macOS LaunchAgent for persistent background watching
  - Installs plist at ~/Library/LaunchAgents/com.fv-copilot.watch-and-sync.plist
  - Auto-starts on login, survives sleep/restart
  - Logs to /tmp/fv-copilot-watcher.log

- `~/FV-Copilot/docs/AUTO-SYNC.md`
  - User-facing documentation for entire system
  - Describes modes, installation options, troubleshooting
  - Quick reference section
  - Should be updated after removing full-path support is finished

- `~/FV-Copilot/.gitignore`
  - Excludes repos/, copilot-cli/, pkg/ (external/generated data)
  - Allows docs/ and skills/ (reference material)
  - Allows .obsidian/ (vault configuration)
  - Critical: copilot-cli/ excluded because it mirrors ~/.copilot (user-specific state)

- `~/git/FV-Platform-Main/.git/hooks/post-merge`
  - Symlink to ~/FV-Copilot/git-hooks/post-merge
  - Auto-repairs symlinks after git operations
  - Should be installed in each managed repo
  - Can be installed via: `ln -sf ~/FV-Copilot/git-hooks/post-merge .git/hooks/post-merge`

</important_files>

<next_steps>
Pending work (user just requested):
- [ ] Update remove-links.sh to support full paths from INDEX.md (same pattern as watch-and-sync.sh)
- [ ] Update merge-and-relink.sh to support full paths from INDEX.md (same pattern as watch-and-sync.sh)
- [ ] Test full path support across all scripts with multi-repo scenarios
- [ ] Update docs/AUTO-SYNC.md to document full path support in INDEX.md

After full path support is complete:
- [ ] Test real git operations (merge, rebase, pull) to verify post-merge hook auto-repair works end-to-end
- [ ] Create team deployment guide (how to share vault across team, install in multiple repos)
- [ ] Consider: automation script to install post-merge hook in all repos listed in INDEX.md

Known limitations to address later:
- Watch mode spawns 3 separate fswatch processes (could consolidate)
- No validation that repos in INDEX.md actually exist before running
- Bi-directional sync conflict resolution is "last-write-wins" (could add smarter logic)
- No built-in way to promote vault-only content to repo-committed status

Current blocking issues:
- None—system is functional. Pending work is consistency improvements.
</next_steps>