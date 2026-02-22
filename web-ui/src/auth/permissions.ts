// Mirrors VALID_ROLES from obscura/auth/models.py
export const VALID_ROLES = [
  'admin',
  'operator',
  'tier:privileged',
  'agent:copilot',
  'agent:claude',
  'agent:localllm',
  'agent:openai',
  'agent:moonshot',
  'agent:read',
  'sync:write',
  'sessions:manage',
  'a2a:invoke',
  'a2a:manage',
] as const;

export type Role = (typeof VALID_ROLES)[number];

export const AGENT_WRITE_ROLES: readonly Role[] = [
  'agent:copilot',
  'agent:claude',
  'agent:localllm',
  'agent:openai',
  'agent:moonshot',
];

export const AGENT_READ_ROLES: readonly Role[] = [
  ...AGENT_WRITE_ROLES,
  'agent:read',
];

export const SECTION_ROLES: Record<string, readonly Role[]> = {
  dashboard: [],
  agents: AGENT_READ_ROLES,
  memory: AGENT_READ_ROLES,
  workflows: AGENT_READ_ROLES,
  approvals: AGENT_READ_ROLES,
  webhooks: AGENT_READ_ROLES,
  audit: AGENT_READ_ROLES,
  sessions: ['sessions:manage'],
  admin: ['admin'],
  health: AGENT_READ_ROLES,
  mcp: AGENT_READ_ROLES,
  a2a: ['a2a:invoke', 'a2a:manage'],
};

export function canAccessSection(section: string, userRoles: string[]): boolean {
  if (userRoles.includes('admin')) return true;
  const required = SECTION_ROLES[section];
  if (!required || required.length === 0) return true;
  return required.some((r) => userRoles.includes(r));
}
