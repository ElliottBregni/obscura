"""Backend method-level conformance evaluator."""

from __future__ import annotations

from typing import Any

from sdk.parity.models import BackendConformance, ContractCheckResult, MethodContract


def evaluate_backend_conformance(
    backend: Any,
    contracts: tuple[MethodContract, ...],
) -> BackendConformance:
    """Evaluate one backend implementation against method contracts."""
    capabilities = backend.capabilities()
    native_features = set(capabilities.native_features)
    # ``backend._backend`` doesn't exist consistently; infer from capabilities owner.
    # Every backend implementation here exposes a ``native`` property and capabilities().
    # Backend enum is available from ``backend.native.meta`` only after start, so use class mapping.
    from sdk.backends.claude import ClaudeBackend
    from sdk.backends.copilot import CopilotBackend
    from sdk.backends.localllm import LocalLLMBackend
    from sdk.backends.moonshot import MoonshotBackend
    from sdk.backends.openai_compat import OpenAIBackend
    from sdk.internal.types import Backend

    if isinstance(backend, MoonshotBackend):
        backend_enum = Backend.MOONSHOT
    elif isinstance(backend, OpenAIBackend):
        backend_enum = Backend.OPENAI
    elif isinstance(backend, ClaudeBackend):
        backend_enum = Backend.CLAUDE
    elif isinstance(backend, CopilotBackend):
        backend_enum = Backend.COPILOT
    elif isinstance(backend, LocalLLMBackend):
        backend_enum = Backend.LOCALLLM
    else:
        raise TypeError(f"Unsupported backend type for conformance: {type(backend)!r}")

    checks: list[ContractCheckResult] = []
    for contract in contracts:
        if backend_enum not in contract.applicable_backends:
            continue

        missing_methods: list[str] = []
        for name in contract.required_methods:
            attr = getattr(backend, name, None)
            if attr is None:
                missing_methods.append(name)
                continue
            # ``native`` is property-like on all backends, not callable.
            if name != "native" and not callable(attr):
                missing_methods.append(name)

        missing_caps = [
            cap_name
            for cap_name in contract.required_capabilities
            if not bool(getattr(capabilities, cap_name, False))
        ]
        missing_native = [
            feature
            for feature in contract.required_native_features
            if feature not in native_features
        ]

        passed = not (missing_methods or missing_caps or missing_native)
        checks.append(
            ContractCheckResult(
                backend=backend_enum,
                contract_id=contract.id,
                passed=passed,
                missing_methods=tuple(missing_methods),
                missing_capabilities=tuple(missing_caps),
                missing_native_features=tuple(missing_native),
            )
        )

    return BackendConformance(backend=backend_enum, checks=tuple(checks))
