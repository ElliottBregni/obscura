import { create } from 'zustand';

export interface ToolCallInfo {
  id: string;
  name: string;
  input: string;
  result?: string;
  status: 'running' | 'complete' | 'error';
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  thinking: string;
  toolCalls: ToolCallInfo[];
  isStreaming: boolean;
  timestamp: number;
}

interface ChatState {
  messages: ChatMessage[];
  isStreaming: boolean;
  currentMessageId: string | null;
  error: string | null;

  addUserMessage: (text: string) => void;
  startAssistantMessage: () => string;
  appendText: (id: string, delta: string) => void;
  appendThinking: (id: string, delta: string) => void;
  startToolCall: (msgId: string, toolName: string, toolId: string) => void;
  appendToolInput: (msgId: string, delta: string) => void;
  completeToolCall: (msgId: string, result: string) => void;
  failToolCall: (msgId: string, error: string) => void;
  finishStream: (id: string) => void;
  setError: (err: string) => void;
  clearMessages: () => void;
}

let _nextId = 0;
function genId(): string {
  return `msg-${Date.now()}-${++_nextId}`;
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  isStreaming: false,
  currentMessageId: null,
  error: null,

  addUserMessage: (text: string) => {
    const id = genId();
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id,
          role: 'user',
          content: text,
          thinking: '',
          toolCalls: [],
          isStreaming: false,
          timestamp: Date.now(),
        },
      ],
      error: null,
    }));
  },

  startAssistantMessage: () => {
    const id = genId();
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id,
          role: 'assistant',
          content: '',
          thinking: '',
          toolCalls: [],
          isStreaming: true,
          timestamp: Date.now(),
        },
      ],
      isStreaming: true,
      currentMessageId: id,
      error: null,
    }));
    return id;
  },

  appendText: (id: string, delta: string) => {
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === id ? { ...m, content: m.content + delta } : m
      ),
    }));
  },

  appendThinking: (id: string, delta: string) => {
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === id ? { ...m, thinking: m.thinking + delta } : m
      ),
    }));
  },

  startToolCall: (msgId: string, toolName: string, toolId: string) => {
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === msgId
          ? {
              ...m,
              toolCalls: [
                ...m.toolCalls,
                { id: toolId, name: toolName, input: '', status: 'running' as const },
              ],
            }
          : m
      ),
    }));
  },

  appendToolInput: (msgId: string, delta: string) => {
    set((state) => ({
      messages: state.messages.map((m) => {
        if (m.id !== msgId || m.toolCalls.length === 0) return m;
        const tools = [...m.toolCalls];
        const last = { ...tools[tools.length - 1], input: tools[tools.length - 1].input + delta };
        tools[tools.length - 1] = last;
        return { ...m, toolCalls: tools };
      }),
    }));
  },

  completeToolCall: (msgId: string, result: string) => {
    set((state) => ({
      messages: state.messages.map((m) => {
        if (m.id !== msgId || m.toolCalls.length === 0) return m;
        const tools = [...m.toolCalls];
        const last = { ...tools[tools.length - 1], status: 'complete' as const, result };
        tools[tools.length - 1] = last;
        return { ...m, toolCalls: tools };
      }),
    }));
  },

  failToolCall: (msgId: string, error: string) => {
    set((state) => ({
      messages: state.messages.map((m) => {
        if (m.id !== msgId || m.toolCalls.length === 0) return m;
        const tools = [...m.toolCalls];
        const last = { ...tools[tools.length - 1], status: 'error' as const, result: error };
        tools[tools.length - 1] = last;
        return { ...m, toolCalls: tools };
      }),
    }));
  },

  finishStream: (id: string) => {
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === id ? { ...m, isStreaming: false } : m
      ),
      isStreaming: false,
      currentMessageId: null,
    }));
  },

  setError: (err: string) => {
    set((state) => ({
      error: err,
      isStreaming: false,
      messages: state.messages.map((m) =>
        m.isStreaming ? { ...m, isStreaming: false } : m
      ),
      currentMessageId: null,
    }));
  },

  clearMessages: () => {
    set({ messages: [], isStreaming: false, currentMessageId: null, error: null });
  },
}));
