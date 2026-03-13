import json
import os
from typing import Dict, Any, List

STORAGE_PATH = os.path.join(os.path.dirname(__file__), "semantic_memory_store.json")

class SemanticMemoryStore:
    """Simple on-disk semantic memory store (local JSON)."""

    def __init__(self, path: str = STORAGE_PATH):
        self.path = path
        if not os.path.exists(self.path):
            with open(self.path, "w") as f:
                json.dump([], f)

    def _load(self) -> List[Dict[str, Any]]:
        with open(self.path, "r") as f:
            return json.load(f)

    def _save(self, data: List[Dict[str, Any]]):
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)

    def add(self, key: str, text: str, metadata: Dict[str, Any] = None):
        """Add a memory entry."""
        if metadata is None:
            metadata = {}
        entries = self._load()
        entry = {"key": key, "text": text, "metadata": metadata}
        entries.append(entry)
        self._save(entries)
        return entry

    def list_all(self) -> List[Dict[str, Any]]:
        """Return all memory entries."""
        return self._load()

    def find(self, query_substring: str) -> List[Dict[str, Any]]:
        """Naive substring search over stored texts and metadata values."""
        results = []
        for e in self._load():
            if query_substring.lower() in e.get("text", "").lower():
                results.append(e)
                continue
            # search metadata values
            for v in e.get("metadata", {}).values():
                if isinstance(v, str) and query_substring.lower() in v.lower():
                    results.append(e)
                    break
        return results


# CLI for quick use
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Local semantic memory store (simple)")
    sub = parser.add_subparsers(dest="cmd")
    add = sub.add_parser("add")
    add.add_argument("key")
    add.add_argument("text")
    listp = sub.add_parser("list")
    find = sub.add_parser("find")
    find.add_argument("query")

    args = parser.parse_args()
    store = SemanticMemoryStore()
    if args.cmd == "add":
        e = store.add(args.key, args.text)
        print("Added:", e)
    elif args.cmd == "list":
        for i, e in enumerate(store.list_all(), 1):
            print(i, e)
    elif args.cmd == "find":
        for i, e in enumerate(store.find(args.query), 1):
            print(i, e)
    else:
        parser.print_help()
