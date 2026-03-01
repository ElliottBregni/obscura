path = "/Users/elliottbregni/dev/obscura-main/obscura/core/tool_policy.py"

with open(path, "r") as f:
    lines = f.readlines()

output = []
i = 0
while i < len(lines):
    # Detect the blank line that precedes the SECOND @classmethod subagent_only
    if (lines[i].strip() == "" and
        i + 1 < len(lines) and lines[i+1].strip() == "@classmethod" and
        i + 2 < len(lines) and "def subagent_only" in lines[i+2] and
        i + 3 < len(lines) and '"""Policy for sub-agents: run_shell only' in lines[i+3]):
        # Skip: blank, @classmethod, def, docstring lines through closing """, return
        j = i
        j += 1  # skip blank line
        j += 1  # skip @classmethod
        j += 1  # skip def line
        # skip docstring lines until closing """
        while j < len(lines) and '"""' not in lines[j]:
            j += 1
        j += 1  # skip closing """ line
        j += 1  # skip return line
        i = j
        continue
    # Fix stray paren in section comment
    if "# -- Backend application methods)" in lines[i]:
        lines[i] = lines[i].replace("# -- Backend application methods)", "# -- Backend application methods")
    output.append(lines[i])
    i += 1

with open(path, "w") as f:
    f.writelines(output)

print("Done. Lines written:", len(output))
