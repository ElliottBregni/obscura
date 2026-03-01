import { create } from 'zustand';

export interface ToolCall {
  id: string;
  name: string;
  input: string;
  result?: string;
  status: 'running' | 'complete' | 'error';
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  thinking?: string;
  toolCalls: ToolCall[];
  timestamp: number;
}

interface ChatState {
  messages: ChatMessage[];
  isStreaming: boolean;
  error: string | null;
  currentToolCallId: string | null;
  sessionId: string | null;

  addUserMessage: (text: string) => void;
  startAssistantMessage: () => string;
  appendText: (msgId: string, text: string) => void;
  appendThinking: (msgId: string, text: string) => void;
  startToolCall: (msgId: string, toolName: string, toolId: string) => void;
  appendToolInput: (msgId: string, delta: string) => void;
  completeToolCall: (msgId: string, result: string) => void;
  finishStream: (msgId: string) => void;
  setError: (error: string) => void;
  setSessionId: (sessionId: string | null) => void;
  clearMessages: () => void;
}

let msgCounter = 0;

export const useChatStore = create<ChatState>()((set) => ({
  messages: [],
  isStreaming: false,
  error: null,
  currentToolCallId: null,
  sessionId: null,

  addUserMessage: (text) =>
    set((s) => ({
      messages: [
        ...s.messages,
        {
          id: `user-${++msgCounter}`,
          role: 'user',
          text,
          toolCalls: [],
          timestamp: Date.now(),
        },
      ],
      error: null,
    })),

  startAssistantMessage: () => {
    const id = `asst-${++msgCounter}`;
    set((s) => ({
      messages: [
        ...s.messages,
        { id, role: 'assistant', text: '', toolCalls: [], timestamp: Date.now() },
      ],
      isStreaming: true,
      error: null,
    }));
    return id;
  },

  appendText: (msgId, text) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === msgId ? { ...m, text: m.text + text } : m
      ),
    })),

  appendThinking: (msgId, text) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === msgId ? { ...m, thinking: (m.thinking || '') + text } : m
      ),
    })),

  startToolCall: (msgId, toolName, toolId) =>
    set((s) => ({
      currentToolCallId: toolId,
      messages: s.messages.map((m) =>
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
    })),

  appendToolInput: (msgId, delta) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === msgId
          ? {
              ...m,
              toolCalls: m.toolCalls.map((tc, i) =>
                i === m.toolCalls.length - 1
                  ? { ...tc, input: tc.input + delta }
                  : tc
              ),
            }
          : m
      ),
    })),

  completeToolCall: (msgId, result) =>
    set((s) => ({
      currentToolCallId: null,
      messages: s.messages.map((m) =>
        m.id === msgId
          ? {
              ...m,
              toolCalls: m.toolCalls.map((tc, i) =>
                i === m.toolCalls.length - 1
                  ? { ...tc, result, status: 'complete' as const }
                  : tc
              ),
            }
          : m
      ),
    })),

  finishStream: (_msgId) =>
    set({ isStreaming: false }),

  setError: (error) =>
    set({ error, isStreaming: false }),

  setSessionId: (sessionId) =>
    set({ sessionId }),

  clearMessages: () =>
    set({ messages: [], isStreaming: false, error: null, currentToolCallId: null }),
}));
