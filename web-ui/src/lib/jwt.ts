export interface JWTPayload {
  sub: string;
  email?: string;
  'urn:zitadel:iam:org:project:roles'?: Record<string, Record<string, string>>;
  'urn:zitadel:iam:org:id'?: string;
  exp: number;
  iat: number;
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

export function extractUser(payload: JWTPayload): DecodedUser {
  const rolesObj = payload['urn:zitadel:iam:org:project:roles'];
  const roles = rolesObj ? Object.keys(rolesObj) : [];

  return {
    userId: payload.sub,
    email: payload.email || '',
    roles,
    orgId: payload['urn:zitadel:iam:org:id'] ?? null,
  };
}

export function isTokenExpired(payload: JWTPayload): boolean {
  return Date.now() >= payload.exp * 1000;
}
