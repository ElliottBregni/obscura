#!/bin/bash
# Universal watch-and-sync script with multiple modes
# Reads repos from repos/INDEX.md (supports full paths)

set -e

VAULT_PATH="$HOME/FV-Copilot"
REPOS_BASE="$VAULT_PATH/repos"
REPOS_INDEX="$REPOS_BASE/INDEX.md"
AGENTS_INDEX="$VAULT_PATH/agents/INDEX.md"
COPILOT_VAULT="$VAULT_PATH/copilot-cli"
COPILOT_PATH="$HOME/.copilot"
SYNC_SCRIPT="$VAULT_PATH/sync-github.sh"
LOCK_FILE="/tmp/fv-copilot-watcher.pid"

MODE="both"
SPECIFIC_REPO=""
AGENT=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --mode) MODE="$2"; shift 2 ;;
        --repo) SPECIFIC_REPO="$2"; shift 2 ;;
        --agent) AGENT="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ ! "$MODE" =~ ^(symlink|watch|both)$ ]]; then
    echo "❌ Invalid mode: $MODE (use: symlink, watch, or both)"
    exit 1
fi

command -v fswatch >/dev/null 2>&1 || {
    echo "❌ fswatch not installed. Run: brew install fswatch"
    exit 1
}

# ============================================================================
# AGENT DETECTION & VALIDATION
# ============================================================================

# Get list of registered agents from agents/INDEX.md
get_registered_agents() {
    if [ ! -f "$AGENTS_INDEX" ]; then
        echo "❌ agents/INDEX.md not found" >&2
        return 1
    fi
    # Extract agent names from "- agentname" lines after "## Active Agents"
    awk '/## Active Agents/ {flag=1; next} /^##/ {flag=0} flag && /^- [a-z]/ {print $2}' "$AGENTS_INDEX"
}

# Detect agent-specific directories in vault (skills.{agent}/, instructions.{agent}/)
detect_agent_dirs() {
    local agent=$1
    found=false
    
    if [ -d "$VAULT_PATH/skills.$agent" ]; then
        found=true
    fi
    
    if [ -d "$VAULT_PATH/instructions.$agent" ]; then
        found=true
    fi
    
    if [ -d "$VAULT_PATH/docs.$agent" ]; then
        found=true
    fi
    
    if $found; then
        return 0
    else
        return 1
    fi
}

# Map agent name to target path in repository
# Usage: get_agent_target_path <agent>
# Returns: .github (for copilot), .claude (for claude), etc.
get_agent_target_path() {
    local agent=$1
    
    if [ -z "$agent" ]; then
        echo "❌ get_agent_target_path: agent required" >&2
        return 1
    fi
    
    case "$agent" in
        copilot)
            echo ".github"
            ;;
        claude)
            echo ".claude"
            ;;
        cursor)
            echo ".cursor"
            ;;
        *)
            # Generic fallback: .agent-name
            echo ".${agent}"
            ;;
    esac
}

# Validate agents: check that registered agents have directories and vice versa
validate_agents() {
    local warnings=0
    
    # Check: registered agents have directories
    while read agent; do
        if ! detect_agent_dirs "$agent"; then
            echo "⚠️  Warning: Agent '$agent' in INDEX.md but no skills.$agent/ or instructions.$agent/ found"
            warnings=$((warnings + 1))
        fi
    done < <(get_registered_agents)
    
    # Check: agent directories have registration
    for dir in "$VAULT_PATH"/skills.* "$VAULT_PATH"/instructions.* "$VAULT_PATH"/docs.*; do
        if [ -d "$dir" ]; then
            local dirname=$(basename "$dir")
            local agent_name=${dirname#*.}
            
            if ! get_registered_agents | grep -q "^${agent_name}$"; then
                echo "⚠️  Warning: Directory '$dirname' found but '$agent_name' not in agents/INDEX.md"
                warnings=$((warnings + 1))
            fi
        fi
    done
    
    if [ $warnings -eq 0 ]; then
        echo "✅ Agent validation passed"
    fi
    
    return 0
}

# Read repos from INDEX.md (paths starting with ~ or /)
get_managed_repos() {
    awk '/^[~\/]/ {print}' "$REPOS_INDEX" | while read line; do
        eval echo "$line"
    done
}

# ============================================================================
# MERGE LOGIC - Multi-Agent Overlay (Generalized)
# ============================================================================

# Build merged overlay for a specific agent
# Scans ALL directories: universal dirs + agent-specific overrides
# Pattern: ANY_DIR/ (universal) + ANY_DIR.agent/ (agent-specific override)
# Usage: merge_agent_overlay <agent> <vault_source_path>
merge_agent_overlay() {
    local agent=$1
    local source_path=${2:-$VAULT_PATH}  # Default to vault root
    
    if [ -z "$agent" ]; then
        echo "❌ merge_agent_overlay: agent required" >&2
        return 1
    fi
    
    local temp_universal="/tmp/merge_overlay_universal_$$.txt"
    local temp_agent="/tmp/merge_overlay_agent_$$.txt"
    
    # Step 1: Find all universal directories (no .agent suffix)
    # Skip hidden dirs, agent-cli dirs, and known excluded patterns
    > "$temp_universal"
    find "$source_path" -maxdepth 1 -type d ! -name ".*" ! -name "*-cli" ! -name "*.${agent}" ! -name "*.*" 2>/dev/null | while read universal_dir; do
        if [ "$universal_dir" = "$source_path" ]; then
            continue  # Skip root itself
        fi
        
        local dir_name=$(basename "$universal_dir")
        
        # Skip if this is an agent-specific variant
        if [[ "$dir_name" =~ \.[a-z]+$ ]]; then
            continue
        fi
        
        # Find all files in this universal directory
        if [ -d "$universal_dir" ]; then
            find "$universal_dir" -type f 2>/dev/null | while read filepath; do
                local filename=$(basename "$filepath")
                local relative_path="${filepath#$source_path/}"
                
                # Check if this is an agent-specific file within universal dir
                # Pattern: filename.agent.ext within skills/, config/, etc.
                if [[ "$filename" =~ \.${agent}\. ]]; then
                    # This is an agent override within the directory
                    # Strip agent suffix: skills/setup.copilot.md → skills/setup.md
                    local dest_filename=$(echo "$filename" | sed "s/\.${agent}\././")
                    local dir_path=$(dirname "$filepath")
                    local relative_dir="${dir_path#$source_path/}"
                    echo "$relative_dir/$dest_filename|$filepath|AGENT_NESTED"
                else
                    # Regular universal file
                    echo "$relative_path|$filepath|UNIVERSAL"
                fi
            done >> "$temp_universal"
        fi
    done
    
    # Step 2: Find all agent-specific directories (DIR.agent)
    > "$temp_agent"
    find "$source_path" -maxdepth 1 -type d -name "*.${agent}" 2>/dev/null | while read agent_dir; do
        local dir_name=$(basename "$agent_dir")
        local base_dir="${dir_name%.${agent}}"  # src.copilot -> src
        
        # Find all files in this agent-specific directory
        if [ -d "$agent_dir" ]; then
            find "$agent_dir" -type f 2>/dev/null | while read filepath; do
                local relative_path="${filepath#$agent_dir/}"
                # Map to universal path: src.copilot/utils.py -> src/utils.py
                echo "$base_dir/$relative_path|$filepath|AGENT"
            done >> "$temp_agent"
        fi
    done
    
    # Step 2b: Find all files with agent suffix in filename (*.agent.ext)
    # These route to agent-specific target paths (.github/ for copilot, .claude/ for claude)
    find "$source_path" -type f -name "*.${agent}.*" 2>/dev/null | while read filepath; do
        local filename=$(basename "$filepath")
        local dirname=$(dirname "$filepath")
        local relative_dir="${dirname#$source_path}"
        relative_dir="${relative_dir#/}"  # Remove leading slash if present
        
        # Strip agent suffix from filename: skills.copilot.md -> skills.md
        local dest_filename=$(echo "$filename" | sed "s/\.${agent}\././")
        
        # Determine target directory based on agent
        local target_prefix=""
        case "$agent" in
            copilot)
                target_prefix=".github"
                ;;
            claude)
                target_prefix=".claude"
                ;;
            *)
                target_prefix=".${agent}"  # Generic fallback
                ;;
        esac
        
        # Build destination path: .github/subdir/filename.ext
        if [ -z "$relative_dir" ]; then
            echo "$target_prefix/$dest_filename|$filepath|AGENT_FILE"
        else
            echo "$target_prefix/$relative_dir/$dest_filename|$filepath|AGENT_FILE"
        fi
    done >> "$temp_agent"
    
    # Step 3: Merge - agent-specific overrides universal by filepath
    # Priority: AGENT (dir-level) > AGENT_NESTED (in-dir override) > AGENT_FILE (routing) > UNIVERSAL
    cat "$temp_universal" "$temp_agent" | \
        awk -F'|' '{
            dest=$1; source=$2; type=$3
            
            # Priority order: AGENT > AGENT_NESTED > AGENT_FILE > UNIVERSAL
            # Higher priority types override lower ones for same destination
            if (!(dest in files) || type == "AGENT" || (type == "AGENT_NESTED" && types[dest] == "UNIVERSAL") || (type == "AGENT_FILE" && types[dest] == "UNIVERSAL")) {
                files[dest]=source
                types[dest]=type
            }
        }
        END {
            for (dest in files) {
                print dest "|" files[dest] "|" types[dest]
            }
        }'
    
    # Cleanup
    rm -f "$temp_universal" "$temp_agent"
}

# Apply merged overlay to target directory
# Copies files from vault to target, respecting agent-specific overrides
apply_overlay_to_target() {
    local agent=$1
    local target_dir=$2
    
    if [ -z "$agent" ] || [ -z "$target_dir" ]; then
        echo "❌ apply_overlay_to_target: agent and target_dir required" >&2
        return 1
    fi
    
    local agent_target_path=$(get_agent_target_path "$agent")
    
    echo "📦 Applying $agent overlay to $target_dir/$agent_target_path..."
    
    # Get merged file list
    merge_agent_overlay "$agent" | while IFS='|' read -r dest_relative source_path file_type; do
        local dest_path="$target_dir/$agent_target_path/$dest_relative"
        local dest_dir=$(dirname "$dest_path")
        
        mkdir -p "$dest_dir"
        cp "$source_path" "$dest_path"
        
        if [ "$file_type" = "AGENT" ]; then
            echo "  ✓ $dest_relative (agent-specific)"
        else
            echo "  ✓ $dest_relative"
        fi
    done
}

# ============================================================================
# SYMLINK MODE
# ============================================================================
symlink_mode() {
    # Delegate to Python sync tool for per-file symlinks with agent filtering
    local args="--mode symlink"
    [ -n "$AGENT" ] && args="$args --agent $AGENT"
    [ -n "$SPECIFIC_REPO" ] && args="$args --repo $SPECIFIC_REPO"
    [ "$DRY_RUN" = "true" ] && args="$args --dry-run"
    python3 "$VAULT_PATH/sync.py" $args
}

symlink_repo() {
    local repo_name="$1"
    local repo_path="$2"
    local vault_repo_path="$3"
    local agent="$4"
    
    if [ -z "$agent" ]; then
        echo "❌ symlink_repo: agent parameter required" >&2
        return 1
    fi
    
    # Get agent-specific target path
    local agent_target_path=$(get_agent_target_path "$agent")
    
    echo "📁 Repo: $repo_name (agent: $agent → $agent_target_path)"
    
    # Check if vault repo directory exists
    if [ ! -d "$vault_repo_path" ]; then
        echo "  ⚠️  Vault directory not found: $vault_repo_path"
        echo "      Skipping $agent symlinks"
        return 0
    fi
    
    local agent_link="$repo_path/$agent_target_path"
    
    # Check if link already exists and is valid
    if [ -L "$agent_link" ]; then
        local current_target=$(readlink "$agent_link")
        if [ "$current_target" = "$vault_repo_path" ]; then
            echo "  ✓ $agent_target_path already symlinked correctly"
            return 0
        elif [ -e "$agent_link" ]; then
            echo "  ⚠️  $agent_target_path symlinked to different target: $current_target"
            echo "      Remove manually to relink"
            return 0
        else
            echo "  🔧 $agent_target_path is broken symlink, repairing..."
            rm "$agent_link"
        fi
    elif [ -d "$agent_link" ]; then
        echo "  ⚠️  $agent_target_path exists as directory, skipping"
        return 0
    elif [ -e "$agent_link" ]; then
        echo "  ⚠️  $agent_target_path exists as file, skipping"
        return 0
    fi
    
    # Create symlink
    if ln -s "$vault_repo_path" "$agent_link" 2>/dev/null; then
        echo "  ✓ Created $agent_target_path symlink"
    else
        echo "  ❌ Failed to create $agent_target_path symlink"
        return 1
    fi
    
    # TODO: Module-level symlinks (detect copilot-instructions.md in subdirectories)
    # Disabled for now until overlay integration is complete
    # find "$vault_repo_path" -mindepth 1 -type d 2>/dev/null | while read dir; do
    #     rel_path="${dir#$vault_repo_path/}"
    #     if [[ "$rel_path" != "."* ]]; then
    #         module_vault="$vault_repo_path/$rel_path"
    #         module_repo="$repo_path/$rel_path/$agent_target_path"
    #         
    #         if [ -f "$module_vault/copilot-instructions.md" ] && [ -d "$repo_path/$rel_path" ]; then
    #             if [ -L "$module_repo" ]; then
    #                 echo "  ✓ $rel_path/$agent_target_path already symlinked"
    #             elif [ ! -e "$module_repo" ]; then
    #                 mkdir -p "$(dirname "$module_repo")"
    #                 ln -s "$module_vault" "$module_repo"
    #                 echo "  ✓ Created $rel_path/$agent_target_path symlink"
    #             fi
    #         fi
    #     fi
    # done
}

# ============================================================================
# WATCH MODE
# ============================================================================
watch_mode() {
    echo "👀 WATCH MODE: Starting bi-directional sync..."
    echo "   Repos: $REPOS_BASE"
    echo "   Copilot: $COPILOT_PATH ↔ $COPILOT_VAULT"
    echo "   Press Ctrl+C to stop"
    echo ""
    
    if [ -f "$LOCK_FILE" ]; then
        OLD_PID=$(cat "$LOCK_FILE")
        if ps -p "$OLD_PID" > /dev/null 2>&1; then
            echo "❌ Watcher already running (PID: $OLD_PID)"
            return 1
        else
            rm "$LOCK_FILE"
        fi
    fi
    
    echo $$ > "$LOCK_FILE"
    trap "rm -f $LOCK_FILE; exit 0" SIGINT SIGTERM EXIT
    
    mkdir -p "$COPILOT_VAULT"
    
    {
        fswatch -r "$REPOS_BASE" --exclude='\.git' --exclude='\.DS_Store' --exclude='node_modules' 2>/dev/null &
        fswatch -r "$COPILOT_VAULT" --exclude='\.git' --exclude='\.DS_Store' 2>/dev/null &
        fswatch -r "$COPILOT_PATH" --exclude='\.git' --exclude='\.DS_Store' 2>/dev/null &
        wait
    } | while read file; do
        sleep 0.5
        [ -e "$file" ] || continue
        
        if [[ "$file" == "$REPOS_BASE"* ]]; then
            repo_relative=$(echo "$file" | sed "s|$REPOS_BASE/||" | cut -d/ -f1)
            if [ -n "$repo_relative" ]; then
                echo "🔄 Vault → Repo: $(basename "$file")"
                while read repo_path; do
                    repo_name=$(basename "$repo_path")
                    if [ "$repo_name" = "$repo_relative" ] && [ -d "$repo_path" ]; then
                        cd "$repo_path"
                        bash "$SYNC_SCRIPT" 2>&1 | sed 's/^/   /'
                        break
                    fi
                done < <(get_managed_repos)
            fi
        elif [[ "$file" == "$COPILOT_VAULT"* ]]; then
            relative_path="${file#$COPILOT_VAULT/}"
            copilot_target="$COPILOT_PATH/$relative_path"
            
            if [ -f "$file" ]; then
                mkdir -p "$(dirname "$copilot_target")"
                cp "$file" "$copilot_target"
                echo "📤 Synced: $(basename "$file") → ~/.copilot"
            elif [ -d "$file" ]; then
                mkdir -p "$copilot_target"
            fi
        elif [[ "$file" == "$COPILOT_PATH"* ]]; then
            relative_path="${file#$COPILOT_PATH/}"
            vault_target="$COPILOT_VAULT/$relative_path"
            
            if [ -f "$file" ]; then
                mkdir -p "$(dirname "$vault_target")"
                cp "$file" "$vault_target"
                echo "📥 Synced: $(basename "$file") → vault"
            elif [ -d "$file" ] && [ ! -L "$file" ]; then
                mkdir -p "$vault_target"
            fi
        fi
    done
}

if [ ! -f "$REPOS_INDEX" ]; then
    echo "❌ INDEX.md not found: $REPOS_INDEX"
    exit 1
fi

if [[ "$MODE" == "symlink" ]] || [[ "$MODE" == "both" ]]; then
    symlink_mode
fi

if [[ "$MODE" == "watch" ]] || [[ "$MODE" == "both" ]]; then
    watch_mode
fi
