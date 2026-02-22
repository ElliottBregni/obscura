export const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8080';

export const WS_URL = API_URL.replace(/^http/, 'ws');

export const STATUS_COLORS = {
  running: 'emerald',
  idle: 'blue',
  error: 'red',
  stopped: 'zinc',
  pending: 'yellow',
  waiting: 'amber',
  completed: 'green',
  failed: 'red',
  healthy: 'emerald',
  warning: 'yellow',
  critical: 'red',
  unknown: 'zinc',
  active: 'emerald',
  paused: 'yellow',
  archived: 'zinc',
} as const;

export const BACKENDS = [
  { value: 'copilot', label: 'GitHub Copilot' },
  { value: 'claude', label: 'Anthropic Claude' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'localllm', label: 'Local LLM' },
  { value: 'moonshot', label: 'Moonshot / Kimi' },
] as const;

export const REFETCH_INTERVALS = {
  agents: 5000,
  health: 10000,
  metrics: 10000,
} as const;
