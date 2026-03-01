import { create } from 'zustand';

export interface Notification {
  id: string;
  type: 'success' | 'error' | 'warning' | 'info';
  title: string;
  message?: string;
  timestamp: number;
}

interface NotificationState {
  notifications: Notification[];
  add: (n: Omit<Notification, 'id' | 'timestamp'>) => void;
  dismiss: (id: string) => void;
  clear: () => void;
}

let notifCounter = 0;

export const useNotificationStore = create<NotificationState>()((set) => ({
  notifications: [],

  add: (n) =>
    set((s) => ({
      notifications: [
        ...s.notifications,
        { ...n, id: `notif-${++notifCounter}`, timestamp: Date.now() },
      ],
    })),

  dismiss: (id) =>
    set((s) => ({
      notifications: s.notifications.filter((n) => n.id !== id),
    })),

  clear: () => set({ notifications: [] }),
}));
