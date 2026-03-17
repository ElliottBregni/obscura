#!/usr/bin/env python3
"""Fix intelligence.py: correct @tool decorator args and ToolPolicy construction."""

content = open('obscura/tools/system/intelligence.py').read()
fixes = 0

# Fix 1: params= → parameters= in @tool() decorator calls
if '    params={\n' in content:
    content = content.replace('    params={\n', '    parameters={\n')
    fixes += 1
    print("Fix 1: params → parameters")

# Fix 2: inline_override ToolPolicy missing name + wrong types
old2 = '''        try:
            policy = ToolPolicy(
                allow_list=list(policy_override.get("allow_list") or []) or None,
                deny_list=list(policy_override.get("deny_list") or []) or None,
                base_dir=policy_override.get("base_dir"),
                full_access=bool(policy_override.get("full_access", False)),
            )
            policy_source = "inline_override"'''
new2 = '''        try:
            raw_allow = policy_override.get("allow_list") or []
            raw_deny = policy_override.get("deny_list") or []
            raw_base = policy_override.get("base_dir")
            policy = ToolPolicy(
                name="inline_override",
                allow_list=frozenset(raw_allow) if raw_allow else frozenset(),
                deny_list=frozenset(raw_deny) if raw_deny else frozenset(),
                base_dir=Path(raw_base) if raw_base else None,
                full_access=bool(policy_override.get("full_access", False)),
            )
            policy_source = "inline_override"'''
if old2 in content:
    content = content.replace(old2, new2, 1)
    fixes += 1
    print("Fix 2: inline_override ToolPolicy")

# Fix 3: DB ToolPolicy missing name + wrong types
old3 = '''                            policy = ToolPolicy(
                                allow_list=data.get("allow_list"),
                                deny_list=data.get("deny_list"),
                                base_dir=data.get("base_dir"),
                                full_access=bool(data.get("full_access", False)),
                            )
                            policy_source = f"db:session={session_id}"'''
new3 = '''                            raw_allow2 = data.get("allow_list") or []
                            raw_deny2 = data.get("deny_list") or []
                            raw_base2 = data.get("base_dir")
                            policy = ToolPolicy(
                                name=f"db:{session_id}",
                                allow_list=frozenset(raw_allow2) if raw_allow2 else frozenset(),
                                deny_list=frozenset(raw_deny2) if raw_deny2 else frozenset(),
                                base_dir=Path(raw_base2) if raw_base2 else None,
                                full_access=bool(data.get("full_access", False)),
                            )
                            policy_source = f"db:session={session_id}"'''
if old3 in content:
    content = content.replace(old3, new3, 1)
    fixes += 1
    print("Fix 3: DB ToolPolicy")

# Fix 4: default ToolPolicy missing name
old4 = '        policy = ToolPolicy(full_access=True)\n        policy_source = "default_permissive"'
new4 = '        policy = ToolPolicy(name="default_permissive", full_access=True)\n        policy_source = "default_permissive"'
if old4 in content:
    content = content.replace(old4, new4, 1)
    fixes += 1
    print("Fix 4: default ToolPolicy name")

# Fix 5: evaluate_policy needs dict not str
old5 = '    result = evaluate_policy(policy, tool_name, path_arg)'
new5 = '    eval_args = {"path": path_arg} if path_arg else None\n    result = evaluate_policy(policy, tool_name, eval_args)'
if old5 in content:
    content = content.replace(old5, new5, 1)
    fixes += 1
    print("Fix 5: evaluate_policy dict arg")

open('obscura/tools/system/intelligence.py', 'w').write(content)
print(f"\nApplied {fixes}/5 fixes. Done!")
