import { create } from 'zustand';

export interface AgentState {
  agent_id: string;
  name: string;
  status: string;
  model?: string;
  updated_at?: string;
}

interface AgentStoreState {
  agents: Map<string, AgentState>;
  wsConnected: boolean;

  updateAgent: (agent: AgentState) => void;
  removeAgent: (agentId: string) => void;
  setAgents: (agents: AgentState[]) => void;
  setWsConnected: (connected: boolean) => void;
}

export const useAgentStore = create<AgentStoreState>()((set) => ({
  agents: new Map(),
  wsConnected: false,

  updateAgent: (agent) =>
    set((s) => {
      const next = new Map(s.agents);
      next.set(agent.agent_id, agent);
      return { agents: next };
    }),

  removeAgent: (agentId) =>
    set((s) => {
      const next = new Map(s.agents);
      next.delete(agentId);
      return { agents: next };
    }),

  setAgents: (agents) =>
    set({
      agents: new Map(agents.map((a) => [a.agent_id, a])),
    }),

  setWsConnected: (connected) => set({ wsConnected: connected }),
}));
