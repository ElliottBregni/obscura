path = '/Users/elliottbregni/dev/obscura-main/obscura/core/client/__init__.py'

with open(path, 'r') as f:
    content = f.read()

old_text = (
    "        return self._circuit_registry\n"
    "\n"
    "    def _enrich_prompt(self, prompt: str) -> str:"
)

new_text = (
    "        return self._circuit_registry\n"
    "\n"
    "    # -- Context window / token awareness ------------------------------------\n"
    "\n"
    "    @property\n"
    "    def context_window(self) -> int:\n"
    '        """Return context window size (tokens) for the active backend + model.\n'
    "\n"
    "        Provider-specific limits per backend (tokens):\n"
    "            claude   -> 200,000  (all current models)\n"
    "            openai   -> 128,000  (gpt-4 family); 16,385 for gpt-3.5-turbo\n"
    "            copilot  -> 128,000\n"
    "            codex    -> 128,000\n"
    "            *        -> 100,000  (safe unknown fallback)\n"
    '        """\n'
    "        _PROVIDER_DEFAULTS: dict[str, int] = {\n"
    '            "claude": 200_000,\n'
    '            "openai": 128_000,\n'
    '            "copilot": 128_000,\n'
    '            "codex": 128_000,\n'
    "        }\n"
    "        provider = self._backend_type.value\n"
    '        model_id = self._model or ""\n'
    "\n"
    "        # OpenAI gpt-3.5-turbo has a smaller window than the gpt-4 family\n"
    '        if provider == "openai" and "3.5" in model_id:\n'
    "            return 16_385\n"
    "\n"
    "        return _PROVIDER_DEFAULTS.get(provider, 100_000)\n"
    "\n"
    "    @property\n"
    "    def context_compact_threshold(self) -> int:\n"
    '        """Token count at which auto-compaction triggers (70% of context window)."""\n'
    "        return int(self.context_window * 0.70)\n"
    "\n"
    "    @property\n"
    "    def context_warn_threshold(self) -> int:\n"
    '        """Token count at which a soft warning is emitted (50% of context window)."""\n'
    "        return int(self.context_window * 0.50)\n"
    "\n"
    "    def _enrich_prompt(self, prompt: str) -> str:"
)

if old_text not in content:
    print('ERROR: target text not found in file')
    exit(1)

count = content.count(old_text)
if count > 1:
    print(f'ERROR: target text found {count} times, expected exactly 1')
    exit(1)

new_content = content.replace(old_text, new_text, 1)

with open(path, 'w') as f:
    f.write(new_content)

print('Replacement done successfully')
