# 🔐 Auth Token Guide

Obscura uses **JWT tokens** for authentication. The server validates tokens against a Zitadel identity provider.

---

## Option 1: Disable Auth (Development) ⭐ RECOMMENDED FOR LOCAL DEV

Just disable auth entirely:

```bash
export OBSCURA_AUTH_ENABLED=false
obscura serve
```

No tokens needed! All endpoints work without Authorization headers.

---

## Option 2: Generate a Test Token (Quick Testing)

Create a self-signed JWT for testing:

```bash
# Install dependencies
pip install python-jose[cryptography]

# Generate a test token
python3 << 'EOF'
from jose import jwt
from datetime import datetime, timedelta

# Create a test token with admin role
payload = {
    "sub": "test-user-123",
    "email": "test@example.com",
    "iss": "http://localhost:8080",
    "aud": "obscura-sdk",
    "iat": datetime.utcnow(),
    "exp": datetime.utcnow() + timedelta(hours=24),
    "urn:zitadel:iam:org:project:roles": {
        "admin": {"org1": "example.com"},
        "agent:copilot": {"org1": "example.com"},
        "agent:claude": {"org1": "example.com"}
    }
}

# Generate (you'd need the private key - see below for full script)
print("Use the generate_test_token.py script below")
EOF
```

Save this as `scripts/generate_test_token.py`:

```python
#!/usr/bin/env python3
"""Generate a test JWT token for Obscura."""

import json
from datetime import datetime, timedelta
from jose import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


def generate_test_token():
    """Generate a test JWT with admin privileges."""
    
    # Generate RSA key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    
    # Get private key in PEM format
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode()
    
    # Get public key in JWK format for JWKS
    public_key = private_key.public_key()
    public_numbers = public_key.public_numbers()
    
    # Create JWT payload
    now = datetime.utcnow()
    payload = {
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
            "sessions:manage": {"local": "obscura.local"}
        }
    }
    
    # Sign the token
    token = jwt.encode(payload, private_pem, algorithm="RS256")
    
    # Create JWKS
    jwks = {
        "keys": [{
            "kty": "RSA",
            "kid": "test-key-1",
            "use": "sig",
            "alg": "RS256",
            "n": _base64url_encode(public_numbers.n.to_bytes(256, 'big')),
            "e": _base64url_encode(public_numbers.e.to_bytes(3, 'big'))
        }]
    }
    
    print("=" * 60)
    print("TEST JWT TOKEN")
    print("=" * 60)
    print(token)
    print("\n" + "=" * 60)
    print("USAGE")
    print("=" * 60)
    print(f'export OBSCURA_TOKEN="{token}"')
    print(f'curl -H "Authorization: Bearer {token[:40]}..." http://localhost:8080/api/v1/agents')
    print("\n" + "=" * 60)
    print("JWKS ENDPOINT (save to .well-known/jwks.json)")
    print("=" * 60)
    print(json.dumps(jwks, indent=2))
    
    return token, jwks


def _base64url_encode(data: bytes) -> str:
    """Base64URL encode without padding."""
    import base64
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


if __name__ == "__main__":
    generate_test_token()
```

Run it:
```bash
python scripts/generate_test_token.py
```

---

## Option 3: Use Zitadel (Production)

### Step 1: Start Zitadel

Using Docker:
```bash
docker run -it --rm \
  -p 8080:8080 \
  -e ZITADEL_MASTERKEY=TestMasterKey \
  ghcr.io/zitadel/zitadel:latest \
  start-from-init --masterkeyFromEnv
```

### Step 2: Configure Obscura

```bash
export OBSCURA_AUTH_ENABLED=true
export OBSCURA_AUTH_ISSUER=http://localhost:8080
export OBSCURA_AUTH_AUDIENCE=obscura-sdk
obscura serve
```

### Step 3: Get Token from Zitadel

1. Go to Zitadel console: http://localhost:8080/ui/console
2. Create a service account
3. Generate a JWT profile key
4. Use the key to get a token:

```bash
# Get token using client credentials
curl -X POST http://localhost:8080/oauth/v2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "scope=openid profile email urn:zitadel:iam:org:project:roles"
```

Response:
```json
{
  "access_token": "eyJhbGciOiJSUzI1Ni...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

### Step 4: Use the Token

```bash
export OBSCURA_TOKEN="eyJhbGciOiJSUzI1Ni..."
curl -H "Authorization: Bearer $OBSCURA_TOKEN" http://localhost:8080/api/v1/agents
```

---

## Option 4: API Key (✅ IMPLEMENTED)

Obscura now supports simple API key authentication via the `X-API-Key` header.

### Using API Keys

```bash
# Use the default dev key
curl -H "X-API-Key: obscura-dev-key-123" http://localhost:8080/api/v1/agents

# Or generate a custom key
python scripts/generate_api_key.py my-app admin agent:copilot
# Set the OBSCURA_API_KEYS env var as shown
```

### How It Works

1. A default dev key is included: `obscura-dev-key-123`
2. Custom keys can be set via `OBSCURA_API_KEYS` env var
3. API keys work even when `OBSCURA_AUTH_ENABLED=true`
4. Keys are checked before JWT validation

### Generate Custom API Keys

```bash
# Generate a key with specific roles
python scripts/generate_api_key.py my-cli-tool admin agent:copilot

# Output:
# Key: obscura_OiKZwXD9lupHoj1RJvOFFdV6Ryxh9Ly8FmvuaZa0xQg
# export OBSCURA_API_KEYS="obscura_OiKZwXD9...:my-cli-tool:admin,agent:copilot"
```

### Environment Variable Format

```bash
# Single key
export OBSCURA_API_KEYS="key1:user1:role1,role2"

# Multiple keys (semicolon-separated)
export OBSCURA_API_KEYS="key1:user1:admin;key2:user2:agent:read;key3:user3:agent:copilot"
```

Format: `api_key:user_id:role1,role2,role3`

Available roles:
- `admin` - Full access
- `agent:copilot` - Use Copilot backend
- `agent:claude` - Use Claude backend  
- `agent:read` - Read-only agent access
- `sync:write` - Trigger sync operations
- `sessions:manage` - Create/delete sessions

---

## Quick Reference

| Method | Use Case | Complexity |
|--------|----------|------------|
| Disable auth | Local dev | ⭐ Easy |
| Test token | Testing | ⭐⭐ Medium |
| Zitadel | Production | ⭐⭐⭐ Hard |
| API key | Simple auth | ⭐⭐ Medium |

---

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `OBSCURA_AUTH_ENABLED` | Enable/disable auth | `false` for dev |
| `OBSCURA_AUTH_ISSUER` | JWT issuer URL | `http://zitadel:8080` |
| `OBSCURA_AUTH_AUDIENCE` | JWT audience | `obscura-sdk` |
| `OBSCURA_TOKEN` | Token for CLI | `eyJhbG...` |
| `OBSCURA_API_KEYS` | Custom API keys | `key1:user1:admin;key2:user2:read` |

---

## Recommended Setup

**For Development:**
```bash
export OBSCURA_AUTH_ENABLED=false
obscura serve
```

**For Staging/Testing:**
```bash
# Generate test token
python scripts/generate_test_token.py
export OBSCURA_AUTH_ENABLED=true
export OBSCURA_AUTH_ISSUER=http://localhost:8080
export OBSCURA_TOKEN="<generated_token>"
obscura serve
```

**For Production:**
```bash
export OBSCURA_AUTH_ENABLED=true
export OBSCURA_AUTH_ISSUER=https://your-zitadel.com
export OBSCURA_AUTH_AUDIENCE=obscura-production
obscura serve
```
