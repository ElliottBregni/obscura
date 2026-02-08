import { create } from 'zustand';

interface SystemState {
  isConnected: boolean;
  metrics: {
    cpu: number;
    memory: number;
    activeAgents: number;
  };
  setConnected: (connected: boolean) => void;
  setMetrics: (metrics: Partial<SystemState['metrics']>) => void;
}

export const useSystemStore = create<SystemState>((set) => ({
  isConnected: false,
  metrics: {
    cpu: 0,
    memory: 0,
    activeAgents: 0,
  },
  setConnected: (connected) => set({ isConnected: connected }),
  setMetrics: (metrics) => set((state) => ({
    metrics: { ...state.metrics, ...metrics }
  })),
}));
