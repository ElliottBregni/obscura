/**
 * JWT decoding for Obscura.
 *
 * Supports tokens from two issuers:
 *   - Supabase (primary) — roles from `app_metadata.roles`
 *   - Zitadel (legacy)   — roles from `urn:zitadel:iam:org:project:roles`
 */

export interface SupabaseAppMetadata {
  roles?: string[];
  role?: string;
  org_id?: string;
}

export interface JWTPayload {
  sub: string;
  email?: string;
  exp: number;
  iat: number;
  // Supabase
  app_metadata?: SupabaseAppMetadata;
  role?: string;
  // Zitadel (legacy)
  'urn:zitadel:iam:org:project:roles'?: Record<string, Record<string, string>>;
  'urn:zitadel:iam:org:id'?: string;
}

export interface DecodedUser {
  userId: string;
  email: string;
  roles: string[];
  orgId: string | null;
}

export function decodeJWT(token: string): JWTPayload {
  const base64 = token.split('.')[1];
  if (!base64) throw new Error('Invalid JWT format');
  const json = atob(base64.replace(/-/g, '+').replace(/_/g, '/'));
  return JSON.parse(json);
}

const DEFAULT_AUTHENTICATED_ROLES = ['agent:read'];

export function extractUser(payload: JWTPayload): DecodedUser {
  const app = payload.app_metadata;
  const zitadelRoles = payload['urn:zitadel:iam:org:project:roles'];

  let roles: string[];
  let orgId: string | null;

  if (app) {
    // Supabase shape — roles live in app_metadata, Supabase's own default
    // `authenticated` role is mapped to `agent:read` to mirror the server.
    if (Array.isArray(app.roles)) {
      roles = app.roles.filter((r) => typeof r === 'string');
    } else if (typeof app.role === 'string') {
      roles = app.role === 'authenticated' ? DEFAULT_AUTHENTICATED_ROLES : [app.role];
    } else if (payload.role === 'authenticated') {
      roles = DEFAULT_AUTHENTICATED_ROLES;
    } else {
      roles = DEFAULT_AUTHENTICATED_ROLES;
    }
    orgId = app.org_id ?? null;
  } else if (zitadelRoles) {
    roles = Object.keys(zitadelRoles);
    orgId = payload['urn:zitadel:iam:org:id'] ?? null;
  } else {
    roles = payload.role === 'authenticated' ? DEFAULT_AUTHENTICATED_ROLES : [];
    orgId = null;
  }

  return {
    userId: payload.sub,
    email: payload.email || '',
    roles,
    orgId,
  };
}

export function isTokenExpired(payload: JWTPayload): boolean {
  return Date.now() >= payload.exp * 1000;
}
