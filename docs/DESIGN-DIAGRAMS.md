# FV-Copilot System Design Diagrams

> Full Mermaid design documentation covering architecture, data flow, and every function scope.

---

## Table of Contents

1. [System Architecture Overview](#1-system-architecture-overview)
2. [Vault Directory Structure](#2-vault-directory-structure)
3. [Agent Routing System](#3-agent-routing-system)
4. [File Resolution Priority](#4-file-resolution-priority)
5. [watch-and-sync.sh — Function Scopes](#5-watch-and-syncsh--function-scopes)
6. [merge-and-relink.sh — Function Scopes](#6-merge-and-relinksh--function-scopes)
7. [remove-links.sh — Function Scope](#7-remove-linkssh--function-scope)
8. [install-launchd-service.sh — Function Scope](#8-install-launchd-servicesh--function-scope)
9. [Git Hooks — Function Scopes](#9-git-hooks--function-scopes)
10. [Sync Modes — State Machine](#10-sync-modes--state-machine)
11. [Data Flow — End to End](#11-data-flow--end-to-end)
12. [Multi-Agent Overlay Merge — Sequence](#12-multi-agent-overlay-merge--sequence)
13. [TypeScript Rewrite — Target Module Map](#13-typescript-rewrite--target-module-map)
14. [Runtime Environment](#14-runtime-environment)

---

## 1. System Architecture Overview

```mermaid
graph TB
    subgraph Vault["FV-Copilot Vault (~/FV-Copilot)"]
        AI[agents/INDEX.md]
        RI[repos/INDEX.md]

        subgraph VaultContent["Vault-Wide Content"]
            SK[skills/]
            IN[instructions/]
            DO[docs/]
        end

        subgraph AgentOverrides["Agent-Specific Overrides"]
            SKC[skills.copilot/]
            SKCL[skills.claude/]
            INC[instructions.copilot/]
            INCL[instructions.claude/]
        end

        subgraph RepoMirrors["Repo Mirrors (repos/)"]
            RP1[repos/FV-Platform-Main/]
            RP2[repos/OtherRepo/]
        end

        subgraph Scripts["CLI Scripts"]
            WS[watch-and-sync.sh]
            MR[merge-and-relink.sh]
            RL[remove-links.sh]
            IL[install-launchd-service.sh]
        end

        subgraph GitHooks["Git Hooks"]
            PM[git-hooks/post-merge]
            PC[git-hooks/post-commit]
        end
    end

    subgraph Repos["Target Repositories (~/git/)"]
        subgraph Repo1["FV-Platform-Main"]
            GH1[".github/ (symlink)"]
            CL1[".claude/ (symlink)"]
        end
        subgraph Repo2["OtherRepo"]
            GH2[".github/ (symlink)"]
            CL2[".claude/ (symlink)"]
        end
    end

    subgraph Agents["AI Agents"]
        COP[GitHub Copilot]
        CLU[Claude]
        CUR[Cursor]
    end

    RP1 -.->|symlink| GH1
    RP1 -.->|symlink| CL1
    RP2 -.->|symlink| GH2
    RP2 -.->|symlink| CL2

    GH1 -->|reads| COP
    CL1 -->|reads| CLU
    GH2 -->|reads| COP
    CL2 -->|reads| CLU

    WS -->|creates symlinks| Repo1
    WS -->|creates symlinks| Repo2
    MR -->|merges & relinks| Repo1
    PM -->|auto-repairs| Repo1

    AI -->|agent registry| WS
    RI -->|repo list| WS
    VaultContent -->|universal content| WS
    AgentOverrides -->|agent overrides| WS
```

---

## 2. Vault Directory Structure

```mermaid
graph LR
    subgraph Root["~/FV-Copilot/"]
        direction TB
        A1["agents/INDEX.md"]
        A2["repos/INDEX.md"]

        subgraph Universal["Universal Content"]
            B1["skills/*.md"]
            B2["instructions/*.md"]
            B3["docs/*.md"]
        end

        subgraph NestedOverrides["Nested Overrides (in-dir)"]
            C1["skills/setup.copilot.md"]
            C2["skills/python.claude.md"]
            C3["instructions/x.copilot.md"]
        end

        subgraph DirOverrides["Directory Overrides"]
            D1["skills.copilot/*.md"]
            D2["skills.claude/*.md"]
            D3["instructions.copilot/*.md"]
        end

        subgraph RootRouting["Root-Level File Routing"]
            E1["quickstart.copilot.md"]
            E2["prompts.claude.txt"]
        end

        subgraph RepoMirror["repos/{RepoName}/"]
            F1["copilot-instructions.md"]
            F2["skills/"]
            F3["agent.md"]
        end

        subgraph Excluded["Vault-Only (gitignored)"]
            G1[".obsidian/"]
            G2["scratch/"]
            G3["thinking/"]
            G4["copilot-cli/"]
        end
    end
```

---

## 3. Agent Routing System

```mermaid
flowchart TD
    Start([File in Vault]) --> CheckAgent{Agent suffix<br/>in filename?}

    CheckAgent -->|"*.copilot.*"| CopilotRoute
    CheckAgent -->|"*.claude.*"| ClaudeRoute
    CheckAgent -->|No suffix| UniversalRoute

    subgraph CopilotRoute["Copilot Routing"]
        CR1{Location?}
        CR1 -->|"In skills.copilot/"| CRP1["Priority: AGENT<br/>(Highest)"]
        CR1 -->|"In skills/"| CRP2["Priority: AGENT_NESTED<br/>(Medium)"]
        CR1 -->|"At vault root"| CRP3["Priority: AGENT_FILE<br/>(Routes to .github/)"]
    end

    subgraph ClaudeRoute["Claude Routing"]
        CLR1{Location?}
        CLR1 -->|"In skills.claude/"| CLRP1["Priority: AGENT<br/>(Highest)"]
        CLR1 -->|"In skills/"| CLRP2["Priority: AGENT_NESTED<br/>(Medium)"]
        CLR1 -->|"At vault root"| CLRP3["Priority: AGENT_FILE<br/>(Routes to .claude/)"]
    end

    subgraph UniversalRoute["Universal Routing"]
        UR1["Priority: UNIVERSAL<br/>(Lowest — fallback)"]
    end

    CRP1 --> Target1[".github/ in repo"]
    CRP2 --> Target1
    CRP3 --> Target1
    CLRP1 --> Target2[".claude/ in repo"]
    CLRP2 --> Target2
    CLRP3 --> Target2
    UR1 --> Target3["All agent dirs"]

    style CRP1 fill:#2d6a4f,color:#fff
    style CLRP1 fill:#2d6a4f,color:#fff
    style CRP2 fill:#40916c,color:#fff
    style CLRP2 fill:#40916c,color:#fff
    style CRP3 fill:#74c69d,color:#000
    style CLRP3 fill:#74c69d,color:#000
    style UR1 fill:#b7e4c7,color:#000
```

---

## 4. File Resolution Priority

```mermaid
flowchart TD
    Req([Resolve: skills/setup.md<br/>for agent=copilot]) --> P1

    P1{skills.copilot/setup.md<br/>exists?}
    P1 -->|Yes| W1["Use AGENT<br/>(Directory Override)"]
    P1 -->|No| P2

    P2{skills/setup.copilot.md<br/>exists?}
    P2 -->|Yes| W2["Use AGENT_NESTED<br/>(In-Dir Override)"]
    P2 -->|No| P3

    P3{skills/setup.md<br/>exists?}
    P3 -->|Yes| W3["Use UNIVERSAL<br/>(Fallback)"]
    P3 -->|No| W4["File not found<br/>(skip)"]

    W1 --> Result([Resolved File])
    W2 --> Result
    W3 --> Result

    style W1 fill:#2d6a4f,color:#fff
    style W2 fill:#40916c,color:#fff
    style W3 fill:#b7e4c7,color:#000
    style W4 fill:#d62828,color:#fff
```

---

## 5. watch-and-sync.sh — Function Scopes

### 5.1 Top-Level Entry Point

```mermaid
flowchart TD
    Entry([watch-and-sync.sh]) --> ParseArgs["Parse CLI Args<br/>--mode, --repo, --agent, --dry-run"]
    ParseArgs --> ValidateMode{Mode valid?<br/>symlink|watch|both}
    ValidateMode -->|No| Err1["Exit: Invalid mode"]
    ValidateMode -->|Yes| CheckFswatch{fswatch<br/>installed?}
    CheckFswatch -->|No| Err2["Exit: fswatch not installed"]
    CheckFswatch -->|Yes| CheckIndex{repos/INDEX.md<br/>exists?}
    CheckIndex -->|No| Err3["Exit: INDEX.md not found"]
    CheckIndex -->|Yes| ModeSwitch

    ModeSwitch{Mode?}
    ModeSwitch -->|symlink / both| SymlinkMode["symlink_mode()"]
    ModeSwitch -->|watch / both| WatchMode["watch_mode()"]

    style Entry fill:#264653,color:#fff
    style SymlinkMode fill:#2a9d8f,color:#fff
    style WatchMode fill:#e9c46a,color:#000
```

### 5.2 get_registered_agents()

```mermaid
flowchart TD
    GRA([get_registered_agents]) --> CheckFile{agents/INDEX.md<br/>exists?}
    CheckFile -->|No| ErrReturn["stderr: not found<br/>return 1"]
    CheckFile -->|Yes| ParseAwk["AWK: extract lines after<br/>'## Active Agents' header<br/>matching '- agentname'"]
    ParseAwk --> Output["stdout: agent names<br/>(one per line)"]

    style GRA fill:#264653,color:#fff
```

### 5.3 detect_agent_dirs()

```mermaid
flowchart TD
    DAD(["detect_agent_dirs(agent)"]) --> Check1{"skills.{agent}/<br/>exists?"}
    Check1 -->|Yes| Found1[found=true]
    Check1 -->|No| Check2

    Check2{"instructions.{agent}/<br/>exists?"}
    Check2 -->|Yes| Found2[found=true]
    Check2 -->|No| Check3

    Check3{"docs.{agent}/<br/>exists?"}
    Check3 -->|Yes| Found3[found=true]
    Check3 -->|No| ReturnFalse["return 1<br/>(not found)"]

    Found1 --> ReturnTrue["return 0<br/>(found)"]
    Found2 --> ReturnTrue
    Found3 --> ReturnTrue

    style DAD fill:#264653,color:#fff
```

### 5.4 get_agent_target_path()

```mermaid
flowchart TD
    GATP(["get_agent_target_path(agent)"]) --> CheckEmpty{agent<br/>empty?}
    CheckEmpty -->|Yes| ErrReturn["stderr: agent required<br/>return 1"]
    CheckEmpty -->|No| Switch

    Switch{agent name?}
    Switch -->|copilot| Out1[".github"]
    Switch -->|claude| Out2[".claude"]
    Switch -->|cursor| Out3[".cursor"]
    Switch -->|other| Out4[".{agent}"]

    Out1 --> Stdout["stdout: path"]
    Out2 --> Stdout
    Out3 --> Stdout
    Out4 --> Stdout

    style GATP fill:#264653,color:#fff
```

### 5.5 validate_agents()

```mermaid
flowchart TD
    VA([validate_agents]) --> Phase1["Phase 1: For each registered agent"]
    Phase1 --> Check1{"detect_agent_dirs(agent)?"}
    Check1 -->|No| Warn1["Warning: agent in INDEX<br/>but no dirs found"]
    Check1 -->|Yes| Next1["Continue"]

    Phase1 --> Phase2["Phase 2: For each skills.*/instructions.*/docs.*"]
    Phase2 --> Extract["Extract agent name<br/>from dir suffix"]
    Extract --> Check2{"agent in<br/>INDEX.md?"}
    Check2 -->|No| Warn2["Warning: dir found but<br/>agent not registered"]
    Check2 -->|Yes| Next2["Continue"]

    Warn1 --> Count["Count warnings"]
    Warn2 --> Count
    Next1 --> Count
    Next2 --> Count
    Count --> Result{warnings == 0?}
    Result -->|Yes| Pass["Agent validation passed"]
    Result -->|No| Warnings["Print warnings"]

    style VA fill:#264653,color:#fff
```

### 5.6 get_managed_repos()

```mermaid
flowchart TD
    GMR([get_managed_repos]) --> ReadIndex["Read repos/INDEX.md"]
    ReadIndex --> AWK["AWK: extract lines<br/>starting with ~ or /"]
    AWK --> Expand["eval echo: expand<br/>~ to $HOME"]
    Expand --> Output["stdout: full repo paths<br/>(one per line)"]

    style GMR fill:#264653,color:#fff
```

### 5.7 merge_agent_overlay()

```mermaid
flowchart TD
    MAO(["merge_agent_overlay(agent, source_path)"]) --> CheckAgent{agent<br/>empty?}
    CheckAgent -->|Yes| ErrReturn["stderr: agent required<br/>return 1"]
    CheckAgent -->|No| Step1

    subgraph Step1["Step 1: Scan Universal Directories"]
        S1A["find -maxdepth 1 -type d<br/>(no . prefix, no agent suffix)"]
        S1A --> S1B["For each universal dir:"]
        S1B --> S1C["find all files"]
        S1C --> S1D{"filename matches<br/>.{agent}. pattern?"}
        S1D -->|Yes| S1E["Emit: dest|source|AGENT_NESTED<br/>(strip agent suffix)"]
        S1D -->|No| S1F["Emit: dest|source|UNIVERSAL"]
    end

    Step1 --> Step2

    subgraph Step2["Step 2: Scan Agent-Specific Directories"]
        S2A["find -maxdepth 1 -type d<br/>-name '*.{agent}'"]
        S2A --> S2B["For each agent dir:"]
        S2B --> S2C["Map dir.agent/ → dir/"]
        S2C --> S2D["Emit: dest|source|AGENT"]
    end

    Step2 --> Step2b

    subgraph Step2b["Step 2b: Scan Root Agent Files"]
        S2bA["find -type f<br/>-name '*.{agent}.*'"]
        S2bA --> S2bB["Strip agent suffix from name"]
        S2bB --> S2bC["Map to target path<br/>(.github/, .claude/, etc.)"]
        S2bC --> S2bD["Emit: dest|source|AGENT_FILE"]
    end

    Step2b --> Step3

    subgraph Step3["Step 3: Priority Merge (AWK)"]
        S3A["Concatenate all emissions"]
        S3A --> S3B["AWK associative arrays:<br/>key = dest path"]
        S3B --> S3C["Override rules:<br/>AGENT > AGENT_NESTED > AGENT_FILE > UNIVERSAL"]
        S3C --> S3D["Output: merged file list<br/>(dest|source|type per line)"]
    end

    style MAO fill:#264653,color:#fff
    style Step1 fill:#e9f5db,color:#000
    style Step2 fill:#d8f3dc,color:#000
    style Step2b fill:#b7e4c7,color:#000
    style Step3 fill:#95d5b2,color:#000
```

### 5.8 apply_overlay_to_target()

```mermaid
flowchart TD
    AOT(["apply_overlay_to_target(agent, target_dir)"]) --> Validate{agent and<br/>target_dir set?}
    Validate -->|No| ErrReturn["stderr: required params"]
    Validate -->|Yes| GetPath["get_agent_target_path(agent)"]
    GetPath --> CallMerge["merge_agent_overlay(agent)"]
    CallMerge --> Loop["For each: dest|source|type"]
    Loop --> MkDir["mkdir -p dest_dir"]
    MkDir --> Copy["cp source → target_dir/{agent_path}/dest"]
    Copy --> Log{"type == AGENT?"}
    Log -->|Yes| LogAgent["Log: (agent-specific)"]
    Log -->|No| LogNormal["Log: dest"]

    style AOT fill:#264653,color:#fff
```

### 5.9 symlink_mode()

```mermaid
flowchart TD
    SM([symlink_mode]) --> ValidateAgent{--agent<br/>specified?}

    ValidateAgent -->|Yes| CheckRegistered{"Agent in<br/>INDEX.md?"}
    CheckRegistered -->|No| ErrAgent["Exit: Agent not registered"]
    CheckRegistered -->|Yes| ProceedAgent["Use single agent"]

    ValidateAgent -->|No| AllAgents["Use all registered agents"]

    ProceedAgent --> CheckRepo{--repo<br/>specified?}
    AllAgents --> CheckRepo

    CheckRepo -->|Yes| SingleRepo["Process single repo"]
    CheckRepo -->|No| AllRepos["Loop: get_managed_repos()"]

    SingleRepo --> ForEachAgent["For each agent:<br/>symlink_repo()"]
    AllRepos --> ForEachRepo["For each repo path:"]
    ForEachRepo --> CheckExists{repo dir<br/>exists?}
    CheckExists -->|No| SkipWarn["Warning: skip"]
    CheckExists -->|Yes| ForEachAgent2["For each agent:<br/>symlink_repo()"]

    style SM fill:#2a9d8f,color:#fff
```

### 5.10 symlink_repo()

```mermaid
flowchart TD
    SR(["symlink_repo(name, path, vault_path, agent)"]) --> CheckAgentParam{agent<br/>param set?}
    CheckAgentParam -->|No| ErrReturn["stderr: agent required"]
    CheckAgentParam -->|Yes| GetTarget["target = get_agent_target_path(agent)<br/>e.g. .github, .claude"]

    GetTarget --> CheckVaultDir{vault repo dir<br/>exists?}
    CheckVaultDir -->|No| SkipWarn["Warning: skip"]
    CheckVaultDir -->|Yes| CheckLink

    CheckLink{repo/{target}<br/>is symlink?}
    CheckLink -->|Yes, correct target| AlreadyOk["Already symlinked correctly"]
    CheckLink -->|Yes, wrong target| WrongTarget["Warning: different target"]
    CheckLink -->|Yes, broken| Repair["Remove broken link"]
    CheckLink -->|No, is directory| DirExists["Warning: real dir exists"]
    CheckLink -->|No, is file| FileExists["Warning: file exists"]
    CheckLink -->|No, nothing| Create

    Repair --> Create["ln -s vault_path → repo/{target}"]
    Create --> Success["Created symlink"]

    style SR fill:#2a9d8f,color:#fff
```

### 5.11 watch_mode()

```mermaid
flowchart TD
    WM([watch_mode]) --> CheckLock{Lock file<br/>exists?}
    CheckLock -->|Yes| CheckPid{"Process<br/>still running?"}
    CheckPid -->|Yes| ErrRunning["Exit: already running"]
    CheckPid -->|No| RemoveLock["Remove stale lock"]
    CheckLock -->|No| WriteLock["Write PID to lock"]
    RemoveLock --> WriteLock

    WriteLock --> SetTrap["trap: cleanup on SIGINT/SIGTERM/EXIT"]
    SetTrap --> StartWatch["Start 3 fswatch processes:"]

    subgraph Watchers["Parallel fswatch Processes"]
        W1["fswatch repos/"]
        W2["fswatch copilot-cli/"]
        W3["fswatch ~/.copilot/"]
    end

    StartWatch --> Watchers
    Watchers --> EventLoop["Event Loop: for each changed file"]

    EventLoop --> Route{File source?}

    Route -->|"repos/*"| VaultToRepo["Vault → Repo sync<br/>(run sync-github.sh)"]
    Route -->|"copilot-cli/*"| VaultToCopilot["Vault → ~/.copilot<br/>(cp file)"]
    Route -->|"~/.copilot/*"| CopilotToVault["~/.copilot → Vault<br/>(cp file)"]

    style WM fill:#e9c46a,color:#000
    style Watchers fill:#fefae0,color:#000
```

---

## 6. merge-and-relink.sh — Function Scopes

### 6.1 Top-Level Entry Point

```mermaid
flowchart TD
    Entry([merge-and-relink.sh]) --> ParseArgs["Parse CLI Args<br/>--force, --repo"]
    ParseArgs --> DryRunCheck{--force<br/>specified?}
    DryRunCheck -->|No| DryMode["DRY RUN MODE"]
    DryRunCheck -->|Yes| LiveMode["LIVE MODE"]

    DryMode --> CheckIndex{INDEX.md<br/>exists?}
    LiveMode --> CheckIndex
    CheckIndex -->|No| ErrExit["Exit: INDEX.md not found"]
    CheckIndex -->|Yes| RepoCheck

    RepoCheck{--repo<br/>specified?}
    RepoCheck -->|Yes| SingleRepo["merge_repo(name, path, vault_path)"]
    RepoCheck -->|No| AllRepos["Loop: get_managed_repos()<br/>→ merge_repo() each"]

    style Entry fill:#264653,color:#fff
```

### 6.2 merge_repo()

```mermaid
flowchart TD
    MR(["merge_repo(name, repo_path, vault_path)"]) --> CheckGithub

    CheckGithub{".github is<br/>real directory?<br/>(not symlink)"}
    CheckGithub -->|"Symlink"| AlreadyLinked["Already symlinked — skip"]
    CheckGithub -->|"No .github"| NoGithub["No .github found"]
    CheckGithub -->|"Real dir"| ScanFiles

    ScanFiles["Count files in .github/"]
    ScanFiles --> ForEachFile["For each file in .github/:"]
    ForEachFile --> CheckVault{"File exists<br/>in vault?"}
    CheckVault -->|Yes| VaultWins["Skip: vault wins"]
    CheckVault -->|No| CopyNew["Copy to vault (new file)"]

    CopyNew --> RemoveReal["rm -rf repo/.github"]
    VaultWins --> RemoveReal
    RemoveReal --> CreateLink["ln -s vault_path → repo/.github"]
    CreateLink --> Done["Merged and relinked"]

    Done --> ScanModules["Scan nested modules<br/>(subdirs with .github/)"]
    ScanModules --> ForEachModule["For each module:"]
    ForEachModule --> CheckModuleGithub{".github is<br/>real directory?"}
    CheckModuleGithub -->|Yes| RepeatMerge["Same merge logic:<br/>copy new → rm → symlink"]
    CheckModuleGithub -->|Symlink| ModuleOk["Already linked"]

    style MR fill:#264653,color:#fff
```

### 6.3 get_managed_repos() (merge-and-relink version)

```mermaid
flowchart TD
    GMR([get_managed_repos]) --> Grep["grep '^- ' INDEX.md"]
    Grep --> Sed["sed: strip '- ' prefix"]
    Sed --> Output["stdout: repo names"]

    style GMR fill:#264653,color:#fff
```

---

## 7. remove-links.sh — Function Scope

```mermaid
flowchart TD
    Entry([remove-links.sh]) --> ParseArgs["Parse: --force, --repo"]
    ParseArgs --> Mode{--force?}
    Mode -->|No| DryRun["DRY RUN"]
    Mode -->|Yes| Live["LIVE MODE"]

    DryRun --> Scope
    Live --> Scope

    Scope{--repo<br/>specified?}
    Scope -->|Yes| SingleRepo["Check single repo"]
    Scope -->|No| AllRepos["Loop: get_managed_repos()"]

    SingleRepo --> CheckLink{".github<br/>is symlink?"}
    AllRepos --> CheckEach["For each repo:"]
    CheckEach --> CheckLink

    CheckLink -->|Yes| RemoveOrPreview{DRY_RUN?}
    CheckLink -->|No| ScanNested["find -type l -name .github"]
    ScanNested --> RemoveNestedOrPreview{DRY_RUN?}

    RemoveOrPreview -->|Dry| Preview["Print: (would remove)"]
    RemoveOrPreview -->|Live| Remove["rm symlink"]
    RemoveNestedOrPreview -->|Dry| PreviewN["Print: (would remove)"]
    RemoveNestedOrPreview -->|Live| RemoveN["rm symlink"]

    Preview --> Summary["Summary: N symlink(s) found"]
    Remove --> Summary
    PreviewN --> Summary
    RemoveN --> Summary

    style Entry fill:#264653,color:#fff
```

---

## 8. install-launchd-service.sh — Function Scope

```mermaid
flowchart TD
    Entry([install-launchd-service.sh]) --> MkDir["mkdir ~/Library/LaunchAgents/"]
    MkDir --> WritePlist["Write .plist file:<br/>com.fv-copilot.watch-and-sync.plist"]

    subgraph PlistConfig["Plist Configuration"]
        P1["Label: com.fv-copilot.watch-and-sync"]
        P2["Program: watch-and-sync.sh"]
        P3["RunAtLoad: true"]
        P4["KeepAlive: true"]
        P5["Logs: /tmp/fv-copilot-watcher.log"]
    end

    WritePlist --> PlistConfig
    PlistConfig --> Load["launchctl load plist"]
    Load --> Check{Load<br/>succeeded?}
    Check -->|Yes| Running["Service loaded and running"]
    Check -->|No| AlreadyLoaded["Warning: may already be loaded"]

    style Entry fill:#264653,color:#fff
    style PlistConfig fill:#fefae0,color:#000
```

---

## 9. Git Hooks — Function Scopes

### 9.1 post-merge Hook

```mermaid
flowchart TD
    Hook([post-merge]) --> GetRoot["git rev-parse --show-toplevel"]
    GetRoot --> GetName["basename → repo name"]
    GetName --> CheckScript{"merge-and-relink.sh<br/>exists?"}
    CheckScript -->|No| Noop["Exit silently"]
    CheckScript -->|Yes| CheckGithub{".github is<br/>real directory?<br/>(not symlink)"}
    CheckGithub -->|No| Noop2["Exit: nothing to repair"]
    CheckGithub -->|Yes| RunMerge["bash merge-and-relink.sh<br/>--force --repo {name}"]
    RunMerge --> Repaired["Symlinks repaired"]

    style Hook fill:#e76f51,color:#fff
```

### 9.2 post-commit Hook

```mermaid
flowchart TD
    Hook([post-commit]) --> CheckScript{"sync-github.sh<br/>exists?"}
    CheckScript -->|No| Noop["Exit silently"]
    CheckScript -->|Yes| RunAsync["bash sync-github.sh &<br/>(background)"]

    style Hook fill:#e76f51,color:#fff
```

---

## 10. Sync Modes — State Machine

```mermaid
stateDiagram-v2
    [*] --> ParseArgs

    ParseArgs --> SymlinkMode: mode=symlink
    ParseArgs --> WatchMode: mode=watch
    ParseArgs --> BothMode: mode=both

    BothMode --> SymlinkMode: phase 1
    BothMode --> WatchMode: phase 2

    state SymlinkMode {
        [*] --> ValidateAgent
        ValidateAgent --> ResolveRepos
        ResolveRepos --> ForEachRepo
        ForEachRepo --> ForEachAgent
        ForEachAgent --> CheckExisting
        CheckExisting --> CreateSymlink: not linked
        CheckExisting --> Skip: already linked
        CheckExisting --> Repair: broken link
        Repair --> CreateSymlink
        CreateSymlink --> [*]
        Skip --> [*]
    }

    state WatchMode {
        [*] --> AcquireLock
        AcquireLock --> StartFswatch
        StartFswatch --> ListenEvents
        ListenEvents --> RouteEvent
        RouteEvent --> VaultToRepo: repos/* changed
        RouteEvent --> VaultToCopilot: copilot-cli/* changed
        RouteEvent --> CopilotToVault: ~/.copilot/* changed
        VaultToRepo --> ListenEvents
        VaultToCopilot --> ListenEvents
        CopilotToVault --> ListenEvents
    }
```

---

## 11. Data Flow — End to End

```mermaid
sequenceDiagram
    participant User as Developer
    participant Vault as FV-Copilot Vault
    participant Script as watch-and-sync.sh
    participant Index as repos/INDEX.md
    participant AgentIdx as agents/INDEX.md
    participant Overlay as merge_agent_overlay()
    participant Repo as Target Repository
    participant Agent as AI Agent

    User->>Script: ./watch-and-sync.sh --mode symlink
    Script->>Index: Read managed repos
    Index-->>Script: [~/git/FV-Platform-Main, ...]
    Script->>AgentIdx: Read registered agents
    AgentIdx-->>Script: [copilot, claude]

    loop For each repo × agent
        Script->>Overlay: merge_agent_overlay(agent)
        Overlay->>Vault: Scan universal dirs
        Overlay->>Vault: Scan agent-specific dirs
        Overlay->>Vault: Scan nested overrides
        Overlay-->>Script: Merged file list (dest|source|priority)
        Script->>Repo: Create symlink: repo/.github → vault/repos/name
    end

    Note over Repo: Symlinks now active

    User->>Vault: Edit skills/python.md
    Note over Repo: Change visible instantly via symlink
    Agent->>Repo: Read .github/skills/python.md
    Repo-->>Agent: Content from vault
```

---

## 12. Multi-Agent Overlay Merge — Sequence

```mermaid
sequenceDiagram
    participant Caller
    participant MAO as merge_agent_overlay()
    participant FS as Filesystem
    participant AWK as AWK Merge

    Caller->>MAO: merge_agent_overlay("copilot")

    Note over MAO: Step 1: Universal Scan
    MAO->>FS: find dirs (no agent suffix)
    FS-->>MAO: [skills/, instructions/, docs/]

    loop For each universal dir
        MAO->>FS: find all files
        FS-->>MAO: [setup.md, python.md, python.copilot.md, ...]

        alt filename matches .copilot.
            MAO->>MAO: Emit: skills/python.md|path|AGENT_NESTED
        else no agent suffix
            MAO->>MAO: Emit: skills/python.md|path|UNIVERSAL
        end
    end

    Note over MAO: Step 2: Agent Dir Scan
    MAO->>FS: find dirs named *.copilot
    FS-->>MAO: [skills.copilot/, instructions.copilot/]

    loop For each agent dir
        MAO->>FS: find all files
        FS-->>MAO: [setup.md, database.md, ...]
        MAO->>MAO: Emit: skills/setup.md|path|AGENT
    end

    Note over MAO: Step 2b: Root Agent Files
    MAO->>FS: find -name "*.copilot.*"
    FS-->>MAO: [quickstart.copilot.md]
    MAO->>MAO: Emit: .github/quickstart.md|path|AGENT_FILE

    Note over MAO: Step 3: Priority Merge
    MAO->>AWK: All emissions
    AWK->>AWK: For same dest: AGENT > AGENT_NESTED > AGENT_FILE > UNIVERSAL
    AWK-->>Caller: Final merged list
```

---

## 13. TypeScript Rewrite — Target Module Map

```mermaid
graph TD
    subgraph CLI["fv-copilot CLI (TypeScript)"]
        Main["src/index.ts<br/>(CLI entry point)"]

        subgraph Core["src/core/"]
            Config["config.ts<br/>VaultConfig, paths, constants"]
            AgentRegistry["agent-registry.ts<br/>getRegisteredAgents()<br/>detectAgentDirs()<br/>getAgentTargetPath()<br/>validateAgents()"]
            RepoIndex["repo-index.ts<br/>getManagedRepos()"]
            Overlay["overlay.ts<br/>mergeAgentOverlay()<br/>applyOverlayToTarget()"]
        end

        subgraph Commands["src/commands/"]
            Symlink["symlink.ts<br/>symlinkMode()<br/>symlinkRepo()"]
            Watch["watch.ts<br/>watchMode()<br/>startFswatch()"]
            Merge["merge.ts<br/>mergeRepo()<br/>mergeAndRelink()"]
            Remove["remove.ts<br/>removeLinks()"]
            Install["install.ts<br/>installLaunchdService()"]
            Validate["validate.ts<br/>validateAll()"]
        end

        subgraph Hooks["src/hooks/"]
            PostMerge["post-merge.ts"]
            PostCommit["post-commit.ts"]
        end

        subgraph Utils["src/utils/"]
            FS["fs-utils.ts<br/>symlinkSafe(), isSymlink()"]
            Shell["shell.ts<br/>exec(), spawn()"]
            Logger["logger.ts<br/>info(), warn(), error()"]
        end
    end

    Main --> Config
    Main --> Commands
    Symlink --> AgentRegistry
    Symlink --> RepoIndex
    Symlink --> Overlay
    Symlink --> FS
    Watch --> RepoIndex
    Watch --> Shell
    Merge --> RepoIndex
    Merge --> FS
    Remove --> RepoIndex
    Remove --> FS
    Install --> Config
    Validate --> AgentRegistry

    style CLI fill:#1a1a2e,color:#fff
    style Core fill:#16213e,color:#fff
    style Commands fill:#0f3460,color:#fff
    style Hooks fill:#533483,color:#fff
    style Utils fill:#e94560,color:#fff
```

---

## 14. Runtime Environment

```mermaid
graph LR
    subgraph DevMachine["Developer Machine (macOS arm64)"]
        subgraph Runtimes["Runtime Managers"]
            PYENV["pyenv 2.6.3<br/>Python 3.13.5 (default)"]
            NVM["nvm 0.39.3<br/>Node 25.2.1 (default)"]
        end

        subgraph Available["Available Runtimes"]
            PY39["Python 3.9-dev"]
            PY313["Python 3.13-dev"]
            PY3135["Python 3.13.5"]
            N18["Node 18.20.4"]
            N22["Node 22.21.1"]
            N25["Node 25.2.1"]
        end

        subgraph Tools["System Tools"]
            BREW["Homebrew 5.0.13<br/>(/opt/homebrew/bin)"]
            GIT["Git"]
            FSWATCH["fswatch"]
        end

        PYENV --> PY39
        PYENV --> PY313
        PYENV --> PY3135
        NVM --> N18
        NVM --> N22
        NVM --> N25
    end

    style DevMachine fill:#1a1a2e,color:#fff
    style Runtimes fill:#16213e,color:#fff
    style Available fill:#0f3460,color:#fff
    style Tools fill:#533483,color:#fff
```

---

## Diagram Index

| # | Diagram | Type | Covers |
|---|---------|------|--------|
| 1 | System Architecture | Graph | Full system overview |
| 2 | Vault Directory Structure | Graph | File/folder layout |
| 3 | Agent Routing System | Flowchart | 3-tier routing logic |
| 4 | File Resolution Priority | Flowchart | Priority cascade |
| 5.1 | watch-and-sync: Entry | Flowchart | CLI parsing, mode dispatch |
| 5.2 | get_registered_agents() | Flowchart | Agent index parser |
| 5.3 | detect_agent_dirs() | Flowchart | Dir existence checks |
| 5.4 | get_agent_target_path() | Flowchart | Agent→path mapping |
| 5.5 | validate_agents() | Flowchart | Bidirectional validation |
| 5.6 | get_managed_repos() | Flowchart | Repo index parser |
| 5.7 | merge_agent_overlay() | Flowchart | 3-step overlay merge |
| 5.8 | apply_overlay_to_target() | Flowchart | File copy with overlay |
| 5.9 | symlink_mode() | Flowchart | Symlink orchestrator |
| 5.10 | symlink_repo() | Flowchart | Per-repo symlink logic |
| 5.11 | watch_mode() | Flowchart | Fswatch event loop |
| 6.1 | merge-and-relink: Entry | Flowchart | CLI parsing, dispatch |
| 6.2 | merge_repo() | Flowchart | Merge+relink per repo |
| 6.3 | get_managed_repos() (v2) | Flowchart | Repo parser variant |
| 7 | remove-links.sh | Flowchart | Full script flow |
| 8 | install-launchd-service.sh | Flowchart | Plist install flow |
| 9.1 | post-merge hook | Flowchart | Auto-repair flow |
| 9.2 | post-commit hook | Flowchart | Auto-sync flow |
| 10 | Sync Modes | State Diagram | Mode state machine |
| 11 | Data Flow (E2E) | Sequence | Full sync sequence |
| 12 | Overlay Merge | Sequence | merge_agent_overlay detail |
| 13 | TypeScript Module Map | Graph | Rewrite target architecture |
| 14 | Runtime Environment | Graph | pyenv, nvm, tools |
