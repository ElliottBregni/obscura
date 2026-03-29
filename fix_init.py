#!/usr/bin/env python3
"""One-shot fix: remove duplicate import block and dangling cast lines from __init__.py"""

f = 'obscura/tools/system/__init__.py'
lines = open(f).readlines()
print(f'Before: {len(lines)} lines')

# 0-indexed line ranges to drop:
#   31-35: duplicate `from obscura.tools.system.intelligence import (...)` block
#   2965-2968: dangling cast lines after the `]` closing static_specs
bad = set(range(31, 36)) | set(range(2965, 2969))
new = [line for i, line in enumerate(lines) if i not in bad]
print(f'After: {len(new)} lines ({len(lines) - len(new)} lines removed)')
open(f, 'w').writelines(new)
print('Done ✓')
