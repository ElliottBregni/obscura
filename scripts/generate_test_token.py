#!/usr/bin/env python3
"""Generate a test JWT token for Obscura.

Usage:
    python scripts/generate_test_token.py

This creates a self-signed JWT with admin privileges for testing.
"""

import json
import base64
from datetime import UTC, datetime, timedelta
from jose import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


def generate_test_token():
    """Generate a test JWT with admin privileges."""

    print("Generating test JWT token...")
    print()

    # Generate RSA key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )

    # Get private key in PEM format
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    # Get public key numbers for JWK
    public_key = private_key.public_key()
    public_numbers = public_key.public_numbers()

    # Create JWT payload with all required roles
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": "test-user-123",
        "email": "test@obscura.local",
        "iss": "http://localhost:8080",
        "aud": "obscura-sdk",
        "iat": now,
        "exp": now + timedelta(hours=24),
        "urn:zitadel:iam:org:project:roles": {
            "admin": {"local": "obscura.local"},
            "agent:copilot": {"local": "obscura.local"},
            "agent:claude": {"local": "obscura.local"},
            "agent:read": {"local": "obscura.local"},
            "sync:write": {"local": "obscura.local"},
            "sessions:manage": {"local": "obscura.local"},
        },
    }

    # Sign the token
    token = jwt.encode(payload, private_pem, algorithm="RS256")

    # Create JWKS for server validation
    def b64url_encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    n_bytes = public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")
    e_bytes = public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")

    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key-1",
                "use": "sig",
                "alg": "RS256",
                "n": b64url_encode(n_bytes),
                "e": b64url_encode(e_bytes),
            }
        ]
    }

    # Save files
    with open("test_private_key.pem", "w") as f:
        f.write(private_pem)

    with open("test_jwks.json", "w") as f:
        json.dump(jwks, f, indent=2)

    # Output results
    print("=" * 70)
    print("✅ TEST JWT TOKEN GENERATED")
    print("=" * 70)
    print()
    print("Token (24h validity):")
    print("-" * 70)
    print(token)
    print()
    print("=" * 70)
    print("USAGE")
    print("=" * 70)
    print()
    print("1. Set environment variable:")
    print(f'   export OBSCURA_TOKEN="{token}"')
    print()
    print("2. Use with curl:")
    print(f'   curl -H "Authorization: Bearer {token[:50]}..." \\\\')
    print("        http://localhost:8080/api/v1/agents")
    print()
    print("3. Or with Python:")
    print("   import os")
    print('   os.environ["OBSCURA_TOKEN"] = "' + token[:50] + '..."')
    print()
    print("=" * 70)
    print("FILES CREATED")
    print("=" * 70)
    print("- test_private_key.pem  (private key for signing)")
    print("- test_jwks.json        (JWKS for server validation)")
    print()
    print("Note: These are test files only. Do not use in production!")
    print()


if __name__ == "__main__":
    generate_test_token()
