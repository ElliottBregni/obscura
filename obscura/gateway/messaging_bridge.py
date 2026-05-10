# DEPRECATED: Use GatewayNetworkBridge from obscura.gateway.network_bridge instead.
# This module is kept for backward compatibility only.

"""Messaging bridge - injects WhatsApp/Discord/iMessage into session context.

This module bridges external messaging platforms (WhatsApp, Discord, iMessage)
directly into Obscura agent sessions, allowing seamless conversation flow.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from obscura.integrations.messaging.models import PlatformMessage

if TYPE_CHECKING:
    from obscura.gateway.orchestrator import GatewayOrchestrator

logger = logging.getLogger(__name__)


@dataclass
class MessagingSession:
    """Links a messaging platform to an Obscura session."""
    
    platform: str  # whatsapp | discord | imessage
    channel_id: str  # phone number, discord channel, etc.
    session_id: str | None = None
    agent_name: str = "assistant"
    context_window: list[dict] = None
    
    def __post_init__(self):
        if self.context_window is None:
            self.context_window = []


class MessagingSessionBridge:
    """Bridges messaging platforms to Obscura sessions.
    
    Automatically creates/destroys sessions based on message activity.
    Maintains conversation context across messages.
    """
    
    def __init__(self, gateway: GatewayOrchestrator) -> None:
        self.gateway = gateway
        self.sessions: dict[str, MessagingSession] = {}  # channel_id -> session
        self._lock = asyncio.Lock()
        
    async def handle_message(self, message: PlatformMessage) -> str:
        """Handle incoming message and route to session.
        
        Args:
            message: Incoming platform message
            
        Returns:
            Response text to send back
        """
        channel_key = f"{message.platform}:{message.channel_id}"
        
        async with self._lock:
            # Get or create session
            if channel_key not in self.sessions:
                self.sessions[channel_key] = MessagingSession(
                    platform=message.platform,
                    channel_id=message.channel_id,
                )
                logger.info(f"Created new session for {channel_key}")
            
            session = self.sessions[channel_key]
        
        # Add message to context
        session.context_window.append({
            "role": "user",
            "content": message.text,
            "timestamp": message.timestamp.isoformat(),
            "platform": message.platform,
            "sender": message.sender_id,
        })
        
        # Trim context window (keep last 20 messages)
        if len(session.context_window) > 20:
            session.context_window = session.context_window[-20:]
        
        # Execute through gateway
        try:
            result = await self.gateway.execute_tool(
                "spawn_agent",
                agent_name=session.agent_name,
                prompt=message.text,
                context=session.context_window,
                session_id=session.session_id,
            )
            
            # Update session ID if created
            if result.get("session_id"):
                session.session_id = result["session_id"]
            
            # Add response to context
            response_text = result.get("response", "I'm thinking...")
            session.context_window.append({
                "role": "assistant",
                "content": response_text,
                "timestamp": result.get("timestamp"),
            })
            
            return response_text
            
        except Exception as e:
            logger.error(f"Failed to process message: {e}")
            return f"Error: {str(e)}"
    
    async def get_session_context(self, channel_key: str) -> list[dict] | None:
        """Get conversation context for a channel."""
        session = self.sessions.get(channel_key)
        return session.context_window if session else None
    
    async def clear_session(self, channel_key: str) -> bool:
        """Clear a messaging session."""
        if channel_key in self.sessions:
            del self.sessions[channel_key]
            return True
        return False


class WhatsAppSessionAdapter:
    """WhatsApp adapter that bridges to sessions."""
    
    def __init__(self, bridge: MessagingSessionBridge) -> None:
        self.bridge = bridge
        
    async def on_message(self, message: PlatformMessage) -> str:
        """Handle WhatsApp message."""
        return await self.bridge.handle_message(message)


class DiscordSessionAdapter:
    """Discord adapter that bridges to sessions."""
    
    def __init__(self, bridge: MessagingSessionBridge) -> None:
        self.bridge = bridge
        
    async def on_message(self, message: PlatformMessage) -> str:
        """Handle Discord message."""
        return await self.bridge.handle_message(message)


class iMessageSessionAdapter:
    """iMessage adapter that bridges to sessions."""
    
    def __init__(self, bridge: MessagingSessionBridge) -> None:
        self.bridge = bridge
        
    async def on_message(self, message: PlatformMessage) -> str:
        """Handle iMessage."""
        return await self.bridge.handle_message(message)
