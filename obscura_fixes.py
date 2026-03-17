import sys

results = []

# Fix 2a - obscura/core/tools.py
try:
    path = "/Users/elliottbregni/dev/obscura-main/obscura/core/tools.py"
    old = "        # TODO: restrict public tier once tier differentiation is enabled\n        return self.all()"
    new = '        if tier_value == "privileged":\n            return self.all()\n        return [t for t in self._tools.values() if getattr(t, "required_tier", None) in (None, "public")]'
    with open(path, "r") as f:
        content = f.read()
    if old not in content:
        results.append(f"Fix 2a FAILED: target string not found in {path}")
    else:
        new_content = content.replace(old, new, 1)
        with open(path, "w") as f:
            f.write(new_content)
        results.append("Fix 2a OK")
except Exception as e:
    results.append(f"Fix 2a FAILED: {e}")

# Fix 2b - obscura/core/agent_loop.py
try:
    path = "/Users/elliottbregni/dev/obscura-main/obscura/core/agent_loop.py"
    old = (
        "                    # TODO: enforce tier restriction once tier differentiation is enabled\n"
        "                    # token_tier = self._capability_token.tier.value\n"
        "                    # if (\n"
        '                    #     spec.required_tier == "privileged"\n'
        '                    #     and token_tier != "privileged"\n'
        "                    # ):\n"
        '                    #     _audit_tool_denied(tc.name, "insufficient_tier")\n'
        "                    #     results.append(\n"
        "                    #         (\n"
        "                    #             tc,\n"
        "                    #             f\"Tool '{tc.name}' requires privileged tier.\",\n"
        "                    #             True,\n"
        "                    #         )\n"
        "                    #     )\n"
        "                    #     continue"
    )
    new = (
        "                    token_tier = self._capability_token.tier.value\n"
        "                    if (\n"
        '                        spec.required_tier == "privileged"\n'
        '                        and token_tier != "privileged"\n'
        "                    ):\n"
        '                        _audit_tool_denied(tc.name, "insufficient_tier")\n'
        "                        results.append(\n"
        "                            (\n"
        "                                tc,\n"
        "                                f\"Tool '{tc.name}' requires privileged tier.\",\n"
        "                                True,\n"
        "                            )\n"
        "                        )\n"
        "                        continue"
    )
    with open(path, "r") as f:
        content = f.read()
    if old not in content:
        results.append(f"Fix 2b FAILED: target string not found in {path}")
    else:
        new_content = content.replace(old, new, 1)
        with open(path, "w") as f:
            f.write(new_content)
        results.append("Fix 2b OK")
except Exception as e:
    results.append(f"Fix 2b FAILED: {e}")

# Fix 5 - obscura/vector_memory/vector_memory.py
try:
    path = "/Users/elliottbregni/dev/obscura-main/obscura/vector_memory/vector_memory.py"
    old_simple_embed = (
        "# Simple embedding function (in production, use OpenAI, sentence-transformers, etc.)\n"
        "def simple_embedding(text: str, dim: int = 384) -> list[float]:\n"
        '    """Create a simple hash-based embedding for demo purposes.\n'
        "\n"
        "    In production, replace with:\n"
        "    - OpenAI text-embedding-3-small\n"
        "    - sentence-transformers/all-MiniLM-L6-v2\n"
        "    - Custom embedding model\n"
        '    """\n'
        '    # Hash the text to get deterministic "embedding"\n'
        "    hash_bytes = hashlib.sha256(text.encode()).digest()\n"
        "\n"
        "    # Convert to float array\n"
        "    floats: list[float] = []\n"
        "    for i in range(0, len(hash_bytes), 4):\n"
        "        chunk = hash_bytes[i : i + 4]\n"
        '        val = int.from_bytes(chunk, "little", signed=True)\n'
        "        floats.append(val / 2**31)  # Normalize to [-1, 1]\n"
        "\n"
        "    # Pad or truncate to desired dimension\n"
        "    if len(floats) < dim:\n"
        "        floats = floats * (dim // len(floats) + 1)\n"
        "\n"
        "    return floats[:dim]"
    )
    new_simple_embed = (
        "def simple_embedding(text: str, dim: int = 384) -> list[float]:\n"
        "    \"\"\"Deterministic hash-based embedding \u2014 used as fallback only.\n"
        "\n"
        "    Not semantically meaningful. For real semantic search, set an\n"
        "    embedding_fn when constructing VectorMemoryStore, or ensure\n"
        "    sentence-transformers is installed (auto-detected below).\n"
        '    """\n'
        "    hash_bytes = hashlib.sha256(text.encode()).digest()\n"
        "    floats: list[float] = []\n"
        "    for i in range(0, len(hash_bytes), 4):\n"
        "        chunk = hash_bytes[i : i + 4]\n"
        '        val = int.from_bytes(chunk, "little", signed=True)\n'
        "        floats.append(val / 2**31)\n"
        "    if len(floats) < dim:\n"
        "        floats = floats * (dim // len(floats) + 1)\n"
        "    return floats[:dim]\n"
        "\n"
        "\n"
        "def _make_default_embedding_fn(dim: int = 384):\n"
        '    """Return the best available embedding function.\n'
        "\n"
        "    Priority:\n"
        "    1. sentence-transformers all-MiniLM-L6-v2 (local, no API key, real semantics)\n"
        "    2. simple_embedding (hash-based fallback, deterministic but not semantic)\n"
        '    """\n'
        "    try:\n"
        "        from sentence_transformers import SentenceTransformer  # type: ignore[import]\n"
        "        import logging as _logging\n"
        "        _log = _logging.getLogger(__name__)\n"
        '        _model = SentenceTransformer("all-MiniLM-L6-v2")\n'
        '        _log.info("vector_memory: using sentence-transformers/all-MiniLM-L6-v2 for embeddings")\n'
        "        def _st_embed(text: str) -> list[float]:\n"
        "            return _model.encode(text, normalize_embeddings=True).tolist()\n"
        "        return _st_embed\n"
        "    except ImportError:\n"
        "        import logging as _logging\n"
        "        _logging.getLogger(__name__).warning(\n"
        '            "vector_memory: sentence-transformers not installed, "\n'
        '            "falling back to hash-based embedding (not semantic). "\n'
        '            "Install with: pip install sentence-transformers"\n'
        "        )\n"
        "        return simple_embedding"
    )
    with open(path, "r") as f:
        content = f.read()
    if old_simple_embed not in content:
        results.append(f"Fix 5 (simple_embedding) FAILED: target string not found in {path}")
    else:
        content = content.replace(old_simple_embed, new_simple_embed, 1)
        old_init = "        self.embedding_fn = embedding_fn or simple_embedding"
        new_init = "        self.embedding_fn = embedding_fn or _make_default_embedding_fn()"
        if old_init not in content:
            results.append(f"Fix 5 (__init__) FAILED: target string not found in {path}")
        else:
            content = content.replace(old_init, new_init, 1)
            with open(path, "w") as f:
                f.write(content)
            results.append("Fix 5 OK")
except Exception as e:
    results.append(f"Fix 5 FAILED: {e}")

for r in results:
    print(r)
