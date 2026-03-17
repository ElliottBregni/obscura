target = '/Users/elliottbregni/dev/obscura-main/obscura/tools/system/__init__.py'

with open(target, 'r') as f:
    content = f.read()

import_anchor = 'from obscura.core.types import ToolSpec\n'
import_insertion = (
    'from obscura.tools.system.intelligence import (\n'
    '    causal_trace,\n'
    '    context_snapshot,\n'
    '    policy_probe,\n'
    ')\n'
)

if import_anchor not in content:
    raise ValueError('Import anchor not found')

if import_insertion in content:
    print('Step 1: import block already present, skipping')
else:
    content = content.replace(import_anchor, import_anchor + import_insertion, 1)
    print('Step 1: import block inserted')

reg_anchor = '        # Append any dynamically created tools'
reg_insertion = (
    '        # Intelligence tools (context_snapshot, causal_trace, policy_probe)\n'
    '        cast(ToolSpec, getattr(cast(Any, context_snapshot), "spec")),\n'
    '        cast(ToolSpec, getattr(cast(Any, causal_trace), "spec")),\n'
    '        cast(ToolSpec, getattr(cast(Any, policy_probe), "spec")),\n'
)

if reg_anchor not in content:
    raise ValueError('Registration anchor not found')

if reg_insertion in content:
    print('Step 2: registration lines already present, skipping')
else:
    content = content.replace(reg_anchor, reg_insertion + reg_anchor, 1)
    print('Step 2: registration lines inserted')

with open(target, 'w') as f:
    f.write(content)

print('File written successfully.')
