"""Test stubs for scripts.sync"""
from dataclasses import dataclass

@dataclass
class VaultSync:
    @staticmethod
    def sync(*args, **kwargs):
        return None

class VariantSelector:
    def __init__(self, *args, **kwargs):
        pass

# provide a simple function expected by tests
def ensure_sync():
    return True
