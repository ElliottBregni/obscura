"""Test config env var handling."""

import os
from sdk.config import ObscuraConfig

def test_auth_enabled_env_var():
    """Test that OBSCURA_AUTH_ENABLED env var works."""
    # Set env var
    os.environ["OBSCURA_AUTH_ENABLED"] = "false"
    
    # Create config from env
    config = ObscuraConfig.from_env()
    
    # Verify auth is disabled
    assert config.auth_enabled == False, f"Expected auth_enabled=False but got {config.auth_enabled}"
    
    print("✓ Auth disabled via env var works")
    
    # Reset
    os.environ["OBSCURA_AUTH_ENABLED"] = "true"
    config2 = ObscuraConfig.from_env()
    assert config2.auth_enabled == True
    print("✓ Auth enabled via env var works")

if __name__ == "__main__":
    test_auth_enabled_env_var()
    print("\nAll config tests passed!")
