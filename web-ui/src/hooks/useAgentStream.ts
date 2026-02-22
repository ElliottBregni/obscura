import { useCallback, useRef } from 'react';
import { useChatStore } from '@/stores/chatStore';
import { useAuthStore } from '@/stores/authStore';
import { API_URL } from '@/lib/constants';

interface StreamOptions {
  backend: string;
  model?: string;
  systemPrompt?: string;
  sessionId?: string;
  agentId?: string;
}

function parseSSELine(
  buffer: string,
  onEvent: (event: string, data: string) => void
): string {
  const lines = buffer.split('\n');
  let currentEvent = '';
  let currentData = '';
  let remaining = '';

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (i === lines.length - 1 && line !== '') {
      remaining = line;
      break;
    }

    if (line.startsWith('event:')) {
      currentEvent = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      currentData = line.slice(5).trim();
    } else if (line === '' && currentEvent) {
      onEvent(currentEvent, currentData);
      currentEvent = '';
      currentData = '';
    }
  }

  return remaining;
}

export function useAgentStream(options: StreamOptions) {
  const abortRef = useRef<AbortController | null>(null);
  const store = useChatStore;

  const sendMessage = useCallback(
    async (prompt: string) => {
      const state = store.getState();
      if (state.isStreaming) return;

      state.addUserMessage(prompt);
      const msgId = state.startAssistantMessage();

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const { token, apiKey } = useAuthStore.getState();
        const headers: Record<string, string> = { 'Content-Type': 'application/json' };
        if (token) headers['Authorization'] = `Bearer ${token}`;
        else if (apiKey) headers['X-API-Key'] = apiKey;

        const endpoint = options.agentId
          ? `${API_URL}/api/v1/agents/${options.agentId}/stream`
          : `${API_URL}/api/v1/stream`;

        const response = await fetch(endpoint, {
          method: 'POST',
          headers,
          body: JSON.stringify({
            backend: options.backend,
            prompt,
            model: options.model ?? null,
            system_prompt: options.systemPrompt ?? '',
            session_id: options.sessionId ?? null,
          }),
          signal: controller.signal,
        });

        if (!response.ok) {
          const err = await response.text();
          store.getState().setError(`Stream failed: ${response.status} ${err}`);
          return;
        }

        const reader = response.body?.getReader();
        if (!reader) {
          store.getState().setError('No response body');
          return;
        }

        const decoder = new TextDecoder();
        let buffer = '';

        const handleEvent = (event: string, data: string) => {
          const s = store.getState();
          let payload: Record<string, string> = {};
          try {
            payload = data ? JSON.parse(data) : {};
          } catch {
            payload = { text: data };
          }

          switch (event) {
            case 'text_delta':
              if (payload.text) s.appendText(msgId, payload.text);
              break;
            case 'thinking_delta':
              if (payload.text) s.appendThinking(msgId, payload.text);
              break;
            case 'tool_use_start':
              s.startToolCall(
                msgId,
                payload.tool_name || 'unknown',
                payload.tool_use_id || `tool-${Date.now()}`
              );
              break;
            case 'tool_use_delta':
              if (payload.tool_input_delta) s.appendToolInput(msgId, payload.tool_input_delta);
              break;
            case 'tool_use_end':
              break;
            case 'tool_result':
              s.completeToolCall(msgId, payload.text || '');
              break;
            case 'done':
              s.finishStream(msgId);
              break;
            case 'error':
              s.setError(payload.text || 'Stream error');
              break;
          }
        };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          buffer = parseSSELine(buffer, handleEvent);
        }

        const finalState = store.getState();
        if (finalState.isStreaming) {
          finalState.finishStream(msgId);
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          store.getState().finishStream(msgId);
        } else {
          store.getState().setError(String(err));
        }
      } finally {
        abortRef.current = null;
      }
    },
    [options.backend, options.model, options.systemPrompt, options.sessionId, options.agentId, store]
  );

  const cancelStream = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return { sendMessage, cancelStream };
}
