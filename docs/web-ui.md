# Web UI Reference

## Overview

The Obscura admin portal is a React 18 single-page application that provides a dashboard for managing agents, sessions, memory, workflows, goals, and system health.

**Stack:**

| Layer | Library | Version |
|-------|---------|---------|
| Framework | React 18 + TypeScript | 18.3.x / 5.7.x |
| Build | Vite | 6.x |
| Styling | Tailwind CSS | 3.4.x |
| Components | Radix UI | 1.x / 2.x |
| State | Zustand | 5.x |
| Data fetching | TanStack React Query | 5.x |
| Routing | React Router DOM | 7.x |
| Forms | React Hook Form + Zod | 7.x / 3.x |
| Charts | Recharts | 2.x |

---

## Setup

```bash
cd web-ui
npm install
npm run dev   # http://localhost:5173
```

Other scripts:

```bash
npm run build       # Production build → dist/
npm run preview     # Preview the production build locally
npm run lint        # ESLint check
npm run test        # Vitest unit tests
npm run test:e2e    # Playwright end-to-end tests
```

---

## Pages

All routes are lazy-loaded via `React.lazy`. Routes under `/admin/*` require the `admin` role.

| Path | Page | Description | Required Role |
|------|------|-------------|---------------|
| `/` | Dashboard | Overview metrics and recent activity | any |
| `/agents` | Agents | List all running and idle agents | any |
| `/agents/spawn` | Spawn Wizard | Step-by-step agent creation wizard | any |
| `/agents/templates` | Agent Templates | Manage reusable agent manifest templates | any |
| `/agents/groups` | Agent Groups | Organize agents into named groups | any |
| `/agents/:agentId` | Agent Detail | Inspect agent state, tools, and logs | any |
| `/agents/:agentId/chat` | Agent Chat | Live chat interface with a specific agent | any |
| `/sessions` | Sessions | Browse and replay session event streams | any |
| `/memory` | Memory | Search and inspect the vector memory store | any |
| `/workflows` | Workflows | List multi-step agent workflows | any |
| `/workflows/create` | Workflow Create | Build a new workflow | any |
| `/workflows/:workflowId` | Workflow Detail | View workflow definition and run history | any |
| `/workflows/executions/:executionId` | Execution Detail | Inspect a specific workflow execution | any |
| `/goals` | Goals | Kairos autonomous goal board | any |
| `/approvals` | Tool Approvals | Review and approve pending tool calls | any |
| `/webhooks` | Webhooks | List outbound webhook subscriptions | any |
| `/webhooks/create` | Webhook Create | Register a new webhook endpoint | any |
| `/audit` | Audit | Immutable audit log of all system events | any |
| `/health` | Health | System-wide health summary | any |
| `/health/:agentId` | Agent Health | Per-agent health detail and heartbeat history | any |
| `/mcp` | MCP | Model Context Protocol server management | any |
| `/a2a` | A2A | Agent-to-agent protocol connections | any |
| `/admin` | Admin | General admin settings | admin |
| `/admin/rate-limits` | Rate Limits | Configure per-user and per-tier rate limits | admin |
| `/admin/capabilities` | Capabilities | Manage capability tiers and feature flags | admin |
| `/admin/metrics` | Metrics | Detailed system metrics and charts | admin |

Route access is gated by `canAccessSection(section, roles, authEnabled)` in `src/auth/permissions.ts`. When `authEnabled` is false every section is visible regardless of role.

---

## Authentication

When `OBSCURA_AUTH_ENABLED=true` the API requires a bearer token on every request:

```
Authorization: Bearer <api-key>
```

**Dev mode (auth disabled or local only):** The UI reads the key from `localStorage` under the key `obscura_api_key`. Set it once in the browser console:

```js
localStorage.setItem('obscura_api_key', 'your-key-here');
```

The `RouteGuard` component wraps the entire router and redirects unauthenticated users to the login screen. The `RequireRole` component renders a fallback when the user lacks the required role.

---

## Theme

The UI is **dark-only**. All color tokens are defined as CSS custom properties in `src/index.css` and consumed by Tailwind via `hsl(var(--token))`.

Key tokens:

| Token | Value | Usage |
|-------|-------|-------|
| `--primary` | `hsl(185 60% 50%)` | Teal accent, active nav, buttons |
| `--background` | dark near-black | Page background |
| `--card` | slightly lighter | Sidebar, cards |
| `--border` | subtle grey | Dividers, input outlines |
| `--muted-foreground` | dimmed text | Secondary labels, icons |

The sidebar collapses to 52 px (icon-only) and expands to 200 px. Collapse state is persisted in `uiStore` (Zustand).

---

## Building for Production

```bash
cd web-ui
npm run build
```

Output is written to `web-ui/dist/`. The FastAPI backend serves this directory as static files when `OBSCURA_SERVE_UI=true`. The build command runs `tsc` (type-check) then `vite build`; it will fail on TypeScript errors.

---

## Adding a New Page

Follow these four steps:

**Step 1 — Create the feature file**

```
web-ui/src/features/<section>/<SectionPage>.tsx
```

Export a default React component.

**Step 2 — Add a lazy import in `routes.tsx`**

```ts
// web-ui/src/routes.tsx
const MyNewPage = lazy(() => import('@/features/my-section/MyNewPage'));
```

Then add a route entry inside the `AppShell` children array:

```tsx
{ path: 'my-section', element: <LazyPage><MyNewPage /></LazyPage> },
```

Wrap with `<AdminGuard>` if admin-only.

**Step 3 — Add a nav item in `Sidebar.tsx`**

```ts
// web-ui/src/components/layout/Sidebar.tsx
import { IconName } from 'lucide-react';

// Inside NAV_ITEMS array:
{ label: 'My Section', path: '/my-section', icon: IconName, section: 'my-section' },
```

**Step 4 — Add a permission in `permissions.ts`**

```ts
// web-ui/src/auth/permissions.ts
export function canAccessSection(section: string, roles: string[], authEnabled: boolean): boolean {
  // Add your section:
  if (section === 'my-section') return roles.includes('admin') || !authEnabled;
  // ...
}
```

If the section should be visible to all authenticated users, return `true` (or `!authEnabled` for dev convenience).
