import { create } from 'zustand';

export interface AgentStoreEntry {
  agent_id: string;
  id: string;
  name: string;
  status: string;
  model?: string;
  created_at: string;
}

interface AgentState {
  agents: AgentStoreEntry[];
  selectedAgent: AgentStoreEntry | null;
  setAgents: (agents: any[]) => void;
  addAgent: (agent: any) => void;
  removeAgent: (id: string) => void;
  selectAgent: (agent: AgentStoreEntry | null) => void;
  updateAgentStatus: (id: string, status: string) => void;
}

function normalize(a: any): AgentStoreEntry {
  return {
    agent_id: a.agent_id || a.id,
    id: a.agent_id || a.id,
    name: a.name,
    status: a.status,
    model: a.model,
    created_at: a.created_at,
  };
}

export const useAgentStore = create<AgentState>((set) => ({
  agents: [],
  selectedAgent: null,
  setAgents: (agents) => set({ agents: agents.map(normalize) }),
  addAgent: (agent) => set((state) => ({ agents: [...state.agents, normalize(agent)] })),
  removeAgent: (id) => set((state) => ({
    agents: state.agents.filter(a => a.agent_id !== id && a.id !== id)
  })),
  selectAgent: (agent) => set({ selectedAgent: agent }),
  updateAgentStatus: (id, status) => set((state) => ({
    agents: state.agents.map((a) =>
      (a.agent_id === id || a.id === id) ? { ...a, status } : a
    ),
  })),
}));
