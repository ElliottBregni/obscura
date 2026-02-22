import { create } from 'zustand';

interface SystemState {
  wsConnected: boolean;
  serverReachable: boolean;
  authEnabled: boolean;

  setWsConnected: (connected: boolean) => void;
  setServerReachable: (reachable: boolean) => void;
  setAuthEnabled: (enabled: boolean) => void;
}

export const useSystemStore = create<SystemState>()((set) => ({
  wsConnected: false,
  serverReachable: true,
  authEnabled: false,

  setWsConnected: (connected) => set({ wsConnected: connected }),
  setServerReachable: (reachable) => set({ serverReachable: reachable }),
  setAuthEnabled: (enabled) => set({ authEnabled: enabled }),
}));
