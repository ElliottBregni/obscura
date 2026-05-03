# Vulture whitelist for ~/dev/obscura-main
#
# Names referenced by frameworks, protocols, or duck-typing that vulture's
# static analysis can't see. Vulture loads this file as one of its scan paths
# (see [tool.vulture] in pyproject.toml), so any reference here counts as a
# "use" and silences the false-positive flag.
#
# Add new entries when triage confirms a finding is framework-driven, not
# real dead code. Group by reason so the next reviewer can re-evaluate.

# --- Context manager protocol (called by `with` / `async with`) ---
_.__enter__
_.__exit__
_.__aenter__
_.__aexit__
exc_val
exc_tb
exc_type

# --- Descriptor protocol ---
_.__get__
_.__set__
_.__delete__
objtype

# --- Pytest fixtures and hooks (autouse / pytest-collected) ---
# (For broad pytest patterns prefer ignore_decorators / ignore_names in
# pyproject.toml — vulture whitelist files only accept literal names.)
_.fixture
_.conftest
