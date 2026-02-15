"""
examples/swarm_code_review.py — Multi-agent code review workflow.

Demonstrates 3 agents working together:
1. CodeAnalyzer — analyzes the PR and extracts key changes
2. TestGenerator — generates tests for the changed code  
3. DocWriter — updates documentation based on changes

Agents communicate via shared memory and message passing.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from sdk.agents import AgentRuntime
from sdk.auth.models import AuthenticatedUser
from sdk.memory import MemoryStore


@dataclass
class PRContext:
    """Context for a code review task."""
    pr_number: int
    repo: str
    branch: str
    changed_files: list[str]
    diff: str


class CodeReviewSwarm:
    """
    Orchestrates a multi-agent code review workflow.
    
    Flow:
    1. Analyzer extracts key changes → writes to memory
    2. TestGenerator reads analysis → generates tests
    3. DocWriter reads analysis → updates docs
    4. All complete → Reviewer aggregates feedback
    """
    
    def __init__(self, user: AuthenticatedUser):
        self.user = user
        self.runtime = AgentRuntime(user)
        self.memory = MemoryStore.for_user(user)
        self.results: dict[str, Any] = {}
    
    async def run(self, pr: PRContext) -> dict[str, Any]:
        """Execute the full review workflow."""
        await self.runtime.start()
        
        try:
            # Store PR context in shared memory
            self.memory.set(
                f"pr_{pr.pr_number}",
                {
                    "pr_number": pr.pr_number,
                    "repo": pr.repo,
                    "branch": pr.branch,
                    "changed_files": pr.changed_files,
                    "diff": pr.diff[:5000],  # Truncate for memory
                },
                namespace="swarm:code_review"
            )
            
            # Spawn all agents
            analyzer = self.runtime.spawn(
                name="code-analyzer",
                model="claude",
                system_prompt="""You are a code analysis expert. 
Analyze the provided PR diff and extract:
1. Key changes and their impact
2. Potential risks or concerns
3. Files that need the most attention

Write your analysis to memory namespace 'swarm:code_review' with key 'analysis_{pr_number}'.
Be concise but thorough.""",
                memory_namespace="swarm:code_review"
            )
            
            test_gen = self.runtime.spawn(
                name="test-generator",
                model="claude",
                system_prompt="""You are a test engineering expert.
Generate comprehensive test cases for the code changes in the PR.
Focus on:
- Unit tests for new functions
- Integration tests for changed APIs
- Edge cases and error conditions

Read the analysis from memory first, then write your test plan to 'tests_{pr_number}'.""",
                memory_namespace="swarm:code_review"
            )
            
            doc_writer = self.runtime.spawn(
                name="doc-writer",
                model="claude",
                system_prompt="""You are a technical documentation expert.
Update documentation based on the code changes:
- API changes → update endpoint docs
- New features → add usage examples
- Breaking changes → add migration notes

Read the analysis from memory first, then write doc updates to 'docs_{pr_number}'.""",
                memory_namespace="swarm:code_review"
            )
            
            # Start all agents
            await asyncio.gather(
                analyzer.start(),
                test_gen.start(),
                doc_writer.start()
            )
            
            # Phase 1: Analyzer runs first (others wait)
            print(f"🔍 Analyzer starting on PR #{pr.pr_number}...")
            await analyzer.run(
                f"Analyze this PR diff and write findings to memory:\n\n{pr.diff[:3000]}"
            )
            print(f"✅ Analyzer complete")
            
            # Signal other agents that analysis is ready
            await analyzer.send_message("broadcast", "analysis_complete")
            
            # Phase 2: TestGenerator and DocWriter run in parallel
            print(f"🧪 TestGenerator and 📝 DocWriter starting...")
            
            async def run_test_gen():
                # Wait for analysis signal
                async for msg in test_gen.receive_messages():
                    if msg.content == "analysis_complete":
                        break
                
                await test_gen.run(
                    f"Generate tests for PR #{pr.pr_number}. "
                    f"Read analysis from memory key 'analysis_{pr.pr_number}' first."
                )
                return "test_gen", test_gen.get_state()
            
            async def run_doc_writer():
                # Wait for analysis signal
                async for msg in doc_writer.receive_messages():
                    if msg.content == "analysis_complete":
                        break
                
                await doc_writer.run(
                    f"Update docs for PR #{pr.pr_number}. "
                    f"Read analysis from memory key 'analysis_{pr.pr_number}' first."
                )
                return "doc_writer", doc_writer.get_state()
            
            # Run in parallel
            await asyncio.gather(
                run_test_gen(),
                run_doc_writer()
            )
            
            print(f"✅ TestGenerator complete")
            print(f"✅ DocWriter complete")
            
            # Phase 3: Aggregator compiles final review
            aggregator = self.runtime.spawn(
                name="review-aggregator",
                model="claude",
                system_prompt="""You are a senior engineer doing final PR review.
Compile findings from analysis, tests, and docs into a cohesive review.
Provide:
1. Overall assessment (approve/request changes)
2. Key concerns
3. Action items

Read from memory: analysis, tests, docs.""",
                memory_namespace="swarm:code_review"
            )
            await aggregator.start()
            
            print(f"📊 Aggregator compiling final review...")
            final_review = await aggregator.run(
                f"Compile final review for PR #{pr.pr_number}. "
                f"Read analysis, tests, and docs from memory."
            )
            print(f"✅ Review complete")
            
            # Gather all results
            self.results = {
                "pr_number": pr.pr_number,
                "analyzer_status": analyzer.get_state().status.name,
                "testgen_status": test_gen.get_state().status.name,
                "docwriter_status": doc_writer.get_state().status.name,
                "analysis": self.memory.get(f"analysis_{pr.pr_number}", namespace="swarm:code_review"),
                "tests": self.memory.get(f"tests_{pr.pr_number}", namespace="swarm:code_review"),
                "docs": self.memory.get(f"docs_{pr.pr_number}", namespace="swarm:code_review"),
                "final_review": final_review,
            }
            
            return self.results
            
        finally:
            await self.runtime.stop()
    
    def get_memory_snapshot(self) -> dict[str, Any]:
        """Get all memory written during the swarm run."""
        keys = self.memory.list_keys(namespace="swarm:code_review")
        return {
            str(key): self.memory.get(key.key, namespace=key.namespace)
            for key in keys
        }


async def main():
    """Example usage of the CodeReviewSwarm."""
    # Mock user
    user = AuthenticatedUser(
        user_id="u-swarm-demo",
        email="demo@obscura.dev",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="demo",
    )
    
    # Mock PR
    pr = PRContext(
        pr_number=42,
        repo="obscura",
        branch="feature/memory-system",
        changed_files=["sdk/memory.py", "sdk/server.py", "tests/test_memory.py"],
        diff="""
diff --git a/sdk/memory.py b/sdk/memory.py
new file mode 100644
+class MemoryStore:
+    '''Multi-tenant memory storage for AI agents'''
+    def set(self, key, value, namespace="default"):
+        # Store in SQLite
+        ...
+
+    def get(self, key, namespace="default"):
+        # Retrieve from SQLite
+        ...
""",
    )
    
    # Run swarm
    swarm = CodeReviewSwarm(user)
    results = await swarm.run(pr)
    
    # Print results
    print("\n" + "="*60)
    print("SWARM RESULTS")
    print("="*60)
    print(json.dumps(results, indent=2, default=str))
    
    # Print memory snapshot
    print("\n" + "="*60)
    print("MEMORY SNAPSHOT")
    print("="*60)
    snapshot = swarm.get_memory_snapshot()
    print(json.dumps(snapshot, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
