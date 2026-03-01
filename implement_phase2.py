#!/usr/bin/env python3
"""
Implement Phase 2: System-Wide Message History Integration

This script modifies:
1. ObscuraClient.run_loop() - add load_session_history parameter
2. AgentLoop.run() - accept initial_messages
3. AgentLoop._run_inner() - handle initial_messages
"""

import re

def modify_obscura_client():
    """Modify ObscuraClient.run_loop() to load and inject session history."""
    
    with open('obscura/core/client/__init__.py', 'r') as f:
        content = f.read()
    
    # Check if already modified
    if 'load_session_history' in content:
        print("✓ ObscuraClient already has load_session_history parameter")
        return
    
    # Find the run_loop method
    pattern = r'(    def run_loop\(\s+self,\s+prompt: str,\s+\*,.*?)(event_store: EventStoreProtocol \| None = None,)'
    
    # Add load_session_history parameter
    modified = re.sub(
        pattern,
        r'\1\2\n        load_session_history: bool = True,',
        content,
        flags=re.DOTALL
    )
    
    # Add session history loading logic before AgentLoop creation
    # Find the line with "from obscura.core.agent_loop import AgentLoop"
    pattern2 = r'(from obscura\.core\.agent_loop import AgentLoop)\n'
    replacement2 = r'\1\n\n        # Load session history if enabled\n        initial_messages = None\n        if load_session_history and session_id:\n            try:\n                from obscura.core.context import load_session_messages\n                from obscura.core.paths import resolve_obscura_home\n                db_path = resolve_obscura_home() / "events.db"\n                initial_messages = load_session_messages(session_id, db_path, max_turns=5)\n                if initial_messages:\n                    _logger.debug(f"Loaded {len(initial_messages)} messages from session {session_id}")\n            except Exception as e:\n                _logger.warning(f"Could not load session history: {e}")\n\n'
    
    modified = re.sub(pattern2, replacement2, modified)
    
    # Add initial_messages to loop.run() call
    pattern3 = r'(return loop\.run\(prompt, session_id=session_id,)'
    replacement3 = r'return loop.run(prompt, session_id=session_id, initial_messages=initial_messages,'
    
    modified = re.sub(pattern3, replacement3, modified)
    
    with open('obscura/core/client/__init__.py', 'w') as f:
        f.write(modified)
    
    print("✓ Modified ObscuraClient.run_loop()")

def modify_agent_loop():
    """Modify AgentLoop to accept and use initial_messages."""
    
    with open('obscura/core/agent_loop.py', 'r') as f:
        content = f.read()
    
    # Check if already modified
    if 'initial_messages: list[Message] | None = None' in content:
        print("✓ AgentLoop already accepts initial_messages")
        return
    
    # Add initial_messages parameter to run() method
    pattern1 = r'(async def run\(\s+self,\s+prompt: str,\s+\*,\s+session_id: str \| None = None,)'
    replacement1 = r'\1\n        initial_messages: list[Message] | None = None,'
    
    modified = re.sub(pattern1, replacement1, content)
    
    # Pass initial_messages to _run_inner
    pattern2 = r'(async for event in self\._run_inner\(prompt, sid, 0, "", kwargs)\):'
    replacement2 = r'async for event in self._run_inner(prompt, sid, 0, "", kwargs, initial_messages):'
    
    modified = re.sub(pattern2, replacement2, modified)
    
    # Add initial_messages parameter to _run_inner
    pattern3 = r'(async def _run_inner\(\s+self,\s+prompt: str,\s+session_id: str \| None,\s+turn_num: int,\s+accumulated_text: str,\s+kwargs: dict\[str, Any\],)'
    replacement3 = r'\1\n        initial_messages: list[Message] | None = None,'
    
    modified = re.sub(pattern3, replacement3, modified)
    
    with open('obscura/core/agent_loop.py', 'w') as f:
        f.write(modified)
    
    print("✓ Modified AgentLoop.run() and _run_inner()")

if __name__ == '__main__':
    print("Starting Phase 2 implementation...\n")
    
    try:
        modify_obscura_client()
        modify_agent_loop()
        
        print("\n✅ Phase 2 implementation complete!")
        print("\nChanges made:")
        print("1. ObscuraClient.run_loop() - added load_session_history parameter")
        print("2. ObscuraClient.run_loop() - loads session messages from events.db")
        print("3. AgentLoop.run() - accepts initial_messages parameter")
        print("4. AgentLoop._run_inner() - receives initial_messages")
        
        print("\n⚠️  Manual step required:")
        print("In AgentLoop._run_inner(), you need to:")
        print("- Find where messages are built (Message list)")
        print("- Prepend initial_messages if provided")
        print("- Look for: messages = [] or similar")
        print("- Change to: messages = list(initial_messages) if initial_messages else []")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
