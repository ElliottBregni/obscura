import re

files = [
    "/Users/bregnie/dev/obscura/.github/workflows/pyright.yml",
    "/Users/bregnie/dev/obscura/.github/workflows/ruff.yml",
    "/Users/bregnie/dev/obscura/.github/workflows/test.yml",
]

correct_on_block = """on:
  push:
    branches: [main]
    tags: ['v*']
  pull_request:
    branches: [main]
"""

for path in files:
    with open(path) as f:
        content = f.read()

    # Replace the entire on: block (from 'on:' up to 'jobs:')
    new_content = re.sub(
        r"^on:.*?(?=^jobs:)",
        correct_on_block + "\n",
        content,
        flags=re.MULTILINE | re.DOTALL,
    )

    with open(path, "w") as f:
        f.write(new_content)

    print(f"Fixed: {path}")
