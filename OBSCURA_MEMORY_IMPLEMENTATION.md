# Obscura System-Wide Memory Integration

## Overview
Make session memory work everywhere an Obscura agent is spawned (CLI, HTTP API, AgentRuntime, etc.)

## Already Completed ✅
1. `load_obscura_memory()` in `obscura/core/context.py` - loads memory as system prompt context
2. CLI integration in `obscura/cli/__init__.py` - injects memory into CLI sessions

## Phase 2: Message History Integration (Option C)

This makes session memory work system-wide by loading actual conversation history as Messages.

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Entry Points                              │
│  CLI │ HTTP API │ AgentRuntime │ TUI │ A2A │ Direct Client  │
└─────────┬────────────────────────────────────────────────────┘
          │
          ▼
    ┌──────────────────┐
    │ ObscuraClient     │
    │  .run_loop()      │──┐
    └──────────────────┘  │
          │               │
          │   load_session_history=True?
          │               │
          ▼               ▼
    ┌──────────────────────────────┐
    │ load_session_messages()       │
    │ (from .obscura/events.db)     │
    └──────────────────────────────┘
          │
          ▼
    ┌──────────────────┐
    │ AgentLoop.run()   │
    │ initial_messages  │
    └──────────────────┘
          │
          ▼
    ┌──────────────────┐
    │ Backend.stream()  │
    │ (with history)    │
    └──────────────────┘
```

### Implementation Steps

#### 1. Add `load_session_messages()` to `obscura/core/context.py`

Add this function after `load_obscura_memory()`:

```python
def load_session_messages(session_id: str, db_path: Path, max_turns: int = 5) -> list:
    """Load session history as Message objects.
    
    Reconstructs conversation turns from .obscura/events.db for context continuity.
    
    Args:
        session_id: Session ID to load
        db_path: Path to events.db
        max_turns: Maximum conversation turns to load (default: 5, 10 messages)
    
    Returns:
        List of Message objects (USER/ASSISTANT pairs)
    """
    import sqlite3
    import json
    from obscura.core.types import Message, Role
    
    if not db_path.exists():
        return []
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get conversation-related events
        events = cursor.execute("""
            SELECT kind, payload, seq
            FROM events
            WHERE session_id = ?
            AND kind IN ('user_message', 'text_delta', 'turn_complete')
            ORDER BY seq ASC
        """, (session_id,)).fetchall()
        
        conn.close()
        
        if not events:
            return []
        
        # Reconstruct message pairs
        messages = []
        assistant_text_parts = []
        
        for kind, payload_json, _ in events:
            try:
                payload = json.loads(payload_json)
                
                if kind == 'user_message':
                    # Flush any pending assistant message
                    if assistant_text_parts:
                        full_text = ''.join(assistant_text_parts)
                        messages.append(Message(role=Role.ASSISTANT, content=full_text))
                        assistant_text_parts = []
                    
                    # Add user message
                    content = payload.get('content', '')
                    if content:
                        messages.append(Message(role=Role.USER, content=content))
                
                elif kind == 'text_delta':
                    # Accumulate assistant response
                    text = payload.get('text', '')
                    if text:
                        assistant_text_parts.append(text)
                
                elif kind == 'turn_complete':
                    # Finalize assistant message
                    if assistant_text_parts:
                        full_text = ''.join(assistant_text_parts)
                        messages.append(Message(role=Role.ASSISTANT, content=full_text))
                        assistant_text_parts = []
            
            except Exception:
                continue
        
        # Keep only recent turns (each turn = user + assistant)
        if len(messages) > max_turns * 2:
            messages = messages[-(max_turns * 2):]
        
        return messages
        
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to load session messages: {e}")
        return []
```

#### 2. Modify `ObscuraClient.run_loop()` in `obscura/core/client/__init__.py`

Find the `run_loop()` method (around line 356) and modify it:

```python
def run_loop(
    self,
    prompt: str,
    *,
    max_turns: int = 10,
    on_confirm: Callable[..., Any] | None = None,
    event_store: EventStoreProtocol | None = None,
    session_id: str | None = None,
    auto_complete: bool = True,
    load_session_history: bool = True,  # ← ADD THIS
    **kwargs: Any,
) -> AsyncIterator[AgentEvent]:
    """Run the agent loop, streaming events.
    
    Args:
        load_session_history: If True and session_id provided, loads previous
            conversation turns as initial context (default: True)
    """
    from obscura.core.agent_loop import AgentLoop
    
    # ↓ ADD THIS BLOCK
    # Load session history if enabled
    initial_messages = None
    if load_session_history and session_id:
        try:
            from obscura.core.context import load_session_messages
            from obscura.core.paths import resolve_obscura_home
            db_path = resolve_obscura_home() / "events.db"
            initial_messages = load_session_messages(session_id, db_path, max_turns=5)
            if initial_messages:
                _logger.debug(f"Loaded {len(initial_messages)} messages from session {session_id}")
        except Exception as e:
            _logger.warning(f"Could not load session history: {e}")
    # ↑ END NEW BLOCK
    
    loop = AgentLoop(
        self._backend,
        self._tool_registry,
        max_turns=max_turns,
        on_confirm=on_confirm,
        capability_token=self._capability_token,
        event_store=event_store,
        auto_complete=auto_complete,
    )
    
    # ↓ MODIFY THIS CALL
    return loop.run(
        prompt,
        session_id=session_id,
        initial_messages=initial_messages,  # ← ADD THIS PARAMETER
        **kwargs
    )
```

#### 3. Modify `AgentLoop` in `obscura/core/agent_loop.py`

**a) Update `run()` method signature:**

Find the `async def run()` method (around line 197):

```python
async def run(
    self,
    prompt: str,
    *,
    session_id: str | None = None,
    initial_messages: list[Message] | None = None,  # ← ADD THIS
    **kwargs: Any,
) -> AsyncIterator[AgentEvent]:
    """Run the agent loop.
    
    Args:
        prompt: User prompt
        session_id: Session ID for persistence
        initial_messages: Previous conversation history to include
    """
    # ... existing code ...
    
    # ↓ MODIFY THIS CALL
    async for event in self._run_inner(prompt, sid, 0, "", kwargs, initial_messages):
        yield event
```

**b) Update `_run_inner()` method:**

Find `_run_inner()` and add `initial_messages` parameter. Then modify where messages are built to include the history.

Look for where `Message(role=Role.USER, content=prompt)` is created and prepend initial_messages.

#### 4. Update HTTP API routes (if applicable)

In `obscura/routes/sessions.py` or chat endpoints:

```python
async def chat_handler(request: ChatRequest):
    async for event in client.run_loop(
        request.message,
        session_id=request.session_id,
        load_session_history=True,  # ← ENSURE THIS
        ...
    ):
        yield event
```

#### 5. Update AgentRuntime (if applicable)

In `obscura/agent/agents.py`:

```python
async for event in self.client.run_loop(
    message,
    session_id=session_id,
    load_session_history=True,  # ← ENSURE THIS
    **kwargs
):
    yield event
```

## Testing

### 1. CLI Test
```bash
# First session
obscura -b claude
> implement authentication
> add JWT support
> /quit

# Resume session (note the session ID from first run)
obscura -b claude --session <session-id>
> add refresh token support
# Should understand context: knows about JWT, authentication implementation
```

### 2. Programmatic Test
```python
from obscura.core.client import ObscuraClient
from obscura.core.paths import resolve_obscura_home

async def test_memory():
    async with ObscuraClient("claude") as client:
        session_id = "test-session-123"
        
        # First interaction
        async for event in client.run_loop(
            "My name is Elliott",
            session_id=session_id,
        ):
            pass
        
        # Second interaction - should remember name
        async for event in client.run_loop(
            "What's my name?",
            session_id=session_id,
            load_session_history=True,
        ):
            if event.kind == AgentEventKind.TEXT_DELTA:
                print(event.text, end='')
        # Should respond with "Elliott"
```

### 3. Verification
```python
# Check what's being loaded
from obscura.core.context import load_session_messages
from pathlib import Path

messages = load_session_messages(
    "test-session-123",
    Path(".obscura/events.db")
)

for msg in messages:
    print(f"{msg.role}: {msg.content[:50]}...")
```

## Configuration Options

Add to your config or environment:

```python
# In obscura/core/config.py
class ObscuraConfig(BaseModel):
    # ... existing ...
    
    # Session memory
    session_memory_enabled: bool = True
    session_memory_max_turns: int = 5  # Conversation turns to load
    session_memory_strategy: str = "messages"  # or "system_prompt" or "both"
```

## Benefits

✅ Works **system-wide** - CLI, HTTP, agents, everywhere  
✅ **Accurate context** - Real conversation history, not summaries  
✅ **Configurable** - Can disable with `load_session_history=False`  
✅ **Performant** - Only loads recent turns  
✅ **Multi-backend** - Works with all LLM providers  
✅ **Backward compatible** - Defaults to enabled, easy to disable  

## What You Get

After implementation, **every time an Obscura agent is spawned**:

1. Checks if `session_id` is provided
2. If yes and `load_session_history=True`:
   - Loads last N conversation turns from `.obscura/events.db`
   - Reconstructs as `Message[]` objects
   - Injects as `initial_messages` to AgentLoop
   - Backend receives full conversation history
3. LLM has complete context from previous interactions

## Summary

**Already working**: System prompt injection (Option A)  
**This implementation**: Message history injection (Option C)  
**Result**: Full session continuity across all Obscura entry points
