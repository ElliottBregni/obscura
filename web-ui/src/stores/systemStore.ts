import { create } from 'zustand';

interface SystemState {
  wsConnected: boolean;
  serverReachable: boolean;

  setWsConnected: (connected: boolean) => void;
  setServerReachable: (reachable: boolean) => void;
}

export const useSystemStore = create<SystemState>()((set) => ({
  wsConnected: false,
  serverReachable: true,

  setWsConnected: (connected) => set({ wsConnected: connected }),
  setServerReachable: (reachable) => set({ serverReachable: reachable }),
}));
