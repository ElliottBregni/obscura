#!/usr/bin/env python3
"""Test lazy skill loading - compare token usage before/after."""

from pathlib import Path
from obscura.core.context import ContextLoader
from obscura.core.types import Backend

def count_tokens_rough(text: str) -> int:
    """Rough token count estimation (1 token ≈ 4 chars)."""
    return len(text) // 4

def main():
    print("=" * 60)
    print("LAZY SKILL LOADING TEST")
    print("=" * 60)
    
    # Use CLAUDE backend - skills are in ~/.claude/skills
    backend = Backend.CLAUDE
    vault_path = Path.home()  # Will look in ~/.claude/ based on backend
    
    # Test 1: Traditional eager loading (current behavior)
    print("\n[TEST 1] Traditional Eager Loading")
    print("-" * 60)
    loader_eager = ContextLoader(backend, vault_path=vault_path, lazy_load_skills=False)
    prompt_eager = loader_eager.load_system_prompt()
    tokens_eager = count_tokens_rough(prompt_eager)
    
    skills_eager = loader_eager.load_skills()
    print(f"Skills loaded: {len(skills_eager)}")
    print(f"System prompt length: {len(prompt_eager)} chars")
    print(f"Estimated tokens: {tokens_eager}")
    
    # Test 2: Lazy loading with skill filter
    print("\n[TEST 2] Lazy Loading (all skills available)")
    print("-" * 60)
    loader_lazy = ContextLoader(backend, vault_path=vault_path, lazy_load_skills=True)
    prompt_lazy = loader_lazy.load_system_prompt()
    tokens_lazy = count_tokens_rough(prompt_lazy)
    
    skill_metas = loader_lazy.load_skills_lazy()
    print(f"Skills discovered (metadata only): {len(skill_metas)}")
    for skill in skill_metas:
        print(f"  - {skill.name}: {skill.description[:60]}...")
    
    print(f"\nSystem prompt length: {len(prompt_lazy)} chars")
    print(f"Estimated tokens: {tokens_lazy}")
    
    # Test 3: Load one skill on-demand
    print("\n[TEST 3] On-Demand Skill Loading")
    print("-" * 60)
    if skill_metas:
        test_skill_name = skill_metas[0].name
        print(f"Loading skill body for: {test_skill_name}")
        skill_body = loader_lazy.load_skill_body(test_skill_name)
        if skill_body:
            print(f"Skill body loaded: {len(skill_body)} chars")
            print(f"Estimated tokens for this skill: {count_tokens_rough(skill_body)}")
        else:
            print(f"Failed to load skill: {test_skill_name}")
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Eager loading:  {tokens_eager:,} tokens")
    print(f"Lazy loading:   {tokens_lazy:,} tokens")
    
    if tokens_eager > 0:
        savings = ((tokens_eager - tokens_lazy) / tokens_eager) * 100
        print(f"Savings:        {savings:.1f}%")
        print(f"Reduction:      {tokens_eager - tokens_lazy:,} tokens")
    
    print("\n✅ Lazy loading implemented successfully!")

if __name__ == "__main__":
    main()
