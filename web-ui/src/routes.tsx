import { createBrowserRouter, Navigate } from 'react-router-dom';
import { RouteGuard } from '@/auth/RouteGuard';
import { AppShell } from '@/components/layout/AppShell';
import { RequireRole } from '@/auth/RequireRole';

// Lazy-loaded feature pages
import { lazy, Suspense } from 'react';
import { Spinner } from '@/components/ui/Spinner';

const DashboardPage = lazy(() => import('@/features/dashboard/DashboardPage'));
const AgentsPage = lazy(() => import('@/features/agents/AgentsPage'));
const AgentDetailPage = lazy(() => import('@/features/agents/AgentDetailPage'));
const AgentChatPage = lazy(() => import('@/features/agents/AgentChatPage'));
const SpawnWizardPage = lazy(() => import('@/features/agents/SpawnWizardPage'));
const AgentTemplatesPage = lazy(() => import('@/features/agents/AgentTemplatesPage'));
const AgentGroupsPage = lazy(() => import('@/features/agents/AgentGroupsPage'));
const MemoryPage = lazy(() => import('@/features/memory/MemoryPage'));
const WorkflowsPage = lazy(() => import('@/features/workflows/WorkflowsPage'));
const WorkflowCreatePage = lazy(() => import('@/features/workflows/WorkflowCreatePage'));
const WorkflowDetailPage = lazy(() => import('@/features/workflows/WorkflowDetailPage'));
const ExecutionDetailPage = lazy(() => import('@/features/workflows/ExecutionDetailPage'));
const ToolApprovalsPage = lazy(() => import('@/features/tool-approvals/ToolApprovalsPage'));
const WebhooksPage = lazy(() => import('@/features/webhooks/WebhooksPage'));
const WebhookCreatePage = lazy(() => import('@/features/webhooks/WebhookCreatePage'));
const AuditPage = lazy(() => import('@/features/audit/AuditPage'));
const SessionsPage = lazy(() => import('@/features/sessions/SessionsPage'));
const AdminPage = lazy(() => import('@/features/admin/AdminPage'));
const RateLimitsPage = lazy(() => import('@/features/admin/RateLimitsPage'));
const CapabilitiesPage = lazy(() => import('@/features/admin/CapabilitiesPage'));
const MetricsPage = lazy(() => import('@/features/admin/MetricsPage'));
const HealthPage = lazy(() => import('@/features/health/HealthPage'));
const AgentHealthPage = lazy(() => import('@/features/health/AgentHealthPage'));
const MCPPage = lazy(() => import('@/features/mcp/MCPPage'));
const A2APage = lazy(() => import('@/features/a2a/A2APage'));

function LazyPage({ children }: { children: React.ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="flex h-64 items-center justify-center">
          <Spinner className="h-6 w-6" />
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

function AdminGuard({ children }: { children: React.ReactNode }) {
  return (
    <RequireRole
      role="admin"
      fallback={
        <div className="flex h-64 items-center justify-center text-muted-foreground">
          Admin access required
        </div>
      }
    >
      {children}
    </RequireRole>
  );
}

export const router = createBrowserRouter([
  {
    element: <RouteGuard />,
    children: [
      {
        element: <AppShell />,
        children: [
          { index: true, element: <LazyPage><DashboardPage /></LazyPage> },

          // Agents
          { path: 'agents', element: <LazyPage><AgentsPage /></LazyPage> },
          { path: 'agents/spawn', element: <LazyPage><SpawnWizardPage /></LazyPage> },
          { path: 'agents/templates', element: <LazyPage><AgentTemplatesPage /></LazyPage> },
          { path: 'agents/groups', element: <LazyPage><AgentGroupsPage /></LazyPage> },
          { path: 'agents/:agentId', element: <LazyPage><AgentDetailPage /></LazyPage> },
          { path: 'agents/:agentId/chat', element: <LazyPage><AgentChatPage /></LazyPage> },

          // Memory
          { path: 'memory', element: <LazyPage><MemoryPage /></LazyPage> },

          // Workflows
          { path: 'workflows', element: <LazyPage><WorkflowsPage /></LazyPage> },
          { path: 'workflows/create', element: <LazyPage><WorkflowCreatePage /></LazyPage> },
          { path: 'workflows/:workflowId', element: <LazyPage><WorkflowDetailPage /></LazyPage> },
          { path: 'workflows/executions/:executionId', element: <LazyPage><ExecutionDetailPage /></LazyPage> },

          // Tool Approvals
          { path: 'approvals', element: <LazyPage><ToolApprovalsPage /></LazyPage> },

          // Webhooks
          { path: 'webhooks', element: <LazyPage><WebhooksPage /></LazyPage> },
          { path: 'webhooks/create', element: <LazyPage><WebhookCreatePage /></LazyPage> },

          // Audit
          { path: 'audit', element: <LazyPage><AuditPage /></LazyPage> },

          // Sessions
          { path: 'sessions', element: <LazyPage><SessionsPage /></LazyPage> },

          // Admin
          { path: 'admin', element: <LazyPage><AdminGuard><AdminPage /></AdminGuard></LazyPage> },
          { path: 'admin/rate-limits', element: <LazyPage><AdminGuard><RateLimitsPage /></AdminGuard></LazyPage> },
          { path: 'admin/capabilities', element: <LazyPage><AdminGuard><CapabilitiesPage /></AdminGuard></LazyPage> },
          { path: 'admin/metrics', element: <LazyPage><AdminGuard><MetricsPage /></AdminGuard></LazyPage> },

          // Health
          { path: 'health', element: <LazyPage><HealthPage /></LazyPage> },
          { path: 'health/:agentId', element: <LazyPage><AgentHealthPage /></LazyPage> },

          // MCP
          { path: 'mcp', element: <LazyPage><MCPPage /></LazyPage> },

          // A2A
          { path: 'a2a', element: <LazyPage><A2APage /></LazyPage> },

          // Catch-all
          { path: '*', element: <Navigate to="/" replace /> },
        ],
      },
    ],
  },
]);
