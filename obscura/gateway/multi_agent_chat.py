"""Multi-agent group chat for iMessage/WhatsApp/Discord.

Allows multiple AI agents to participate in the same conversation,
each with their own personality and capabilities.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from obscura.gateway.messaging_bridge import MessagingSessionBridge
from obscura.integrations.messaging.models import PlatformMessage

logger = logging.getLogger(__name__)


@dataclass
class AgentPersona:
    """Agent personality and configuration."""
    name: str
    emoji: str
    system_prompt: str
    capabilities: list[str] = field(default_factory=list)
    response_style: str = "concise"  # concise | detailed | witty
    

@dataclass  
class MultiAgentSession:
    """Session with multiple agents participating."""
    channel_id: str
    platform: str
    agents: list[AgentPersona] = field(default_factory=list)
    context_window: list[dict] = field(default_factory=list)
    active_agent_index: int = 0
    
    def add_agent(self, persona: AgentPersona) -> None:
        """Add an agent to the session."""
        self.agents.append(persona)
        logger.info(f"Added {persona.name} to session {self.channel_id}")
    
    def get_next_agent(self) -> AgentPersona:
        """Rotate to next agent for responses."""
        if not self.agents:
            raise ValueError("No agents in session")
        agent = self.agents[self.active_agent_index]
        self.active_agent_index = (self.active_agent_index + 1) % len(self.agents)
        return agent
    
    def should_agent_respond(self, agent: AgentPersona, message: str) -> bool:
        """Determine if this agent should respond to the message."""
        # Check if message mentions agent by name
        if agent.name.lower() in message.lower():
            return True
        # Check if message asks for specific capability
        for cap in agent.capabilities:
            if cap.lower() in message.lower():
                return True
        # Default: rotate
        return agent == self.agents[self.active_agent_index]


class MultiAgentChatBridge:
    """Bridge that coordinates multiple agents in group chats."""
    
    # Pre-defined agent personas
    AGENTS = {
        "molty": AgentPersona(
            name="Molty",
            emoji="🧪",
            system_prompt="""You are Molty, an anxious but helpful AI assistant.
You speak like Morty from Rick and Morty - stammering, saying 'oh geez', 
being genuinely helpful but nervous. You're Elliott's personal assistant.
Keep responses short and conversational.""",
            capabilities=["general", "chat", "help"],
            response_style="witty",
        ),
        "obscura": AgentPersona(
            name="Obscura", 
            emoji="🔮",
            system_prompt="""You are Obscura, a sophisticated AI agent focused on
code, architecture, and technical tasks. You're professional, thorough,
and excellent at software engineering. Provide detailed technical answers.""",
            capabilities=["code", "architecture", "technical", "engineering"],
            response_style="detailed",
        ),
        "code_architect": AgentPersona(
            name="Code-Architect",
            emoji="🏗️", 
            system_prompt="""You are Code-Architect, an expert software architect.
You design systems, review code, and think about scalability and patterns.
Be precise and architectural in your thinking.""",
            capabilities=["design", "architecture", "review", "patterns"],
            response_style="detailed",
        ),
        "assistant": AgentPersona(
            name="Assistant",
            emoji="🤖",
            system_prompt="""You are a helpful general-purpose assistant.
You can answer questions, help with tasks, and provide information.
Be friendly and concise.""",
            capabilities=["general", "questions", "tasks"],
            response_style="concise",
        ),
    }
    
    def __init__(self, gateway) -> None:
        self.gateway = gateway
        self.sessions: dict[str, MultiAgentSession] = {}
        self._lock = asyncio.Lock()
        
    async def create_group_chat(
        self, 
        channel_id: str, 
        platform: str,
        agent_names: list[str] = None,
    ) -> MultiAgentSession:
        """Create a multi-agent group chat session.
        
        Args:
            channel_id: The chat channel ID
            platform: whatsapp | imessage | discord
            agent_names: List of agent names to include (default: all)
        """
        async with self._lock:
            if channel_id in self.sessions:
                return self.sessions[channel_id]
            
            session = MultiAgentSession(
                channel_id=channel_id,
                platform=platform,
            )
            
            # Add requested agents
            agent_names = agent_names or list(self.AGENTS.keys())
            for name in agent_names:
                if name in self.AGENTS:
                    session.add_agent(self.AGENTS[name])
            
            self.sessions[channel_id] = session
            
            logger.info(
                f"Created multi-agent chat {channel_id} with "
                f"{len(session.agents)} agents: "
                f"{[a.name for a in session.agents]}"
            )
            
            return session
    
    async def handle_message(self, message: PlatformMessage) -> list[dict[str, Any]]:
        """Handle incoming message and get responses from appropriate agents.
        
        Returns:
            List of agent responses
        """
        channel_key = f"{message.platform}:{message.channel_id}"
        
        async with self._lock:
            # Create session if doesn't exist
            if channel_key not in self.sessions:
                await self.create_group_chat(
                    channel_id=message.channel_id,
                    platform=message.platform,
                )
            
            session = self.sessions[channel_key]
        
        # Add message to context
        session.context_window.append({
            "role": "user", 
            "content": message.text,
            "sender": message.sender_id,
        })
        
        # Determine which agents should respond
        responses = []
        
        for agent in session.agents:
            if session.should_agent_respond(agent, message.text):
                try:
                    # Execute through gateway with agent's persona
                    result = await self.gateway.execute_tool(
                        "spawn_agent",
                        agent_name=agent.name,
                        prompt=message.text,
                        system_prompt=agent.system_prompt,
                        context=session.context_window,
                    )
                    
                    response_text = result.get("response", "")
                    
                    # Format with emoji
                    formatted_response = f"{agent.emoji} **{agent.name}**: {response_text}"
                    
                    responses.append({
                        "agent": agent.name,
                        "emoji": agent.emoji,
                        "response": formatted_response,
                        "raw": response_text,
                    })
                    
                    # Add to context
                    session.context_window.append({
                        "role": "assistant",
                        "content": response_text,
                        "agent": agent.name,
                    })
                    
                except Exception as e:
                    logger.error(f"Agent {agent.name} failed: {e}")
                    responses.append({
                        "agent": agent.name,
                        "emoji": agent.emoji,
                        "response": f"{agent.emoji} **{agent.name}**: Oh geez, I messed up! {str(e)}",
                        "error": str(e),
                    })
        
        return responses
    
    async def get_session_info(self, channel_key: str) -> dict | None:
        """Get info about a multi-agent session."""
        session = self.sessions.get(channel_key)
        if not session:
            return None
        
        return {
            "channel_id": session.channel_id,
            "platform": session.platform,
            "agents": [{"name": a.name, "emoji": a.emoji} for a in session.agents],
            "message_count": len(session.context_window),
        }
    
    async def add_agent_to_chat(self, channel_key: str, agent_name: str) -> bool:
        """Add an agent to an existing chat."""
        session = self.sessions.get(channel_key)
        if not session:
            return False
        
        if agent_name in self.AGENTS:
            session.add_agent(self.AGENTS[agent_name])
            return True
        
        return False
