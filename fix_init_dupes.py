f = '/Users/elliottbregni/dev/obscura-main/obscura/tools/system/__init__.py'
lines = open(f).readlines()
print(f'Before: {len(lines)} lines')
bad = set(range(31, 36)) | set(range(2965, 2969))
new = [line for i, line in enumerate(lines) if i not in bad]
print(f'After: {len(new)} lines')
open(f, 'w').writelines(new)
print('Done')
