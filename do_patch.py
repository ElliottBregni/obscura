path = '/Users/elliottbregni/dev/obscura-main/obscura/core/client/__init__.py'

with open(path, 'r') as f:
    lines = f.readlines()

# Lines are 1-indexed; line 634 is index 633
# We need to insert after line 633 (index 632: "        return self._circuit_registry\n")
# and before line 634 (index 633: "\n") and line 635 (index 634: "    def _enrich_prompt...")

# Find the insertion point
target_idx = None
for i, line in enumerate(lines):
    if line.strip() == 'return self._circuit_registry':
        # Check the next non-blank line is _enrich_prompt
        for j in range(i+1, min(i+5, len(lines))):
            if lines[j].strip() == '':
                continue
            if 'def _enrich_prompt' in lines[j]:
                target_idx = i
                break
        if target_idx is not None:
            break

if target_idx is None:
    print('ERROR: could not find insertion point')
    exit(1)

print(f'Found insertion point at line {target_idx + 1}')

# Build the block to insert between "return self._circuit_registry" and "def _enrich_prompt"
insert_lines = [
    '\n',
    '    # -- Context window / token awareness ------------------------------------\n',
    '\n',
    '    @property\n',
    '    def context_window(self) -> int:\n',
    '        """Return context window size (tokens) for the active backend + model.\n',
    '\n',
    '        Provider-specific limits per backend (tokens):\n',
    '            claude   -> 200,000  (all current models)\n',
    '            openai   -> 128,000  (gpt-4 family); 16,385 for gpt-3.5-turbo\n',
    '            copilot  -> 128,000\n',
    '            codex    -> 128,000\n',
    '            *        -> 100,000  (safe unknown fallback)\n',
    '        """\n',
    '        _PROVIDER_DEFAULTS: dict[str, int] = {\n',
    '            "claude": 200_000,\n',
    '            "openai": 128_000,\n',
    '            "copilot": 128_000,\n',
    '            "codex": 128_000,\n',
    '        }\n',
    '        provider = self._backend_type.value\n',
    '        model_id = self._model or ""\n',
    '\n',
    '        # OpenAI gpt-3.5-turbo has a smaller window than the gpt-4 family\n',
    '        if provider == "openai" and "3.5" in model_id:\n',
    '            return 16_385\n',
    '\n',
    '        return _PROVIDER_DEFAULTS.get(provider, 100_000)\n',
    '\n',
    '    @property\n',
    '    def context_compact_threshold(self) -> int:\n',
    '        """Token count at which auto-compaction triggers (70% of context window)."""\n',
    '        return int(self.context_window * 0.70)\n',
    '\n',
    '    @property\n',
    '    def context_warn_threshold(self) -> int:\n',
    '        """Token count at which a soft warning is emitted (50% of context window)."""\n',
    '        return int(self.context_window * 0.50)\n',
]

# Remove the existing blank line between return and def _enrich_prompt (it will be replaced)
# target_idx+1 is the blank line, target_idx+2 is def _enrich_prompt
new_lines = lines[:target_idx+1] + insert_lines + lines[target_idx+2:]

with open(path, 'w') as f:
    f.writelines(new_lines)

print('Done writing file')
