# FV-Copilot System Design Diagrams

> Full Mermaid design documentation covering architecture, data flow, and every function scope.

---

## Table of Contents

1. [System Architecture Overview](#1-system-architecture-overview)
2. [Vault Directory Structure](#2-vault-directory-structure)
3. [Agent Routing System](#3-agent-routing-system)
4. [File Resolution Priority](#4-file-resolution-priority)
5. [sync.py — Function Scopes](#5-syncpy--function-scopes)
6. [install-launchd-service.sh — Function Scope](#6-install-launchd-servicesh--function-scope)
7. [Git Hooks — Function Scopes](#7-git-hooks--function-scopes)
8. [Sync Modes — State Machine](#8-sync-modes--state-machine)
9. [Data Flow — End to End](#9-data-flow--end-to-end)
10. [Multi-Agent Overlay Merge — Sequence](#10-multi-agent-overlay-merge--sequence)
11. [Runtime Environment](#11-runtime-environment)

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

        subgraph Scripts["CLI Tools"]
            SP[sync.py]
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

    SP -->|creates symlinks| Repo1
    SP -->|creates symlinks| Repo2
    PM -->|auto-repairs| Repo1

    AI -->|agent registry| SP
    RI -->|repo list| SP
    VaultContent -->|universal content| SP
    AgentOverrides -->|agent overrides| SP
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

## 5. sync.py — Function Scopes

### 5.1 Top-Level Entry Point

```mermaid
flowchart TD
    Entry([sync.py]) --> ParseArgs["Parse CLI Args<br/>--mode, --repo, --agent, --dry-run,<br/>--clean, --merge, --watch"]
    ParseArgs --> Dispatch

    Dispatch{Action?}
    Dispatch -->|--watch| WatchMode["VaultWatcher.run()"]
    Dispatch -->|--clean| CleanMode["remove_all_links()"]
    Dispatch -->|--merge| MergeMode["merge_and_relink()"]
    Dispatch -->|--mode symlink| SymlinkMode["sync_all() → symlink"]
    Dispatch -->|--mode copy| CopyMode["sync_all() → copy"]

    style Entry fill:#264653,color:#fff
    style WatchMode fill:#e9c46a,color:#000
    style SymlinkMode fill:#2a9d8f,color:#fff
    style CopyMode fill:#2a9d8f,color:#fff
```

### 5.2 VaultSync.classify_file()

```mermaid
flowchart TD
    CF(["classify_file(path, agent)"]) --> CheckDir{"In DIR.agent/<br/>directory?"}
    CheckDir -->|Yes| AgentDir["Return: AGENT_DIR (priority 3)"]
    CheckDir -->|No| CheckNested{"Filename matches<br/>*.agent.ext?"}
    CheckNested -->|Yes| InSubdir{"In subdirectory?"}
    InSubdir -->|Yes| AgentNested["Return: AGENT_NESTED (priority 2)"]
    InSubdir -->|No| AgentNamed["Return: AGENT_NAMED (priority 1)"]
    CheckNested -->|No| Universal["Return: UNIVERSAL (priority 0)"]

    style CF fill:#264653,color:#fff
    style AgentDir fill:#2d6a4f,color:#fff
    style AgentNested fill:#40916c,color:#fff
    style AgentNamed fill:#74c69d,color:#000
    style Universal fill:#b7e4c7,color:#000
```

### 5.3 VaultSync.discover_sync_targets()

```mermaid
flowchart TD
    DST(["discover_sync_targets(agent, repo)"]) --> Walk["_walk_vault(vault_repo_dir, repo_dir)"]
    Walk --> Recursive["Recursive: match vault dirs to repo dirs"]
    Recursive --> Collect["Collect SyncTarget per matching level"]
    Collect --> Classify["classify_file() for each vault file"]
    Classify --> Priority["Priority merge: higher priority wins"]
    Priority --> Output["List of SyncTarget objects"]

    style DST fill:#264653,color:#fff
```

### 5.4 VaultSync.sync_all()

```mermaid
flowchart TD
    SA([sync_all]) --> GetAgents["get_agents()"]
    GetAgents --> GetRepos["get_repos()"]
    GetRepos --> Domain1["Domain 1: In-Repo Sync"]

    Domain1 --> ForRepo["For each repo × agent:"]
    ForRepo --> Discover["discover_sync_targets()"]
    Discover --> ForTarget["For each SyncTarget:"]
    ForTarget --> CreateDir["mkdir -p agent_dir"]
    CreateDir --> ForFile["For each file in target:"]
    ForFile --> ModeCheck{Mode?}
    ModeCheck -->|symlink| Symlink["ln -s vault_file → agent_dir/file"]
    ModeCheck -->|copy| Copy["cp vault_file → agent_dir/file"]

    SA --> Domain2["Domain 2: System-Level"]
    Domain2 --> SysSync["Sync to ~/.github, ~/.claude"]

    style SA fill:#2a9d8f,color:#fff
```

### 5.5 VaultWatcher.run()

```mermaid
flowchart TD
    WM([VaultWatcher.run]) --> CheckFswatch{fswatch<br/>installed?}
    CheckFswatch -->|No| ErrExit["Exit: fswatch not found"]
    CheckFswatch -->|Yes| AcquireLock["Acquire PID lock"]
    AcquireLock --> InitSync["Initial sync_all()"]
    InitSync --> SpawnFswatch["Spawn single fswatch process"]

    SpawnFswatch --> EventLoop["Read stdout line-by-line"]
    EventLoop --> Debounce{"Within 0.5s<br/>of last sync?"}
    Debounce -->|Yes| Skip["Skip (debounce)"]
    Debounce -->|No| ReSync["sync_all()"]
    Skip --> EventLoop
    ReSync --> EventLoop

    style WM fill:#e9c46a,color:#000
```

---

## 6. install-launchd-service.sh — Function Scope

```mermaid
flowchart TD
    Entry([install-launchd-service.sh]) --> CleanOld["Remove old plist if exists"]
    CleanOld --> MkDir["mkdir ~/Library/LaunchAgents/"]
    MkDir --> WritePlist["Write .plist file:<br/>com.obscura.watcher.plist"]

    subgraph PlistConfig["Plist Configuration"]
        P1["Label: com.obscura.watcher"]
        P2["Program: python3 sync.py --watch"]
        P3["RunAtLoad: true"]
        P4["KeepAlive: true"]
        P5["Logs: /tmp/obscura-watcher.log"]
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

## 7. Git Hooks — Function Scopes

### 7.1 post-merge Hook

```mermaid
flowchart TD
    Hook([post-merge]) --> GetRoot["git rev-parse --show-toplevel"]
    GetRoot --> GetName["basename → repo name"]
    GetName --> CheckScript{"sync.py<br/>exists?"}
    CheckScript -->|No| Noop["Exit silently"]
    CheckScript -->|Yes| RunMerge["python3 sync.py --merge<br/>--repo {path}"]
    RunMerge --> Repaired["Symlinks repaired"]

    style Hook fill:#e76f51,color:#fff
```

### 7.2 post-commit Hook

```mermaid
flowchart TD
    Hook([post-commit]) --> CheckScript{"sync.py<br/>exists?"}
    CheckScript -->|No| Noop["Exit silently"]
    CheckScript -->|Yes| RunAsync["python3 sync.py --mode symlink &<br/>(background)"]

    style Hook fill:#e76f51,color:#fff
```

---

## 8. Sync Modes — State Machine

```mermaid
stateDiagram-v2
    [*] --> ParseArgs

    ParseArgs --> SymlinkMode: --mode symlink
    ParseArgs --> CopyMode: --mode copy
    ParseArgs --> WatchMode: --watch
    ParseArgs --> CleanMode: --clean
    ParseArgs --> MergeMode: --merge

    state SymlinkMode {
        [*] --> GetAgents_S
        GetAgents_S --> GetRepos_S
        GetRepos_S --> ForEachRepo_S
        ForEachRepo_S --> ForEachAgent_S
        ForEachAgent_S --> DiscoverTargets
        DiscoverTargets --> ForEachTarget
        ForEachTarget --> CreateSymlinks
        CreateSymlinks --> [*]
    }

    state CopyMode {
        [*] --> GetAgents_C
        GetAgents_C --> GetRepos_C
        GetRepos_C --> DiscoverTargets_C
        DiscoverTargets_C --> CopyFiles
        CopyFiles --> [*]
    }

    state WatchMode {
        [*] --> AcquireLock
        AcquireLock --> InitialSync
        InitialSync --> StartFswatch
        StartFswatch --> ListenEvents
        ListenEvents --> Debounce
        Debounce --> SyncAll: >0.5s since last
        Debounce --> ListenEvents: within 0.5s
        SyncAll --> ListenEvents
    }
```

---

## 9. Data Flow — End to End

```mermaid
sequenceDiagram
    participant User as Developer
    participant Vault as FV-Copilot Vault
    participant Sync as sync.py
    participant Index as repos/INDEX.md
    participant AgentIdx as agents/INDEX.md
    participant Repo as Target Repository
    participant Agent as AI Agent

    User->>Sync: python3 sync.py --mode symlink
    Sync->>Index: Read managed repos
    Index-->>Sync: [~/git/FV-Platform-Main, ...]
    Sync->>AgentIdx: Read registered agents
    AgentIdx-->>Sync: [copilot, claude]

    loop For each repo × agent
        Sync->>Vault: discover_sync_targets(agent, repo)
        Vault-->>Sync: List of SyncTarget (repo_path + files)
        loop For each SyncTarget
            Sync->>Repo: Create per-file symlinks in agent dir
        end
    end

    Note over Repo: Per-file symlinks now active

    User->>Vault: Edit skills/python.md
    Note over Repo: Change visible instantly via symlink
    Agent->>Repo: Read .github/skills/python.md
    Repo-->>Agent: Content from vault
```

---

## 10. Multi-Agent File Classification — Sequence

```mermaid
sequenceDiagram
    participant Caller
    participant DST as discover_sync_targets()
    participant CF as classify_file()
    participant FS as Filesystem

    Caller->>DST: discover_sync_targets("copilot", repo)

    Note over DST: Walk vault directory tree
    DST->>FS: Scan CONTENT_DIRS (skills/, instructions/, docs/)
    FS-->>DST: All files found

    loop For each file
        DST->>CF: classify_file(path, "copilot")

        alt In skills.copilot/ directory
            CF-->>DST: AGENT_DIR (priority 3)
        else Named *.copilot.md in subdir
            CF-->>DST: AGENT_NESTED (priority 2)
        else Named *.copilot.md at root
            CF-->>DST: AGENT_NAMED (priority 1)
        else No agent suffix
            CF-->>DST: UNIVERSAL (priority 0)
        end

        Note over DST: Priority merge: keep highest per dest path
    end

    DST->>FS: Walk repo mirror (repos/RepoName/)
    FS-->>DST: Matching directories at each level

    Note over DST: Build SyncTarget per matching level
    DST-->>Caller: List[SyncTarget]
```

---

## 11. Runtime Environment

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
| 5.1 | sync.py: Entry | Flowchart | CLI parsing, mode dispatch |
| 5.2 | classify_file() | Flowchart | File classification logic |
| 5.3 | discover_sync_targets() | Flowchart | Recursive target discovery |
| 5.4 | sync_all() | Flowchart | Full sync orchestration |
| 5.5 | VaultWatcher.run() | Flowchart | Fswatch event loop |
| 6 | install-launchd-service.sh | Flowchart | Plist install flow |
| 7.1 | post-merge hook | Flowchart | Auto-repair flow |
| 7.2 | post-commit hook | Flowchart | Auto-sync flow |
| 8 | Sync Modes | State Diagram | Mode state machine |
| 9 | Data Flow (E2E) | Sequence | Full sync sequence |
| 10 | File Classification | Sequence | classify_file detail |
| 11 | Runtime Environment | Graph | pyenv, nvm, tools |
