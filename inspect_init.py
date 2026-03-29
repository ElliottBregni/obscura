f = '/Users/elliottbregni/dev/obscura-main/obscura/tools/system/__init__.py'
lines = open(f).readlines()
print(f'Total lines: {len(lines)}')
print('Lines 29-40 (1-indexed):')
for i, line in enumerate(lines[28:40], start=29):
    print(f'{i}: {repr(line)}')
print()
print('Lines 2960-2975 (1-indexed):')
for i, line in enumerate(lines[2959:2975], start=2960):
    print(f'{i}: {repr(line)}')
